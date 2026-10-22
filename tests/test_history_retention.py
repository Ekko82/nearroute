"""Expired telemetry is discarded without deleting valid out-of-scope history."""

import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


class HistoryRetentionTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.RuntimeTests("test_unchanged_probe_preserves_history_on_normal_reload")
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.cfg.update(enabled=False, scan_mode="nodes", scan_nodes=["JP old"])
        self.clock = [10000.0]
        clock_patch = patch.object(optimizer.time, "time", side_effect=lambda: self.clock[0])
        clock_patch.start()
        self.addCleanup(clock_patch.stop)

    def restart(self, history, quarantine):
        state = self.fixture.state
        optimizer.atomic_json(state / "history.json", history)
        optimizer.atomic_json(state / "quarantine.json", quarantine)
        fingerprint = optimizer.read_json(state / "history-probe.json", {})
        self.assertEqual(fingerprint["fingerprint"], optimizer.probe_identity(self.fixture.cfg))
        return optimizer.Optimizer()

    def assert_saved(self, helper):
        self.assertEqual(optimizer.read_json(self.fixture.state / "history.json", None), helper.history)
        self.assertEqual(optimizer.read_json(self.fixture.state / "quarantine.json", None), helper.quarantine)

    def test_same_probe_restart_removes_expired_and_empty_keys_but_keeps_valid_hidden_history(self):
        recent_hidden = [{"at": 9990.0, "delay": 150.0}]
        at_boundary = {"at": 8200.0, "delay": None}
        helper = self.restart({
            "JP removed": [{"at": 8199.0, "delay": 100.0}],
            "JP empty": [],
            "JP old": [{"at": 8199.0, "delay": 101.0}, at_boundary],
            "SG hidden": recent_hidden,
        }, {"JP removed": 9999.0, "JP expired at boundary": 10000.0, "SG hidden": 10060.0})

        self.assertEqual(helper.history, {"JP old": [at_boundary], "SG hidden": recent_hidden})
        self.assertEqual(helper.quarantine, {"SG hidden": 10060.0})
        self.assert_saved(helper)

    def test_measurement_prunes_records_that_expire_while_the_probe_is_running(self):
        recent_hidden = [{"at": 9990.0, "delay": 150.0}]
        helper = self.restart({
            "JP removed": [{"at": 8201.0, "delay": 100.0}],
            "SG hidden": recent_hidden,
        }, {"JP removed": 10001.0, "SG hidden": 10060.0})
        self.assertIn("JP removed", helper.history)

        def finish_after_expiry(_name):
            self.clock[0] = 10003.0
            return 200.0

        with patch.object(self.fixture.api, "delay", side_effect=finish_after_expiry):
            self.assertEqual(helper.measure_round(["JP old"], "fixture", forced=True), {"JP old": 200.0})

        self.assertEqual(helper.history, {
            "SG hidden": recent_hidden,
            "JP old": [{"at": 10003.0, "delay": 200.0}],
        })
        self.assertEqual(helper.quarantine, {"SG hidden": 10060.0})
        self.assert_saved(helper)
        self.assertEqual(self.fixture.api.select_calls, [])

    def test_new_quarantine_write_omits_old_expired_entries(self):
        self.fixture.cfg["scan_nodes"] = ["JP old", "JP fast"]
        helper = self.restart({}, {"JP removed": 10001.0, "SG hidden": 10060.0})
        self.clock[0] = 10003.0
        self.fixture.cfg["enabled"] = True
        self.fixture.verify.return_value = {"ok": False, "reason": "fixture path failure"}

        self.assertFalse(helper.change_candidate(self.fixture.api.general, "Main", "JP old", "JP fast"))

        self.assertEqual(helper.quarantine, {
            "SG hidden": 10060.0,
            "JP fast": 10003.0 + self.fixture.cfg["node_quarantine_seconds"],
        })
        self.assert_saved(helper)
        self.assertEqual(self.fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])


if __name__ == "__main__":
    unittest.main()
