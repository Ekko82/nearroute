"""Node scores retain the probe provenance of the measurements they display."""

import copy
import unittest
from unittest.mock import patch

import optimizer
import test_runtime


class ProbeStatusTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.RuntimeTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.cfg["concurrency"] = 1

    def read_status(self):
        return optimizer.read_json(self.fixture.state / "status.json", {})

    def publish_cached_rows(self, name="JP old"):
        helper = self.fixture.helper
        helper.add_sample(name, 123)
        helper.write_status(nodes=helper.statistics([name]), last_scan_at=optimizer.iso(1000))
        return self.read_status()

    def change_probe(self, **extra):
        self.fixture.cfg.update(probe_url="https://example.invalid/new-probe",
                                probe_expected_statuses=[204], **extra)
        self.fixture.cfg = optimizer.validate_config(self.fixture.cfg)
        return optimizer.probe_identity(self.fixture.cfg)

    def test_scan_progress_and_completed_rows_have_their_own_probe_identity(self):
        fixture = self.fixture
        fixture.cfg["enabled"] = False
        fixture.helper.status["last_scan_at"] = optimizer.iso(1000)
        published = []
        write = fixture.helper.write_status

        def capture(**updates):
            result = write(**updates)
            if "nodes" in updates:
                published.append(copy.deepcopy(fixture.helper.status))
            return result

        with patch.object(fixture.helper, "write_status", side_effect=capture):
            fixture.helper.scan()

        identity = optimizer.probe_identity(fixture.cfg)
        self.assertTrue(published)
        self.assertIsNone(published[0]["last_scan_at"])
        self.assertTrue(all(status["nodes_probe_identity"] == identity for status in published))
        completed = self.read_status()
        self.assertEqual(completed["last_scan_at"], optimizer.iso(fixture.helper.last_scan))
        self.assertNotEqual(completed["last_scan_at"], optimizer.iso(1000))
        self.assertTrue(any(row["score_ms"] is not None for row in completed["nodes"]))
        self.assertEqual(fixture.api.select_calls, [])

    def test_restart_and_heartbeat_keep_cached_scores_old_until_new_rows_are_published(self):
        fixture = self.fixture
        fixture.cfg["enabled"] = False
        cached = self.publish_cached_rows()
        old_identity = cached["nodes_probe_identity"]
        new_identity = self.change_probe()
        fixture.helper.reload_config()
        self.assertEqual(optimizer.read_json(fixture.state / "history-probe.json", {})["fingerprint"],
                         new_identity)
        self.assertEqual(self.read_status()["nodes"], cached["nodes"])

        restarted = optimizer.Optimizer()
        self.assertEqual(restarted.history, {})
        self.assertEqual(restarted.history_probe, new_identity)
        restarted.write_status(message="heartbeat before new measurements")
        status = self.read_status()
        self.assertEqual(status["nodes"], cached["nodes"])
        self.assertEqual(status["nodes_probe_identity"], old_identity)
        self.assertNotEqual(status["nodes_probe_identity"], new_identity)
        self.assertEqual(status["last_scan_at"], cached["last_scan_at"])

        restarted.write_status(nodes=restarted.statistics(["JP old"]))
        status = self.read_status()
        self.assertEqual(status["nodes_probe_identity"], new_identity)
        self.assertEqual(status["nodes"][0]["name"], "JP old")
        self.assertIsNone(status["nodes"][0]["score_ms"])
        self.assertIsNone(status["last_scan_at"])
        restarted.scan()
        completed = self.read_status()
        self.assertEqual(completed["nodes_probe_identity"], new_identity)
        self.assertEqual(completed["last_scan_at"], optimizer.iso(restarted.last_scan))
        self.assertTrue(any(row["score_ms"] is not None for row in completed["nodes"]))

    def test_restart_does_not_assign_provenance_to_unidentified_legacy_rows(self):
        fixture = self.fixture
        cached = self.publish_cached_rows()
        cached.pop("nodes_probe_identity")
        optimizer.atomic_json(fixture.state / "status.json", cached)

        restarted = optimizer.Optimizer()
        restarted.write_status(message="legacy cache heartbeat")

        status = self.read_status()
        self.assertEqual(status["nodes"], cached["nodes"])
        self.assertNotIn("nodes_probe_identity", status)

    def test_tentative_config_reload_cannot_relabel_old_history_as_the_new_probe(self):
        fixture = self.fixture
        cached = self.publish_cached_rows()
        old_identity = cached["nodes_probe_identity"]
        fixture.helper.tentative = True
        try:
            new_identity = self.change_probe()
            fixture.helper.reload_config()
            self.assertEqual(optimizer.probe_identity(fixture.helper.cfg), new_identity)
            fixture.helper.write_status(nodes=fixture.helper.statistics(["JP old"]))
        finally:
            fixture.helper.tentative = False

        status = self.read_status()
        self.assertEqual(status["nodes"][0]["median_ms"], 123)
        self.assertEqual(status["nodes_probe_identity"], old_identity)
        self.assertNotEqual(status["nodes_probe_identity"], new_identity)
        self.assertEqual(status["last_scan_at"], cached["last_scan_at"])

    def test_probe_change_and_pause_during_validation_do_not_publish_old_rankings_as_new(self):
        fixture = self.fixture
        changed = False

        def verify(*_args, **_kwargs):
            nonlocal changed
            if fixture.api.proxies["Main"]["now"] == "JP fast":
                self.change_probe(enabled=False)
                changed = True
            return {"ok": True, "http_status": 405}

        fixture.verify.side_effect = verify
        fixture.helper.scan()

        self.assertTrue(changed)
        self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
        self.assertEqual(fixture.helper.history, {})
        status = self.read_status()
        self.assertEqual(status["phase"], "paused")
        self.assertEqual(status["nodes"], [])
        self.assertEqual(status["nodes_probe_identity"], optimizer.probe_identity(fixture.cfg))
        self.assertIsNone(status["last_scan_at"])
        self.assertFalse((fixture.state / "pending-selection.json").exists())

    def test_restore_keeps_cached_node_identity_after_probe_settings_changed(self):
        fixture = self.fixture
        self.assertTrue(fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast"))
        cached = self.publish_cached_rows("JP fast")
        new_identity = self.change_probe()

        with patch.object(optimizer, "stop_daemon", return_value=True):
            self.assertEqual(optimizer.restore_original(), ["Main"])

        status = self.read_status()
        self.assertEqual(status["selected"], "JP old")
        self.assertEqual(status["phase"], "stopped")
        self.assertEqual(status["nodes"], cached["nodes"])
        self.assertEqual(status["nodes_probe_identity"], cached["nodes_probe_identity"])
        self.assertNotEqual(status["nodes_probe_identity"], new_identity)
        self.assertEqual(status["last_scan_at"], cached["last_scan_at"])
        self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])


if __name__ == "__main__":
    unittest.main()
