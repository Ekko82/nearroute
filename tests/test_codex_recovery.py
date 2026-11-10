"""Bounded real-request recovery with offline logs and a fake controller."""

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import unittest
from unittest.mock import patch

import optimizer
from tests import test_log_signals, test_runtime


class CodexRecoveryTests(unittest.TestCase):
    @contextmanager
    def runtime(self):
        fixture = test_runtime.RuntimeTests()
        fixture.clock = [10000.0]
        try:
            with patch.object(optimizer.time, "time", side_effect=lambda: fixture.clock[0]):
                fixture.setUp()
                fixture.cfg["concurrency"] = 1
                # The current path wins ordinary latency selection.
                fixture.api.delays.update({"JP old": 100, "JP fast": 300, "JP manual": 400})
                fixture.signals.error_observations = []
                fixture.helper.tick_health()
                yield fixture
        finally:
            fixture.doCleanups()

    @staticmethod
    def observation(started, failed, *, kind="decode", transport="responses_http"):
        return {"request_at": datetime.fromtimestamp(started, optimizer.TZ).isoformat(),
                "error_at": datetime.fromtimestamp(failed, optimizer.TZ).isoformat(),
                "transport": transport, "error_kind": kind, "context_state": "missing"}

    def errors(self, fixture, *, start=None):
        base = fixture.clock[0] if start is None else start
        for offset in (1, 61, 121):
            fixture.clock[0] = base + offset + 10
            fixture.signals.last_new_error_at = fixture.clock[0]
            fixture.signals.error_observations = [self.observation(base + offset, fixture.clock[0])]
            fixture.helper.poll_transport_signals(force=True)
        fixture.signals.last_new_error_at = None
        fixture.signals.error_observations = []

    def test_real_sqlite_errors_switch_a_get_healthy_path_through_run_loop(self):
        logs = test_log_signals.LogSignalsTests()
        try:
            logs.setUp()
            with self.runtime() as fixture:
                logs.clock.side_effect = lambda: fixture.clock[0]
                fixture.helper.signals = logs.signals
                logs.prime()
                for offset in (1, 61, 121):
                    logs.request(stamp=10000 + offset)
                    logs.insert(test_log_signals.body(test_log_signals.DECODE), stamp=10010 + offset)
                fixture.clock[0] = 10131
                fixture.helper.poll_transport_signals(force=True)
                self.assertTrue(fixture.helper.codex_recovery_due())
                self.assertEqual(fixture.helper.failures, 0)
                fixture.clock[0] = 10190
                fixture.helper.scan()
                self.assertEqual(fixture.api.select_calls, [])

                fixture.clock[0] = 10191
                messages = []
                write_status = fixture.helper.write_status

                def advance(seconds):
                    fixture.clock[0] += seconds

                def publish(**updates):
                    messages.append(updates.get("message", ""))
                    return write_status(**updates)

                with patch.object(optimizer.time, "sleep", side_effect=advance), \
                     patch.object(fixture.helper, "write_status", side_effect=publish), \
                     patch.object(fixture.helper, "stopping", side_effect=lambda: fixture.clock[0] >= 10192):
                    fixture.helper.run()

                self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
                self.assertEqual(fixture.helper.failures, 0)
                self.assertEqual(fixture.helper.quarantine, {})
                self.assertTrue(any("正在观察" in message for message in messages))
                saved = optimizer.read_json(fixture.state / "codex-recovery.json", {})
                self.assertEqual(len(saved["attempts"]), 1)
                self.assertFalse((fixture.state / "pending-selection.json").exists())
        finally:
            logs.doCleanups()

    def test_duplicate_old_or_unattributed_requests_do_not_trigger_recovery(self):
        with self.runtime() as fixture:
            fixture.clock[0] = 10100
            observation = self.observation(10001, 10090)
            fixture.signals.error_observations = [observation] * 3 + [
                dict(observation, error_kind="send"), dict(observation, error_kind="tls"),
                self.observation(9999, 10090), self.observation(10002, 10090, transport=None),
                dict(observation, request_at=None),
            ]
            fixture.helper.poll_transport_signals(force=True)
            self.assertEqual(len(fixture.helper.codex_errors), 1)
            fixture.signals.error_observations = []
            fixture.clock[0] = 10161
            fixture.helper.scan()
            self.assertEqual(fixture.api.select_calls, [])
            fixture.clock[0] = 10701
            self.assertFalse(fixture.helper.codex_recovery_due())

    def test_pause_feature_toggle_and_manual_grace_stop_recovery(self):
        for setting in ("enabled", "codex_recovery_enabled", "manual_until"):
            with self.subTest(setting=setting), self.runtime() as fixture:
                self.errors(fixture)
                fixture.clock[0] += 60
                if setting == "manual_until":
                    fixture.helper.manual_until = fixture.clock[0] + 600
                else:
                    fixture.cfg[setting] = False
                fixture.helper.scan()
                self.assertEqual(fixture.api.select_calls, [])
                self.assertFalse((fixture.state / "codex-recovery.json").exists())

    def test_manual_and_automatic_selection_start_fresh_request_observation(self):
        for manual in (True, False):
            with self.subTest(manual=manual), self.runtime() as fixture:
                self.errors(fixture)
                fixture.clock[0] += 60
                if manual:
                    optimizer.request_manual_selection("JP manual")
                    fixture.helper.process_manual_selection()
                else:
                    fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast")
                self.assertEqual(fixture.helper.codex_errors, {})
                fixture.signals.error_observations = [self.observation(10001, fixture.clock[0])]
                fixture.helper.poll_transport_signals(force=True)
                self.assertFalse(fixture.helper.codex_recovery_due())

    def test_budget_cooldown_and_limit_survive_restarts_and_keep_context(self):
        with self.runtime() as fixture:
            fixture.cfg.update(switch_cooldown_seconds=30, max_candidate_attempts=2)
            self.errors(fixture)
            fixture.clock[0] += 60
            fixture.helper.scan()
            first_at = fixture.clock[0]
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
            fixture.helper = optimizer.Optimizer()
            fixture.helper.tick_health()
            _, attempts, reason = fixture.helper.codex_recovery_budget()
            self.assertEqual(len(attempts), 1)
            self.assertIn("冷却", reason)
            # New requests on the new path can trigger one different candidate.
            self.errors(fixture)
            fixture.clock[0] += 60
            fixture.helper.scan()
            self.assertEqual(fixture.api.select_calls[-1], ("Main", "JP manual"))
            fixture.helper = optimizer.Optimizer()
            fixture.helper.tick_health()
            self.errors(fixture)
            fixture.clock[0] += 60
            fixture.helper.scan()
            self.assertEqual(len(fixture.api.select_calls), 2)
            self.assertIn("上限", fixture.helper.status["message"])
            fixture.cfg["controller"] = "http://127.0.0.1:9999"
            fixture.helper.reload_config()
            _, attempts, reason = fixture.helper.codex_recovery_budget()
            self.assertEqual(attempts, [])
            self.assertIsNone(reason)
            fixture.cfg["controller"] = optimizer.DEFAULTS["controller"]
            fixture.helper.reload_config()
            self.assertEqual(len(fixture.helper.codex_recovery_budget()[1]), 2)
            fixture.clock[0] = first_at + 3601
            self.assertEqual(len(fixture.helper.codex_recovery_budget()[1]), 1)

    def test_budget_read_or_write_failure_never_changes_selection(self):
        for failure in ("read", "write"):
            with self.subTest(failure=failure), self.runtime() as fixture:
                self.errors(fixture)
                fixture.clock[0] += 60
                if failure == "read":
                    (fixture.state / "codex-recovery.json").write_text("{broken", encoding="utf-8")
                    fixture.helper.scan()
                else:
                    write = optimizer.atomic_json

                    def fail_budget(path, data):
                        if Path(path).name == "codex-recovery.json":
                            raise OSError("fixture disk failure")
                        return write(path, data)

                    with patch.object(optimizer, "atomic_json", side_effect=fail_budget):
                        fixture.helper.scan()
                self.assertEqual(fixture.api.select_calls, [])
                self.assertFalse((fixture.state / "pending-selection.json").exists())

    def test_failed_candidate_rolls_back_and_consumes_one_attempt(self):
        with self.runtime() as fixture:
            fixture.verify.side_effect = lambda *_args, **_kwargs: {"ok": fixture.api.proxies["Main"]["now"] == "JP old"}
            self.errors(fixture)
            fixture.clock[0] += 60
            fixture.helper.scan()
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
            self.assertEqual(len(fixture.helper.codex_recovery_budget()[1]), 1)
            self.assertIn("JP fast", fixture.helper.quarantine)
            self.assertNotIn("JP old", fixture.helper.quarantine)
            self.assertFalse((fixture.state / "pending-selection.json").exists())
            self.assertIn("已恢复原选择", fixture.helper.status["message"])

    def test_recovery_applies_region_policy_after_excluding_attempted_nodes(self):
        for other_japan in (True, False):
            with self.subTest(other_japan=other_japan), self.runtime() as fixture:
                fixture.api.proxies["HK backup"] = {"type": "Trojan"}
                names = ["JP old", "HK backup"]
                if other_japan:
                    names.append("JP fast")
                fixture.api.proxies["Main"]["all"] = names
                fixture.api.delays["HK backup"] = 110
                self.errors(fixture)
                fixture.clock[0] += 60
                fixture.helper.scan()
                expected = "JP fast" if other_japan else "HK backup"
                self.assertEqual(fixture.api.select_calls, [("Main", expected)])

    def test_get_failures_keep_existing_emergency_recovery_when_codex_budget_is_exhausted(self):
        with self.runtime() as fixture:
            fixture.cfg["max_candidate_attempts"] = 1
            optimizer.atomic_json(fixture.state / "codex-recovery.json", {
                "schema_version": 1, "attempts": [{"context": list(fixture.helper.state_context),
                    "at": fixture.clock[0], "previous": "JP manual", "candidate": "JP old"}],
            })
            self.errors(fixture)
            fixture.clock[0] += 60
            fixture.verify.side_effect = lambda *_args, **_kwargs: {"ok": fixture.api.proxies["Main"]["now"] != "JP old"}
            fixture.helper.scan()
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
            self.assertEqual(len(fixture.helper.codex_recovery_budget()[1]), 1)

    def test_stable_mode_holds_healthy_route_after_cooldown_but_real_errors_recover(self):
        with self.runtime() as fixture:
            fixture.cfg["switch_on_improvement"] = False
            fixture.api.proxies["SG stable"] = {"type": "Trojan"}
            fixture.api.proxies["Main"]["all"].append("SG stable")
            fixture.api.delays["SG stable"] = 200
            optimizer.request_manual_selection("SG stable")
            self.assertTrue(fixture.helper.process_manual_selection())
            fixture.clock[0] += fixture.cfg["switch_cooldown_seconds"] + 1
            self.assertLess(fixture.helper.manual_until, fixture.clock[0])

            fixture.helper.scan()

            self.assertEqual(fixture.api.select_calls, [("Main", "SG stable")])
            self.assertEqual(fixture.helper.status["message"], "稳定模式保持当前可用线路")
            self.errors(fixture)
            fixture.clock[0] += 60
            fixture.helper.scan()
            self.assertEqual(fixture.api.select_calls[-1], ("Main", "JP old"))
            self.assertEqual(len(fixture.api.select_calls), 2)
            self.assertIn("正在观察", fixture.helper.status["message"])

    def test_stable_mode_keeps_initial_japan_selection_and_get_fault_recovery(self):
        for initial in (True, False):
            with self.subTest(initial=initial), self.runtime() as fixture:
                fixture.cfg["switch_on_improvement"] = False
                fixture.api.proxies["SG stable"] = {"type": "Trojan"}
                fixture.api.proxies["Main"]["all"].append("SG stable")
                fixture.api.delays["SG stable"] = 200
                if initial:
                    fixture.api.proxies["Main"]["now"] = "SG stable"
                    fixture.helper = optimizer.Optimizer()
                    fixture.helper.tick_health()
                    self.assertEqual(fixture.helper.record, {})
                else:
                    optimizer.request_manual_selection("SG stable")
                    self.assertTrue(fixture.helper.process_manual_selection())
                    fixture.clock[0] += fixture.cfg["switch_cooldown_seconds"] + 1
                    fixture.verify.side_effect = lambda *_args, **_kwargs: {
                        "ok": fixture.api.proxies["Main"]["now"] != "SG stable"}

                fixture.helper.scan()

                self.assertEqual(fixture.api.select_calls[-1], ("Main", "JP old"))
                self.assertEqual(len(fixture.api.select_calls), 1 if initial else 2)

    def test_flag_defaults_on_and_requires_a_boolean(self):
        for flag in ("codex_recovery_enabled", "switch_on_improvement"):
            with self.subTest(flag=flag):
                self.assertTrue(optimizer.validate_config({})[flag])
                with self.assertRaisesRegex(ValueError, flag):
                    optimizer.validate_config({flag: "true"})


if __name__ == "__main__":
    unittest.main()
