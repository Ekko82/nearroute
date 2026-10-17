"""Offline UI checks for manual selection and stable measurement rows."""

from contextlib import ExitStack
import queue
from types import SimpleNamespace
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

import dashboard


def dashboard_state():
    app = dashboard.Dashboard.__new__(dashboard.Dashboard)
    app.root = Mock()
    app.closed = app.exiting = app.restore_busy = app.manual_busy = False
    app.manual_results = queue.SimpleQueue()
    app.manual_request_id = app.manual_attempt = None
    app.manual_request_name = ""
    app.manual_request_started = 0
    app.node_signature = ""
    app.settings_dialog = app.tray = None
    app.after_id = app.tray_after_id = app.service_after_id = None
    app.window_properties_configured = False
    app.notify = Mock()
    app.ensure_service = Mock(return_value=True)
    app.table = Mock()
    return app


class ManualSelectionTests(unittest.TestCase):
    def setUp(self):
        self.app = dashboard_state()
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.request = self.patches.enter_context(patch.object(dashboard.core, "request_manual_selection", return_value="request-1"))
        self.cancel = self.patches.enter_context(patch.object(dashboard.core, "cancel_manual_selection", return_value=True))
        self.update_enabled = self.patches.enter_context(patch.object(dashboard, "update_enabled"))
        self.workers = []
        def thread(**kwargs):
            self.workers.append(kwargs["target"])
            return Mock()
        self.thread = self.patches.enter_context(patch.object(dashboard.threading, "Thread", side_effect=thread))

    def publish_id(self):
        self.workers[-1]()
        self.app.poll_manual_selection({})

    def acknowledgement(self, *, request_id="request-1", name="JP A", ok=True):
        return {"enabled": False, "manual_selection_result": {
            "request_id": request_id, "name": name, "ok": ok, "message": "mock result",
        }}

    def test_request_is_async_busy_before_id_and_does_not_toggle_pause(self):
        self.app.switch_node("JP A")
        self.assertTrue(self.app.manual_busy)
        self.assertIsNone(self.app.manual_request_id)
        self.app.ensure_service.assert_called_once()
        self.request.assert_not_called()
        self.app.notify.reset_mock()
        self.workers[0]()
        self.request.assert_called_once_with("JP A")
        self.app.notify.assert_not_called()  # worker only posts to its queue
        self.app.poll_manual_selection(self.acknowledgement())
        self.assertFalse(self.app.manual_busy)
        self.assertIn("JP A", self.app.notify.call_args.args[0])
        self.update_enabled.assert_not_called()

    def test_duplicate_click_is_ignored_while_request_id_is_pending(self):
        self.app.switch_node("JP A")
        self.app.switch_node("JP B")
        self.assertEqual(len(self.workers), 1)
        self.app.ensure_service.assert_called_once()
        self.publish_id()
        self.request.assert_called_once_with("JP A")

    def test_auto_ineligible_node_can_be_selected_manually(self):
        self.app.table.selection.return_value = ("node not auto eligible",)
        self.assertEqual(self.app.switch_selected_node(), "break")
        self.publish_id()
        self.request.assert_called_once_with("node not auto eligible")

    def test_double_click_uses_row_under_pointer_and_heading_click_does_nothing(self):
        self.app.table.identify_row.return_value = "JP A"
        self.app.switch_clicked_node(SimpleNamespace(y=25))
        self.app.table.selection_set.assert_called_once_with("JP A")
        self.publish_id()
        self.request.assert_called_once_with("JP A")
        self.app.table.identify_row.return_value = ""
        self.app.switch_clicked_node(SimpleNamespace(y=0))
        self.assertEqual(len(self.workers), 1)

    def test_right_click_targets_that_row_and_releases_menu_grab(self):
        self.app.node_menu = Mock()
        self.app.table.identify_row.return_value = "JP B"
        self.app.show_node_menu(SimpleNamespace(y=30, x_root=100, y_root=200))
        self.app.table.selection_set.assert_called_once_with("JP B")
        self.app.node_menu.entryconfigure.assert_called_once_with(0, state="normal")
        self.app.node_menu.tk_popup.assert_called_once_with(100, 200)
        self.app.node_menu.grab_release.assert_called_once()
        self.assertEqual(self.workers, [])

    def test_exiting_and_restore_block_manual_requests(self):
        for flag in ("exiting", "restore_busy", "closed"):
            setattr(self.app, flag, True)
            self.app.switch_node("JP A")
            setattr(self.app, flag, False)
        self.assertEqual(self.workers, [])
        self.app.ensure_service.assert_not_called()

    def test_failed_helper_start_does_not_queue_and_allows_retry(self):
        self.app.ensure_service.return_value = False
        self.app.switch_node("JP A")
        self.assertFalse(self.app.manual_busy)
        self.assertEqual(self.workers, [])
        self.request.assert_not_called()

    def test_write_failure_clears_busy_and_can_be_retried(self):
        self.request.side_effect = OSError("mock write failure")
        self.app.switch_node("JP A")
        self.publish_id()
        self.assertFalse(self.app.manual_busy)
        self.assertIn("可重试", self.app.notify.call_args.args[0])
        self.request.side_effect = None
        self.app.switch_node("JP B")
        self.publish_id()
        self.assertTrue(self.app.manual_busy)

    def test_old_result_does_not_finish_current_request(self):
        self.app.switch_node("JP A")
        self.publish_id()
        self.app.poll_manual_selection(self.acknowledgement(request_id="older"))
        self.assertTrue(self.app.manual_busy)
        self.assertEqual(self.app.manual_request_id, "request-1")

    def test_backend_failure_is_shown_and_can_be_retried(self):
        self.app.switch_node("JP A")
        self.publish_id()
        self.app.poll_manual_selection(self.acknowledgement(ok=False))
        self.assertFalse(self.app.manual_busy)
        self.assertIn("失败", self.app.notify.call_args.args[0])
        self.app.switch_node("JP A")
        self.assertEqual(len(self.workers), 2)

    def test_success_must_confirm_requested_node_name(self):
        self.app.switch_node("JP A")
        self.publish_id()
        self.app.poll_manual_selection(self.acknowledgement(name="JP B"))
        self.assertFalse(self.app.manual_busy)
        self.assertIn("不一致", self.app.notify.call_args.args[0])

    def test_timeout_cancels_only_own_request_and_releases_busy(self):
        self.app.switch_node("JP A")
        self.publish_id()
        with patch.object(dashboard.time, "monotonic", return_value=self.app.manual_request_started + 30):
            self.app.poll_manual_selection({})
        self.cancel.assert_called_once_with("request-1")
        self.assertFalse(self.app.manual_busy)
        self.assertIn("超时", self.app.notify.call_args.args[0])

    def test_late_id_after_timeout_cannot_replace_a_new_attempt(self):
        self.app.switch_node("JP A")
        with patch.object(dashboard.time, "monotonic", return_value=self.app.manual_request_started + 31):
            self.app.poll_manual_selection({})
        self.app.switch_node("JP B")
        self.request.return_value = "late-old-request"
        self.workers[0]()
        self.app.poll_manual_selection({})
        self.cancel.assert_called_once_with("late-old-request")
        self.assertTrue(self.app.manual_busy)
        self.assertEqual(self.app.manual_request_name, "JP B")
        self.assertIsNone(self.app.manual_request_id)
        self.request.return_value = "new-request"
        self.publish_id()
        self.assertEqual(self.app.manual_request_id, "new-request")

    def test_manual_busy_blocks_restore_and_exit(self):
        self.app.switch_node("JP A")
        self.app.show_window = Mock()
        self.app.request_exit()
        self.app.restore()
        self.assertFalse(self.app.exiting)
        self.app.root.destroy.assert_not_called()
        self.update_enabled.assert_not_called()
        self.assertEqual(len(self.workers), 1)


class NodeTableTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root = tk.Tk()
            self.root.withdraw()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Tk display unavailable: {error}")
        self.app = dashboard_state()
        self.app.root = self.root
        self.app.table = dashboard.ttk.Treeview(self.root, columns=("name", "score", "median", "success", "jitter", "samples", "eligible"), show="headings")
        self.app.table.place(x=0, y=0, width=500, height=180)
        self.addCleanup(self.root.destroy)
        self.nodes = [{"name": f"JP {i:02d}", "score_ms": i + 100, "median_ms": i + 90, "eligible": True, "samples": 3} for i in range(40)]

    def test_live_measurements_keep_selection_focus_and_both_scroll_positions(self):
        self.app.render_nodes("JP 00", self.nodes)
        self.root.update_idletasks()
        self.app.table.selection_set("JP 20")
        self.app.table.focus("JP 20")
        self.app.table.yview_moveto(0.4)
        self.app.table.xview_moveto(0.25)
        before_y, before_x = self.app.table.yview()[0], self.app.table.xview()[0]
        updated = [dict(row, median_ms=25) for row in reversed(self.nodes)]
        self.app.render_nodes("JP 00", updated)
        self.root.update_idletasks()
        self.assertEqual(self.app.table.selection(), ("JP 20",))
        self.assertEqual(self.app.table.focus(), "JP 20")
        self.assertEqual(self.app.table.item("JP 20", "values")[2], "25")
        self.assertAlmostEqual(self.app.table.yview()[0], before_y, places=2)
        self.assertAlmostEqual(self.app.table.xview()[0], before_x, places=2)

    def test_current_node_outside_measurement_scope_is_still_visible(self):
        self.app.render_nodes("US current", self.nodes)
        self.assertIn("US current", self.app.table.get_children())
        self.assertIn("selected", self.app.table.item("US current", "tags"))
        values = self.app.table.item("US current", "values")
        self.assertIn("当前使用", values[-1])
        self.assertEqual(values[1], "—")
        self.assertEqual(len(self.app.table.get_children()), 41)


