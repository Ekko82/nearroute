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


class NewPathHealthSchedulingTests(unittest.TestCase):
    @contextmanager
    def external_path_after_scan(self):
        fixture = test_runtime.RuntimeTests()
        fixture.clock = [1000.0]
        try:
            with patch.object(optimizer.time, "time", side_effect=lambda: fixture.clock[0]):
                fixture.setUp()
                fixture.cfg.update(concurrency=1, probe_timeout_ms=15000)
                fixture.cfg = optimizer.validate_config(fixture.cfg)
                select = fixture.helper.change_candidate

                def external_choice(*args, **kwargs):
                    changed = select(*args, **kwargs)
                    if changed:
                        fixture.api.proxies["Main"]["now"] = "JP manual"
                    return changed

                with patch.object(fixture.helper, "change_candidate", side_effect=external_choice):
                    fixture.helper.scan(forced=True)
                self.assertEqual(fixture.helper.status["selected"], "JP manual")
                self.assertIsNone(fixture.helper.status["health"])
                self.assertEqual(fixture.helper.next_scan, 0)
                self.assertEqual(fixture.helper.last_health, 0)
                yield fixture
        finally:
            fixture.doCleanups()

    def run_until_next_scan(self, fixture, healthy=True):
        events, health_statuses, scan_statuses, delays = [], [], [], []
        stopped = False
        deadline = fixture.clock[0] + 180
        scan = fixture.helper.scan
        tick = fixture.helper.tick_health
        fixture.verify.return_value = ({"ok": True, "http_status": 405}
                                       if healthy else {"ok": False, "http_status": 403, "reason": "new path failed"})

        def slow_delay(name):
            delays.append(name)
            fixture.clock[0] += 5
            return 5000

        def record_health():
            events.append(("health", fixture.clock[0], len(delays)))
            tick()
            health_statuses.append(optimizer.read_json(fixture.state / "status.json", {}))

        def record_scan(*args, **kwargs):
            nonlocal stopped
            events.append(("scan", fixture.clock[0], kwargs.get("forced", False)))
            scan_statuses.append(optimizer.read_json(fixture.state / "status.json", {}))
            scan(*args, **kwargs)
            stopped = True

        def advance(seconds):
            fixture.clock[0] += seconds

        with patch.object(fixture.api, "delay", side_effect=slow_delay), \
             patch.object(fixture.helper, "tick_health", side_effect=record_health), \
             patch.object(fixture.helper, "scan", side_effect=record_scan), \
             patch.object(fixture.helper, "stopping", side_effect=lambda: stopped or fixture.clock[0] >= deadline), \
             patch.object(optimizer.time, "sleep", side_effect=advance):
            fixture.helper.run()
        self.assertTrue(stopped, "the next scan must complete without starving behind health checks")
        self.assertTrue(delays, "run must execute the real candidate measurement path")
        return events, health_statuses, scan_statuses

    def test_new_external_path_health_is_published_before_another_ordinary_scan(self):
        for healthy in (True, False):
            with self.subTest(healthy=healthy), self.external_path_after_scan() as fixture:
                selected_before = list(fixture.api.select_calls)
                changed_at = fixture.clock[0]

                events, health_statuses, scan_statuses = self.run_until_next_scan(fixture, healthy)

                self.assertEqual([event[0] for event in events], ["health", "scan"])
                self.assertEqual(events[0][2], 0, "new-path health must not wait for candidate probes")
                self.assertLess(events[0][1] - changed_at, fixture.cfg["health_interval_seconds"])
                status = health_statuses[0]
                self.assertEqual(status["selected"], "JP manual")
                self.assertEqual(status["health"]["ok"], healthy)
                self.assertEqual(status["health"]["http_status"], 405 if healthy else 403)
                self.assertEqual(status["last_health_at"], optimizer.iso(events[0][1]))
                self.assertEqual(status["consecutive_health_failures"], 0 if healthy else 1)
                self.assertEqual(scan_statuses[0]["health"], status["health"])
                self.assertGreater(fixture.helper.last_scan, changed_at)
                self.assertEqual(fixture.api.select_calls, selected_before)

    def test_existing_fault_and_transport_recovery_scans_keep_priority(self):
        for reason in ("fault", "transport_recovery"):
            with self.subTest(reason=reason), self.external_path_after_scan() as fixture:
                if reason == "fault":
                    fixture.helper.failures = 2
                else:
                    fixture.helper.recovery_scan_pending = True
                    fixture.helper.recovery_until = fixture.clock[0]

                events, health_statuses, _ = self.run_until_next_scan(fixture)

                self.assertEqual(events[0][0], "scan")
                self.assertFalse(events[0][2])
                self.assertEqual(health_statuses, [])

    def test_forced_scan_keeps_priority_over_unverified_path_health(self):
        with self.external_path_after_scan() as fixture:
            (fixture.state / "scan.request").touch()

            events, health_statuses, _ = self.run_until_next_scan(fixture)

            self.assertEqual(events[0][0], "scan")
            self.assertTrue(events[0][2])
            self.assertEqual(health_statuses, [])
            self.assertFalse((fixture.state / "scan.request").exists())


