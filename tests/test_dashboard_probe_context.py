"""Probe changes keep node names visible without attributing old scores to a new target."""

import json
from pathlib import Path
import unittest
from unittest.mock import patch

import dashboard
import optimizer
from tests import test_dashboard_manual
from tests import test_measure_responsiveness


WAIT_REASON = "等待按当前测试目标重新测速"


class DashboardProbeContextTests(unittest.TestCase):
    def setUp(self):
        ui = test_dashboard_manual.StatusFreshnessTests()
        ui.setUp()
        self.app = ui.app
        self.config = dict(optimizer.DEFAULTS)
        self.nodes = [{"name": "JP last-known", "score_ms": 101, "median_ms": 100,
                       "success_rate": 1.0, "jitter_ms": 2, "samples": 4,
                       "eligible": True, "reason": "可用"}]
        self.status = ui.status | {"nodes": self.nodes, "last_scan_at": "2026-10-20T13:14:15+08:00"}

    def refresh(self, status, config):
        with patch.object(dashboard, "read_json", side_effect=lambda path: status if path == dashboard.STATUS else config):
            self.app.refresh()

    def test_saved_probe_change_hides_draining_old_scores_until_a_real_new_scan(self):
        changes = ({"probe_url": "https://fixture.invalid/new-health"},
                   {"probe_expected_statuses": [204]})
        for fields in changes:
            case = test_measure_responsiveness.MeasureResponsivenessTests()
            with self.subTest(fields=fields), case.measurement() as run:
                fixture = run.fixture
                config_path = Path(fixture.temp.name) / "config.json"
                status_path = fixture.state / "status.json"
                config_path.write_text(json.dumps(fixture.cfg), encoding="utf-8")
                old_rows = [self.nodes[0] | {"name": "JP old"}]
                fixture.helper.write_status(
                    phase="scanning", controller_available=True, mode="rule", group="Main",
                    selected="JP old", nodes=old_rows, last_scan_at=self.status["last_scan_at"])
                old_identity = optimizer.probe_identity(fixture.cfg)
                with patch.object(dashboard, "CONFIG", config_path), \
                     patch.object(dashboard, "STATE", fixture.state), \
                     patch.object(dashboard, "STATUS", status_path):
                    saved = dashboard.save_settings(fields)
                    fixture.cfg.update(saved)  # Runtime fixture's load_config reads this mapping.
                    self.app.settings_saved(saved)
                    self.assertTrue(run.shutdown.wait(2.5), "old probe policy was not retired")
                    self.assertFalse(run.release.is_set(), "old request must still be draining")
                    self.app.refresh()
                    self.assertIn(saved["probe_url"], self.app.policy.set.call_args.args[0])
                    self.assertIn(", ".join(map(str, saved["probe_expected_statuses"])), self.app.policy.set.call_args.args[0])
                    self.app.render_nodes.assert_called_with(
                        "JP old", [{"name": "JP old", "eligible": False, "reason": WAIT_REASON}])
                    self.assertIn("可选节点：0 / 1", self.app.health.set.call_args.args[0])
                    self.assertIn("最近测速：—", self.app.timings.set.call_args.args[0])
                    old_status = dashboard.read_json(status_path)
                    self.assertEqual(old_status["nodes"], old_rows)
                    self.assertEqual(old_status["nodes_probe_identity"], old_identity)

                    run.release.set()
                    run.worker.join(3)
                    self.assertFalse(run.worker.is_alive())
                    fixture.helper.scan(forced=True)
                    new_status = dashboard.read_json(status_path)
                    self.assertNotEqual(new_status["nodes_probe_identity"], old_identity)
                    self.assertEqual(new_status["nodes_probe_identity"], optimizer.probe_identity(saved))
                    self.assertIsNotNone(new_status["last_scan_at"])
                    self.app.refresh()
                    self.app.render_nodes.assert_called_with("JP old", new_status["nodes"])
                    current = next(row for row in new_status["nodes"] if row["name"] == "JP old")
                    self.assertEqual(current["median_ms"], 100)
                    self.assertTrue(current["eligible"])
                    self.assertNotEqual(current.get("reason"), WAIT_REASON)
                    self.assertIn("最近测速：" + dashboard.display_time(new_status["last_scan_at"]),
                                  self.app.timings.set.call_args.args[0])
                    self.assertEqual(fixture.api.select_calls, [])

    def test_same_probe_normalized_statuses_and_scope_preferences_keep_scores(self):
        changes = ({}, {"probe_expected_statuses": [405, 405]},
                   {"scan_mode": "nodes", "scan_nodes": ["JP another"]},
                   {"scan_regions": ["TW", "SG"], "prefer_japan": False})
        for fields in changes:
            with self.subTest(fields=fields):
                config = self.config | fields
                self.assertEqual(optimizer.probe_identity(optimizer.validate_config(config)),
                                 self.status["nodes_probe_identity"])
                self.refresh(self.status, config)
                self.app.render_nodes.assert_called_with("JP last-known", self.nodes)
                self.assertIn("可选节点：1 / 1", self.app.health.set.call_args.args[0])
                self.assertIn("最近测速：" + dashboard.display_time(self.status["last_scan_at"]),
                              self.app.timings.set.call_args.args[0])

    def test_missing_or_old_probe_tag_does_not_discard_controller_or_manual_ack(self):
        for legacy in (True, False):
            with self.subTest(legacy=legacy):
                status = self.status | {"manual_selection_result": {
                    "request_id": "request", "name": "JP last-known", "ok": True}}
                config = dict(self.config)
                if legacy:
                    status.pop("nodes_probe_identity")
                else:
                    config["probe_url"] = "https://fixture.invalid/new-health"
                self.app.manual_busy = True
                self.app.manual_request_id = "request"
                self.app.manual_request_name = "JP last-known"
                self.app.manual_attempt = object()
                self.app.manual_request_started = dashboard.time.monotonic()
                self.refresh(status, config)
                self.app.render_nodes.assert_called_with(
                    "JP last-known", [{"name": "JP last-known", "eligible": False, "reason": WAIT_REASON}])
                self.assertIn("最近测速：—", self.app.timings.set.call_args.args[0])
                self.assertIn("可选节点：0 / 1", self.app.health.set.call_args.args[0])
                self.assertIn("控制接口：已连接", self.app.health.set.call_args.args[0])
                self.assertIn("当前节点：JP last-known", self.app.current.set.call_args.args[0])
                self.assertEqual(self.app.node_context, optimizer.state_context(config))
                self.assertEqual(self.app.node_mode, ("rule", config["group"]))
                self.assertFalse(self.app.manual_busy)
                self.assertIsNone(self.app.manual_request_id)
                self.assertIn("已手动切换", self.app.notify.call_args.args[0])
                self.assertEqual(status["nodes"], self.nodes)


if __name__ == "__main__":
    unittest.main()
