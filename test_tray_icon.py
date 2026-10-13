"""Tray unit tests; real Explorer integration is explicitly opt-in.

Windows smoke: set NEARROUTE_TRAY_SMOKE=1 for this test process.
Only uniquely named temporary test icons are created and removed.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import queue
import threading
import unittest
from unittest.mock import Mock, call, patch
import uuid

import tray_icon as tray


class TrayUnitTests(unittest.TestCase):
    def native_icon_fixture(self):
        icon = tray.TrayIcon("NearRoute unit icon", "mock-icon-owner", Mock())
        api = Mock()
        api.GetDpiForWindow = None
        api.GetSystemMetricsForDpi = None
        api.SetThreadDpiAwarenessContext = None
        api.GetSystemMetrics.side_effect = lambda index: {tray.SM_CXSMICON: 16, tray.SM_CYSMICON: 16}[index]
        api.LoadImageW.return_value = 101
        api.LoadIconW.return_value = 202
        api.DestroyIcon.return_value = 1
        api.Shell_NotifyIconW.return_value = 1
        icon._api = api
        icon._hwnd = 123
        return icon, api

    def test_loads_owned_file_icon_with_absolute_path_and_exact_small_dimensions(self):
        icon, api = self.native_icon_fixture()
        api.GetSystemMetrics.side_effect = lambda index: {tray.SM_CXSMICON: 20, tray.SM_CYSMICON: 24}[index]
        self.assertTrue(icon._add_icon())
        args = api.LoadImageW.call_args.args
        self.assertIsNone(args[0])
        self.assertTrue(Path(args[1]).is_absolute())
        self.assertEqual(Path(args[1]), Path(tray.__file__).resolve().parent / "assets" / "nearroute.ico")
        self.assertEqual(args[2:], (tray.IMAGE_ICON, 20, 24, tray.LR_LOADFROMFILE))
        self.assertFalse(args[-1] & 0x8000, "LR_SHARED would invalidate owned-handle cleanup")
        self.assertEqual(icon._owned_icon, 101)
        self.assertEqual(icon._data.hIcon, 101)
        api.LoadIconW.assert_not_called()

    def test_dpi_specific_small_icon_metrics_are_used_when_available(self):
        icon, api = self.native_icon_fixture()
        api.GetDpiForWindow = Mock(return_value=144)
        api.GetSystemMetricsForDpi = Mock(side_effect=lambda index, dpi: 24)
        self.assertEqual(icon._small_icon_size(), (24, 24))
        api.GetDpiForWindow.assert_called_once_with(123)
        self.assertEqual(api.GetSystemMetricsForDpi.call_args_list, [call(tray.SM_CXSMICON, 144), call(tray.SM_CYSMICON, 144)])
        api.GetSystemMetrics.assert_not_called()

    def test_missing_optional_dpi_api_falls_back_to_system_metrics(self):
        for dpi_function, metrics_function in ((None, Mock()), (Mock(return_value=144), None), (Mock(return_value=0), Mock())):
            with self.subTest(dpi_available=dpi_function is not None, metrics_available=metrics_function is not None):
                icon, api = self.native_icon_fixture()
                api.GetDpiForWindow = dpi_function
                api.GetSystemMetricsForDpi = metrics_function
                self.assertEqual(icon._small_icon_size(), (16, 16))
                self.assertEqual(api.GetSystemMetrics.call_args_list, [call(tray.SM_CXSMICON), call(tray.SM_CYSMICON)])
                if metrics_function is not None:
                    metrics_function.assert_not_called()

    def test_nonpositive_small_icon_metrics_use_safe_sixteen_pixel_fallback(self):
        icon, api = self.native_icon_fixture()
        api.GetSystemMetrics.side_effect = [0, -1]
        self.assertEqual(icon._small_icon_size(), (16, 16))

    def test_owned_icon_is_destroyed_after_shell_removal_once_and_reset(self):
        icon, api = self.native_icon_fixture()
        self.assertTrue(icon._add_icon())
        order = []
        api.Shell_NotifyIconW.side_effect = lambda operation, data: order.append(("shell", operation)) or 1
        api.DestroyIcon.side_effect = lambda handle: order.append(("destroy", handle)) or 1
        icon._delete_icon()
        icon._delete_icon()
        self.assertEqual(order, [("shell", tray.NIM_DELETE), ("destroy", 101)])
        self.assertEqual(icon._owned_icon, 0)
        self.assertFalse(icon._data.hIcon)
        self.assertFalse(icon._icon_added)
        self.assertFalse(icon.available)

    def test_load_failure_uses_shared_fallback_without_destroying_it(self):
        icon, api = self.native_icon_fixture()
        api.LoadImageW.return_value = 0
        self.assertTrue(icon._add_icon())
        self.assertEqual(icon.last_error, "icon:load_failed")
        self.assertEqual(icon._owned_icon, 0)
        self.assertEqual(icon._data.hIcon, 202)
        resource = api.LoadIconW.call_args.args[1]
        self.assertEqual(ctypes.cast(resource, ctypes.c_void_p).value, tray.IDI_APPLICATION)
        icon._delete_icon()
        icon._delete_icon()
        api.DestroyIcon.assert_not_called()
        self.assertFalse(icon._data.hIcon)

    def test_shell_add_failure_frees_loaded_owned_icon_immediately(self):
        icon, api = self.native_icon_fixture()
        api.Shell_NotifyIconW.return_value = 0
        self.assertFalse(icon._add_icon())
        api.DestroyIcon.assert_called_once_with(101)
        self.assertEqual(icon._owned_icon, 0)
        self.assertFalse(icon._data.hIcon)
        self.assertFalse(icon.available)
        icon._delete_icon()
        api.DestroyIcon.assert_called_once_with(101)

    def test_shell_add_failure_never_destroys_shared_fallback(self):
        icon, api = self.native_icon_fixture()
        api.LoadImageW.return_value = 0
        api.Shell_NotifyIconW.return_value = 0
        self.assertFalse(icon._add_icon())
        icon._delete_icon()
        api.DestroyIcon.assert_not_called()
        self.assertEqual(icon._owned_icon, 0)

    def test_failed_shell_delete_still_releases_owned_handle(self):
        icon, api = self.native_icon_fixture()
        self.assertTrue(icon._add_icon())
        api.Shell_NotifyIconW.return_value = 0
        icon._delete_icon()
        api.DestroyIcon.assert_called_once_with(101)
        self.assertEqual(icon._owned_icon, 0)

    def test_dpi_and_taskbar_rebuilds_release_old_handle_before_loading_next(self):
        for message in (tray.WM_DPICHANGED, 49000):
            with self.subTest(message=message):
                icon, api = self.native_icon_fixture()
                icon._taskbar_message = 49000
                api.LoadImageW.side_effect = [101, 303]
                self.assertTrue(icon._add_icon())
                self.assertEqual(icon._window_message(123, message, 0, 0), 0)
                api.DestroyIcon.assert_called_once_with(101)
                self.assertEqual(icon._owned_icon, 303)
                self.assertEqual(icon._data.hIcon, 303)
                self.assertTrue(icon.available)
                icon._delete_icon()
                self.assertEqual(api.DestroyIcon.call_args_list, [call(101), call(303)])

    def test_dpi_context_is_set_and_restored_only_on_tray_worker(self):
        icon, api = self.native_icon_fixture()
        api.GetCurrentThreadId.return_value = 17
        api.GetModuleHandleW.return_value = 18
        api.RegisterWindowMessageW.side_effect = [49001, 49002]
        api.RegisterClassW.return_value = 1
        api.CreateWindowExW.return_value = 123
        api.GetMessageW.return_value = 0
        api.IsWindow.return_value = 1
        api.UnregisterClassW.return_value = 1
        contexts = []
        def set_context(context):
            value = getattr(context, "value", context)
            contexts.append((ctypes.c_ssize_t(value).value, threading.get_ident()))
            return 111
        api.SetThreadDpiAwarenessContext = Mock(side_effect=set_context)
        with patch.object(tray, "_NativeAPI", return_value=api):
            worker_id = self.run_tray_callback(icon._run)
        self.assertEqual(contexts, [(-4, worker_id), (111, worker_id)])
        self.assertNotEqual(worker_id, threading.get_ident())
        self.assertEqual(icon._owned_icon, 0)
        api.DestroyIcon.assert_called_once_with(101)

    def test_dpi_context_is_restored_when_tray_window_creation_fails(self):
        icon, api = self.native_icon_fixture()
        api.GetCurrentThreadId.return_value = 17
        api.GetModuleHandleW.return_value = 0
        api.RegisterWindowMessageW.side_effect = [49001, 49002]
        api.SetThreadDpiAwarenessContext = Mock(return_value=111)
        with patch.object(tray, "_NativeAPI", return_value=api):
            self.run_tray_callback(icon._run)
        self.assertEqual(api.SetThreadDpiAwarenessContext.call_count, 2)
        self.assertEqual(api.SetThreadDpiAwarenessContext.call_args.args, (111,))

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
    def assert_native_owned_icon(self, icon, api):
        """Inspect Win32's loaded bitmap and delete GetIconInfo's bitmap copies.

        https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-geticoninfo
        https://learn.microsoft.com/windows/win32/api/wingdi/nf-wingdi-getdibits
        """
        class ICONINFO(ctypes.Structure):
            _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD),
                        ("yHotspot", wintypes.DWORD), ("hbmMask", wintypes.HBITMAP),
                        ("hbmColor", wintypes.HBITMAP)]
        class BITMAP(ctypes.Structure):
            _fields_ = [("bmType", wintypes.LONG), ("bmWidth", wintypes.LONG),
                        ("bmHeight", wintypes.LONG), ("bmWidthBytes", wintypes.LONG),
                        ("bmPlanes", wintypes.WORD), ("bmBitsPixel", wintypes.WORD),
                        ("bmBits", ctypes.c_void_p)]
        class BITMAPINFOHEADER(ctypes.Structure):
            _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD)]
        class BITMAPINFO(ctypes.Structure):
            _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 1)]

        self.assertTrue(icon._owned_icon, icon.last_error)
        self.assertEqual(icon._data.hIcon, icon._owned_icon)
        self.assertNotEqual(icon.last_error, "icon:load_failed")
        api.user.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
        api.user.GetIconInfo.restype = wintypes.BOOL
        api.user.GetDC.argtypes = [wintypes.HWND]
        api.user.GetDC.restype = wintypes.HDC
        api.user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        api.user.ReleaseDC.restype = ctypes.c_int
        gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        gdi.GetObjectW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p]
        gdi.GetObjectW.restype = ctypes.c_int
        gdi.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT,
                                 wintypes.UINT, ctypes.c_void_p, ctypes.POINTER(BITMAPINFO), wintypes.UINT]
        gdi.GetDIBits.restype = ctypes.c_int
        gdi.DeleteObject.argtypes = [wintypes.HANDLE]
        gdi.DeleteObject.restype = wintypes.BOOL
        info = ICONINFO()
        dc = None
        self.assertTrue(api.user.GetIconInfo(icon._owned_icon, ctypes.byref(info)))
        try:
            self.assertTrue(info.fIcon)
            self.assertTrue(info.hbmColor, "expected a colored NearRoute icon")
            bitmap = BITMAP()
            self.assertEqual(gdi.GetObjectW(info.hbmColor, ctypes.sizeof(bitmap), ctypes.byref(bitmap)), ctypes.sizeof(bitmap))
            self.assertEqual((bitmap.bmWidth, bitmap.bmHeight), icon._small_icon_size())
            self.assertEqual(bitmap.bmPlanes * bitmap.bmBitsPixel, 32)
            byte_count = bitmap.bmWidth * bitmap.bmHeight * 4
            pixels = (ctypes.c_ubyte * byte_count)()
            output = BITMAPINFO()
            output.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            output.bmiHeader.biWidth = bitmap.bmWidth
            output.bmiHeader.biHeight = -bitmap.bmHeight  # top-down BGRA
            output.bmiHeader.biPlanes = 1
            output.bmiHeader.biBitCount = 32
            output.bmiHeader.biCompression = 0  # BI_RGB
            output.bmiHeader.biSizeImage = byte_count
            dc = api.user.GetDC(None)
            self.assertTrue(dc)
            copied = gdi.GetDIBits(dc, info.hbmColor, 0, bitmap.bmHeight, pixels, ctypes.byref(output), 0)
            self.assertEqual(copied, bitmap.bmHeight)
            alpha = bytes(pixels)[3::4]
            self.assertGreater(max(alpha), 0, "loaded icon has no visible alpha")
            self.assertIn(0, alpha, "loaded icon lost its transparent background")
        finally:
            if dc:
                api.user.ReleaseDC(None, dc)
            for bitmap_handle in (info.hbmMask, info.hbmColor):
                if bitmap_handle:
                    gdi.DeleteObject(bitmap_handle)

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

        # Warm up native loader/shell caches before checking resource counts.
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
                self.assert_native_owned_icon(icon, api)
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
                for rebuild in (icon._taskbar_message, tray.WM_DPICHANGED):
                    if rebuild == tray.WM_DPICHANGED:
                        # This system message carries a RECT pointer: send it
                        # synchronously so its memory remains valid until handled.
                        send_message = api.user.SendMessageW
                        send_message.argtypes = [wintypes.HWND, wintypes.UINT, tray.WPARAM, tray.LPARAM]
                        send_message.restype = tray.LRESULT
                        rectangle = wintypes.RECT(0, 0, 0, 0)
                        dpi = api.GetDpiForWindow(hwnd) if api.GetDpiForWindow else 96
                        send_message(hwnd, rebuild, (dpi << 16) | dpi, ctypes.addressof(rectangle))
                    else:
                        self.assertTrue(api.PostMessageW(hwnd, rebuild, 0, 0))
                    self.assertTrue(tray.request_restore(key))
                    self.assertEqual(actions.get(timeout=2), ("open", icon._thread.ident))
                    self.assertTrue(icon.available)
                    self.assert_native_owned_icon(icon, api)
            finally:
                icon.stop()
                icon.stop()
            self.assertFalse(icon.available)
            self.assertFalse(icon._thread.is_alive())
            self.assertFalse(icon._icon_added)
            self.assertEqual(icon._owned_icon, 0)
            self.assertFalse(icon._data.hIcon)
            self.assertIsNone(icon._hwnd)
            self.assertEqual(icon._class_atom, 0)
            self.assertFalse(api.IsWindow(hwnd))
            self.assertFalse(tray.request_restore(key))
        self.assertEqual(resources(), before, "GDI/USER object count grew across tray lifecycles")


if __name__ == "__main__":
    unittest.main()
