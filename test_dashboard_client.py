"""Client connection UI checks with Tcl variables and no windows or real controller."""

from contextlib import ExitStack
from datetime import datetime
import json
from pathlib import Path
import queue
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dashboard


class ClientDialogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nearroute-client-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        self.owner = SimpleNamespace(root=Mock(), exiting=False, restore_busy=False, manual_busy=False,
                                     client_busy=False, client_dialog=None, client_saved=Mock(), cancel_service_start=Mock(), started_helpers=[])
        self.interpreter = tk.Tcl()
        string_var = tk.StringVar
        self.workers = []
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(dashboard, "CONFIG", self.config))
        self.patches.enter_context(patch.object(dashboard.tk, "Toplevel"))
        self.patches.enter_context(patch.object(dashboard.tk, "StringVar", side_effect=lambda **kwargs: string_var(master=self.interpreter, **kwargs)))
        self.patches.enter_context(patch.object(dashboard, "ttk"))
        self.patches.enter_context(patch.object(dashboard.threading, "Thread", side_effect=self.thread))
        self.inspection = {"mode": "rule", "groups": ["PROXY", "GLOBAL"], "active_group": "PROXY", "proxy_port": 7890}
        self.inspect = self.patches.enter_context(patch.object(dashboard.core, "inspect_controller", return_value=self.inspection))
        self.configure = self.patches.enter_context(patch.object(dashboard.core, "configure_client"))
        self.wait_for_startup = self.patches.enter_context(patch.object(dashboard, "wait_for_helper_startup", return_value=True))
        self.start_daemon = self.patches.enter_context(patch.object(dashboard, "start_daemon", side_effect=AssertionError("real helper forbidden")))
        self.dialog = dashboard.ClientDialog(self.owner, visible=False)
        self.owner.client_dialog = self.dialog
        self.addCleanup(self.close_dialog)

    def thread(self, **kwargs):
        self.workers.append(kwargs["target"])
        return Mock()

    def close_dialog(self):
        self.dialog.applying = False
        if not self.dialog.closed:
            self.dialog.close()

    def finish(self):
        self.workers.pop(0)()
        self.dialog.poll_results()

    def test_inspect_is_async_and_only_populates_readonly_groups(self):
        self.dialog.controller.set("http://127.0.0.1:9090/")
        self.dialog.secret_env.set("TEST_CLASH_SECRET")
        self.dialog.inspect_connection()
        self.assertTrue(self.dialog.loading)
        self.inspect.assert_not_called()
        before = self.dialog.message.get()
        self.workers.pop(0)()
        self.assertEqual(self.dialog.message.get(), before)  # Worker never touches Tcl.
        self.dialog.poll_results()
        self.inspect.assert_called_once_with({"controller": "http://127.0.0.1:9090", "controller_secret_env": "TEST_CLASH_SECRET", "group": dashboard.core.DEFAULTS["group"]})
        self.assertFalse(self.dialog.loading)
        self.assertEqual(self.dialog.selector_groups, ["PROXY", "GLOBAL"])
        self.assertEqual(self.dialog.group.get(), "PROXY")
        self.assertIn("7890", self.dialog.message.get())
        self.configure.assert_not_called()
        self.start_daemon.assert_not_called()
        self.inspection = self.inspection | {"groups": ["GLOBAL", "PROXY"], "active_group": None}
        self.inspect.return_value = self.inspection
        self.dialog.group.set("old unavailable group")
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.group.get(), "")
        self.dialog.save()
        self.configure.assert_not_called()
        self.assertEqual(self.workers, [])
        self.inspect.return_value = self.inspection | {"mode": "global", "active_group": "GLOBAL"}
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.group.get(), "GLOBAL")

    def test_identity_edit_discards_inflight_result_and_requires_new_inspection(self):
        self.dialog.inspect_connection()
        self.dialog.controller.set("http://127.0.0.1:9090")
        self.finish()
        self.assertFalse(self.dialog.loading)
        self.assertIsNone(self.dialog.verified_identity)
        self.assertEqual(self.dialog.selector_groups, [])
        self.assertIn("重新测试", self.dialog.message.get())
        self.dialog.save()
        self.configure.assert_not_called()
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.verified_identity, ("http://127.0.0.1:9090", ""))
        self.dialog.secret_env.set("OTHER_SECRET")
        self.assertIsNone(self.dialog.verified_identity)
        self.assertEqual(self.dialog.selector_groups, [])

    def test_save_requires_verified_group_then_passes_original_context_without_starting_service(self):
        self.dialog.save()
        self.assertEqual(self.workers, [])
        self.dialog.controller.set("http://127.0.0.1:9090")
        self.dialog.inspect_connection()
        self.finish()
        self.dialog.group.set("not a selector")
        self.dialog.save()
        self.configure.assert_not_called()
        self.assertEqual(self.workers, [])
        self.dialog.group.set("PROXY")
        saved = dashboard.core.DEFAULTS | self.dialog.fields() | {"enabled": False}
        self.configure.return_value = saved
        self.owner.started_helpers = [Mock(name="starting_helper")]
        original = self.dialog.original_context
        self.dialog.save()
        self.assertTrue(self.owner.client_busy)
        self.assertTrue(self.dialog.applying)
        self.owner.cancel_service_start.assert_called_once()
        self.configure.assert_not_called()
        self.dialog.close()
        self.assertFalse(self.dialog.closed)  # Applying cannot lose its completion callback.
        self.workers.pop(0)()
        self.owner.client_saved.assert_not_called()
        self.dialog.poll_results()
        self.configure.assert_called_once_with({"controller": "http://127.0.0.1:9090", "controller_secret_env": "", "group": "PROXY"}, expected_context=original)
        self.wait_for_startup.assert_called_once_with(self.owner.started_helpers)
        self.owner.client_saved.assert_called_once_with(saved)
        self.assertFalse(self.owner.client_busy)
        self.assertTrue(self.dialog.closed)
        self.assertIsNone(self.owner.client_dialog)
        self.start_daemon.assert_not_called()

    def test_failed_inspection_or_save_releases_busy_and_allows_retry(self):
        self.inspect.side_effect = dashboard.core.ControllerError("无法认证")
        self.dialog.inspect_connection()
        self.finish()
        self.assertFalse(self.dialog.loading)
        self.assertIsNone(self.dialog.verified_identity)
        self.assertIn("无法认证", self.dialog.message.get())
        self.inspect.side_effect = None
        self.dialog.inspect_connection()
        self.finish()
        for error in (ValueError("原连接已改变，请重新打开"), RuntimeError("后台尚未停止")):
            with self.subTest(error=type(error).__name__):
                self.configure.side_effect = error
                self.dialog.save()
                self.finish()
                self.assertFalse(self.owner.client_busy)
                self.assertFalse(self.dialog.applying)
                self.assertFalse(self.dialog.closed)
                self.assertIn("未保存", self.dialog.message.get())
                self.owner.client_saved.assert_not_called()
        self.configure.reset_mock()
        self.wait_for_startup.return_value = False
        self.dialog.save()
        self.finish()
        self.configure.assert_not_called()
        self.assertFalse(self.owner.client_busy)
        self.assertFalse(self.dialog.applying)
        self.assertFalse(self.dialog.closed)
        self.owner.client_saved.assert_not_called()
        self.dialog.close()
        self.assertTrue(self.dialog.closed)

    def test_closed_dialog_ignores_late_inspection_without_touching_window(self):
        self.dialog.inspect_connection()
        self.dialog.close()
        self.dialog.window.reset_mock()
        self.finish()
        self.assertTrue(self.dialog.closed)
        self.assertEqual(self.dialog.window.mock_calls, [])
        self.owner.client_saved.assert_not_called()


