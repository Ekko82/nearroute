"""Offline layout checks using only transparent, task-owned Tk windows."""

from contextlib import ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import tkinter as tk
import unittest
from unittest.mock import patch

import dashboard


class DashboardLayoutTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
            self.root.attributes("-alpha", 0)
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.location = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.stack.callback(lambda: self.app.close() if hasattr(self, "app") else self.root.destroy())
        (self.location / "VERSION").write_text("1.0.0", encoding="utf-8")
        self.config = dashboard.core.DEFAULTS | {"group": "NearRoute UI fixture"}
        self.nodes = [{
            "name": f"1.0x JP 日本 东京 - {index:02d}",
            "score_ms": 120 + index, "median_ms": 110 + index,
            "success_rate": 1.0, "jitter_ms": 14, "samples": 6,
            "eligible": True, "reason": "可用",
        } for index in range(40)]
        now = datetime.now(dashboard.CHINA_TIME)
        self.status = {
            "pid": 12345, "phase": "monitoring", "controller_available": True,
            "controller_context": list(dashboard.core.state_context(self.config)),
            "nodes_probe_identity": dashboard.core.probe_identity(self.config),
            "mode": "rule", "group": self.config["group"],
            "selected": self.nodes[0]["name"], "nodes": self.nodes,
            "updated_at": now.isoformat(), "last_scan_at": now.isoformat(),
            "next_scan_at": (now + timedelta(minutes=5)).isoformat(),
            "last_switch_at": (now - timedelta(minutes=10)).isoformat(),
            "network_errors_recent": 0,
            "message": "当前线路表现稳定，保持连接路径。",
        }
        for name, value in (("ROOT", self.location), ("STATE", self.location / "state"),
                            ("CONFIG", self.location / "config.json"),
                            ("STATUS", self.location / "state" / "status.json")):
            self.stack.enter_context(patch.object(dashboard, name, value))
        for name in ("start_daemon", "TrayIcon"):
            self.stack.enter_context(patch.object(dashboard, name, side_effect=AssertionError("offline UI fixture")))
        self.stack.enter_context(patch.object(dashboard.threading, "Thread", side_effect=AssertionError("offline UI fixture")))
        self.publish()
        self.app = dashboard.Dashboard(self.root, start_service=False, enable_tray=False, enable_updates=False)
        # The test window is transparent before it is mapped; no user app is inspected.
        self.root.deiconify()
        self.arrange(1120, 780)

    def publish(self):
        dashboard.STATE.mkdir(exist_ok=True)
        dashboard.CONFIG.write_text(json.dumps(self.config), encoding="utf-8")
        dashboard.STATUS.write_text(json.dumps(self.status), encoding="utf-8")

    def arrange(self, width, height):
        self.root.geometry(f"{width}x{height}+0+0")
        self.root.update()
        self.root.update_idletasks()

    def widgets(self, parent=None):
        for child in (self.root if parent is None else parent).winfo_children():
            yield child
            yield from self.widgets(child)

    def assert_inside_window(self, widget):
        self.assertTrue(widget.winfo_ismapped(), str(widget))
        x = widget.winfo_rootx() - self.root.winfo_rootx()
        y = widget.winfo_rooty() - self.root.winfo_rooty()
        self.assertGreaterEqual(x, 0, str(widget))
        self.assertGreaterEqual(y, 0, str(widget))
        self.assertLessEqual(x + widget.winfo_width(), self.root.winfo_width(), str(widget))
        self.assertLessEqual(y + widget.winfo_height(), self.root.winfo_height(), str(widget))

    def assert_layout(self, minimum_table_height):
        for name in ("enable_button", "pause_button", "scan_button", "restore_button", "settings_button",
                     "client_button", "update_button", "exit_button", "policy_button"):
            self.assert_inside_window(getattr(self.app, name))
        variables = {str(getattr(self.app, name)) for name in (
            "node_title", "node_meta", "connection_value", "node_count_value", "error_value",
            "scan_time_value", "switch_time_value", "next_scan_value", "update_time_value")}
        labels = [widget for widget in self.widgets() if widget.winfo_class() == "TLabel"
                  and str(widget.cget("textvariable")) in variables]
        self.assertEqual(len(labels), len(variables))
        for label in labels:
            self.assert_inside_window(label)
        self.assert_inside_window(self.app.table)
        self.assertGreaterEqual(self.app.table.winfo_height(), minimum_table_height)

    def test_main_actions_and_node_table_fit_default_and_minimum_sizes(self):
        row_height = int(dashboard.ttk.Style(self.root).lookup("Treeview", "rowheight"))
        for width, height, minimum_rows in ((1120, 780, 8), (920, 650, 4)):
            with self.subTest(size=(width, height)):
                self.arrange(width, height)
                actual_width, actual_height = self.root.winfo_width(), self.root.winfo_height()
                minimum_width, minimum_height = self.root.minsize()
                self.assertGreaterEqual(actual_width, minimum_width)
                self.assertGreaterEqual(actual_height, minimum_height)
                self.assertLessEqual(actual_width, width)
                self.assertLessEqual(actual_height, height)
                # The desktop can constrain a requested window on a smaller screen.
                # Check the same controls and usable table within its realized size.
                height_reduction = height - actual_height
                self.assert_layout(max(4 * row_height, minimum_rows * row_height - height_reduction))
        self.app.policy_button.invoke()
        self.root.update()
        self.assertTrue(self.app.policy_label.winfo_ismapped())
        self.assert_inside_window(self.app.policy_label)
        self.assert_layout(80)
        self.app.policy_button.invoke()
        self.root.update()
        self.assertFalse(self.app.policy_label.winfo_ismapped())

    def test_long_current_node_wraps_without_hiding_actions_or_footer(self):
        long_name = "1.0x JP 日本 东京 Premium 专线 - IPLC / 高峰稳定线路 / 支持流媒体与长期连接"
        self.status["selected"] = long_name
        self.nodes[0]["name"] = long_name
        self.publish()
        self.app.refresh()
        for width, height in ((1120, 780), (920, 650)):
            with self.subTest(size=(width, height)):
                self.arrange(width, height)
                self.assertEqual(self.app.node_title.get(), long_name)
                self.assert_layout(100)

    def test_scanning_refresh_keeps_rows_selection_and_scroll_position(self):
        self.arrange(920, 650)
        picked = self.nodes[20]["name"]
        self.app.table.selection_set(picked)
        self.app.table.focus(picked)
        self.app.table.yview_moveto(0.4)
        self.app.table.xview_moveto(0.2)
        self.root.update()
        old_y, old_x = self.app.table.yview()[0], self.app.table.xview()[0]
        self.status["phase"] = "scanning"
        self.status["message"] = "候选复测 1/2：10/18"
        self.nodes[20]["median_ms"] = 132
        self.publish()
        self.app.refresh()
        self.root.update()
        self.assertEqual(self.app.table.get_children(), tuple(node["name"] for node in self.nodes))
        self.assertEqual(self.app.table.selection(), (picked,))
        self.assertEqual(self.app.table.focus(), picked)
        self.assertEqual(self.app.table.item(picked, "values")[2], "132")
        self.assertAlmostEqual(self.app.table.yview()[0], old_y, places=2)
        self.assertAlmostEqual(self.app.table.xview()[0], old_x, places=2)
        self.assertIn("selected", self.app.table.item(self.status["selected"], "tags"))


if __name__ == "__main__":
    unittest.main()
