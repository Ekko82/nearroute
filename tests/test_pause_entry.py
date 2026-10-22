"""Pausing after run chooses a scan must stop automatic work at its entry."""

import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


REAL_LOAD_CONFIG = optimizer.load_config


class PauseEntryTests(unittest.TestCase):
    def test_pause_at_run_scan_entry_cancels_only_automatic_measurement(self):
        for forced in (False, True):
            with self.subTest(forced=forced):
                fixture = test_runtime.RuntimeTests()
                fixture.setUp()
                try:
                    optimizer.atomic_json(optimizer.CONFIG, fixture.cfg)
                    if forced:
                        (fixture.state / "scan.request").touch()
                    fixture.helper.last_scan = 100.0
                    fixture.helper.status["last_scan_at"] = optimizer.iso(100)
                    finished = [False]
                    scan = fixture.helper.scan

                    def pause_at_entry(*args, **kwargs):
                        saved = REAL_LOAD_CONFIG()
                        saved["enabled"] = False
                        optimizer.atomic_json(optimizer.CONFIG, saved)
                        try:
                            return scan(*args, **kwargs)
                        finally:
                            finished[0] = True

                    with patch.object(optimizer, "load_config", side_effect=REAL_LOAD_CONFIG), \
                         patch.object(fixture.helper, "scan", side_effect=pause_at_entry), \
                         patch.object(fixture.helper, "stopping", side_effect=lambda: finished[0]), \
                         patch.object(fixture.api, "snapshot", wraps=fixture.api.snapshot) as snapshot, \
                         patch.object(fixture.api, "delay", wraps=fixture.api.delay) as delay:
                        fixture.helper.run()

                    self.assertTrue(finished[0], "run did not reach the scan entry")
                    self.assertFalse(REAL_LOAD_CONFIG()["enabled"])
                    self.assertEqual(fixture.api.select_calls, [])
                    fixture.verify.assert_not_called()
                    if forced:
                        self.assertEqual(delay.call_count, 9)  # Initial round and two confirmation rounds.
                        self.assertGreater(snapshot.call_count, 0)
                        self.assertGreater(fixture.helper.last_scan, 100.0)
                        self.assertNotEqual(fixture.helper.status["last_scan_at"], optimizer.iso(100))
                    else:
                        delay.assert_not_called()
                        snapshot.assert_not_called()
                        self.assertEqual(fixture.helper.last_scan, 100.0)
                        self.assertEqual(fixture.helper.status["last_scan_at"], optimizer.iso(100))
                        self.assertEqual(fixture.helper.next_scan, 0)
                finally:
                    fixture.doCleanups()


if __name__ == "__main__":
    unittest.main()
