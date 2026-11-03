"""Headless transport UI tests: no real Codex home, processes, or Tk windows."""

from contextlib import ExitStack
from datetime import datetime
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import dashboard
from tests.test_dashboard_tray import DeferredThread
from tests.test_dashboard_updates import RELEASE


def transport_state(**fields):
    return {"mode": "original", "status": "ready", "running": True,
            "engine_available": True, "pending": None, "explanation": "当前使用原始连接方式。",
            "affected_count": 0, "ok": True, "supported": True, "can_request": True,
            "can_restore": False, "compatible_http": False, **fields}


class Value:
    def __init__(self, value="", **_kwargs):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class CodexTransportUITests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.location = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="nearroute-transport-ui-")))
        self.stack.enter_context(patch.object(dashboard, "ROOT", self.location))
        self.stack.enter_context(patch.object(Path, "home", side_effect=AssertionError("real home forbidden")))
        self.stack.enter_context(patch.object(dashboard, "ttk"))
        dashboard.ttk.Button.side_effect = lambda *_args, **_kwargs: Mock()
        self.stack.enter_context(patch.object(dashboard.tk, "StringVar", side_effect=Value))
        self.stack.enter_context(patch.object(dashboard.tk, "BooleanVar", side_effect=Value))
        self.stack.enter_context(patch.object(dashboard.tk, "Menu"))
        self.stack.enter_context(patch.object(dashboard, "apply_theme"))
        self.inspect = self.stack.enter_context(patch.object(dashboard.codex_transport, "inspect", return_value=transport_state()))
        self.request = self.stack.enter_context(patch.object(dashboard.codex_transport, "request_mode"))
        self.apply = self.stack.enter_context(patch.object(dashboard.codex_transport, "process_pending"))
        self.cancel = self.stack.enter_context(patch.object(dashboard.codex_transport, "cancel_pending", return_value=transport_state()))
        self.threads, self.timers = [], {}
        self.root = Mock()
        self.root.after.side_effect = self.after
        self.root.after_cancel.side_effect = lambda timer: self.timers.pop(timer, None)
        self.stack.enter_context(patch.object(dashboard.threading, "Thread", side_effect=self.thread))
        self.stack.enter_context(patch.object(dashboard.subprocess, "run", side_effect=AssertionError("real process forbidden")))
        self.stack.enter_context(patch.object(dashboard.subprocess, "Popen", side_effect=AssertionError("real process forbidden")))
        self.app = dashboard.Dashboard.__new__(dashboard.Dashboard)
        self.app.root = self.root
        self.app.closed = self.app.exiting = self.app.restore_busy = self.app.manual_busy = self.app.client_busy = False
        self.app.update_busy = None
        self.app.initialize_codex_transport(enabled=True)
        self.app.codex_transport_state = transport_state()
        self.app.notify = Mock()
        self.app.show_window = Mock()
        self.app.tray = None

    def after(self, delay, callback):
        timer = object()
        self.timers[timer] = (delay, callback)
        return timer

    def thread(self, **kwargs):
        worker = DeferredThread(**kwargs)
        self.threads.append(worker)
        return worker

    def fire(self, callback):
        timer = next(key for key, value in self.timers.items() if value[1] == callback)
        delay, work = self.timers.pop(timer)
        work()
        return delay

    def finish(self):
        self.threads.pop(0).target()
        self.fire(self.app.poll_codex_transport)

    def panel(self):
        panel = dashboard.CodexTransportDialog(self.app, parent=Mock())
        self.app.codex_transport_dialog = panel
        return panel

    def construct_dashboard(self, *, start_service):
        with patch.object(dashboard.updater, "cached_release", return_value=None), \
             patch.object(dashboard.updater, "can_update", return_value=False), \
             patch.object(dashboard.updater, "load_preferences", return_value={"auto_check": False, "mode": "notify"}), \
             patch.object(dashboard.updater, "read_update_result", return_value={}), \
             patch.object(dashboard.Dashboard, "poll_tray"), \
             patch.object(dashboard.Dashboard, "refresh"):
            self.app = dashboard.Dashboard(self.root, start_service=start_service, enable_tray=False, enable_updates=False)

    def test_startup_recovers_persisted_pending_without_opening_tab_and_preserves_proxy_start(self):
        self.construct_dashboard(start_service=True)
        self.assertIsNone(self.app.codex_transport_dialog)
        self.assertTrue(any(delay == 100 and callback == self.app.begin_auto_connection
                            for delay, callback in self.timers.values()))
        self.assertEqual(self.fire(self.app.refresh_codex_transport), 500)
        self.inspect.assert_not_called()
        self.inspect.return_value = transport_state(pending="http", status="pending", running=False)
        self.finish()
        self.assertEqual(self.fire(self.app.apply_pending_codex_transport), 0)
        self.apply.return_value = transport_state(mode="http", status="applied", running=False, can_restore=True)
        self.finish()
        self.apply.assert_called_once_with(root=self.location)
        self.assertIsNone(self.app.codex_transport_after_id)
        self.assertIsNone(self.app.codex_transport_busy)
        self.assertIsNone(self.app.codex_transport_dialog)

    def test_offline_constructor_and_page_never_read_or_mutate_codex(self):
        self.construct_dashboard(start_service=False)
        self.app.ensure_page("codex")
        self.app.refresh_codex_transport()
        self.app.request_codex_transport("http")
        self.app.cancel_codex_transport()
        self.assertEqual(self.timers, {})
        self.assertEqual(self.threads, [])
        for api in (self.inspect, self.request, self.apply, self.cancel):
            api.assert_not_called()
        self.assertIn("离线", self.app.codex_transport_dialog.message.get())

    def test_reentering_codex_page_refreshes_last_check_without_concurrent_or_idle_workers(self):
        self.app.pages = {"overview": Mock(name="overview_page"), "codex": Mock(name="codex_page")}
        self.app.tabs = Mock()
        selected = [str(self.app.pages["overview"])]

        def select(page=None):
            if page is not None:
                selected[0] = str(page)
            return selected[0]

        self.app.tabs.select.side_effect = select
        self.app.active_tab = "overview"
        self.app.codex_transport_state = transport_state(mode="http", owned=True, running=False, can_restore=True)
        self.inspect.return_value = transport_state(mode="http", owned=True, running=True, can_restore=True)
        self.app.show_tab("codex")
        panel = self.app.codex_transport_dialog
        self.assertEqual(panel.running.get(), "上次检查：Codex 已退出。")
        self.app.tab_changed()  # A programmatic selection also emits the Notebook event.
        self.assertEqual(len(self.threads), 1)
        self.inspect.assert_not_called()
        self.finish()
        self.assertEqual(panel.running.get(), "上次检查：Codex 正在运行。")
        self.assertIn("已保存配置：HTTP/SSE", panel.mode.get())
        self.assertEqual(self.timers, {})
        self.app.show_tab("overview")
        self.inspect.return_value = transport_state(mode="http", owned=True, running=False, can_restore=True)
        self.app.show_tab("codex")
        self.app.tab_changed()
        self.assertIs(self.app.codex_transport_dialog, panel)
        self.assertEqual(len(self.threads), 1)
        self.finish()
        self.assertEqual(self.inspect.call_count, 2)
        self.assertEqual(panel.running.get(), "上次检查：Codex 已退出。")
        self.assertEqual(self.timers, {})
        for mutation in (self.request, self.apply, self.cancel):
            mutation.assert_not_called()

    def test_running_codex_request_queues_then_applies_after_normal_exit(self):
        panel = self.panel()
        self.request.return_value = transport_state(pending="http", status="pending", reopen_requested=True)
        self.app.request_codex_transport("http")
        self.assertEqual(self.app.codex_transport_busy, "request")
        self.request.assert_not_called()
        self.finish()
        self.request.assert_called_once_with("http", reopen=True, root=self.location)
        self.assertIn("当前尚未完成切换", panel.message.get())
        self.assertIn("自动备份、应用并重新打开", panel.message.get())
        self.assertIn("切换目标：HTTP/SSE", panel.message.get())
        self.assertIn("已保存配置：原始连接方式", panel.mode.get())
        self.assertIsNone(self.app.codex_transport_busy)
        self.assertFalse(self.app.exiting)
        self.inspect.return_value = transport_state(pending="http", status="pending")
        self.assertEqual(self.fire(self.app.apply_pending_codex_transport), 4000)
        self.assertEqual(self.app.codex_transport_busy, "inspect")
        self.assertFalse(self.app.codex_transport_applying())
        self.finish()
        self.apply.assert_not_called()
        self.inspect.return_value = transport_state(pending="http", status="pending", running=False)
        self.assertEqual(self.fire(self.app.apply_pending_codex_transport), 4000)
        self.finish()
        self.apply.return_value = transport_state(mode="http", status="applied", running=False, can_restore=True,
                                                  explanation="连接设置已保存，请重新打开 Codex。实际传输方式以日志为准。")
        self.assertEqual(self.fire(self.app.apply_pending_codex_transport), 0)
        self.finish()
        self.assertEqual(self.apply.call_count, 1)
        self.assertIn("已保存配置：HTTP/SSE", panel.mode.get())
        self.assertIn("重新打开", panel.message.get())
        self.assertIsNone(self.app.codex_transport_after_id)
        self.assertEqual(self.timers, {})

    def test_worker_publishes_plain_values_and_only_poll_updates_tk(self):
        panel = self.app.codex_transport_dialog = Mock()
        self.app.refresh_codex_transport()
        self.assertEqual(panel.refresh.call_count, 1)
        self.threads.pop().target()
        self.assertEqual(panel.refresh.call_count, 1)
        self.assertEqual(self.app.codex_transport_busy, "inspect")
        self.fire(self.app.poll_codex_transport)
        self.assertEqual(panel.refresh.call_count, 2)
        self.assertIsNone(self.app.codex_transport_busy)

    def test_configuration_and_waiting_are_not_displayed_as_verified_transport(self):
        panel = self.panel()
        self.assertIn("尚未启用", panel.mode.get())
        self.app.codex_transport_state = transport_state(affected_count=828, skipped_count=1032)
        panel.refresh()
        self.assertIn("待检查聊天：828", panel.scope.get())
        self.assertIn("1032", panel.scope.get())
        self.app.codex_transport_state = transport_state(mode="http", compatible_http=True)
        panel.refresh()
        self.assertIn("配置已启用", panel.mode.get())
        self.assertIn("实际模型请求的传输方式尚未在此验证", panel.message.get())
        self.app.codex_transport_state = transport_state(pending="http", status="pending", reopen_requested=True)
        panel.refresh()
        self.assertIn("等待 Codex 退出", panel.mode.get())
        self.assertIn("当前尚未完成切换", panel.message.get())
        panel.reopen_check.state.assert_called_with(["disabled"])

    def test_automatic_open_is_chosen_on_click_and_can_be_disabled(self):
        panel = self.panel()
        self.assertTrue(panel.reopen.get())
        self.request.assert_not_called()
        panel.reopen.set(False)
        self.request.return_value = transport_state(pending="http", status="pending", reopen_requested=False)
        self.app.request_codex_transport("http")
        self.finish()
        self.request.assert_called_once_with("http", reopen=False, root=self.location)
        self.assertIn("完成提示出现后重新打开", panel.message.get())

    def test_apply_completion_notifies_while_window_remains_in_tray(self):
        self.app.tray = Mock()
        self.app.codex_transport_state = transport_state(pending="http", status="pending", running=False)
        explanation = "配置已保存，已请求打开 Codex；实际传输待验证。"
        self.apply.return_value = transport_state(mode="http", status="applied", running=False, can_restore=True,
                                                  explanation=explanation, reopened=True)
        self.app.apply_pending_codex_transport()
        self.finish()
        self.app.notify.assert_called_once_with(explanation, 45)
        self.app.tray.show_notification.assert_called_once_with("Codex 连接配置完成", explanation)

    def test_cancelling_pending_releases_watcher_without_applying(self):
        self.app.codex_transport_state = transport_state(pending="http", status="pending")
        self.app.schedule_codex_transport_check()
        self.app.cancel_codex_transport()
        self.assertIsNone(self.app.codex_transport_after_id)
        self.finish()
        self.cancel.assert_called_once_with(root=self.location)
        self.apply.assert_not_called()
        self.assertIsNone(self.app.codex_transport_state["pending"])
        self.assertEqual(self.timers, {})

    def test_existing_http_has_no_rewrite_or_unowned_restore_action(self):
        self.app.codex_transport_state = transport_state(mode="http", compatible_http=True)
        panel = self.panel()
        panel.http_button.state.assert_called_with(["disabled"])
        panel.restore_button.state.assert_called_with(["disabled"])
        self.app.request_codex_transport("original")
        self.assertEqual(self.threads, [])
        self.request.assert_not_called()
        self.assertIn("现有配置", panel.mode.get())
        self.app.codex_transport_state = transport_state(mode="http", can_restore=True)
        panel.refresh()
        panel.restore_button.state.assert_called_with(["!disabled"])
        self.app.request_codex_transport("original")
        self.assertEqual(self.app.codex_transport_busy, "request")

    def test_pending_failure_is_visible_and_stops_automatic_retry(self):
        panel = self.panel()
        self.app.codex_transport_state = transport_state(pending="http", status="pending", running=False)
        self.apply.return_value = transport_state(pending="http", status="error", ok=False,
                                                  explanation="配置文件被修改，已保留原文件。请刷新后重试。")
        self.app.apply_pending_codex_transport()
        self.finish()
        self.assertIn("已保留原文件", panel.message.get())
        self.assertNotIn("已安排", panel.message.get())
        self.assertEqual(self.timers, {})
        panel.cancel_button.state.assert_called_with(["!disabled"])

    def test_partial_migration_failure_keeps_http_retry_available(self):
        self.app.codex_transport_state = transport_state(mode="http", compatible_http=True, owned=True,
                                                        pending="http", status="error", ok=False, can_restore=True,
                                                        explanation="部分聊天尚未迁移，可重试。")
        panel = self.panel()
        self.assertIn("已保存配置：HTTP/SSE", panel.mode.get())
        self.assertFalse(panel.reopen.get(), "旧的待执行请求没有授权自动打开，重试时应保留这一选择")
        panel.http_button.state.assert_called_with(["!disabled"])
        self.request.return_value = transport_state(mode="http", compatible_http=True, owned=True,
                                                    pending="http", status="pending", can_restore=True)
        self.app.request_codex_transport("http")
        self.finish()
        self.request.assert_called_once_with("http", reopen=False, root=self.location)
        self.assertEqual(self.app.codex_transport_state["status"], "pending")

    def test_engine_unavailable_disables_switches_and_explains_support(self):
        self.app.codex_transport_state = transport_state(status="unsupported", engine_available=False,
                                                        can_request=False, supported=False,
                                                        explanation="未找到支持的 Codex 引擎。")
        panel = self.panel()
        panel.http_button.state.assert_called_with(["disabled"])
        panel.restore_button.state.assert_called_with(["disabled"])
        self.assertIn("未找到", panel.message.get())

    def test_worker_error_releases_busy_and_does_not_start_idle_watcher(self):
        self.inspect.side_effect = OSError("无法读取配置")
        self.app.refresh_codex_transport()
        self.finish()
        self.assertEqual(self.app.codex_transport_state["status"], "error")
        self.assertIn("无法读取", self.app.codex_transport_state["explanation"])
        self.assertIsNone(self.app.codex_transport_busy)
        self.assertEqual(self.timers, {})

    def test_waiting_does_not_hold_exit_or_install_lock_but_application_does(self):
        self.app.active_tab = "overview"
        self.app.hidden_to_tray = False
        self.app.client_dialog = self.app.settings_dialog = self.app.update_dialog = None
        self.app.update_release = RELEASE
        self.app.update_can_install = True
        self.app.codex_transport_state = transport_state(pending="http", status="pending")
        status = {"phase": "monitoring", "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat()}
        self.assertTrue(self.app.automatic_install_ready(status))
        self.app.codex_transport_busy = "inspect"
        self.assertTrue(self.app.automatic_install_ready(status))
        self.app.codex_transport_busy = "apply"
        self.assertFalse(self.app.automatic_install_ready(status))
        self.app.request_exit()
        self.app.request_update_install()
        self.assertFalse(self.app.exiting)
        self.assertEqual(self.threads, [])
        self.assertIn("Codex 连接", self.app.notify.call_args.args[0])

    def test_pending_application_waits_for_client_setup_without_blocking_it(self):
        self.app.codex_transport_state = transport_state(pending="http", status="pending", running=False)
        self.app.client_busy = True
        self.app.apply_pending_codex_transport()
        self.assertEqual(self.threads, [])
        self.assertIsNone(self.app.codex_transport_busy)
        self.assertTrue(self.app.client_busy)
        self.app.client_busy = False
        self.apply.return_value = transport_state(mode="http", status="applied", running=False)
        self.fire(self.app.apply_pending_codex_transport)
        self.finish()
        self.apply.assert_called_once()

    def test_close_disposes_tab_cancels_timers_and_preserves_pending_intent(self):
        panel = self.panel()
        self.app.codex_transport_state = transport_state(pending="http", status="pending")
        self.app.schedule_codex_transport_check()
        self.app.after_id = self.app.tray_after_id = self.app.service_after_id = None
        self.app.tray = None
        self.app.close()
        self.assertTrue(panel.closed)
        self.assertEqual(self.timers, {})
        self.assertTrue(self.app.closed)
        self.assertEqual(self.app.codex_transport_state["pending"], "http")
        self.cancel.assert_not_called()
        self.root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
