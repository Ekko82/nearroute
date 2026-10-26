"""Adaptive connection changes use isolated config/state and never touch real clients."""

from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import optimizer
from tests.test_runtime import FakeController


class AdaptiveRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix="nearroute-adaptive-"))
        self.config = Path(folder) / "config.json"
        self.state = Path(folder) / "state"
        self.stack.enter_context(patch.object(optimizer, "CONFIG", self.config))
        self.stack.enter_context(patch.object(optimizer, "STATE", self.state))
        self.cfg = optimizer.validate_config({"group": "Main", "codex_log_db": "",
                                              "controller_secret_env": "OLD_CLIENT_SECRET"})
        optimizer.atomic_json(self.config, self.cfg)
        self.api = FakeController()
        self.stack.enter_context(patch.object(optimizer, "Controller", return_value=self.api))
        signals = Mock(last_new_error_at=None)
        signals.poll.return_value = 0
        self.stack.enter_context(patch.object(optimizer, "LogSignals", return_value=signals))
        self.discovery = self.stack.enter_context(patch.object(optimizer, "discover_clients"))
        self.discovery.return_value = self.result()
        self.helper = optimizer.Optimizer()

    def result(self, endpoint="http://127.0.0.1:19090", group="Main"):
        return {"selected_controller": endpoint, "message": "识别到唯一可靠结果", "candidates": [
            {"controller": endpoint, "verified": True, "mode": "rule", "groups": [group],
             "recommended_group": group, "active_group": group}]}

    def test_unique_result_changes_context_without_put_and_preserves_enable_state(self):
        self.helper.write_status(nodes=[{"name": "old cached"}], selected="old", health={"ok": True})
        self.helper.add_sample("JP old", 250)
        self.assertTrue(self.helper.adapt_client())
        saved = optimizer.load_config()
        self.assertEqual(saved["controller"], "http://127.0.0.1:19090")
        self.assertEqual(saved["controller_secret_env"], "")
        self.assertTrue(saved["enabled"])
        self.assertEqual(self.helper.state_context, optimizer.state_context(saved))
        self.assertEqual(self.helper.status["nodes"], [])
        self.assertIsNone(self.helper.status["selected"])
        self.assertIsNone(self.helper.status["health"])
        self.assertEqual(self.helper.history, {})
        self.assertEqual(self.api.select_calls, [])

    def test_paused_client_stays_paused_after_unique_detection(self):
        optimizer.atomic_json(self.config, self.cfg | {"enabled": False})
        self.helper.reload_config()
        self.assertTrue(self.helper.adapt_client())
        self.assertFalse(optimizer.load_config()["enabled"])
        self.assertEqual(self.helper.status["phase"], "paused")

    def test_running_helper_recovers_failed_client_while_paused(self):
        optimizer.atomic_json(self.config, self.cfg | {"enabled": False})
        self.api.offline = True
        recovered = FakeController()
        states = []
        write = self.helper.write_status
        stopped = {"done": False, "waits": 0}

        def publish(**updates):
            write(**updates)
            states.append(dict(self.helper.status))
            if updates.get("controller_available") and updates.get("phase") == "paused":
                stopped["done"] = True

        def wait(_seconds):
            stopped["waits"] += 1

        def controller(cfg):
            return self.api if cfg["controller"] == self.cfg["controller"] else recovered

        with patch.object(optimizer, "Controller", side_effect=controller), \
             patch.object(self.helper, "write_status", side_effect=publish), \
             patch.object(self.helper, "stopping", side_effect=lambda: stopped["done"] or stopped["waits"] > 20), \
             patch.object(optimizer.time, "sleep", side_effect=wait):
            self.helper.run()
        self.assertTrue(stopped["done"], "helper did not publish the recovered client")
        self.assertTrue(any(state["controller_available"] and state["selected"] == "JP old" for state in states))
        self.assertFalse(optimizer.load_config()["enabled"])
        self.assertEqual(self.api.select_calls + recovered.select_calls, [])

    def test_paused_explicit_scan_waits_once_for_unsupported_config_read(self):
        optimizer.atomic_json(self.config, self.cfg | {"enabled": False})
        before = self.config.read_bytes()
        (self.state / "scan.request").touch()
        error = optimizer.ConfigReadUnsupported()
        self.discovery.return_value = {
            "selected_controller": None, "message": str(error), "candidates": [
                {"controller": self.cfg["controller"], "verified": False,
                 "needs_auth": False, "unsupported": True}],
        }
        waiting = []
        write = self.helper.write_status

        def publish(**updates):
            result = write(**updates)
            if updates.get("phase") == "waiting":
                waiting.append(dict(self.helper.status))
            return result

        with patch.object(self.api, "snapshot", side_effect=error), \
             patch.object(self.helper, "write_status", side_effect=publish), \
             patch.object(self.helper, "stopping", side_effect=lambda: bool(waiting)), \
             patch.object(optimizer.time, "sleep", side_effect=AssertionError("helper did not stop after waiting")):
            self.helper.run()

        self.assertEqual(len(waiting), 1)
        self.assertFalse(waiting[0]["controller_available"])
        self.assertEqual(waiting[0]["message"], str(error))
        self.assertEqual(waiting[0]["message"].count("HTTP 405"), 1)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.api.select_calls, [])
        self.discovery.assert_called_once()

    def test_pending_rollback_or_user_choice_blocks_discovery(self):
        for name in ("pending-selection.json", "select.request.json"):
            with self.subTest(name=name):
                path = self.state / name
                optimizer.atomic_json(path, {"preserve": True})
                self.assertFalse(self.helper.adapt_client())
                self.assertTrue(path.exists())
                self.discovery.assert_not_called()
                path.unlink()

    def test_choice_arriving_during_discovery_blocks_commit(self):
        def discovered(_cfg):
            optimizer.atomic_json(self.state / "select.request.json", {"request_id": "queued"})
            return self.result()
        self.discovery.side_effect = discovered
        self.assertFalse(self.helper.adapt_client())
        self.assertEqual(optimizer.load_config(), self.cfg)

    def test_concurrent_settings_are_preserved(self):
        def discovered(_cfg):
            optimizer.atomic_json(self.config, self.cfg | {"prefer_japan": False})
            return self.result()
        self.discovery.side_effect = discovered
        self.assertFalse(self.helper.adapt_client())
        self.assertFalse(optimizer.load_config()["prefer_japan"])
        self.assertEqual(optimizer.load_config()["controller"], self.cfg["controller"])

    def test_multiple_clients_require_choice_and_rechecks_are_bounded(self):
        self.discovery.return_value = {"selected_controller": None, "candidates": [], "message": "请选择客户端"}
        self.assertFalse(self.helper.adapt_client())
        self.assertFalse(self.helper.adapt_client())
        self.assertEqual(self.discovery.call_count, 1)
        self.assertEqual(optimizer.load_config(), self.cfg)
        self.assertEqual(self.helper.status["client_detection_message"], "请选择客户端")

    def test_missing_group_can_be_repaired_on_same_endpoint(self):
        self.discovery.return_value = self.result(self.cfg["controller"], "New Main")
        self.assertTrue(self.helper.adapt_client())
        self.assertEqual(optimizer.load_config()["group"], "New Main")
        self.assertEqual(optimizer.load_config()["controller_secret_env"], "OLD_CLIENT_SECRET")

    def test_valid_group_and_disabled_group_autoselection_are_respected(self):
        self.discovery.return_value = self.result(self.cfg["controller"], "Main")
        self.discovery.return_value["candidates"][0]["recommended_group"] = "Other"
        self.assertFalse(self.helper.adapt_client())
        optimizer.atomic_json(self.config, self.cfg | {"auto_select_group": False})
        self.helper.reload_config()
        self.helper.last_client_detection = -float("inf")
        self.discovery.return_value = self.result(group="Other")
        self.assertFalse(self.helper.adapt_client())
        self.assertEqual(optimizer.load_config()["controller"], self.cfg["controller"])

    def test_disabled_discovery_checks_only_configured_endpoint(self):
        optimizer.atomic_json(self.config, self.cfg | {"auto_detect_client": False})
        self.helper.reload_config()
        info = self.result(self.cfg["controller"], "New Main")["candidates"][0]
        with patch.object(optimizer, "inspect_controller", return_value=info) as inspect:
            self.assertTrue(self.helper.adapt_client())
        inspect.assert_called_once_with(self.cfg | {"auto_detect_client": False}, timeout=0.6, adaptive=True)
        self.discovery.assert_not_called()

    def test_dry_run_and_inflight_transaction_never_apply(self):
        for field in ("dry_run", "tentative"):
            with self.subTest(field=field):
                setattr(self.helper, field, True)
                self.assertFalse(self.helper.adapt_client())
                setattr(self.helper, field, False)
        self.discovery.assert_not_called()

    def test_config_write_failure_preserves_context(self):
        context = self.helper.state_context
        with patch.object(optimizer, "atomic_json", side_effect=OSError("simulated disk error")):
            with self.assertRaises(OSError):
                self.helper.adapt_client()
        self.assertEqual(optimizer.load_config(), self.cfg)
        self.assertEqual(self.helper.state_context, context)


