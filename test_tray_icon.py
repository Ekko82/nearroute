"""Tray unit tests; real Explorer integration is explicitly opt-in.

Windows smoke: set NEARROUTE_TRAY_SMOKE=1 for this test process.
Only uniquely named temporary test icons are created and removed.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
import queue
import threading
import unittest
from unittest.mock import Mock, patch
import uuid

import tray_icon as tray


class TrayUnitTests(unittest.TestCase):
    def test_instance_key_produces_stable_separate_window_identity(self):
        self.assertEqual(tray._window_class("same"), tray._window_class("same"))
        self.assertNotEqual(tray._window_class("one"), tray._window_class("two"))
        self.assertNotIn("secret/path", tray._window_class("secret/path"))

    def test_tooltip_limit_is_utf16_units_and_has_no_null(self):
        value = tray._tooltip("船" * 30 + "🌊" * 120 + "\0extra")
        self.assertLessEqual(len(value.encode("utf-16-le")), 254)
        self.assertNotIn("\0", value)
        self.assertEqual(tray._tooltip("Near\0Route"), "Near Route")

    def test_version4_and_legacy_events_decode_correctly(self):
        self.assertEqual(tray._decode_event(123, (7 << 16) | tray.NIN_KEYSELECT, True), (tray.NIN_KEYSELECT, 7))
        self.assertEqual(tray._decode_event(7, tray.WM_LBUTTONDBLCLK, False), (tray.WM_LBUTTONDBLCLK, 7))

    def test_missing_native_api_returns_false_without_live_thread(self):
        icon = tray.TrayIcon("test", "no-native-test", Mock())
        with patch.object(tray.os, "name", "nt"), patch.object(tray, "_NativeAPI", side_effect=OSError("unavailable")):
            self.assertFalse(icon.start())
            self.assertFalse(icon.available)
            icon.stop()
            icon.stop()
        self.assertFalse(icon._thread.is_alive())
        icon.on_action.assert_not_called()

    def test_request_restore_targets_the_installation_specific_window(self):
        api = Mock()
        api.FindWindowW.return_value = 123
        api.RegisterWindowMessageW.return_value = 40001
        api.PostMessageW.return_value = 1
        with patch.object(tray.os, "name", "nt"), patch.object(tray, "_NativeAPI", return_value=api):
            self.assertTrue(tray.request_restore("selected-installation"))
        api.FindWindowW.assert_called_once_with(tray._window_class("selected-installation"), None)
        api.PostMessageW.assert_called_once_with(123, 40001, 0, 0)

    def test_request_restore_missing_window_does_not_post(self):
        api = Mock()
        api.FindWindowW.return_value = 0
        with patch.object(tray.os, "name", "nt"), patch.object(tray, "_NativeAPI", return_value=api):
            self.assertFalse(tray.request_restore("missing"))
        api.PostMessageW.assert_not_called()

    def run_tray_callback(self, target):
        errors = []
        def worker():
            try:
                target()
            except Exception as error:
                errors.append(error)
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(timeout=2)
        self.assertFalse(thread.is_alive())
        if errors:
            raise errors[0]
        return thread.ident

    def test_taskbar_rebuild_failure_notifies_gui_from_tray_worker(self):
        actions = queue.Queue()
        icon = tray.TrayIcon("test", "test-rebuild", lambda action: actions.put((action, threading.get_ident())))
        icon._taskbar_message = 49000
        icon._delete_icon = Mock()
        icon._add_icon = Mock(return_value=False)
        worker_id = self.run_tray_callback(lambda: icon._window_message(123, 49000, 0, 0))
        self.assertEqual(actions.get_nowait(), ("tray_unavailable", worker_id))
        icon._delete_icon.assert_called_once()
        icon._add_icon.assert_called_once()

    def test_menu_returns_command_synchronously_and_destroys_menu(self):
        actions = queue.Queue()
        icon = tray.TrayIcon("test", "test-menu", lambda action: actions.put((action, threading.get_ident())))
        api = Mock()
        api.CreatePopupMenu.return_value = 456
        api.AppendMenuW.return_value = 1
        api.TrackPopupMenu.return_value = tray.EXIT_COMMAND
        icon._api = api
        worker_id = self.run_tray_callback(lambda: icon._show_menu(123))
        self.assertEqual(actions.get_nowait(), ("exit", worker_id))
        flags = api.TrackPopupMenu.call_args.args[1]
        self.assertTrue(flags & tray.TPM_RETURNCMD)
        self.assertTrue(flags & tray.TPM_NONOTIFY)
        api.SetForegroundWindow.assert_called_once_with(123)
        api.DestroyMenu.assert_called_once_with(456)
        api.PostMessageW.assert_called_once_with(123, tray.WM_NULL, 0, 0)
        self.assertFalse(icon._in_menu)

    def test_menu_cleanup_runs_even_if_native_tracking_raises(self):
        icon = tray.TrayIcon("test", "menu-error", Mock())
        api = Mock()
        api.CreatePopupMenu.return_value = 456
        api.AppendMenuW.return_value = 1
        api.TrackPopupMenu.side_effect = OSError("menu failure")
        icon._api = api
        with self.assertRaises(OSError):
            icon._show_menu(123)
        api.DestroyMenu.assert_called_once_with(456)
        self.assertFalse(icon._in_menu)

    def test_other_icon_id_is_ignored(self):
        callback = Mock()
        icon = tray.TrayIcon("test", "test-callback", callback)
        icon._version4 = True
        self.assertEqual(icon._window_message(123, tray.WM_TRAY, 0, (99 << 16) | tray.NIN_KEYSELECT), 0)
        callback.assert_not_called()


@unittest.skipUnless(os.name == "nt" and os.environ.get("NEARROUTE_TRAY_SMOKE") == "1", "explicit Windows tray smoke only")
class WindowsTraySmokeTests(unittest.TestCase):
    def test_real_icon_lifecycle_restore_keyboard_and_resource_cleanup(self):
        api = tray._NativeAPI()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.argtypes = []
        kernel.GetCurrentProcess.restype = wintypes.HANDLE
        api.user.GetGuiResources.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        api.user.GetGuiResources.restype = wintypes.DWORD
        process = kernel.GetCurrentProcess()
        def resources():
            return tuple(api.user.GetGuiResources(process, kind) for kind in (0, 1))

        # Warm up Windows' shared application icon cache before checking counts.
        warmup = tray.TrayIcon("NearRoute tray smoke", "smoke-warmup-" + uuid.uuid4().hex, lambda _action: None)
        try:
            self.assertTrue(warmup.start(), warmup.last_error)
        finally:
            warmup.stop()
        before = resources()
        for _ in range(3):
            key = "nearroute-smoke-" + uuid.uuid4().hex
            actions = queue.Queue()
            icon = tray.TrayIcon("NearRoute test icon (temporary)", key, lambda action: actions.put((action, threading.get_ident())))
            try:
                self.assertTrue(icon.start(), icon.last_error)
                self.assertTrue(icon.available)
                hwnd = icon._hwnd
                self.assertTrue(api.IsWindow(hwnd))
                self.assertTrue(tray.request_restore(key))
                self.assertEqual(actions.get(timeout=2), ("open", icon._thread.ident))
                for event in (tray.NIN_KEYSELECT, tray.WM_LBUTTONDBLCLK):
                    wparam = 0 if icon._version4 else tray.ICON_ID
                    lparam = (tray.ICON_ID << 16) | event if icon._version4 else event
                    self.assertTrue(api.PostMessageW(hwnd, tray.WM_TRAY, wparam, lparam))
                    self.assertEqual(actions.get(timeout=2), ("open", icon._thread.ident))
                # Notify only this test window; never restart Explorer or broadcast.
                self.assertTrue(api.PostMessageW(hwnd, icon._taskbar_message, 0, 0))
                self.assertTrue(tray.request_restore(key))
                self.assertEqual(actions.get(timeout=2), ("open", icon._thread.ident))
                self.assertTrue(icon.available)
            finally:
                icon.stop()
                icon.stop()
            self.assertFalse(icon.available)
            self.assertFalse(icon._thread.is_alive())
            self.assertFalse(icon._icon_added)
            self.assertIsNone(icon._hwnd)
            self.assertEqual(icon._class_atom, 0)
            self.assertFalse(api.IsWindow(hwnd))
            self.assertFalse(tray.request_restore(key))
        self.assertEqual(resources(), before, "GDI/USER object count grew across tray lifecycles")


if __name__ == "__main__":
    unittest.main()