class ChangedPathSchedulingTests(unittest.TestCase):
    def run_path_change(self, entry, *, health_error=False):
        fixture = test_runtime.RuntimeTests()
        clock = [1000.0]
        changed_at = None
        finished = False
        scan_count = 0
        new_path_checks, new_scan_probes = [], []
        try:
            with patch.object(optimizer.time, "time", side_effect=lambda: clock[0]):
                fixture.setUp()
                fixture.cfg.update(concurrency=1, probe_timeout_ms=15000)
                fixture.cfg = optimizer.validate_config(fixture.cfg)
                fixture.helper.next_scan = 1001.0 if entry == "scan_guard" else 1300.0
                scan, tick = fixture.helper.scan, fixture.helper.tick_health

                def change_path():
                    nonlocal changed_at
                    fixture.api.proxies["Main"]["now"] = "JP manual"
                    changed_at = clock[0]

                def verify(*_args, **_kwargs):
                    current = fixture.api.proxies["Main"]["now"]
                    if current == "JP old":
                        if entry == "health_second_snapshot" and changed_at is None:
                            change_path()
                        return {"ok": True, "http_status": 405}
                    new_path_checks.append(clock[0])
                    return {"ok": False, "http_status": 403, "reason": "new path failed"}

                def delay(name):
                    if entry == "scan_guard" and scan_count == 1:
                        clock[0] += 0.1
                        if changed_at is None:
                            change_path()
                        return 100
                    new_scan_probes.append(name)
                    clock[0] += 5
                    return 5000

                def record_scan(*args, **kwargs):
                    nonlocal scan_count
                    scan_count += 1
                    return scan(*args, **kwargs)

                def record_health():
                    nonlocal finished
                    if health_error and fixture.api.proxies["Main"]["now"] == "JP manual":
                        new_path_checks.append(clock[0])
                        fixture.api.offline = True
                    tick()
                    if (fixture.helper.status.get("selected") == "JP manual"
                            and fixture.helper.status.get("health") is not None):
                        finished = True

                def advance(seconds):
                    clock[0] = round(clock[0] + seconds, 6)

                fixture.verify.side_effect = verify
                with patch.object(fixture.api, "delay", side_effect=delay), \
                     patch.object(fixture.helper, "scan", side_effect=record_scan), \
                     patch.object(fixture.helper, "tick_health", side_effect=record_health), \
                     patch.object(fixture.helper, "stopping", side_effect=lambda: finished or clock[0] >= 1050), \
                     patch.object(optimizer.time, "sleep", side_effect=advance):
                    fixture.helper.run()
                self.assertEqual(fixture.api.select_calls, [])
                return {
                    "changed_at": changed_at,
                    "checks": new_path_checks,
                    "new_scan_probes": new_scan_probes,
                    "status": optimizer.read_json(fixture.state / "status.json", {}),
                }
        finally:
            fixture.doCleanups()

    def assert_new_path_checked_first(self, result):
        self.assertTrue(result["checks"])
        self.assertLess(result["checks"][0] - result["changed_at"], 2)
        self.assertEqual(result["new_scan_probes"], [], "new path health must precede another scan")
        self.assertEqual(result["status"]["selected"], "JP manual")
        self.assertEqual(result["status"]["health"]["http_status"], 403)
        self.assertEqual(result["status"]["consecutive_health_failures"], 1)
        self.assertEqual(result["status"]["last_health_at"], optimizer.iso(result["checks"][0]))

    def test_scan_guard_checks_new_path_despite_recent_old_path_health(self):
        self.assert_new_path_checked_first(self.run_path_change("scan_guard"))

    def test_health_second_snapshot_change_is_checked_before_another_scan(self):
        self.assert_new_path_checked_first(self.run_path_change("health_second_snapshot"))

    def test_new_path_health_error_preserves_controller_retry_backoff(self):
        result = self.run_path_change("health_second_snapshot", health_error=True)
        self.assertEqual(result["checks"], [1001, 1016, 1031, 1046])
        self.assertEqual(result["new_scan_probes"], [])
        self.assertFalse(result["status"]["controller_available"])
        self.assertIsNone(result["status"]["health"])
        self.assertIn("mock controller unavailable", result["status"]["last_error"])


if __name__ == "__main__":
    unittest.main()
