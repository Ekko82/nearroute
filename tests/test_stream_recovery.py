"""Observed and source-derived Codex retry logs drive recovery without network traffic."""

import unittest
from unittest.mock import patch

import optimizer
from tests import test_log_signals
from tests import test_runtime


class StreamRecoveryTests(unittest.TestCase):
    def test_retry_rows_defer_measurement_and_switch_until_recovery_expires(self):
        rows = [test_log_signals.sampling_retry(error) for error in
                (test_log_signals.WS_CLOSE, test_log_signals.RESET_ZH, test_log_signals.TLS_EOF)]
        rows.append(test_log_signals.compact_retry())
        for row in rows:
            with self.subTest(row=row):
                logs = test_log_signals.LogSignalsTests()
                runtime = test_runtime.RuntimeTests()
                try:
                    # Build the real SQLite reader before RuntimeTests replaces
                    # LogSignals; every controller and proxy request stays fake.
                    logs.setUp()
                    runtime.setUp()
                    clock = [1000.0]
                    logs.clock.side_effect = lambda: clock[0]
                    runtime.cfg["codex_log_db"] = str(logs.path)
                    runtime.helper.cfg["codex_log_db"] = str(logs.path)
                    runtime.helper.signals = logs.signals
                    logs.prime()
                    logs.insert(row)
                    runtime.helper.poll_transport_signals(force=True)

                    clock[0] = 1010.0
                    runtime.helper.poll_transport_signals(force=True)
                    self.assertIsNone(logs.signals.last_new_error_at)
                    self.assertEqual(runtime.helper.recovery_until, 1060)

                    scans, health_checks, measurements, selections = [], [], [], []
                    scan = runtime.helper.scan
                    tick = runtime.helper.tick_health
                    delay = runtime.api.delay
                    select = runtime.api.select

                    def scan_at(*args, **kwargs):
                        started, before = clock[0], len(measurements)
                        result = scan(*args, **kwargs)
                        if len(measurements) > before:
                            scans.append(started)
                        return result

                    def health_at():
                        health_checks.append(clock[0])
                        return tick()

                    def measure_at(name):
                        measurements.append(clock[0])
                        return delay(name)

                    def select_at(group, name):
                        selections.append((clock[0], group, name))
                        return select(group, name)

                    def advance(seconds):
                        clock[0] = round(clock[0] + seconds, 3)

                    with patch.object(runtime.helper, "scan", side_effect=scan_at), \
                         patch.object(runtime.helper, "tick_health", side_effect=health_at), \
                         patch.object(runtime.api, "delay", side_effect=measure_at), \
                         patch.object(runtime.api, "select", side_effect=select_at), \
                         patch.object(optimizer.time, "sleep", side_effect=advance), \
                         patch.object(runtime.helper, "stopping", side_effect=lambda: clock[0] >= 1190):
                        runtime.helper.run()

                    protected_checks = [at for at in health_checks if at < 1060]
                    self.assertEqual(len(protected_checks), 4)
                    self.assertLessEqual(protected_checks[0], 1011)
                    self.assertEqual([b - a for a, b in zip(protected_checks, protected_checks[1:])], [15, 15, 15])
                    self.assertEqual(scans, [1060], "expiry must schedule exactly one catch-up scan")
                    self.assertTrue(measurements)
                    self.assertTrue(all(at >= 1060 for at in measurements))
                    self.assertEqual(selections, [(1060, "Main", "JP fast")])
                    self.assertEqual(runtime.api.proxies["Main"]["now"], "JP fast")
                    self.assertEqual(runtime.helper.recovery_until, 1060, "quiet polls must not extend recovery")
                    self.assertFalse(runtime.helper.recovery_scan_pending)
                finally:
                    runtime.doCleanups()
                    logs.doCleanups()


if __name__ == "__main__":
    unittest.main()
