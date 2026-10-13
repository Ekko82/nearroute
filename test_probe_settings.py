"""Offline regressions for configurable probes and user-selected scan scopes."""

import copy
from contextlib import ExitStack
import ssl
import unittest
from unittest.mock import MagicMock, patch
import urllib.parse

import optimizer


class ProbeConfigTests(unittest.TestCase):
    def test_complete_http_and_https_urls_are_accepted(self):
        for url in ("https://example.com", "http://example.com/check?x=1&y=2",
                    "https://example.com:8443/a%20b?x=1", "http://[::1]:8080/health"):
            with self.subTest(url=url):
                cfg = optimizer.validate_config({"probe_url": url})
                self.assertEqual(cfg["probe_url"], url)

    def test_invalid_probe_urls_are_rejected(self):
        invalid = (None, 12, "", "example.com", "//example.com/a", "ftp://example.com/a",
                   "https:///a", "https://user@example.com/a", "https://user:password@example.com/a",
                   "https://@example.com/a", "https://example.com/a#section",
                   "https://example.com/\npath", "https://example.com/\rpath",
                   "https://example.com/ path", "https://example.com:abc/a",
                   "https://example.com:0/a", "https://example.com:65536/a",
                   "https://[broken/a", "https://example.com\\other/a")
        for url in invalid:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    optimizer.validate_config({"probe_url": url})

    def test_status_codes_include_range_boundaries_and_remove_duplicates(self):
        cfg = optimizer.validate_config({"probe_expected_statuses": [100, 204, 405, 599, 204]})
        self.assertEqual(cfg["probe_expected_statuses"], [100, 204, 405, 599])

    def test_invalid_status_codes_or_containers_are_rejected(self):
        for statuses in (None, [], (200,), "200", [99], [600], [200.0], [True],
                         ["200"], [None], list(range(200, 211))):
            with self.subTest(statuses=statuses):
                with self.assertRaises(ValueError):
                    optimizer.validate_config({"probe_expected_statuses": statuses})

    def test_unknown_configuration_fields_survive_validation_without_mutation(self):
        raw = {"custom_extension": {"nested": [1, 2]}, "scan_regions": ["jp", "JP"],
               "probe_expected_statuses": [204, 204]}
        before = copy.deepcopy(raw)
        cfg = optimizer.validate_config(raw)
        self.assertEqual(cfg["custom_extension"], raw["custom_extension"])
        self.assertEqual(raw, before)
        self.assertEqual(cfg["scan_regions"], ["JP"])
        self.assertEqual(cfg["probe_expected_statuses"], [204])

    def test_probe_identity_changes_with_target_or_expected_status(self):
        cfg = optimizer.validate_config({})
        original = optimizer.probe_identity(cfg)
        self.assertNotEqual(original, optimizer.probe_identity(dict(cfg, probe_url="https://example.com/check")))
        self.assertNotEqual(original, optimizer.probe_identity(dict(cfg, probe_expected_statuses=[200])))

    def test_probe_identity_ignores_status_order_and_unrelated_options(self):
        cfg = optimizer.validate_config({"probe_expected_statuses": [200, 204]})
        identity = optimizer.probe_identity(cfg)
        self.assertEqual(identity, optimizer.probe_identity(dict(cfg, probe_expected_statuses=[204, 200])))
        self.assertEqual(identity, optimizer.probe_identity(dict(cfg, health_interval_seconds=20)))


class ConfigurableScopeTests(unittest.TestCase):
    def test_region_codes_are_case_normalized_deduplicated_and_uk_aliased(self):
        cfg = optimizer.validate_config({"scan_regions": ["jp", "uk", "GB", "za", "unknown", "JP"]})
        self.assertEqual(cfg["scan_regions"], ["JP", "GB", "ZA", "UNKNOWN"])

    def test_any_two_letter_region_code_is_valid_configuration(self):
        cfg = optimizer.validate_config({"scan_regions": ["ZA", "MY", "KR", "ZZ"]})
        self.assertEqual(cfg["scan_regions"], ["ZA", "MY", "KR", "ZZ"])

    def test_invalid_regions_and_modes_are_rejected(self):
        for raw in ({"scan_regions": []}, {"scan_regions": "JP"}, {"scan_regions": ["JPN"]},
                    {"scan_regions": ["J1"]}, {"scan_regions": [None]},
                    {"scan_regions": [" JP"]}, {"scan_mode": "automatic"}):
            with self.subTest(raw=raw):
                with self.assertRaises(ValueError):
                    optimizer.validate_config(raw)

    def test_unknown_region_selects_only_unclassified_nodes(self):
        cfg = optimizer.validate_config({"scan_regions": ["UNKNOWN"]})
        self.assertEqual(optimizer.scan_candidates(["JP 01", "mystery node", "US 01", "premium"], cfg),
                         ["mystery node", "premium"])

    def test_regions_outside_defaults_can_be_explicitly_selected(self):
        cfg = optimizer.validate_config({"scan_regions": ["US", "MY", "KR"]})
        names = ["JP 01", "美国 01", "马来西亚 01", "🇰🇷 01", "mystery"]
        self.assertEqual(optimizer.scan_candidates(names, cfg), ["美国 01", "马来西亚 01", "🇰🇷 01"])

    def test_node_mode_uses_exact_names_in_available_order(self):
        cfg = optimizer.validate_config({"scan_mode": "nodes", "scan_nodes": ["US 01", "JP 01", "US 01"],
                                         "scan_regions": []})
        names = ["JP 010", "JP 01", "US 01", "us 01", "mystery"]
        before = names.copy()
        self.assertEqual(optimizer.scan_candidates(names, cfg), ["JP 01", "US 01"])
        self.assertEqual(names, before)
        self.assertEqual(cfg["scan_nodes"], ["US 01", "JP 01"])

    def test_missing_named_nodes_do_not_expand_scope(self):
        cfg = optimizer.validate_config({"scan_mode": "nodes", "scan_nodes": ["deleted JP 01"]})
        self.assertEqual(optimizer.scan_candidates(["JP 01", "US 01"], cfg), [])

    def test_invalid_node_names_or_empty_node_mode_are_rejected(self):
        for nodes in ([], "JP 01", [""], ["   "], [None], [1]):
            with self.subTest(nodes=nodes):
                with self.assertRaises(ValueError):
                    optimizer.validate_config({"scan_mode": "nodes", "scan_nodes": nodes})

    def test_all_mode_includes_unclassified_nodes_without_aliasing_input(self):
        cfg = optimizer.validate_config({"scan_mode": "all", "scan_regions": [], "scan_nodes": []})
        names = ["JP 01", "US 01", "mystery"]
        selected = optimizer.scan_candidates(names, cfg)
        self.assertEqual(selected, names)
        self.assertIsNot(selected, names)


class ConfigurableProbeTransportTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.conn = MagicMock()
        self.response = MagicMock(status=204)
        self.conn.getresponse.return_value = self.response
        self.https = self.stack.enter_context(patch.object(optimizer.http.client, "HTTPSConnection", return_value=self.conn))
        self.http = self.stack.enter_context(patch.object(optimizer.http.client, "HTTPConnection", return_value=self.conn))
        self.stack.enter_context(patch.object(optimizer.urllib.request, "build_opener",
                                             side_effect=AssertionError("real network forbidden")))

    def verify(self, url="https://example.com/check", expected=(204,)):
        return optimizer.verify_current_path({"mixed-port": 10090}, probe_url=url, expected_statuses=expected)

    def test_https_custom_port_path_and_query_use_connect(self):
        result = self.verify("https://example.com:8443/a%20b?x=1&y=2")
        self.assertTrue(result["ok"])
        self.http.assert_not_called()
        self.assertEqual(self.https.call_args.args[:2], ("127.0.0.1", 10090))
        self.conn.set_tunnel.assert_called_once_with("example.com", 8443)
        self.assertEqual(self.conn.request.call_args.args[:2], ("GET", "/a%20b?x=1&y=2"))
        self.conn.close.assert_called_once()

    def test_https_empty_path_is_slash_and_default_port_is_443(self):
        self.assertTrue(self.verify("https://example.com?ping=1")["ok"])
        self.conn.set_tunnel.assert_called_once_with("example.com", 443)
        self.assertEqual(self.conn.request.call_args.args[:2], ("GET", "/?ping=1"))

    def test_http_proxy_request_uses_absolute_url_without_connect(self):
        url = "http://example.com:8080/health?x=1"
        self.assertTrue(self.verify(url)["ok"])
        self.https.assert_not_called()
        self.assertEqual(self.http.call_args.args[:2], ("127.0.0.1", 10090))
        self.conn.set_tunnel.assert_not_called()
        self.assertEqual(self.conn.request.call_args.args[:2], ("GET", url))

    def test_configured_200_204_or_405_is_accepted_and_reported(self):
        for code in (200, 204, 405):
            with self.subTest(code=code):
                self.response.status = code
                result = self.verify(expected=(code,))
                self.assertTrue(result["ok"])
                self.assertEqual(result["http_status"], code)

    def test_one_of_multiple_expected_statuses_is_accepted(self):
        self.response.status = 200
        self.assertTrue(self.verify(expected=(200, 204))["ok"])

    def test_unexpected_status_is_rejected_without_reading_body(self):
        self.response.status = 405
        result = self.verify(expected=(204,))
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 405)
        self.response.read.assert_not_called()

    def test_redirect_is_not_followed(self):
        self.response.status = 302
        self.response.getheader.return_value = "https://elsewhere.example/"
        result = self.verify()
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 302)
        self.conn.request.assert_called_once()
        self.https.assert_called_once()
        self.http.assert_not_called()

    def test_tls_context_verifies_certificate_and_hostname_for_custom_target(self):
        self.assertTrue(self.verify()["ok"])
        context = self.https.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)

    def test_custom_target_certificate_failure_has_no_insecure_retry(self):
        self.conn.request.side_effect = ssl.SSLCertVerificationError("mock bad certificate")
        result = self.verify()
        self.assertFalse(result["ok"])
        self.assertIn("SSLCertVerificationError", result["reason"])
        self.https.assert_called_once()
        self.http.assert_not_called()
        self.conn.close.assert_called_once()


class ControllerProbeTests(unittest.TestCase):
    def test_delay_uses_configured_probe_url_and_encoded_node_name(self):
        url = "https://example.com:8443/health?x=1&y=2"
        cfg = optimizer.validate_config({"probe_url": url})
        controller = optimizer.Controller(cfg)
        with patch.object(controller, "request", return_value={"delay": 123}) as request:
            self.assertEqual(controller.delay("JP 01 / special"), 123)
        parsed = urllib.parse.urlsplit(request.call_args.args[0])
        self.assertEqual(parsed.path, "/proxies/JP%2001%20%2F%20special/delay")
        self.assertEqual(urllib.parse.parse_qs(parsed.query),
                         {"url": [url], "timeout": [str(cfg["probe_timeout_ms"])]})


if __name__ == "__main__":
    unittest.main()