class AdaptiveScopeTests(unittest.TestCase):
    def test_adaptive_scope_tracks_actual_nearby_nodes_only(self):
        cfg = optimizer.validate_config({"scan_mode": "adaptive", "scan_regions": []})
        nodes = ["US one", "TW one", "unknown", "JP one", "PH one"]
        self.assertEqual(optimizer.scan_candidates(nodes, cfg), ["TW one", "JP one", "PH one"])
        self.assertEqual(optimizer.scan_candidates(["SG new", "US one"], cfg), ["SG new"])
        self.assertEqual(optimizer.scan_candidates(["US one", "unknown"], cfg), [])
        self.assertTrue(cfg["prefer_japan"])

    def test_automatic_switches_require_boolean_values(self):
        for key in ("auto_detect_client", "auto_select_group"):
            with self.subTest(key=key):
                with self.assertRaises(ValueError):
                    optimizer.validate_config({key: "true"})

    def test_malformed_proxy_schema_does_not_abort_other_discoveries(self):
        for malformed in ({"Main": {"type": "Selector", "all": [123]}},
                          {"Main": {"type": "Selector", "all": [{}]}},
                          {"Main": {"type": "Selector", "all": None}},
                          {"Main": "invalid"}):
            with self.subTest(proxies=malformed):
                api = Mock()
                api.request.side_effect = [{"version": "test"}, {"mode": "rule", "mixed-port": 7890},
                                           {"proxies": malformed}]
                with patch.object(optimizer, "Controller", return_value=api):
                    with self.assertRaisesRegex(optimizer.ControllerError, "格式无效"):
                        optimizer.inspect_controller({"group": "Main"}, adaptive=True)


if __name__ == "__main__":
    unittest.main()