class TaskbarWiringTests(unittest.TestCase):
    def test_taskbar_configuration_precedes_tk_and_window_configuration(self):
        order = []
        root = Mock()
        app = Mock()
        with patch.object(dashboard, "WindowInstance") as instance, \
             patch.object(dashboard, "configure_taskbar", side_effect=lambda: order.append("taskbar")), \
             patch.object(dashboard.tk, "Tk", side_effect=lambda: order.append("tk") or root), \
             patch.object(dashboard, "Dashboard", side_effect=lambda value: order.append("dashboard") or app), \
             patch.object(dashboard, "configure_window", side_effect=lambda value: order.append("window")):
            dashboard.main()
        self.assertEqual(order, ["taskbar", "tk", "dashboard", "window"])
        self.assertTrue(app.window_properties_configured)
        app.close.assert_called_once()
        instance.return_value.close.assert_called_once()

    def test_dashboard_constructor_failure_destroys_root_and_releases_instance(self):
        root = Mock()
        with patch.object(dashboard, "WindowInstance") as instance, \
             patch.object(dashboard, "configure_taskbar"), \
             patch.object(dashboard.tk, "Tk", return_value=root), \
             patch.object(dashboard, "Dashboard", side_effect=RuntimeError("mock UI init failure")):
            with self.assertRaises(RuntimeError):
                dashboard.main()
        root.destroy.assert_called_once()
        instance.return_value.close.assert_called_once()

    def test_window_properties_are_cleared_before_destroy(self):
        app = dashboard_state()
        app.window_properties_configured = True
        order = []
        app.root.destroy.side_effect = lambda: order.append("destroy")
        with patch.object(dashboard, "clear_window", side_effect=lambda root: order.append("clear")):
            app.close()
        self.assertEqual(order, ["clear", "destroy"])


if __name__ == "__main__":
    unittest.main()
