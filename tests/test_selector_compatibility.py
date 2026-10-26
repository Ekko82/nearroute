"""Explicit selector compatibility never invents mode or active-route evidence."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import threading
import unittest
import urllib.parse
from unittest.mock import patch

import optimizer
from tests import test_runtime


class SelectorCompatibilityTests(unittest.TestCase):
    @contextmanager
    def controller(self):
        """A local synthetic API; delay URLs are recorded, never fetched."""
        state = {
            "calls": [],
            "delay_queries": [],
            "proxies": {
                "Main": {"type": "Selector", "all": ["JP old", "JP fast"], "now": "JP old"},
                "JP old": {"type": "Shadowsocks"},
                "JP fast": {"type": "Trojan"},
            },
        }
        secret_env = "NEARROUTE_SELECTOR_TEST_SECRET"
        secret = "synthetic-selector-secret"

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *_args):
                pass

            def send_json(self, code, value):
                body = json.dumps(value).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urllib.parse.urlsplit(self.path)
                state["calls"].append(("GET", parsed.path))
                if self.headers.get("Authorization") != "Bearer " + secret:
                    self.send_json(401, {})
                elif parsed.path == "/version":
                    self.send_json(200, {"version": "fixture-mihomo"})
                elif parsed.path == "/configs":
                    if state.get("general") is None:
                        self.send_json(405, {"message": "read disabled in fixture"})
                    else:
                        self.send_json(200, state["general"])
                elif parsed.path == "/proxies":
                    self.send_json(200, {"proxies": state["proxies"]})
                elif parsed.path.endswith("/delay") and parsed.path.startswith("/proxies/"):
                    state["delay_queries"].append(urllib.parse.parse_qs(parsed.query))
                    self.send_json(200, {"delay": 100})
                else:
                    self.send_json(404, {})

            def do_PUT(self):
                state["calls"].append(("PUT", self.path))
                self.send_json(500, {"message": "inspection must never select"})

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        cfg = dict(optimizer.DEFAULTS, controller=f"http://127.0.0.1:{server.server_port}",
                   controller_secret_env=secret_env, group="Main", controller_api_mode="selector")
        try:
            with patch.dict(os.environ, {secret_env: secret}):
                yield cfg, state
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive(), "the fixture server must be released")

    @contextmanager
    def runtime(self):
        fixture = test_runtime.RuntimeTests()
        try:
            fixture.setUp()
            fixture.cfg.update(controller_api_mode="selector", verification_idle_seconds=1, concurrency=1)
            original_request = fixture.api.request

            def request(path, *args, **kwargs):
                if path == "/configs":
                    raise AssertionError("selector mode cannot read or synthesize General")
                return original_request(path, *args, **kwargs)

            fixture.stack.enter_context(patch.object(fixture.api, "request", side_effect=request))
            fixture.helper = optimizer.Optimizer()
            yield fixture
        finally:
            fixture.doCleanups()

    def assert_selector_evidence(self, result):
        self.assertEqual(result.get("verification_scope"), "selector")
        for invented_evidence in ("http_status", "proxy_port", "mode", "tls_reused"):
            self.assertNotIn(invented_evidence, result)

    def test_configuration_requires_an_explicit_supported_api_mode(self):
        self.assertEqual(optimizer.validate_config({})["controller_api_mode"], "standard")
        self.assertEqual(optimizer.validate_config({"controller_api_mode": "selector"})["controller_api_mode"],
                         "selector")
        for invalid in ("auto", "rule", None, True):
            with self.subTest(mode=invalid), self.assertRaises(ValueError):
                optimizer.validate_config({"controller_api_mode": invalid})

    def test_context_and_probe_history_separate_the_two_contracts(self):
        standard = optimizer.validate_config({"group": "Main"})
        selector = standard | {"controller_api_mode": "selector"}
        self.assertEqual(len(optimizer.state_context(standard)), 3)
        self.assertEqual(optimizer.state_context(selector), (*optimizer.state_context(standard), "selector"))
        self.assertNotEqual(optimizer.probe_identity(standard), optimizer.probe_identity(selector))

    def test_managed_authentication_refreshes_without_restarting_the_process(self):
        name = "NEARROUTE_MAOMAO_SECRET_9790"
        old, imported, renewed = "a" * 48, "b" * 48, "c" * 48
        cfg = dict(optimizer.DEFAULTS, controller="http://127.0.0.1:9790", controller_secret_env=name,
                   controller_api_mode="selector")
        with patch.dict(os.environ, {name: old}), patch.object(optimizer.windows_app, "IS_WINDOWS", True):
            with patch.object(optimizer.windows_app, "_read_user_environment",
                              side_effect=[imported, renewed]) as read:
                self.assertEqual(optimizer.Controller(cfg).secret, imported)
                self.assertEqual(optimizer.Controller(cfg).secret, renewed)
            self.assertEqual([call.args for call in read.call_args_list], [(name,), (name,)])
            self.assertEqual(os.environ[name], old)

    def test_invalid_managed_authentication_never_reuses_stale_process_credentials(self):
        name = "NEARROUTE_MAOMAO_SECRET_9790"
        custom = "NEARROUTE_SELECTOR_CUSTOM_SECRET"
        cfg = dict(optimizer.DEFAULTS, controller="http://127.0.0.1:9790", controller_secret_env=name,
                   controller_api_mode="selector")
        with patch.dict(os.environ, {name: "a" * 48, custom: "ordinary-custom-secret"}):
            with patch.object(optimizer.windows_app, "IS_WINDOWS", True):
                for invalid in ("deleted", "", "not-a-credential", "b" * 47, None, 123):
                    with self.subTest(registry_value=invalid):
                        replacement = ({"side_effect": FileNotFoundError()} if invalid == "deleted"
                                       else {"return_value": invalid})
                        with patch.object(optimizer.windows_app, "_read_user_environment", **replacement):
                            self.assertEqual(optimizer.Controller(cfg).secret, "")
                with patch.object(optimizer.windows_app, "_read_user_environment", return_value="b" * 48) as read:
                    self.assertEqual(optimizer.Controller(cfg | {"controller": "http://127.0.0.1:9090"}).secret, "")
                    read.assert_not_called()
                with patch.object(optimizer.windows_app, "_read_user_environment",
                                  side_effect=AssertionError("custom environment must not access the registry")):
                    self.assertEqual(optimizer.Controller(cfg | {"controller_secret_env": custom}).secret,
                                     "ordinary-custom-secret")

    def test_selector_inspection_succeeds_without_general_or_an_invented_port(self):
        with self.controller() as (cfg, state):
            result = optimizer.inspect_controller(cfg, adaptive=True)
        self.assertEqual(result["mode"], "unknown")
        self.assertIsNone(result["proxy_port"])
        self.assertEqual(result["active_group"], "Main")
        self.assertEqual(result["groups"], ["Main"])
        self.assertEqual(result["controller_api_mode"], "selector")
        self.assertEqual(result["verification_scope"], "selector")
        self.assertNotIn(("GET", "/configs"), state["calls"])
        self.assertTrue(all(method == "GET" for method, _path in state["calls"]))

    def test_snapshot_metadata_is_not_a_fake_general_configuration(self):
        with self.controller() as (cfg, state):
            metadata, proxies, group = optimizer.Controller(cfg).snapshot(cfg)
        self.assertEqual(metadata, {"controller_api_mode": "selector"})
        self.assertEqual(optimizer.active_mode(metadata, group), ("unknown", "Main"))
        self.assertEqual(optimizer.active_path(metadata, proxies, group), ("unknown", "Main", "JP old", "JP old"))
        self.assertNotIn(("GET", "/configs"), state["calls"])

    def test_standard_mode_never_silently_falls_back_after_configs_405(self):
        with self.controller() as (cfg, state):
            cfg["controller_api_mode"] = "standard"
            with self.assertRaises(optimizer.ConfigReadUnsupported):
                optimizer.Controller(cfg).snapshot(cfg)
            with self.assertRaises(optimizer.ConfigReadUnsupported):
                optimizer.inspect_controller(cfg, adaptive=True)
        self.assertNotIn(("GET", "/proxies"), state["calls"])
        self.assertTrue(all(method == "GET" for method, _path in state["calls"]))

    def test_standard_general_cannot_enable_compatibility_via_a_provider_extension(self):
        with self.controller() as (cfg, state):
            cfg["controller_api_mode"] = "standard"
            state["general"] = {"mode": "rule", "mixed-port": 7890, "controller_api_mode": "selector"}
            general, _proxies, group = optimizer.Controller(cfg).snapshot(cfg)
        self.assertEqual(optimizer.active_mode(general, group), ("rule", "Main"))
        self.assertEqual(optimizer.local_proxy_endpoint(general), ("127.0.0.1", 7890))

    def test_selector_snapshot_still_requires_the_configured_selector(self):
        with self.controller() as (cfg, state):
            state["proxies"]["Main"]["type"] = "URLTest"
            with self.assertRaises(optimizer.ControllerError):
                optimizer.Controller(cfg).snapshot(cfg)
        self.assertTrue(all(method == "GET" for method, _path in state["calls"]))

    def test_node_probe_passes_the_configured_target_and_expected_statuses(self):
        with self.controller() as (cfg, state):
            cfg.update(probe_url="https://example.invalid/custom-health", probe_expected_statuses=[204, 405])
            self.assertEqual(optimizer.Controller(cfg).delay("JP fast"), 100)
        self.assertEqual(len(state["delay_queries"]), 1)
        query = state["delay_queries"][0]
        self.assertEqual(query["url"], [cfg["probe_url"]])
        self.assertEqual(query["expected"], ["204,405"])
        self.assertTrue(all(method == "GET" for method, _path in state["calls"]))

    def test_probe_measures_the_selected_leaf_and_never_opens_a_proxy_connection(self):
        with self.runtime() as fixture:
            metadata, _, _ = fixture.api.snapshot(fixture.cfg)
            with patch.object(fixture.api, "delay", wraps=fixture.api.delay) as delay:
                result = fixture.helper.verify_path(metadata, idle_seconds=1)
            self.assertTrue(result["ok"])
            self.assert_selector_evidence(result)
            self.assertGreaterEqual(delay.call_count, 3)
            self.assertTrue(all(call.args == ("JP old",) for call in delay.call_args_list))
            fixture.verify.assert_not_called()

    def test_probe_cancellation_is_not_a_failed_node(self):
        with self.runtime() as fixture:
            metadata, _, _ = fixture.api.snapshot(fixture.cfg)
            cancelled = [False]

            def delay(_name):
                cancelled[0] = True
                return 100

            with patch.object(fixture.api, "delay", side_effect=delay):
                result = fixture.helper.verify_path(metadata, idle_seconds=0, cancel=lambda: cancelled[0])
            self.assertFalse(result["ok"])
            self.assertTrue(result["cancelled"])
            self.assertEqual(fixture.helper.quarantine, {})
            fixture.verify.assert_not_called()

    def test_candidate_commit_is_scoped_to_the_selected_group(self):
        with self.runtime() as fixture:
            metadata, proxies, group = fixture.api.snapshot(fixture.cfg)
            path = optimizer.active_path(metadata, proxies, group)
            self.assertTrue(fixture.helper.change_candidate(metadata, group, "JP old", "JP fast", expected_path=path))
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
            saved = optimizer.read_json(fixture.state / "selection.json", {})
            self.assertEqual(saved["name"], "JP fast")
            self.assert_selector_evidence(saved["verification"])
            self.assertEqual(fixture.helper.status["mode"], "unknown")
            fixture.verify.assert_not_called()

    def test_automatic_scan_selects_a_node_with_only_selector_evidence(self):
        with self.runtime() as fixture:
            fixture.helper.scan(forced=True)
            self.assertEqual(fixture.api.proxies["Main"]["now"], "JP fast")
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
            self.assert_selector_evidence(fixture.helper.status["verification"])
            self.assertEqual(fixture.helper.status["mode"], "unknown")
            self.assertNotIn("已选用通过连接验证", fixture.helper.status["message"])
            fixture.verify.assert_not_called()

    def test_failed_candidate_rolls_back_only_the_owned_change(self):
        with self.runtime() as fixture:
            metadata, _, group = fixture.api.snapshot(fixture.cfg)
            with patch.object(fixture.api, "delay", return_value=None):
                self.assertFalse(fixture.helper.change_candidate(metadata, group, "JP old", "JP fast"))
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
            self.assertEqual(fixture.api.proxies[group]["now"], "JP old")
            self.assertFalse((fixture.state / "selection.json").exists())
            fixture.helper.recover_pending()
            self.assertFalse((fixture.state / "pending-selection.json").exists())
            fixture.verify.assert_not_called()

    def test_manual_change_before_put_cancels_stale_candidate(self):
        with self.runtime() as fixture:
            metadata, proxies, group = fixture.api.snapshot(fixture.cfg)
            path = optimizer.active_path(metadata, proxies, group)
            fixture.api.proxies[group]["now"] = "JP manual"
            with self.assertRaises(optimizer.PathChanged):
                fixture.helper.change_candidate(metadata, group, "JP old", "JP fast", expected_path=path)
            self.assertEqual(fixture.api.select_calls, [])
            self.assertEqual(fixture.api.proxies[group]["now"], "JP manual")

    def test_manual_change_during_node_checks_is_preserved(self):
        with self.runtime() as fixture:
            metadata, _, group = fixture.api.snapshot(fixture.cfg)

            def delay(_name):
                fixture.api.proxies[group]["now"] = "JP manual"
                return 100

            with patch.object(fixture.api, "delay", side_effect=delay):
                self.assertFalse(fixture.helper.change_candidate(metadata, group, "JP old", "JP fast"))
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
            self.assertEqual(fixture.api.proxies[group]["now"], "JP manual")
            self.assertFalse((fixture.state / "selection.json").exists())
            self.assertNotIn("JP fast", fixture.helper.quarantine)
            fixture.helper.recover_pending()
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])

    def test_candidate_replaced_by_a_group_cannot_inherit_leaf_verification(self):
        with self.runtime() as fixture:
            metadata, _, group = fixture.api.snapshot(fixture.cfg)

            def delay(_name):
                fixture.api.proxies["JP fast"] = {
                    "type": "Selector", "all": ["JP manual"], "now": "JP manual",
                }
                return 100

            with patch.object(fixture.api, "delay", side_effect=delay):
                with self.assertRaises(optimizer.PathChanged):
                    fixture.helper.change_candidate(metadata, group, "JP old", "JP fast")
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
            self.assertFalse((fixture.state / "selection.json").exists())
            self.assertEqual(optimizer.resolve_leaf(fixture.api.proxies, "JP fast"), "JP manual")
            self.assertNotIn("JP fast", fixture.helper.quarantine)

    def test_manual_selection_works_when_paused_with_unknown_actual_mode(self):
        with self.runtime() as fixture:
            fixture.cfg["enabled"] = False
            request_id = optimizer.request_manual_selection("JP manual", expected_mode=("unknown", "Main"))
            self.assertTrue(fixture.helper.process_manual_selection())
            self.assertEqual(fixture.api.select_calls, [("Main", "JP manual")])
            self.assertEqual(fixture.helper.status["manual_selection_result"]["request_id"], request_id)
            self.assertEqual(fixture.helper.status["mode"], "unknown")
            self.assertEqual(fixture.helper.status["phase"], "paused")
            fixture.verify.assert_not_called()

    def test_queued_request_cannot_cross_from_standard_to_selector_contract(self):
        with self.runtime() as fixture:
            fixture.cfg["controller_api_mode"] = "standard"
            optimizer.request_manual_selection("JP manual")
            fixture.cfg["controller_api_mode"] = "selector"
            self.assertFalse(fixture.helper.process_manual_selection())
            self.assertEqual(fixture.api.select_calls, [])
            self.assertFalse(fixture.helper.status["manual_selection_result"]["ok"])

    def test_restore_uses_ownership_without_needing_general(self):
        with self.runtime() as fixture:
            metadata, _, group = fixture.api.snapshot(fixture.cfg)
            self.assertTrue(fixture.helper.change_candidate(metadata, group, "JP old", "JP fast"))
            fixture.helper.recover_pending()
            with patch.object(optimizer, "stop_daemon", return_value=True):
                self.assertEqual(optimizer.restore_original(), ["Main"])
            self.assertEqual(fixture.api.proxies[group]["now"], "JP old")
            status = optimizer.read_json(fixture.state / "status.json", {})
            self.assertTrue(status["controller_available"])
            self.assertEqual(status["mode"], "unknown")
            self.assertIsNone(status["health"])


if __name__ == "__main__":
    unittest.main()
