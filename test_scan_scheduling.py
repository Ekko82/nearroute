"""Cooldown scan scheduling through isolated runtime fixtures and virtual time."""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
import test_runtime


class ScanSchedulingTests(unittest.TestCase):
    @contextmanager
    def runtime(self):
        fixture = test_runtime.RuntimeTests("test_paused_manual_switch_is_confirmed_and_survives_helper_restart")
        fixture.clock = [1000.0]
        try:
            with patch.object(optimizer.time, "time", side_effect=lambda: fixture.clock[0]):
                fixture.setUp()
                fixture.cfg.update(scan_interval_seconds=300, switch_cooldown_seconds=1800,
                                   health_interval_seconds=15, concurrency=1)
                fixture.helper.record = {
                    **optimizer.controller_identity(fixture.cfg), "configured_group": "Main",
                    "group": "Main", "name": "JP old", "last_switch_at": 1000.0,
                    "verification": {"ok": True},
                }
                yield fixture
        finally:
            fixture.doCleanups()

    def run_until(self, fixture, until, action=None):
        scans, health_checks = [], []
        scan, tick = fixture.helper.scan, fixture.helper.tick_health

        def scan_at(*args, **kwargs):
            scans.append((fixture.clock[0], kwargs.get("forced", False)))
            return scan(*args, **kwargs)

        def health_at():
            health_checks.append(fixture.clock[0])
            return tick()

        def advance(seconds):
            fixture.clock[0] = round(fixture.clock[0] + seconds, 3)
            if action:
                action(fixture.clock[0])

        with patch.object(fixture.helper, "scan", side_effect=scan_at), \
             patch.object(fixture.helper, "tick_health", side_effect=health_at), \
             patch.object(optimizer.time, "sleep", side_effect=advance), \
             patch.object(fixture.helper, "stopping", side_effect=lambda: fixture.clock[0] >= until):
            fixture.helper.run()
        return scans, health_checks

    def assert_deadline(self, fixture, expected):
        self.assertEqual(fixture.helper.next_scan, expected)
        self.assertEqual(fixture.helper.status["next_scan_at"], optimizer.iso(expected))

    def test_cold_start_scans_once_during_cooldown_and_keeps_15_second_health_checks(self):
        with self.runtime() as f:
            scans, checks = self.run_until(f, 2799)
            self.assertEqual(scans, [(1000, False)])
            self.assertTrue(checks)
            self.assertLessEqual(checks[0], 1001)
            self.assertEqual(set(b - a for a, b in zip(checks, checks[1:])), {15})
            self.assertGreater(checks[-1], 2780)
            self.assertEqual(f.helper.last_scan, 1000)
            self.assert_deadline(f, 2800)
            self.assertEqual(f.api.select_calls, [])

    def test_confirmation_success_and_new_candidate_validation_use_latest_healthy_result(self):
        for scenario in ("confirmation", "new_candidate"):
            with self.subTest(scenario=scenario), self.runtime() as f:
                if scenario == "confirmation":
                    f.verify.side_effect = [
                        {"ok": False, "http_status": 500, "reason": "temporary failure"},
                        {"ok": True, "http_status": 405},
                    ]
                else:
                    f.helper.record = {}
                f.helper.scan()
                self.assertEqual(f.helper.last_scan, 1000)
                self.assertEqual(f.helper.failures, 0)
                self.assertTrue(f.helper.status["health"]["ok"])
                self.assert_deadline(f, 2800)
                if scenario == "confirmation":
                    self.assertEqual(f.verify.call_count, 2)
                    self.assertEqual(f.api.select_calls, [])
                else:
                    self.assertEqual(f.api.proxies["Main"]["now"], "JP fast")
                    self.assertEqual(f.helper.record["name"], "JP fast")

    def test_unscored_current_path_respects_health_and_cooldown_through_real_scan(self):
        for samples in ("missing", "outside_scope"):
            for outcome in ("healthy_cooldown", "fault_cooldown", "cooldown_boundary", "first_selection"):
                with self.subTest(samples=samples, outcome=outcome), self.runtime() as f:
                    f.clock[0] = 2800 if outcome == "cooldown_boundary" else 1100
                    if samples == "missing":
                        f.api.delays["JP old"] = None
                    else:
                        f.cfg.update(scan_mode="nodes", scan_nodes=["JP fast"])
                    if outcome == "first_selection":
                        f.helper.record = {}
                    if outcome == "fault_cooldown":
                        f.verify.side_effect = lambda *_args, **_kwargs: {
                            "ok": f.api.proxies["Main"]["now"] != "JP old",
                            "reason": "fake active path failure",
                        }

                    f.helper.scan()

                    # Candidate eligibility alone does not establish a path failure.
                    f.verify.assert_called()
                    self.assertEqual(f.helper.failures, 0)
                    if outcome == "healthy_cooldown":
                        self.assertTrue(f.helper.status["health"]["ok"])
                        self.assertEqual(f.api.select_calls, [])
                        self.assertEqual(f.verify.call_count, 1)
                        self.assertEqual(f.api.proxies["Main"]["now"], "JP old")
                        self.assertEqual(f.helper.record["last_switch_at"], 1000)
                    else:
                        self.assertEqual(f.api.select_calls, [("Main", "JP fast")])
                        self.assertEqual(f.api.proxies["Main"]["now"], "JP fast")
                        self.assertEqual(f.helper.record["last_switch_at"], f.clock[0])
                        self.assertEqual(f.verify.call_count, 3 if outcome == "fault_cooldown" else 2)

    def test_unqualified_unhealthy_paused_dry_run_and_mismatched_records_keep_normal_interval(self):
        for scenario in ("unqualified", "unhealthy", "paused", "dry_run", "wrong_group", "wrong_name"):
            with self.subTest(scenario=scenario), self.runtime() as f:
                if scenario == "unqualified":
                    f.api.delays = {name: None for name in f.api.delays}
                elif scenario == "unhealthy":
                    f.verify.return_value = {"ok": False, "http_status": 500, "reason": "offline"}
                elif scenario == "paused":
                    f.cfg["enabled"] = False
                elif scenario == "dry_run":
                    f.helper.dry_run = True
                elif scenario == "wrong_group":
                    f.helper.record["group"] = "Other"
                else:
                    f.helper.record["name"] = "JP other"
                f.helper.scan()
                self.assertEqual(f.helper.last_scan, 1000)
                self.assert_deadline(f, 1300)
                if scenario == "unhealthy":
                    self.assertGreaterEqual(f.helper.failures, 2)

    def test_user_force_scan_runs_before_delayed_regular_deadline(self):
        with self.runtime() as f:
            requested = False

            def request(now):
                nonlocal requested
                if now >= 1050 and not requested:
                    (f.state / "scan.request").touch()
                    requested = True

            scans, _checks = self.run_until(f, 1060, request)
            self.assertEqual(scans, [(1000, False), (1050, True)])
            self.assertEqual(f.helper.last_scan, 1050)
            self.assertEqual(f.api.select_calls, [])

    def test_two_health_failures_trigger_early_scan_and_verified_fault_switch(self):
        with self.runtime() as f:
            def verify(_general, *_args, **_kwargs):
                ok = f.clock[0] < 1015 or f.api.proxies["Main"]["now"] != "JP old"
                return {"ok": ok, "http_status": 405 if ok else 500, "reason": "fake path check"}

            f.verify.side_effect = verify
            scans, checks = self.run_until(f, 1040)
            self.assertEqual(scans, [(1000, False), (1032, False)])
            self.assertIn(1016, checks)
            self.assertIn(1031, checks)
            self.assertEqual(f.api.proxies["Main"]["now"], "JP fast")
            self.assertEqual(f.helper.last_scan, 1032)
            self.assert_deadline(f, 2832)

    def test_transport_recovery_expiry_scans_before_regular_deadline_only_once(self):
        with self.runtime() as f:
            emitted = False

            def poll():
                nonlocal emitted
                if not emitted and f.clock[0] >= 1050:
                    f.signals.last_new_error_at = f.clock[0]
                    emitted = True
                else:
                    f.signals.last_new_error_at = None
                return int(emitted)

            f.signals.poll.side_effect = poll
            scans, checks = self.run_until(f, 1150)
            self.assertEqual(scans, [(1000, False), (1110, False)])
            self.assertTrue(any(1050 < at < 1110 for at in checks))
            self.assertFalse(f.helper.recovery_scan_pending)
            self.assertEqual(f.api.select_calls, [])

    def test_policy_and_shorter_intervals_revoke_or_advance_the_delayed_schedule(self):
        for setting, initial, changed, expected in (
            ("prefer_japan", True, False, 2800),
            ("scan_interval_seconds", 3600, 60, 2800),
            ("switch_cooldown_seconds", 1800, 60, 1350),
        ):
            with self.subTest(setting=setting), self.runtime() as f:
                f.cfg[setting] = initial
                f.helper.scan()
                previous = f.helper.next_scan
                f.clock[0] = 1050
                f.cfg[setting] = changed
                f.helper.reload_config()
                self.assertEqual(f.helper.next_scan, 0)
                self.assertEqual(f.helper.last_scan, 1000)
                f.helper.scan()
                self.assertEqual(f.helper.last_scan, 1050)
                self.assert_deadline(f, expected)
                if setting == "scan_interval_seconds":
                    self.assertLess(f.helper.next_scan, previous)

        with self.subTest(setting="scan_interval_changed_during_health_check"), self.runtime() as f:
            def verify(_general, *_args, **_kwargs):
                f.cfg["scan_interval_seconds"] = 3600
                return {"ok": True, "http_status": 405}

            f.verify.side_effect = verify
            f.helper.scan()
            self.assertEqual(f.helper.last_scan, 1000)
            self.assert_deadline(f, 4600)
            self.assertEqual(f.api.select_calls, [])

    def test_manual_and_external_selection_are_not_held_to_old_nodes_cooldown(self):
        for source in ("manual", "external"):
            with self.subTest(source=source), self.runtime() as f:
                f.helper.scan()
                self.assert_deadline(f, 2800)
                f.clock[0] = 1050
                if source == "manual":
                    optimizer.request_manual_selection("JP manual")
                    self.assertTrue(f.helper.process_manual_selection())
                    self.assertEqual(f.helper.record["name"], "JP manual")
                else:
                    f.api.proxies["Main"]["now"] = "JP manual"
                    f.helper.tick_health()
                self.assertEqual(f.helper.status["selected"], "JP manual")
                self.assertGreater(f.helper.manual_until, f.clock[0])
                self.assertLessEqual(f.helper.next_scan, 1350)
                self.assertEqual(f.helper.failures, 0)


if __name__ == "__main__":
    unittest.main()
