"""Offline recovery-window scheduling with virtual time and fake proxy traffic."""

import concurrent.futures
from contextlib import contextmanager
import threading
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


class TransportRecoveryTests(unittest.TestCase):
    @contextmanager
    def runtime(self):
        fixture = test_runtime.RuntimeTests(
            "test_paused_manual_switch_is_confirmed_and_survives_helper_restart"
        )
        fixture.clock = [1000.0]
        try:
            with patch.object(optimizer.time, "time", side_effect=lambda: fixture.clock[0]):
                fixture.setUp()
                fixture.cfg["concurrency"] = 1
                fixture.signals.last_new_error_at = None
                yield fixture
        finally:
            fixture.doCleanups()

    def start_recovery(self, fixture):
        fixture.signals.last_new_error_at = fixture.clock[0]
        fixture.signals.poll.return_value = 2
        fixture.helper.poll_transport_signals(force=True)
        fixture.signals.last_new_error_at = None

    def test_poll_is_throttled_and_old_counts_do_not_extend_the_window(self):
        with self.runtime() as f:
            self.start_recovery(f)
            self.assertEqual(f.helper.recovery_until, 1060)
            self.assertTrue(f.helper.recovery_scan_pending)
            reads = f.signals.poll.call_count
            f.clock[0] = 1004
            f.helper.poll_transport_signals()
            self.assertEqual(f.signals.poll.call_count, reads)
            f.clock[0] = 1005
            f.helper.poll_transport_signals()
            self.assertEqual(f.signals.poll.call_count, reads + 1)
            self.assertEqual(f.helper.recovery_until, 1060)
            f.clock[0] = 1010
            f.signals.last_new_error_at = 1010
            f.helper.poll_transport_signals()
            self.assertEqual(f.helper.recovery_until, 1070)
            f.clock[0] = 1200
            f.helper.recovery_scan_pending = False
            f.signals.last_new_error_at = 1100
            f.helper.poll_transport_signals()
            self.assertEqual(f.helper.recovery_until, 1070)
            self.assertFalse(f.helper.recovery_scan_pending)

    def test_run_keeps_health_checks_and_scans_once_when_recovery_expires(self):
        with self.runtime() as f:
            self.start_recovery(f)
            f.api.delays.update({"JP old": 100, "JP fast": 300, "JP manual": 400})
            scans, health_checks, measurements = [], [], []
            scan, tick, delay = f.helper.scan, f.helper.tick_health, f.api.delay

            def scan_at(*args, **kwargs):
                started, before = f.clock[0], len(measurements)
                result = scan(*args, **kwargs)
                if len(measurements) > before:
                    scans.append(started)
                return result

            def health_at():
                health_checks.append(f.clock[0])
                return tick()

            def measure_at(name):
                measurements.append(f.clock[0])
                return delay(name)

            def advance(seconds):
                f.clock[0] = round(f.clock[0] + seconds, 3)

            with patch.object(f.helper, "scan", side_effect=scan_at), \
                 patch.object(f.helper, "tick_health", side_effect=health_at), \
                 patch.object(f.api, "delay", side_effect=measure_at), \
                 patch.object(optimizer.time, "sleep", side_effect=advance), \
                 patch.object(f.helper, "stopping", side_effect=lambda: f.clock[0] >= 1190):
                f.helper.run()

            during_recovery = [at for at in health_checks if at < 1060]
            self.assertEqual(len(during_recovery), 4)
            self.assertLessEqual(during_recovery[0], 1001)
            self.assertEqual([b - a for a, b in zip(during_recovery, during_recovery[1:])], [15, 15, 15])
            self.assertEqual(scans, [1060])
            self.assertTrue(measurements)
            self.assertTrue(all(at >= 1060 for at in measurements))
            self.assertEqual(f.api.select_calls, [])
            self.assertFalse(f.helper.recovery_scan_pending)

    def test_background_scan_cancels_unstarted_measurements_when_an_error_arrives(self):
        with self.subTest(stage="before_submit"), self.runtime() as f:
            f.helper.tick_health()
            self.start_recovery(f)
            f.helper.failures = 2
            f.api.proxies["Main"]["now"] = "JP manual"
            with patch.object(f.api, "delay", wraps=f.api.delay) as delay:
                f.helper.scan()
            delay.assert_not_called()
            self.assertEqual(f.helper.failures, 0)
            self.assertEqual(f.api.proxies["Main"]["now"], "JP manual")
            self.assertEqual(f.api.select_calls, [])

        with self.subTest(stage="during_measurement"), self.runtime() as f:
            names = ["JP old", "JP fast", "JP manual"] + [f"JP extra {i}" for i in range(8)]
            for name in names:
                f.api.proxies.setdefault(name, {"type": "Trojan"})
            f.api.proxies["Main"]["all"] = names
            release = threading.Event()
            measured = []
            real_executor = concurrent.futures.ThreadPoolExecutor

            def make_executor(*args, **kwargs):
                executor = real_executor(*args, **kwargs)
                shutdown = executor.shutdown

                def release_running_request(*args, **kwargs):
                    release.set()
                    return shutdown(*args, **kwargs)

                executor.shutdown = release_running_request
                return executor

            def interrupted_delay(name):
                measured.append(name)
                if len(measured) == 1:
                    f.clock[0] += 5
                    f.signals.last_new_error_at = f.clock[0]
                elif not release.wait(3):
                    raise AssertionError("scan did not cancel its pending measurements")
                return f.api.delays.get(name, 200)

            try:
                with patch.object(optimizer.concurrent.futures, "ThreadPoolExecutor", side_effect=make_executor), \
                     patch.object(f.api, "delay", side_effect=interrupted_delay):
                    f.helper.scan()
            finally:
                release.set()

            self.assertLess(len(measured), len(names))
            self.assertEqual(f.api.select_calls, [])
            self.assertTrue(f.helper.recovery_scan_pending)
            self.assertEqual(f.helper.status["phase"], "monitoring")

    def test_explicit_scan_measures_but_does_not_switch_a_healthy_path_during_recovery(self):
        with self.runtime() as f:
            self.start_recovery(f)
            with patch.object(f.api, "delay", wraps=f.api.delay) as delay:
                f.helper.scan(forced=True)
            self.assertGreater(delay.call_count, 0)
            self.assertTrue(f.helper.status["nodes"])
            self.assertEqual(f.api.select_calls, [])
            self.assertEqual(f.api.proxies["Main"]["now"], "JP old")

    def test_two_health_failures_allow_emergency_recovery_unless_the_path_recovers(self):
        for outcome in ("unavailable", "healthy", "confirmation_healthy"):
            with self.subTest(outcome=outcome), self.runtime() as f:
                self.start_recovery(f)
                f.verify.return_value = {"ok": False, "reason": "mock path unavailable"}
                f.helper.tick_health()
                f.helper.tick_health()
                self.assertEqual(f.helper.failures, 2)

                checks = 0

                def verify_current(*args, **kwargs):
                    nonlocal checks
                    checks += 1
                    ok = (outcome == "healthy" or (outcome == "confirmation_healthy" and checks >= 2)
                          or f.api.proxies["Main"]["now"] == "JP fast")
                    return {"ok": ok, "http_status": 405 if ok else 503}

                f.verify.side_effect = verify_current
                with patch.object(f.api, "delay", wraps=f.api.delay) as delay:
                    f.helper.scan()
                self.assertGreater(delay.call_count, 0)
                self.assertEqual(f.api.select_calls, [("Main", "JP fast")] if outcome == "unavailable" else [])
                self.assertEqual(f.helper.failures, 0)

    def test_new_error_just_before_put_is_seen_even_inside_the_poll_interval(self):
        with self.runtime() as f:
            f.helper.poll_transport_signals(force=True)
            reload_config = f.helper.reload_config

            def error_during_reload():
                reload_config()
                f.signals.last_new_error_at = f.clock[0]

            with patch.object(f.helper, "reload_config", side_effect=error_during_reload):
                self.assertFalse(f.helper.change_candidate(f.api.general, "Main", "JP old", "JP fast"))
            self.assertEqual(f.api.select_calls, [])
            self.assertNotIn("JP fast", f.helper.quarantine)

    def test_new_error_during_candidate_validation_rolls_back_without_quarantine(self):
        for stage in ("cancel_callback", "commit_guard"):
            with self.subTest(stage=stage), self.runtime() as f:
                f.helper.poll_transport_signals(force=True)
                cancellations = []

                def fail_while_verifying(general, idle_seconds, cancel, **kwargs):
                    if stage == "cancel_callback":
                        # Cancellation polls are throttled; advance to their next due read.
                        f.clock[0] += 5
                    f.signals.last_new_error_at = f.clock[0]
                    cancelled = cancel()
                    cancellations.append(cancelled)
                    return {"ok": not cancelled, "cancelled": cancelled, "reason": "transport recovery"}

                f.verify.side_effect = fail_while_verifying
                self.assertFalse(f.helper.change_candidate(f.api.general, "Main", "JP old", "JP fast"))
                self.assertEqual(cancellations, [stage == "cancel_callback"])
                self.assertEqual(f.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
                self.assertNotIn("JP fast", f.helper.quarantine)
                f.helper.recover_pending()
                self.assertFalse((f.state / "pending-selection.json").exists())

    def test_manual_selection_remains_available_during_recovery(self):
        with self.runtime() as f:
            self.start_recovery(f)
            optimizer.request_manual_selection("JP manual")
            self.assertTrue(f.helper.process_manual_selection())
            self.assertEqual(f.api.select_calls, [("Main", "JP manual")])
            self.assertTrue(f.helper.status["manual_selection_result"]["ok"])


if __name__ == "__main__":
    unittest.main()
