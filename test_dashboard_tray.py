"""Headless tray integration tests: no Tk window, Win32 tray, or helper starts."""

import queue
import subprocess
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, call, patch

import dashboard


class FakeTray:
    def __init__(self, title, instance_key, on_action, available=True):
        self.title = title
        self.instance_key = instance_key
        self.on_action = on_action
        self.available = available
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1
        return self.available

    def stop(self):
        self.stop_calls += 1
        self.available = False

    def emit(self, action):
        self.on_action(action)


class DeferredThread:
    """Let a test execute the worker after verifying the UI is still responsive."""
    def __init__(self, *, target, name=None, daemon=None):
        self.target = target
        self.name = name
        self.daemon = daemon
        self.started = False

    def start(self):
        self.started = True


class DashboardTrayTests(unittest.TestCase):
    def setUp(self):
        # __new__ bypasses Tk widget construction and all automatic startup.
        self.app = dashboard.Dashboard.__new__(dashboard.Dashboard)
        self.app.root = MagicMock(name="tk_root")
        self.app.root.after.return_value = "next-tray-poll"
        self.app.closed = False
        self.app.restore_busy = False
        self.app.exiting = False
        self.app.hidden_to_tray = False
        self.app.settings_dialog = None
        self.app.after_id = "refresh-poll"
        self.app.tray_after_id = "tray-poll"
        self.app.service_after_id = "service-start"
        self.app.started_helpers = []
        self.app.results = queue.SimpleQueue()
        self.app.tray_actions = queue.SimpleQueue()
        self.app.message = MagicMock(name="status_message")
        self.app.notice_until = 0
        for name in ("enable_button", "pause_button", "scan_button", "restore_button", "settings_button", "exit_button"):
            setattr(self.app, name, MagicMock(name=name))
        self.app.tray = FakeTray("NearRoute test", "offline-instance", self.app.tray_actions.put)
        self.threads = []

        def make_thread(**kwargs):
            thread = DeferredThread(**kwargs)
            self.threads.append(thread)
            return thread

        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.thread_factory = self.patches.enter_context(patch.object(dashboard.threading, "Thread", side_effect=make_thread))
        self.run = self.patches.enter_context(patch.object(
            dashboard.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
        ))
        self.patches.enter_context(patch.object(dashboard.subprocess, "Popen", side_effect=AssertionError("real helper forbidden")))
        self.patches.enter_context(patch.object(dashboard, "start_daemon", side_effect=AssertionError("real helper forbidden")))
        self.patches.enter_context(patch.object(dashboard, "python_executable", return_value="mock-python"))
        self.patches.enter_context(patch.object(dashboard, "update_enabled", side_effect=AssertionError("real config mutation forbidden")))
        self.patches.enter_context(patch.object(dashboard.messagebox, "showerror", side_effect=AssertionError("real dialog forbidden")))

    def settings(self):
        dialog = MagicMock(name="settings_dialog")
        dialog.closed = False
        self.app.settings_dialog = dialog
        return dialog

    def finish_worker(self):
        self.assertEqual(len(self.threads), 1)
        self.assertTrue(self.threads[0].started)
        self.threads[0].target()

    def test_close_button_with_available_tray_hides_without_stopping_helper(self):
        self.app.hide_to_tray()
        self.app.root.withdraw.assert_called_once()
        self.app.root.iconify.assert_not_called()
        self.app.root.destroy.assert_not_called()
        self.assertTrue(self.app.hidden_to_tray)
        self.assertFalse(self.app.closed)
        self.assertEqual(self.app.tray.stop_calls, 0)
        self.run.assert_not_called()

    def test_unavailable_tray_minimizes_instead_of_permanently_hiding(self):
        self.app.tray.available = False
        self.app.hide_to_tray()
        self.app.root.withdraw.assert_not_called()
        self.app.root.iconify.assert_called_once()
        self.app.root.destroy.assert_not_called()
        self.assertFalse(self.app.hidden_to_tray)
        self.app.message.set.assert_called()

    def test_missing_tray_also_minimizes_to_taskbar(self):
        self.app.tray = None
        self.app.hide_to_tray()
        self.app.root.iconify.assert_called_once()
        self.app.root.withdraw.assert_not_called()

    def test_withdraw_failure_restores_window_then_falls_back_to_taskbar(self):
        self.app.root.withdraw.side_effect = dashboard.tk.TclError("mock withdraw failure")
        self.app.hide_to_tray()
        self.app.root.deiconify.assert_called_once()
        self.app.root.iconify.assert_called_once()
        self.assertFalse(self.app.hidden_to_tray)

    def test_hiding_settings_releases_grab_and_hides_both_windows(self):
        dialog = self.settings()
        self.app.hide_to_tray()
        dialog.window.grab_release.assert_called_once()
        dialog.window.withdraw.assert_called_once()
        self.app.root.withdraw.assert_called_once()

    def test_show_restores_main_window_and_existing_settings_modal(self):
        dialog = self.settings()
        self.app.hidden_to_tray = True
        self.app.show_window()
        self.app.root.deiconify.assert_called_once()
        self.app.root.lift.assert_called_once()
        self.app.root.focus_force.assert_called_once()
        dialog.window.deiconify.assert_called_once()
        dialog.window.lift.assert_called_once()
        dialog.window.grab_set.assert_called_once()
        self.assertFalse(self.app.hidden_to_tray)

    def test_tray_thread_callback_only_queues_until_main_thread_polls(self):
        self.app.hidden_to_tray = True
        self.app.tray.emit("open")
        self.app.root.deiconify.assert_not_called()
        self.app.poll_tray()
        self.app.root.deiconify.assert_called_once()
        self.assertFalse(self.app.hidden_to_tray)
        self.app.root.after.assert_called_once_with(200, self.app.poll_tray)

    def test_icon_loss_while_hidden_restores_accessible_gui(self):
        self.app.hidden_to_tray = True
        self.app.tray.available = False
        self.app.tray.emit("tray_unavailable")
        self.app.poll_tray()
        self.app.root.deiconify.assert_called_once()
        self.assertFalse(self.app.hidden_to_tray)
        self.app.message.set.assert_called()
        self.app.root.destroy.assert_not_called()

    def test_exit_is_async_and_gui_closes_only_after_success_is_polled(self):
        self.app.request_exit()
        self.assertTrue(self.app.exiting)
        self.run.assert_not_called()
        self.app.root.destroy.assert_not_called()
        self.finish_worker()
        self.app.root.destroy.assert_not_called()
        self.assertEqual(self.run.call_args.args[0][-1], "stop")
        self.assertEqual(self.run.call_args.kwargs["timeout"], 35)
        self.assertFalse(self.run.call_args.kwargs["check"])
        self.app.poll_tray()
        self.assertTrue(self.app.closed)
        self.app.root.destroy.assert_called_once()
        self.assertEqual(self.app.tray.stop_calls, 1)
        self.app.root.after.assert_not_called()

    def test_nonzero_stop_result_keeps_gui_open_and_allows_retry(self):
        self.run.return_value = subprocess.CompletedProcess([], 1, stderr="mock private output")
        self.app.request_exit()
        self.finish_worker()
        self.app.poll_tray()
        self.assertFalse(self.app.closed)
        self.assertFalse(self.app.exiting)
        self.app.root.destroy.assert_not_called()
        self.app.root.deiconify.assert_called()
        self.assertNotIn("mock private output", self.app.message.set.call_args.args[0])
        self.app.request_exit()
        self.assertEqual(len(self.threads), 2)

    def test_stop_timeout_keeps_gui_open(self):
        self.run.side_effect = subprocess.TimeoutExpired(["mock-python", "optimizer.py", "stop"], 35)
        self.app.request_exit()
        self.finish_worker()
        self.app.poll_tray()
        self.assertFalse(self.app.exiting)
        self.assertFalse(self.app.closed)
        self.app.root.destroy.assert_not_called()

    def test_missing_python_keeps_gui_open(self):
        with patch.object(dashboard, "python_executable", side_effect=FileNotFoundError("mock Python unavailable")):
            self.app.request_exit()
            self.finish_worker()
        self.app.poll_tray()
        self.assertFalse(self.app.exiting)
        self.assertFalse(self.app.closed)
        self.run.assert_not_called()

    def test_exit_during_restore_does_not_launch_stop_or_destroy_gui(self):
        self.app.restore_busy = True
        self.app.request_exit()
        self.assertFalse(self.app.exiting)
        self.assertEqual(self.threads, [])
        self.run.assert_not_called()
        self.app.root.destroy.assert_not_called()
        self.app.root.deiconify.assert_called_once()
        self.app.message.set.assert_called()

    def test_duplicate_exit_requests_launch_only_one_worker(self):
        self.app.request_exit()
        self.app.request_exit()
        self.assertEqual(len(self.threads), 1)
        self.finish_worker()
        self.assertEqual(self.run.call_count, 1)

    def test_controls_cannot_restart_or_change_helper_during_exit(self):
        self.app.request_exit()
        self.app.enable()
        self.app.pause()
        self.app.scan()
        self.app.restore()
        self.assertFalse(self.app.ensure_service())
        self.assertEqual(len(self.threads), 1)

    def test_restore_busy_blocks_a_late_automatic_helper_start(self):
        self.app.restore_busy = True
        with patch.object(dashboard, "start_daemon") as start:
            self.assertFalse(self.app.ensure_service())
        start.assert_not_called()
        self.assertEqual(self.app.started_helpers, [])

    def test_successful_helper_start_is_tracked_for_shutdown_barrier(self):
        child = MagicMock(pid=23456)
        with patch.object(dashboard, "start_daemon", return_value=child) as start:
            self.assertTrue(self.app.ensure_service())
        start.assert_called_once()
        self.assertEqual(self.app.started_helpers, [child])

    def test_exit_cancels_pending_automatic_service_start(self):
        self.app.request_exit()
        self.app.root.after_cancel.assert_called_once_with("service-start")
        self.assertIsNone(self.app.service_after_id)

    def test_restore_cancels_pending_automatic_service_start(self):
        with patch.object(dashboard, "update_enabled"):
            self.app.restore()
        self.app.root.after_cancel.assert_called_once_with("service-start")
        self.assertIsNone(self.app.service_after_id)
        self.assertTrue(self.app.restore_busy)
        self.run.assert_not_called()

    def test_startup_barrier_timeout_keeps_gui_open_without_sending_stop(self):
        child = MagicMock(pid=23456)
        self.app.started_helpers = [child]
        with patch.object(dashboard, "wait_for_helper_startup", return_value=False) as wait:
            self.app.request_exit()
            self.finish_worker()
        wait.assert_called_once_with([child])
        self.run.assert_not_called()
        self.app.poll_tray()
        self.assertFalse(self.app.exiting)
        self.assertFalse(self.app.closed)
        self.app.root.destroy.assert_not_called()

    def test_stop_runs_only_after_tracked_helper_startup_barrier(self):
        child = MagicMock(pid=23456)
        self.app.started_helpers = [child]
        order = []
        def ready(processes):
            self.assertEqual(processes, [child])
            order.append("startup-ready")
            return True
        def stopped(*args, **kwargs):
            order.append("stop")
            return subprocess.CompletedProcess([], 0)
        self.run.side_effect = stopped
        with patch.object(dashboard, "wait_for_helper_startup", side_effect=ready):
            self.app.request_exit()
            self.finish_worker()
        self.assertEqual(order, ["startup-ready", "stop"])
        self.app.root.destroy.assert_not_called()
        self.app.poll_tray()
        self.assertTrue(self.app.closed)

    def test_close_cancels_all_callbacks_stops_tray_and_is_idempotent(self):
        dialog = self.settings()
        self.app.close()
        self.app.close()
        self.assertTrue(self.app.closed)
        self.assertEqual(
            self.app.root.after_cancel.call_args_list,
            [call("refresh-poll"), call("tray-poll"), call("service-start")],
        )
        dialog.close.assert_called_once()
        self.assertEqual(self.app.tray.stop_calls, 1)
        self.app.root.destroy.assert_called_once()
        self.run.assert_not_called()

    def test_cancelled_or_expired_after_ids_do_not_prevent_cleanup(self):
        self.app.root.after_cancel.side_effect = dashboard.tk.TclError("mock callback already removed")
        self.app.close()
        self.assertEqual(self.app.root.after_cancel.call_count, 3)
        self.assertEqual(self.app.tray.stop_calls, 1)
        self.app.root.destroy.assert_called_once()

    def test_late_worker_result_after_close_does_not_touch_destroyed_gui(self):
        self.app.request_exit()
        self.app.close()
        self.app.root.reset_mock()
        self.finish_worker()
        self.app.poll_tray()
        self.app.show_window()
        self.app.hide_to_tray()
        self.assertEqual(self.app.root.mock_calls, [])

    def test_thread_start_failure_releases_exit_busy_state(self):
        self.thread_factory.side_effect = None
        self.thread_factory.return_value.start.side_effect = RuntimeError("mock thread resource exhaustion")
        self.app.request_exit()
        self.assertFalse(self.app.exiting)
        self.app.root.destroy.assert_not_called()
        self.app.message.set.assert_called()

    def test_destroyed_settings_dialog_cannot_prevent_tray_cleanup(self):
        dialog = self.settings()
        dialog.close.side_effect = dashboard.tk.TclError("mock dialog already destroyed")
        self.app.close()
        self.assertTrue(self.app.closed)
        self.assertEqual(self.app.tray.stop_calls, 1)
        self.app.root.destroy.assert_called_once()


if __name__ == "__main__":
    unittest.main()
