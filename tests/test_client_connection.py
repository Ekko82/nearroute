"""Client connection changes use isolated local controllers and temporary state."""

from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import urllib.parse

import optimizer


class ClientConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nearroute-client-test-")
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / "config.json"
        self.state = Path(temporary.name) / "state"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(optimizer, "CONFIG", self.config))
        self.stack.enter_context(patch.object(optimizer, "STATE", self.state))
        self.stop = self.stack.enter_context(patch.object(optimizer, "stop_daemon", return_value=True))
        self.lock = Mock()
        self.lock.acquire.return_value = True
        self.stack.enter_context(patch.object(optimizer, "InstanceLock", return_value=self.lock))
        self.secret_env = "NEARROUTE_CONNECTION_TEST_SECRET"
        self.missing_env = "NEARROUTE_CONNECTION_TEST_ABSENT"
        self.secret = "synthetic-local-controller-secret"
        self.stack.enter_context(patch.dict(os.environ, {self.secret_env: self.secret}))
        os.environ.pop(self.missing_env, None)
        self.ports = set()
        self.auth_headers = {}
        build_opener = optimizer.urllib.request.build_opener

        def local_opener(*args, **kwargs):
            opener = build_opener(*args, **kwargs)
            open_url = opener.open

            def guarded_open(request, *open_args, **open_kwargs):
                parsed = urllib.parse.urlsplit(request.full_url)
                if parsed.hostname != "127.0.0.1" or parsed.port not in self.ports:
                    raise AssertionError("test attempted a non-fixture network request")
                return open_url(request, *open_args, **open_kwargs)

            opener.open = guarded_open
            return opener

        self.stack.enter_context(patch.object(optimizer.urllib.request, "build_opener", side_effect=local_opener))
        self.initial = {
            **optimizer.DEFAULTS,
            "controller": "http://127.0.0.1:1", "controller_secret_env": "", "group": "Main",
            "codex_log_db": "", "scan_mode": "nodes", "scan_nodes": ["JP selected scope"],
            "custom_note": "preserve this",
        }
        optimizer.atomic_json(self.config, self.initial)

    def controller(self, *, mode="rule", mixed_port=7890, port=0, secret="", redirect_to=None,
                   version="test-mihomo", connections=(), extra_group=False):
        calls = []
        headers = []
        general = {"mode": mode, "mixed-port": mixed_port, "port": port, "socks-port": 7891}
        proxies = {
            "Main": {"type": "Selector", "all": ["JP one"], "now": "JP one"},
            "GLOBAL": {"type": "Selector", "all": ["Main"], "now": "Main"},
            "Auto": {"type": "URLTest", "all": ["JP one"], "now": "JP one"},
            "JP one": {"type": "Trojan"}, "DIRECT": {"type": "Direct"},
        }
        if extra_group:
            proxies["Other"] = {"type": "Selector", "all": ["JP one"], "now": "JP one"}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                headers.append(self.headers.get("Authorization"))
                authorized = not secret or self.headers.get("Authorization") == "Bearer " + secret
                calls.append(("GET", self.path, authorized))
                if authorized and redirect_to is not None:
                    self.send_response(302)
                    self.send_header("Location", redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                payload = {"/version": {"version": version}, "/configs": general,
                           "/connections": {"connections": list(connections)}}.get(self.path, {"proxies": proxies})
                data = json.dumps(payload).encode()
                self.send_response(200 if authorized else 401)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_PUT(self):
                calls.append(("PUT", self.path, False))
                self.send_error(405)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.ports.add(server.server_port)
        endpoint = f"http://127.0.0.1:{server.server_port}"
        self.auth_headers[endpoint] = headers
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()

        def close():
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive(), "temporary controller thread did not stop")

        self.addCleanup(close)
        return endpoint, calls

    def fields(self, endpoint, **updates):
        return {"controller": endpoint, "controller_secret_env": "", "group": "Main", **updates}

    def assert_config_unchanged(self):
        self.assertEqual(optimizer.read_json(self.config, None), self.initial)

    def test_automatic_discovery_verifies_api_and_does_not_broadcast_auth(self):
        current, calls = self.controller(secret=self.secret)
        other, other_calls = self.controller(secret=self.secret)
        fields = self.fields(current, controller_secret_env=self.secret_env)
        with patch.object(optimizer.client_detection, "controller_addresses", return_value=[current, other]):
            result = optimizer.discover_clients(fields)
        self.assertEqual(result["selected_controller"], current)
        self.assertEqual([item["verified"] for item in result["candidates"]], [True, False])
        self.assertTrue(result["candidates"][1]["needs_auth"])
        self.assertTrue(all(header == "Bearer " + self.secret for header in self.auth_headers[current]))
        self.assertEqual(self.auth_headers[other], [None])
        self.assertTrue(all(method == "GET" for method, _, _ in calls + other_calls))
        self.assert_config_unchanged()

    def test_automatic_discovery_rejects_non_clash_service(self):
        endpoint, calls = self.controller(version=None)
        with patch.object(optimizer.client_detection, "controller_addresses", return_value=[endpoint]):
            result = optimizer.discover_clients(self.fields(endpoint))
        self.assertEqual(result["candidates"], [])
        self.assertIsNone(result["selected_controller"])
        self.assertEqual(calls, [("GET", "/version", True)])

    def test_target_connection_disambiguates_missing_group_without_switching(self):
        endpoint, calls = self.controller(extra_group=True, connections=[
            {"metadata": {"host": "chatgpt.com"}, "chains": ["JP one", "Main"]},
            {"metadata": {"host": "example.com"}, "chains": ["JP one", "Other"]}])
        info = optimizer.inspect_controller(self.fields(endpoint, group="Missing"), adaptive=True)
        self.assertIsNone(info["active_group"])
        self.assertEqual(info["recommended_group"], "Main")
        self.assertEqual(info["nearby_regions"], ["JP"])
        self.assertTrue(all(method == "GET" for method, _, _ in calls))
        self.assertEqual(calls[-1][1], "/connections")

    def test_manual_auto_flags_can_change_without_stopping_valid_client(self):
        saved = optimizer.configure_client({"auto_detect_client": False, "auto_select_group": False})
        self.assertFalse(saved["auto_detect_client"])
        self.assertFalse(saved["auto_select_group"])
        self.assertTrue(saved["enabled"])
        self.stop.assert_not_called()

    def test_discovery_is_authenticated_read_only_and_keeps_controller_ports_distinct(self):
        endpoint_a, calls_a = self.controller(secret=self.secret)
        endpoint_b, calls_b = self.controller(mode="global", mixed_port=0, port=8090)
        before = self.config.read_bytes()
        info_a = optimizer.inspect_controller(self.fields(endpoint_a, controller_secret_env=self.secret_env))
        info_b = optimizer.inspect_controller(self.fields(endpoint_b))
        self.assertEqual(info_a["mode"], "rule")
        self.assertEqual(info_a["active_group"], "Main")
        self.assertEqual(info_a["proxy_port"], 7890)
        self.assertEqual(set(info_a["groups"]), {"Main", "GLOBAL"})
        self.assertEqual(info_b["mode"], "global")
        self.assertEqual(info_b["active_group"], "GLOBAL")
        self.assertEqual(info_b["proxy_port"], 8090)
        for calls in (calls_a, calls_b):
            self.assertEqual(calls, [("GET", "/configs", True), ("GET", "/proxies", True)])
        self.assertEqual(self.config.read_bytes(), before)
        self.stop.assert_not_called()

    def test_discovery_handles_direct_and_unknown_saved_group_without_selecting(self):
        for mode, active in (("direct", None), ("rule", None)):
            with self.subTest(mode=mode):
                endpoint, calls = self.controller(mode=mode)
                info = optimizer.inspect_controller(self.fields(endpoint, group="Previous client group"))
                self.assertEqual(info["active_group"], active)
                self.assertTrue(all(method == "GET" for method, _path, _auth in calls))
        self.assert_config_unchanged()

    def test_authenticated_discovery_does_not_forward_a_redirect(self):
        destination, destination_calls = self.controller()
        source, source_calls = self.controller(secret=self.secret, redirect_to=destination + "/configs")
        with self.assertRaisesRegex(optimizer.ControllerError, "HTTP 302"):
            optimizer.inspect_controller(self.fields(source, controller_secret_env=self.secret_env))
        self.assertEqual(source_calls, [("GET", "/configs", True)])
        self.assertEqual(destination_calls, [])
        self.assert_config_unchanged()
        self.stop.assert_not_called()

    def test_controller_http_errors_close_the_response_before_raising(self):
        for code in (401, 302):
            with self.subTest(http_status=code):
                response = io.BytesIO(b"synthetic error response")
                error = optimizer.urllib.error.HTTPError(
                    "http://127.0.0.1:1/configs", code, "fixture error", {}, response,
                )
                self.addCleanup(error.close)
                opener = Mock()
                opener.open.side_effect = error
                with patch.object(optimizer.urllib.request, "build_opener", return_value=opener):
                    with self.assertRaisesRegex(optimizer.ControllerError, f"^控制接口 HTTP {code}$") as caught:
                        optimizer.Controller(self.initial).request("/configs")
                self.assertIs(caught.exception.__context__, error)
                self.assertTrue(response.closed)

    def test_invalid_endpoint_or_environment_is_rejected_before_stopping(self):
        endpoint, calls = self.controller()
        for change in (
            {"controller": "https://127.0.0.1:9790"},
            {"controller": "http://example.org:9790"},
            {"controller": "http://user:password@127.0.0.1:9790"},
            {"controller": endpoint + "?token=not-a-controller-path"},
            {"controller": "http://127.0.0.1:0"},
            {"controller": "http://127.0.0.1:65536"},
            {"controller_secret_env": self.missing_env},
            {"controller_secret_env": "INVALID NAME"},
            {"enabled": False},
        ):
            with self.subTest(change=change):
                with self.assertRaises(ValueError):
                    optimizer.configure_client(self.fields(endpoint, **change))
                self.assert_config_unchanged()
        self.assertEqual(calls, [])
        self.stop.assert_not_called()

    def test_failed_auth_or_missing_http_port_discovery_preserves_config(self):
        for server_args in ({"secret": "different-synthetic-secret"}, {"mixed_port": 0, "port": 0}):
            with self.subTest(case=server_args):
                endpoint, calls = self.controller(**server_args)
                with self.assertRaises(optimizer.ControllerError):
                    optimizer.inspect_controller(self.fields(endpoint))
                self.assert_config_unchanged()
                self.assertTrue(all(method == "GET" for method, _path, _auth in calls))
        self.stop.assert_not_called()

    def test_connection_change_merges_latest_config_pauses_and_clears_old_ui_rows(self):
        endpoint, calls = self.controller(secret=self.secret)
        optimizer.atomic_json(self.state / "status.json", {
            "phase": "monitoring", "selected": "old client node", "nodes": [{"name": "old client node"}],
            "controller_available": True,
        })

        def stopped():
            latest = optimizer.load_config()
            latest.update(custom_note="changed while stopping", health_interval_seconds=30)
            optimizer.atomic_json(self.config, latest)
            return True

        self.stop.side_effect = stopped
        result = optimizer.configure_client(
            self.fields(endpoint, controller_secret_env=self.secret_env),
            expected_context=optimizer.state_context(self.initial),
        )
        self.assertFalse(result["enabled"])
        self.assertEqual(result["custom_note"], "changed while stopping")
        self.assertEqual(result["health_interval_seconds"], 30)
        self.assertEqual(result["scan_nodes"], self.initial["scan_nodes"])
        self.assertEqual(result["scan_mode"], "nodes")
        self.assertEqual(result["controller_secret_env"], self.secret_env)
        self.assertNotIn(self.secret, self.config.read_text(encoding="utf-8"))
        self.assertEqual(os.environ[self.secret_env], self.secret)
        status = optimizer.read_json(self.state / "status.json", {})
        self.assertEqual(status["phase"], "stopped")
        self.assertEqual(status["nodes"], [])
        self.assertIsNone(status.get("selected"))
        self.assertEqual(status["controller_context"], list(optimizer.state_context(result)))
        self.assertTrue(all(method == "GET" for method, _path, _auth in calls))
        self.stop.assert_called_once()
        self.lock.close.assert_called_once()

    def test_same_connection_preserves_enabled_and_does_not_stop_helper(self):
        endpoint, _calls = self.controller()
        self.initial.update(self.fields(endpoint))
        optimizer.atomic_json(self.config, self.initial)
        result = optimizer.configure_client(self.fields(endpoint), expected_context=list(optimizer.state_context(self.initial)))
        self.assertTrue(result["enabled"])
        self.assertEqual(result["custom_note"], self.initial["custom_note"])
        self.stop.assert_not_called()
        self.lock.acquire.assert_not_called()

    def test_new_connection_discards_old_scan_before_paused_helper_restarts(self):
        self.state.mkdir()
        request = self.state / "scan.request"
        request.touch()
        saved = optimizer.configure_client(
            self.fields("http://127.0.0.1:2"), expected_context=optimizer.state_context(self.initial),
        )
        self.assertFalse(saved["enabled"])
        self.assertFalse(request.exists())
        self.lock.close.assert_called_once()

        helper = optimizer.Optimizer()
        stopped = {"value": False}

        def finish(*_args, **_kwargs):
            stopped["value"] = True

        with patch.object(helper, "stopping", side_effect=lambda: stopped["value"]), \
             patch.object(helper, "scan", side_effect=finish) as scan, \
             patch.object(helper, "observe_paused_path", side_effect=finish) as observe:
            helper.run()
        scan.assert_not_called()
        observe.assert_called_once()

    def test_same_connection_keeps_pending_scan(self):
        self.state.mkdir()
        request = self.state / "scan.request"
        request.touch()
        optimizer.configure_client(
            self.fields(self.initial["controller"]), expected_context=optimizer.state_context(self.initial),
        )
        self.assertTrue(request.exists())
        self.assert_config_unchanged()
        self.stop.assert_not_called()
        self.lock.acquire.assert_not_called()

    def test_rejected_connection_change_keeps_pending_scan(self):
        self.state.mkdir()
        request = self.state / "scan.request"
        for reason in ("stop", "lock", "context", "context_after_stop"):
            with self.subTest(reason=reason):
                optimizer.atomic_json(self.config, self.initial)
                request.touch()
                self.stop.side_effect = None
                self.stop.return_value = reason != "stop"
                self.lock.acquire.return_value = reason != "lock"
                expected = optimizer.state_context(self.initial)
                stored_context = expected
                if reason == "context":
                    expected = ("http://127.0.0.1:3", *expected[1:])
                elif reason == "context_after_stop":
                    stored_context = ("http://127.0.0.1:3", *expected[1:])
                    def changed_while_stopping():
                        optimizer.atomic_json(self.config, self.initial | {"controller": "http://127.0.0.1:3"})
                        return True
                    self.stop.side_effect = changed_while_stopping
                with self.assertRaises((RuntimeError, ValueError)):
                    optimizer.configure_client(self.fields("http://127.0.0.1:2"), expected_context=expected)
                self.assertTrue(request.exists())
                self.assertEqual(optimizer.state_context(optimizer.load_config()), stored_context)

    def test_config_write_failure_restores_only_the_existing_scan_intent(self):
        self.state.mkdir()
        request = self.state / "scan.request"
        write = optimizer.atomic_json

        def occupied_config(path, value):
            if Path(path) == self.config:
                self.assertFalse(request.exists())
                raise PermissionError("fixture config file is occupied")
            return write(path, value)

        for had_request in (True, False):
            with self.subTest(had_request=had_request):
                request.touch() if had_request else request.unlink(missing_ok=True)
                self.lock.close.reset_mock()
                with patch.object(optimizer, "atomic_json", side_effect=occupied_config):
                    with self.assertRaises(PermissionError):
                        optimizer.configure_client(self.fields("http://127.0.0.1:2"))
                self.assertEqual(request.exists(), had_request)
                self.lock.close.assert_called_once()
                with optimizer.config_update_lock():
                    self.assert_config_unchanged()

    def test_status_write_failure_preserves_connection_and_releases_locks(self):
        write = optimizer.atomic_json

        def occupied_status(path, value):
            if Path(path) == self.state / "status.json":
                raise PermissionError("fixture status file is occupied")
            return write(path, value)

        with patch.object(optimizer, "atomic_json", side_effect=occupied_status):
            with self.assertRaises(PermissionError):
                optimizer.configure_client(self.fields("http://127.0.0.1:2"))
        self.lock.close.assert_called_once()
        with optimizer.config_update_lock():
            self.assert_config_unchanged()

    def test_pending_transaction_is_preserved_and_blocks_connection_change(self):
        endpoint, _calls = self.controller()
        pending = {"transaction_id": "synthetic-pending", "candidate": "JP pending", **optimizer.controller_identity(self.initial)}
        optimizer.atomic_json(self.state / "pending-selection.json", pending)
        with self.assertRaises(RuntimeError):
            optimizer.configure_client(self.fields(endpoint))
        self.assert_config_unchanged()
        self.assertEqual(optimizer.read_json(self.state / "pending-selection.json", None), pending)
        self.lock.close.assert_called_once()

    def test_stop_or_instance_lock_failure_does_not_modify_connection(self):
        endpoint, _calls = self.controller()
        for stop_ok, lock_ok in ((False, True), (True, False)):
            with self.subTest(stop=stop_ok, lock=lock_ok):
                self.stop.return_value = stop_ok
                self.lock.acquire.return_value = lock_ok
                with self.assertRaises(RuntimeError):
                    optimizer.configure_client(self.fields(endpoint))
                self.assert_config_unchanged()

    def test_old_context_cannot_queue_nodes_on_new_client_and_status_names_its_context(self):
        endpoint, _calls = self.controller()
        old_context = optimizer.state_context(self.initial)
        current = self.initial | self.fields(endpoint)
        optimizer.atomic_json(self.config, current)
        with self.assertRaises(ValueError):
            optimizer.configure_client(self.fields(endpoint), expected_context=old_context)
        self.assertEqual(optimizer.read_json(self.config, None), current)
        self.stop.assert_not_called()
        request_path = self.state / "select.request.json"
        original = {"request_id": "already-pending", "name": "JP original"}
        optimizer.atomic_json(request_path, original)
        with self.assertRaises(ValueError):
            optimizer.request_manual_selection("JP stale table", expected_context=old_context)
        self.assertEqual(optimizer.read_json(request_path, None), original)
        request_id = optimizer.request_manual_selection("JP new client", expected_context=list(optimizer.state_context(current)))
        request = optimizer.read_json(request_path, {})
        self.assertEqual(request["request_id"], request_id)
        self.assertEqual(request["name"], "JP new client")
        self.assertEqual(request["controller"], endpoint)
        for label, context in (("old client", {"controller_context": list(old_context)}), ("legacy", {})):
            with self.subTest(cached_status=label):
                optimizer.atomic_json(self.state / "status.json", {
                    **context, "selected": "JP stale table", "group": "Old client group",
                    "nodes": [{"name": "JP stale table"}], "health": {"ok": True},
                })
                helper = optimizer.Optimizer()
                status = optimizer.read_json(self.state / "status.json", {})
                self.assertEqual(status["controller_context"], list(optimizer.state_context(current)))
                self.assertEqual(status.get("nodes", []), [])
                self.assertIsNone(status.get("selected"))
                self.assertIsNone(status.get("group"))
                self.assertIsNone(status.get("health"))
                helper.write_status(message="test snapshot")
                self.assertEqual(optimizer.read_json(self.state / "status.json", {})["controller_context"],
                                 list(optimizer.state_context(current)))


if __name__ == "__main__":
    unittest.main()
