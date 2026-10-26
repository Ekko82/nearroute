"""Controller capabilities are checked offline without touching user clients."""

import io
import json
import os
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.parse

import client_detection as detection
import optimizer


class ClientCapabilityTests(unittest.TestCase):
    def setUp(self):
        self.current = "http://127.0.0.1:9790"
        self.other = "http://127.0.0.1:9090"
        self.secret_env = "NEARROUTE_CAPABILITY_TEST_SECRET"
        self.secret = "synthetic-capability-secret"
        self.cfg = optimizer.validate_config({
            "controller": self.current, "controller_secret_env": self.secret_env,
            "group": "Main",
        })
        environment = patch.dict(os.environ, {self.secret_env: self.secret})
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def information():
        return {"groups": ["Main"], "active_group": "Main", "mode": "rule", "proxy_port": 7890}

    def discover(self, inspector, addresses=None):
        with patch.object(detection, "controller_addresses", return_value=addresses or [self.current, self.other]):
            return detection.discover_clients(self.cfg, inspector)

    def test_get_configs_405_is_a_safe_capability_error_and_closes_response(self):
        response = io.BytesIO(("private-body " + self.secret).encode())
        error = urllib.error.HTTPError(self.current + "/configs", 405, self.secret, {}, response)
        self.addCleanup(error.close)
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(optimizer.urllib.request, "build_opener", return_value=opener):
            with self.assertRaises(optimizer.ControllerError) as caught:
                optimizer.Controller(self.cfg).request("/configs")

        self.assertTrue(response.closed)
        self.assertIs(type(caught.exception), optimizer.ConfigReadUnsupported)
        self.assertIs(optimizer.ConfigReadUnsupported.unsupported_config_read, True)
        self.assertEqual(str(caught.exception), str(optimizer.ConfigReadUnsupported()))
        self.assertIn("GET /configs", str(caught.exception))
        self.assertIn("HTTP 405", str(caught.exception))
        for private_text in (self.secret, self.secret_env, "private-body"):
            self.assertNotIn(private_text, str(caught.exception))

    def test_other_http_errors_keep_their_status_without_capability_marker(self):
        cases = [("GET", "/version", 405), ("GET", "/proxies", 405),
                 ("PATCH", "/configs", 405), ("GET", "/configs", 401),
                 ("GET", "/configs", 403), ("GET", "/configs", 500)]
        for method, path, code in cases:
            with self.subTest(method=method, path=path, code=code):
                response = io.BytesIO(("private-body " + self.secret).encode())
                error = urllib.error.HTTPError(self.current + path, code, self.secret, {}, response)
                self.addCleanup(error.close)
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(optimizer.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaises(optimizer.ControllerError) as caught:
                        optimizer.Controller(self.cfg).request(path, method=method)
                self.assertTrue(response.closed)
                self.assertFalse(getattr(caught.exception, "unsupported_config_read", False))
                self.assertEqual(str(caught.exception), f"控制接口 HTTP {code}")
                self.assertNotIn(self.secret, str(caught.exception))
                self.assertNotIn("private-body", str(caught.exception))

    def test_adaptive_discovery_requires_valid_version_before_retaining_configs_405(self):
        cases = [("valid version", {"version": "test-mihomo"}, True),
                 ("version HTTP 405", 405, False),
                 ("invalid version", {"version": None}, False)]
        for label, version, retained in cases:
            with self.subTest(case=label):
                calls = []
                responses = []

                def open_request(request, **_kwargs):
                    path = urllib.parse.urlsplit(request.full_url).path
                    calls.append((request.get_method(), path))
                    if path == "/version" and isinstance(version, dict):
                        response = io.BytesIO(json.dumps(version).encode())
                        responses.append(response)
                        return response
                    self.assertIn(path, ("/version", "/configs"))
                    response = io.BytesIO(b"private response body")
                    responses.append(response)
                    raise urllib.error.HTTPError(request.full_url, 405, "fixture", {}, response)

                opener = Mock()
                opener.open.side_effect = open_request
                with patch.object(optimizer.urllib.request, "build_opener", return_value=opener):
                    with patch.object(detection, "controller_addresses", return_value=[self.current]):
                        result = optimizer.discover_clients(self.cfg)
                self.assertEqual(len(result["candidates"]), int(retained))
                if retained:
                    self.assertTrue(result["candidates"][0]["unsupported"])
                self.assertEqual(calls, [("GET", "/version"), ("GET", "/configs")] if retained
                                 else [("GET", "/version")])
                self.assertTrue(all(response.closed for response in responses))
                self.assertIsNone(result["selected_controller"])

    def test_configured_unsupported_client_blocks_automatic_migration_and_keeps_secret_local(self):
        seen = {}

        def inspector(fields, **_kwargs):
            seen[fields["controller"]] = fields["controller_secret_env"]
            if fields["controller"] == self.current:
                raise optimizer.ConfigReadUnsupported()
            return self.information()

        result = self.discover(inspector)
        self.assertIsNone(result["selected_controller"])
        self.assertEqual(len(result["candidates"]), 2)
        current = result["candidates"][0]
        self.assertEqual(current["controller"], self.current)
        self.assertFalse(current["verified"])
        self.assertFalse(current["needs_auth"])
        self.assertTrue(current["unsupported"])
        self.assertTrue(result["candidates"][1]["verified"])
        self.assertEqual(seen, {self.current: self.secret_env, self.other: ""})
        self.assertNotIn(self.secret, str(result))

    def test_only_unsupported_client_is_retained_with_capability_explanation(self):
        def inspector(_fields, **_kwargs):
            raise optimizer.ConfigReadUnsupported()

        result = self.discover(inspector, [self.current])
        self.assertEqual(result["candidates"], [{"controller": self.current, "verified": False,
                                                "needs_auth": False, "unsupported": True}])
        self.assertIsNone(result["selected_controller"])
        self.assertRegex(result["message"], r"不支持|HTTP 405|GET /configs")
        self.assertNotIn("未发现", result["message"])
        self.assertNotIn("设置密钥", result["message"])

    def test_valid_configured_client_stays_selected_with_unsupported_alternative(self):
        def inspector(fields, **_kwargs):
            if fields["controller"] == self.other:
                raise optimizer.ConfigReadUnsupported()
            return self.information()

        result = self.discover(inspector)
        self.assertEqual(result["selected_controller"], self.current)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertTrue(result["candidates"][0]["verified"])
        self.assertTrue(result["candidates"][1]["unsupported"])

    def test_unsupported_alternative_prevents_selecting_a_different_valid_client(self):
        third = "http://127.0.0.1:32123"

        def inspector(fields, **_kwargs):
            if fields["controller"] == self.current:
                raise OSError("closed synthetic client")
            if fields["controller"] == self.other:
                raise optimizer.ConfigReadUnsupported()
            return self.information()

        result = self.discover(inspector, [self.current, self.other, third])
        self.assertIsNone(result["selected_controller"])
        self.assertEqual([item["controller"] for item in result["candidates"]], [self.other, third])
        self.assertTrue(result["candidates"][0]["unsupported"])
        self.assertTrue(result["candidates"][1]["verified"])


if __name__ == "__main__":
    unittest.main()
