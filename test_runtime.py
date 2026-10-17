"""Offline runtime regressions; all controller and proxy traffic is mocked."""

import copy
from contextlib import ExitStack
import json
from pathlib import Path
import ssl
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

import optimizer


REAL_VERIFY = optimizer.verify_current_path
REAL_SNAPSHOT = optimizer.Controller.snapshot


class FakeController:
    def __init__(self):
        self.general = {"mode": "rule", "mixed-port": 10090}
        self.proxies = {
            "Main": {"type": "Selector", "all": ["JP old", "JP fast", "JP manual"], "now": "JP old"},
            "JP old": {"type": "Shadowsocks"},
            "JP fast": {"type": "Trojan"},
            "JP manual": {"type": "Hysteria2"},
        }
        self.select_calls = []
        self.offline = False
        self.lose_next_put_response = False
        self.delays = {"JP old": 600, "JP fast": 100, "JP manual": 800}

    def request(self, path, method="GET", body=None, timeout=5):
        if self.offline:
            raise optimizer.ControllerError("mock controller unavailable")
        if path == "/proxies":
            return {"proxies": copy.deepcopy(self.proxies)}
        if path == "/configs":
            return copy.deepcopy(self.general)
        raise AssertionError("unexpected mock controller request: " + path)

    def snapshot(self, cfg, *, allow_direct=False):
        return REAL_SNAPSHOT(self, cfg, allow_direct=allow_direct)

    def delay(self, name):
        return self.delays.get(name, 150)

    def select(self, group, name):
        if self.offline:
            raise optimizer.ControllerError("mock controller unavailable")
        self.select_calls.append((group, name))
        if name not in self.proxies[group]["all"]:
            raise optimizer.ControllerError("mock candidate is not a direct selector member")
        self.proxies[group]["now"] = name
        if self.lose_next_put_response:
            self.lose_next_put_response = False
            self.offline = True
            raise optimizer.ControllerError("mock PUT applied but reply lost")


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="codex-optimizer-runtime-test-")
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name) / "state"
        self.cfg = dict(optimizer.DEFAULTS, group="Main")
        self.cfg["codex_log_db"] = str(Path(self.temp.name) / "unused.sqlite")
        self.api = FakeController()
        self.signals = MagicMock()
        self.signals.poll.return_value = 0
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(optimizer, "STATE", self.state))
        self.stack.enter_context(patch.object(optimizer, "CONFIG", Path(self.temp.name) / "config.json"))
        self.stack.enter_context(patch.object(optimizer, "load_config", side_effect=lambda: dict(self.cfg)))
        self.stack.enter_context(patch.object(optimizer, "Controller", return_value=self.api))
        self.instance_lock = MagicMock()
        self.instance_lock.acquire.return_value = True
        self.stack.enter_context(patch.object(optimizer, "InstanceLock", return_value=self.instance_lock))
        self.stack.enter_context(patch.object(optimizer, "LogSignals", return_value=self.signals))
        self.stack.enter_context(
            patch.object(optimizer.urllib.request, "build_opener", side_effect=AssertionError("real network forbidden"))
        )
        self.stack.enter_context(
            patch.object(optimizer.http.client, "HTTPSConnection", side_effect=AssertionError("real proxy traffic forbidden"))
        )
        self.verify = self.stack.enter_context(
            patch.object(optimizer, "verify_current_path", return_value={"ok": True, "http_status": 405})
        )
        self.helper = optimizer.Optimizer()

    def journal(self, previous="JP old", candidate="JP fast", **extra):
        value = {**optimizer.controller_identity(self.cfg), "group": "Main",
                 "previous": previous, "candidate": candidate, "at": optimizer.iso()}
        value.update(extra)
        optimizer.atomic_json(self.state / "pending-selection.json", value)
        return value

    def events(self):
        path = self.state / "events.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []

    def test_scan_keeps_nodes_visible_and_publishes_progress_samples(self):
        self.cfg["enabled"] = False
        self.helper.add_sample("JP old", 320)
        statuses = []
        write = self.helper.write_status
        def capture(**updates):
            write(**updates)
            statuses.append(copy.deepcopy(self.helper.status))
        with patch.object(self.helper, "write_status", side_effect=capture):
            self.helper.scan()
        scanning = [s for s in statuses if s["phase"] == "scanning"]
        self.assertTrue(scanning)
        self.assertTrue(all(s["nodes"] for s in scanning))
        initial = next(r for r in scanning[0]["nodes"] if r["name"] == "JP old")
        self.assertEqual(initial["median_ms"], 320)
        self.assertTrue(any(s.get("progress_done") and any(r["samples"] > 0 for r in s["nodes"]) for s in scanning[1:]))

    def test_paused_manual_switch_is_confirmed_and_survives_helper_restart(self):
        self.cfg["enabled"] = False
        request_id = optimizer.request_manual_selection("JP manual")
        self.assertTrue(self.helper.process_manual_selection())
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")
        self.assertEqual(self.helper.status["manual_selection_result"]["request_id"], request_id)
        self.assertTrue(self.helper.status["manual_selection_result"]["ok"])
        self.assertEqual(self.helper.status["phase"], "paused")
        self.assertGreater(self.helper.manual_until, time.time())
        restarted = optimizer.Optimizer()
        self.assertEqual(restarted.manual_until, self.helper.manual_until)

    def test_forced_scan_respects_manual_switch_grace_period(self):
        optimizer.request_manual_selection("JP manual")
        self.assertTrue(self.helper.process_manual_selection())
        self.helper.scan(forced=True)
        self.assertEqual(self.api.select_calls, [("Main", "JP manual")])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")

    def test_manual_request_during_scan_takes_priority_over_auto_switch(self):
        optimizer.request_manual_selection("JP manual")
        self.helper.scan(forced=True)
        self.assertEqual(self.api.select_calls, [("Main", "JP manual")])
        self.assertEqual(self.helper.status["selected"], "JP manual")
        self.assertTrue(self.helper.status["manual_selection_result"]["ok"])

    def test_manual_request_interrupts_candidate_validation_then_selects_user_choice(self):
        def request_during_verification(*args, **kwargs):
            optimizer.request_manual_selection("JP manual")
            self.assertTrue(args[2]())
            return {"ok": False, "cancelled": True, "reason": "验证已取消"}
        self.verify.side_effect = request_during_verification
        self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast"))
        self.assertTrue(self.helper.process_manual_selection())
        self.assertEqual(self.api.select_calls, [("Main", "JP fast"), ("Main", "JP old"), ("Main", "JP manual")])
        self.assertNotIn("JP fast", self.helper.quarantine)
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")

    def test_deleted_manual_node_reports_failure_without_changing_selection(self):
        request_id = optimizer.request_manual_selection("deleted node")
        self.assertFalse(self.helper.process_manual_selection())
        result = self.helper.status["manual_selection_result"]
        self.assertEqual(result["request_id"], request_id)
        self.assertFalse(result["ok"])
        self.assertEqual(self.api.select_calls, [])

    def test_changed_controller_rejects_stale_manual_request(self):
        optimizer.request_manual_selection("JP manual")
        self.cfg["controller"] = "http://127.0.0.1:9999"
        self.assertFalse(self.helper.process_manual_selection())
        self.assertFalse(self.helper.status["manual_selection_result"]["ok"])
        self.assertEqual(self.api.select_calls, [])

    def test_changed_auth_environment_rejects_stale_manual_request(self):
        optimizer.request_manual_selection("JP manual")
        self.cfg["controller_secret_env"] = "OTHER_CONTROLLER_TOKEN"
        self.assertFalse(self.helper.process_manual_selection())
        self.assertEqual(self.api.select_calls, [])

    def test_dry_run_leaves_manual_request_for_the_real_helper(self):
        self.helper.dry_run = True
        optimizer.request_manual_selection("JP manual")
        self.helper.scan(forced=True)
        self.assertEqual(self.api.select_calls, [])
        self.assertTrue((self.state / "select.request.json").exists())

    def test_unconfirmed_manual_put_keeps_journal_for_recovery(self):
        optimizer.request_manual_selection("JP manual")
        self.api.lose_next_put_response = True
        self.assertFalse(self.helper.process_manual_selection())
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")
        self.assertTrue((self.state / "pending-selection.json").exists())
        self.api.offline = False
        self.helper.recover_pending()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertFalse((self.state / "pending-selection.json").exists())

    def test_failed_manual_request_with_new_probe_does_not_mix_old_measurements(self):
        optimizer.request_manual_selection("deleted node")
        def change_probe(name):
            self.cfg.update(probe_url="https://example.com/health", probe_expected_statuses=[204])
            return 123
        with patch.object(self.api, "delay", side_effect=change_probe):
            self.helper.scan(forced=True)
        self.assertEqual(self.helper.history, {})
        self.assertEqual(self.helper.history_probe, optimizer.probe_identity(self.cfg))
        self.assertEqual(self.helper.next_scan, 0)
        self.assertEqual(self.api.select_calls, [])

    def test_cancelling_old_request_preserves_newer_choice(self):
        older = optimizer.request_manual_selection("JP old")
        newer = optimizer.request_manual_selection("JP manual")
        self.assertFalse(optimizer.cancel_manual_selection(older))
        self.assertEqual(optimizer.read_json(self.state / "select.request.json", {})["request_id"], newer)
        self.assertTrue(optimizer.cancel_manual_selection(newer))
        self.assertFalse((self.state / "select.request.json").exists())

    def test_failed_manual_commit_keeps_durable_recovery_and_previous_memory_record(self):
        optimizer.request_manual_selection("JP manual")
        previous_record = copy.deepcopy(self.helper.record)
        write = optimizer.atomic_json
        def fail_commit(path, value):
            if Path(path).name == "selection.json":
                raise OSError("mock disk write failure")
            return write(path, value)
        with patch.object(optimizer, "atomic_json", side_effect=fail_commit):
            self.assertFalse(self.helper.process_manual_selection())
        self.assertEqual(self.helper.record, previous_record)
        self.assertEqual(self.helper.manual_until, 0)
        self.assertTrue((self.state / "pending-selection.json").exists())
        self.helper.recover_pending()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertFalse((self.state / "pending-selection.json").exists())

    def test_client_choice_seen_at_scan_start_gets_the_manual_protection_period(self):
        self.helper.observed_selection = ("Main", "JP old")
        self.api.proxies["Main"]["now"] = "JP manual"
        self.helper.scan()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")
        self.assertEqual(self.api.select_calls, [])
        self.assertGreater(self.helper.manual_until, time.time())
        self.assertIn("external_selection_change", [row["event"] for row in self.events()])

    def test_old_path_failures_cannot_bypass_protection_for_a_new_client_choice(self):
        self.helper.observed_selection = ("Main", "JP old")
        self.helper.failures = 2
        self.api.proxies["Main"]["now"] = "JP manual"
        self.helper.scan()
        self.assertEqual(self.helper.failures, 0)
        self.assertGreater(self.helper.manual_until, time.time())
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")
        self.assertEqual(self.api.select_calls, [])

    def test_http_403_candidate_is_rolled_back_and_quarantined(self):
        self.verify.return_value = {"ok": False, "reason": "Codex 探测返回 HTTP 403", "http_status": 403}
        switched = self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast")
        self.assertFalse(switched)
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertEqual(self.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
        self.assertGreater(self.helper.quarantine["JP fast"], time.time())
        self.assertFalse(self.helper.tentative)
        self.assertIn("candidate_rejected", [row["event"] for row in self.events()])
        self.helper.recover_pending()
        self.assertFalse((self.state / "pending-selection.json").exists())

    def test_manual_change_before_put_is_never_overwritten(self):
        self.api.proxies["Main"]["now"] = "JP manual"
        self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast"))
        self.helper.recover_pending()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")
        self.assertEqual(self.api.select_calls, [])
        self.verify.assert_not_called()

    def test_manual_change_during_validation_is_never_rolled_back(self):
        def user_changes_selection(*args, **kwargs):
            self.api.proxies["Main"]["now"] = "JP manual"
            return {"ok": True, "http_status": 405}
        self.verify.side_effect = user_changes_selection
        self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast"))
        self.helper.recover_pending()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")
        self.assertEqual(self.api.select_calls, [("Main", "JP fast")])
        self.assertNotIn("JP fast", self.helper.quarantine)

    def test_paused_forced_scan_measures_without_selecting(self):
        self.cfg["enabled"] = False
        self.helper.scan(forced=True)
        self.assertEqual(self.api.select_calls, [])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertEqual(self.helper.status["phase"], "paused")
        self.assertTrue(self.helper.status["nodes"])
        self.verify.assert_not_called()

    def test_pause_during_validation_restores_previous_selection(self):
        def pause(*args, **kwargs):
            self.cfg["enabled"] = False
            return {"ok": True, "http_status": 405}
        self.verify.side_effect = pause
        self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast"))
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertEqual(self.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])

    def test_ambiguous_put_failure_preserves_journal_and_recovers_later(self):
        self.api.lose_next_put_response = True
        with self.assertRaises(optimizer.ControllerError):
            self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast")
        self.assertEqual(self.api.proxies["Main"]["now"], "JP fast")
        self.assertTrue((self.state / "pending-selection.json").exists())
        self.assertFalse(self.helper.tentative)
        self.api.offline = False
        self.helper.recover_pending()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertFalse((self.state / "pending-selection.json").exists())

    def test_daemon_stays_alive_until_stopped_when_controller_is_offline(self):
        self.journal()
        self.api.offline = True
        stop = {"requested": False}
        def request_stop_after_wait(*args):
            stop["requested"] = True
        with patch.object(self.helper, "stopping", side_effect=lambda: stop["requested"]), \
             patch.object(optimizer.time, "sleep", side_effect=request_stop_after_wait), \
             patch.object(self.helper, "write_status", wraps=self.helper.write_status) as status:
            self.helper.run()
        self.assertIn("waiting", [call.kwargs.get("phase") for call in status.call_args_list])
        self.assertEqual(self.helper.status["phase"], "stopped")
        self.assertTrue((self.state / "pending-selection.json").exists())
        self.assertEqual(self.api.select_calls, [])

    def test_health_observation_uses_snapshot_after_pending_rollback(self):
        self.helper.observed_selection = ("Main", "JP old")
        self.api.proxies["Main"]["now"] = "JP fast"
        self.journal()
        self.helper.tick_health()
        self.assertEqual(self.helper.status["selected"], "JP old")
        self.assertEqual(self.helper.observed_selection, ("Main", "JP old"))
        self.assertEqual(self.helper.manual_until, 0)
        self.assertNotIn("external_selection_change", [row["event"] for row in self.events()])

    def test_old_commit_for_same_name_does_not_complete_new_pending_switch(self):
        self.helper.record = {
            **optimizer.controller_identity(self.cfg),
            "group": "Main", "name": "JP fast", "last_switch_at": 1000,
            "transaction_id": "old-commit",
        }
        optimizer.save_owned_record(self.state / "selection.json", self.helper.record, latest=True)
        self.api.proxies["Main"]["now"] = "JP fast"
        self.journal(at=optimizer.iso(2000), transaction_id="new-pending")
        self.helper.recover_pending()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertEqual(self.api.select_calls, [("Main", "JP old")])

    def test_committed_current_pending_switch_is_preserved(self):
        now = time.time()
        self.helper.record = {
            **optimizer.controller_identity(self.cfg),
            "group": "Main", "name": "JP fast", "last_switch_at": now,
            "transaction_id": "current-commit",
        }
        optimizer.save_owned_record(self.state / "selection.json", self.helper.record, latest=True)
        self.api.proxies["Main"]["now"] = "JP fast"
        self.journal(at=optimizer.iso(now - 1), transaction_id="current-commit")
        self.helper.recover_pending()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP fast")
        self.assertEqual(self.api.select_calls, [])
        self.assertFalse((self.state / "pending-selection.json").exists())

    def test_failed_commit_and_offline_rollback_are_not_treated_as_durable_commit(self):
        write_json = optimizer.atomic_json
        def fail_selection_commit(path, value):
            if Path(path).name == "selection.json":
                self.api.offline = True
                raise OSError("mock disk full before commit")
            return write_json(path, value)
        with patch.object(optimizer, "atomic_json", side_effect=fail_selection_commit):
            with self.assertRaises((OSError, optimizer.ControllerError)):
                self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast")
        self.assertTrue((self.state / "pending-selection.json").exists())
        self.assertFalse((self.state / "selection.json").exists())
        self.api.offline = False
        self.helper.recover_pending()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertFalse((self.state / "pending-selection.json").exists())

    def test_nested_only_leaf_is_not_selected_after_first_candidate_rejection(self):
        self.api.proxies["Main"]["all"] = ["JP old", "JP fast", "Nested"]
        self.api.proxies["Nested"] = {"type": "URLTest", "all": ["JP indirect"], "now": "JP indirect"}
        self.api.proxies["JP indirect"] = {"type": "Trojan"}
        self.api.delays["JP indirect"] = 50
        rejected = {"ok": False, "http_status": 403, "reason": "mock forbidden"}
        # Current active path passes; the direct fast candidate then fails validation.
        self.verify.side_effect = [{"ok": True, "http_status": 405}, rejected]
        self.helper.scan(forced=True)
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertNotIn(("Main", "JP indirect"), self.api.select_calls)

    def region_nodes(self, current="US current"):
        names = ["JP stable", "TW nearby", "SG nearby", "HK nearby", "US current", "DE far", "unknown"]
        self.api.proxies = {name: {"type": "Trojan"} for name in names}
        self.api.proxies["Main"] = {"type": "Selector", "all": names, "now": current}
        self.api.delays = {name: 100 for name in names}
        return names

    def test_scan_never_probes_excluded_nodes_even_when_current_is_excluded(self):
        self.region_nodes()
        self.cfg["enabled"] = False
        with patch.object(self.api, "delay", wraps=self.api.delay) as probe:
            self.helper.scan(forced=True)
        measured = {call.args[0] for call in probe.call_args_list}
        self.assertEqual(measured, {"JP stable", "TW nearby", "SG nearby", "HK nearby"})
        self.assertEqual(self.helper.status["scan_node_count"], 4)
        self.assertEqual(self.helper.status["excluded_node_count"], 3)
        self.assertEqual(self.api.select_calls, [])

    def test_no_nearby_nodes_means_no_scan_and_no_automatic_scope_expansion(self):
        self.api.proxies = {"Main": {"type": "Selector", "all": ["US far", "unknown"], "now": "US far"}, "US far": {"type": "Trojan"}, "unknown": {"type": "Trojan"}}
        with patch.object(self.api, "delay", wraps=self.api.delay) as probe:
            with self.assertRaises(optimizer.ControllerError):
                self.helper.scan(forced=True)
        probe.assert_not_called()
        self.assertEqual(self.api.select_calls, [])
        self.assertEqual(self.helper.status["nodes"], [])

    def test_actual_japan_path_failure_allows_nearby_fallback(self):
        self.region_nodes(current="JP stable")
        self.api.delays.update({"JP stable": 50, "SG nearby": 80, "TW nearby": 120, "HK nearby": 150})
        self.verify.side_effect = [{"ok": False}, {"ok": False}, {"ok": True, "http_status": 405}]
        self.helper.scan(forced=True)
        self.assertEqual(self.api.select_calls, [("Main", "SG nearby")])

    def test_fast_foreign_node_does_not_override_healthy_japan(self):
        self.region_nodes(current="JP stable")
        self.api.delays.update({"JP stable": 200, "SG nearby": 20, "TW nearby": 10, "HK nearby": 30})
        self.helper.scan(forced=True)
        self.assertEqual(self.api.select_calls, [])
        self.assertEqual(self.helper.status["selected"], "JP stable")

    def test_scope_narrowed_before_switch_never_selects_previous_candidate(self):
        self.region_nodes(current="JP stable")
        self.cfg["scan_regions"] = ["JP"]
        self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP stable", "HK nearby"))
        self.assertEqual(self.api.select_calls, [])

    def test_scope_narrowed_during_validation_rolls_back_without_quarantine(self):
        self.region_nodes(current="JP stable")
        def narrow_scope(*args, **kwargs):
            self.cfg["scan_regions"] = ["JP"]
            return {"ok": True, "http_status": 405}
        self.verify.side_effect = narrow_scope
        self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP stable", "HK nearby"))
        self.assertEqual(self.api.select_calls, [("Main", "HK nearby"), ("Main", "JP stable")])
        self.assertNotIn("HK nearby", self.helper.quarantine)

    def test_policy_changed_during_measurements_schedules_new_scan_without_switching(self):
        self.region_nodes()
        original_measure = self.helper.measure_round
        def measure_then_change_policy(names, title):
            result = original_measure(names, title)
            self.cfg["prefer_japan"] = False
            return result
        with patch.object(self.helper, "measure_round", side_effect=measure_then_change_policy):
            self.helper.scan(forced=True)
        self.assertEqual(self.api.select_calls, [])
        self.assertEqual(self.helper.next_scan, 0)

    def test_explicit_node_scope_can_select_an_unknown_region_without_scanning_others(self):
        self.region_nodes()
        self.cfg.update(scan_mode="nodes", scan_nodes=["unknown"])
        self.helper.scan(forced=True)
        self.assertEqual(set(self.helper.history), {"unknown"})
        self.assertEqual(self.api.select_calls, [("Main", "unknown")])

    def test_probe_change_clears_old_samples_and_quarantine(self):
        self.helper.add_sample("JP old", 100)
        self.helper.quarantine["JP old"] = time.time() + 300
        self.cfg.update(probe_url="https://example.com/health", probe_expected_statuses=[204])
        self.helper.reload_config()
        self.assertEqual(self.helper.history, {})
        self.assertEqual(self.helper.quarantine, {})
        self.assertEqual(self.helper.next_scan, 0)
        self.assertEqual(optimizer.read_json(self.state / "history.json", None), {})

    def test_unchanged_probe_preserves_history_on_normal_reload(self):
        self.helper.add_sample("JP old", 100)
        self.helper.reload_config()
        self.assertEqual(len(self.helper.history["JP old"]), 1)

    def test_health_check_uses_configured_probe_and_status(self):
        self.cfg.update(probe_url="https://example.com/check", probe_expected_statuses=[200, 204])
        self.helper.reload_config()
        self.helper.tick_health()
        self.assertEqual(self.verify.call_args.kwargs["probe_url"], "https://example.com/check")
        self.assertEqual(self.verify.call_args.kwargs["expected_statuses"], [200, 204])

    def test_probe_change_during_candidate_verification_rolls_back(self):
        def change_probe(*args, **kwargs):
            self.cfg.update(probe_url="https://example.com/health", probe_expected_statuses=[204])
            return {"ok": True, "http_status": 405}
        self.verify.side_effect = change_probe
        self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast"))
        self.assertEqual(self.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])

    def test_old_probe_failure_cannot_quarantine_candidate_for_new_probe(self):
        def change_probe(*args, **kwargs):
            self.cfg.update(probe_url="https://example.com/health", probe_expected_statuses=[204])
            return {"ok": False, "http_status": 403}
        self.verify.side_effect = change_probe
        self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast"))
        self.assertEqual(self.helper.quarantine, {})
        self.assertEqual(self.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])

    def test_controller_change_during_validation_keeps_rollback_on_original_controller(self):
        other = FakeController()
        def change_controller(*args, **kwargs):
            self.cfg["controller"] = "http://127.0.0.1:9791"
            return {"ok": True, "http_status": 405}
        self.verify.side_effect = change_controller
        with patch.object(optimizer, "Controller", return_value=other):
            self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP old", "JP fast"))
        self.assertEqual(self.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
        self.assertEqual(other.select_calls, [])

    def test_pending_recovery_uses_original_controller_from_journal(self):
        self.api.proxies["Main"]["now"] = "JP fast"
        self.journal(controller="http://127.0.0.1:9790", controller_secret_env="")
        other = FakeController()
        other.proxies["Main"]["now"] = "JP fast"
        self.helper.cfg["controller"] = "http://127.0.0.1:9791"
        self.helper.api = other
        with patch.object(optimizer, "Controller", return_value=self.api):
            self.helper.recover_pending()
        self.assertEqual(self.api.select_calls, [("Main", "JP old")])
        self.assertEqual(other.select_calls, [])

    def test_unconfirmed_rollback_keeps_pending_for_a_confirmed_retry(self):
        self.api.proxies["Main"]["now"] = "JP fast"
        self.journal(transaction_id="unconfirmed-rollback")
        with patch.object(self.api, "select") as select:
            with self.assertRaises(optimizer.ControllerError):
                self.helper.recover_pending()
        select.assert_called_once_with("Main", "JP old")
        self.assertEqual(self.api.proxies["Main"]["now"], "JP fast")
        self.assertTrue((self.state / "pending-selection.json").exists())
        self.helper.recover_pending()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertFalse((self.state / "pending-selection.json").exists())

    def test_restore_only_changes_selections_still_owned_by_helper(self):
        self.api.proxies["Main"]["now"] = "JP fast"
        self.api.proxies["Other"] = {"type": "Selector", "all": ["old-other", "fast-other"], "now": "fast-other"}
        self.api.proxies["ManualGroup"] = {"type": "Selector", "all": ["old-manual", "owned-manual", "user-manual"], "now": "user-manual"}
        for group, baseline, selected in (("Main", "JP old", "JP fast"),
                                           ("Other", "old-other", "fast-other"),
                                           ("ManualGroup", "old-manual", "owned-manual")):
            optimizer.save_owned_record(self.state / "original-selection.json",
                                        {**optimizer.controller_identity(self.cfg), "group": group, "name": baseline})
            optimizer.save_owned_record(self.state / "selection.json",
                                        {**optimizer.controller_identity(self.cfg), "group": group, "name": selected}, latest=True)
        with patch.object(optimizer, "stop_daemon", return_value=True):
            restored = optimizer.restore_original()
        self.assertEqual(set(restored), {"Main", "Other"})
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertEqual(self.api.proxies["Other"]["now"], "old-other")
        self.assertEqual(self.api.proxies["ManualGroup"]["now"], "user-manual")
        self.assertNotIn("ManualGroup", [group for group, _ in self.api.select_calls])

    def test_saved_original_without_ownership_never_overwrites_selection(self):
        self.api.proxies["Main"]["now"] = "JP manual"
        optimizer.atomic_json(self.state / "original-selection.json", {"Main": {"name": "JP old"}})
        with patch.object(optimizer, "stop_daemon", return_value=True):
            self.assertEqual(optimizer.restore_original(), [])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")
        self.assertEqual(self.api.select_calls, [])


class TLSVerificationTests(unittest.TestCase):
    def test_proxy_tls_verifies_certificates_and_hostname(self):
        conn = MagicMock()
        response = MagicMock(status=405)
        conn.getresponse.return_value = response
        with patch.object(optimizer.http.client, "HTTPSConnection", return_value=conn) as make_conn:
            result = REAL_VERIFY({"mixed-port": 10090})
        self.assertTrue(result["ok"])
        context = make_conn.call_args.kwargs["context"]
        self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
        self.assertTrue(context.check_hostname)
        conn.set_tunnel.assert_called_once_with("chatgpt.com", 443)
        conn.close.assert_called_once()

    def test_certificate_failure_has_no_insecure_retry(self):
        conn = MagicMock()
        conn.request.side_effect = ssl.SSLCertVerificationError("mock certificate rejection")
        with patch.object(optimizer.http.client, "HTTPSConnection", return_value=conn) as make_conn:
            result = REAL_VERIFY({"mixed-port": 10090})
        self.assertFalse(result["ok"])
        self.assertIn("SSLCertVerificationError", result["reason"])
        make_conn.assert_called_once()
        self.assertEqual(conn.request.call_count, 1)
        conn.close.assert_called_once()

    def test_http_403_is_rejected_without_reading_response_body(self):
        conn = MagicMock()
        response = MagicMock(status=403)
        conn.getresponse.return_value = response
        with patch.object(optimizer.http.client, "HTTPSConnection", return_value=conn):
            result = REAL_VERIFY({"mixed-port": 10090})
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 403)
        response.read.assert_not_called()
        conn.close.assert_called_once()

    def test_idle_verification_rejects_replaced_tls_socket(self):
        conn = MagicMock()
        sockets = iter([object(), object(), object()])
        def response_with_new_socket():
            conn.sock = next(sockets)
            return MagicMock(status=405)
        conn.getresponse.side_effect = response_with_new_socket
        clock = iter(range(0, 100, 2))
        with patch.object(optimizer.http.client, "HTTPSConnection", return_value=conn), \
             patch.object(optimizer.time, "monotonic", side_effect=lambda: next(clock)), \
             patch.object(optimizer.time, "sleep") as sleep:
            result = REAL_VERIFY({"mixed-port": 10090}, idle_seconds=1)
        self.assertFalse(result["ok"])
        self.assertEqual(conn.request.call_count, 2)
        sleep.assert_not_called()
        conn.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