class ClientContextTests(unittest.TestCase):
    def setUp(self):
        self.context = dashboard.core.state_context(dashboard.core.DEFAULTS)
        self.app = dashboard.Dashboard.__new__(dashboard.Dashboard)
        self.app.root = Mock()
        self.app.closed = self.app.exiting = self.app.restore_busy = self.app.manual_busy = self.app.client_busy = False
        self.app.update_busy = None
        self.app.settings_dialog = self.app.client_dialog = None
        self.app.results = queue.SimpleQueue()
        self.app.manual_results = queue.SimpleQueue()
        self.app.node_context = self.context
        self.app.manual_attempt = self.app.manual_request_id = None
        self.app.manual_request_name = ""
        self.app.manual_request_started = self.app.started_at = self.app.notice_until = 0
        self.app.node_signature = "old nodes"
        for field in ("current", "health", "policy", "timings", "message", "badge", "badge_label", "enable_button", "pause_button", "scan_button", "settings_button", "restore_button", "exit_button", "client_button"):
            setattr(self.app, field, Mock())
        self.app.notify = Mock()
        self.app.render_nodes = Mock()
        self.app.poll_updates = Mock()
        self.app.poll_manual_selection = Mock()
        self.app.ensure_service = Mock(return_value=True)
        self.app.show_window = Mock()
        self.app.started_helpers = []

    def test_old_or_missing_status_context_clears_nodes_and_cannot_queue_old_selection(self):
        cfg = dict(zip(("controller", "controller_secret_env", "group"), self.context))
        old_status = {"controller_context": ["http://127.0.0.1:9090", "", "PROXY"],
                      "selected": "old node", "nodes": [{"name": "old node"}],
                      "phase": "monitoring", "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat()}
        with patch.object(dashboard, "read_json", side_effect=lambda path: old_status if path == dashboard.STATUS else cfg), \
             patch.object(dashboard.core, "request_manual_selection") as request:
            for status in (old_status, {key: value for key, value in old_status.items() if key != "controller_context"}):
                old_status = status
                self.app.refresh()
                self.assertIsNone(self.app.node_context)
                self.app.render_nodes.assert_called_with("—", [])
                self.app.poll_manual_selection.assert_called_with({})
                self.app.poll_updates.assert_called_with({})
                self.app.switch_node("old node")
            request.assert_not_called()
            self.app.ensure_service.assert_not_called()
            old_status = old_status | {"controller_context": list(self.context), "selected": "new node", "nodes": [{"name": "new node"}]}
            self.app.refresh()
            self.assertEqual(self.app.node_context, self.context)
            self.app.render_nodes.assert_called_with("new node", [{"name": "new node"}])

    def test_manual_worker_keeps_the_context_of_its_visible_node_list(self):
        work = []
        with patch.object(dashboard.threading, "Thread", side_effect=lambda **kwargs: work.append(kwargs["target"]) or Mock()), \
             patch.object(dashboard.core, "request_manual_selection", return_value="request") as request:
            self.app.switch_node("visible node")
            self.app.node_context = ("http://127.0.0.1:9090", "", "OTHER")
            work.pop()()
        request.assert_called_once_with("visible node", expected_context=self.context)

    def test_client_save_clears_old_nodes_and_leaves_background_stopped(self):
        cfg = dashboard.core.DEFAULTS | {"controller": "http://127.0.0.1:9090", "group": "PROXY", "enabled": False}
        with patch.object(dashboard, "start_daemon", side_effect=AssertionError("must stay stopped")):
            self.app.client_saved(cfg)
        self.assertIsNone(self.app.node_context)
        self.app.render_nodes.assert_called_once_with("—", [])
        self.app.ensure_service.assert_not_called()
        self.assertIn("立即测速", self.app.notify.call_args.args[0])

    def test_client_save_busy_blocks_node_actions_and_auto_install(self):
        self.app.client_busy = True
        with patch.object(dashboard, "update_enabled") as enabled, \
             patch.object(dashboard, "start_daemon") as daemon, \
             patch.object(dashboard.core, "request_manual_selection") as request:
            self.assertFalse(dashboard.Dashboard.ensure_service(self.app))
            self.app.enable()
            self.app.pause()
            self.app.scan()
            self.app.restore()
            self.app.request_exit()
            self.app.switch_node("old node")
            self.assertFalse(self.app.automatic_install_ready({"phase": "monitoring", "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat()}))
        enabled.assert_not_called()
        daemon.assert_not_called()
        request.assert_not_called()
        self.app.ensure_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
