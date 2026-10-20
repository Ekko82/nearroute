"""NearRoute's native Windows notification icon, with no Tk dependency.

``on_action`` runs on the icon's dedicated thread. Pass a thread-safe queue's
``put`` method and handle ``open``, ``exit`` and ``tray_unavailable`` on the GUI
thread. A false ``start()`` result means the GUI must remain visible.

Win32 references:
https://learn.microsoft.com/windows/win32/api/shellapi/ns-shellapi-notifyicondataw
https://learn.microsoft.com/windows/win32/api/winuser/nf-winuser-trackpopupmenu
https://learn.microsoft.com/windows/win32/shell/taskbar#taskbar-creation-notification
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import queue
import threading
from typing import Callable


WM_NULL = 0x0000
WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_CONTEXTMENU = 0x007B
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_DPICHANGED = 0x02E0
WM_USER = 0x0400
WM_APP = 0x8000
WM_TRAY = WM_APP + 71
WM_NOTIFICATION = WM_APP + 72
NIN_SELECT = WM_USER
NIN_KEYSELECT = WM_USER + 1
NIN_BALLOONUSERCLICK = WM_USER + 5
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIM_SETFOCUS = 3
NIM_SETVERSION = 4
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
NIF_INFO = 0x10
NIF_SHOWTIP = 0x80
NOTIFYICON_VERSION_4 = 4
ICON_ID = 1
OPEN_COMMAND = 1001
EXIT_COMMAND = 1002
TPM_RIGHTBUTTON = 0x0002
TPM_NONOTIFY = 0x0080
TPM_RETURNCMD = 0x0100
MF_STRING = 0
MF_SEPARATOR = 0x0800
WS_EX_TOOLWINDOW = 0x00000080
IDI_APPLICATION = 32512
IMAGE_ICON = 1
LR_LOADFROMFILE = 0x0010
SM_CXSMICON = 49
SM_CYSMICON = 50
ICON_PATH = Path(__file__).resolve().parent / "assets" / "nearroute.ico"

LRESULT = ctypes.c_ssize_t
WPARAM = ctypes.c_size_t
LPARAM = ctypes.c_ssize_t
WNDPROC = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)(
    LRESULT, wintypes.HWND, wintypes.UINT, WPARAM, LPARAM,
)


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD), ("Data4", wintypes.BYTE * 8),
    ]


class _Version(ctypes.Union):
    _fields_ = [("uTimeout", wintypes.UINT), ("uVersion", wintypes.UINT)]


class NOTIFYICONDATAW(ctypes.Structure):
    _anonymous_ = ("version",)
    _fields_ = [
        ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
        ("version", _Version), ("szInfoTitle", wintypes.WCHAR * 64),
        ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
        ("hBalloonIcon", wintypes.HICON),
    ]


class WNDCLASSW(ctypes.Structure):
    _fields_ = [
        ("style", wintypes.UINT), ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
        ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
        ("lpszMenuName", wintypes.LPCWSTR), ("lpszClassName", wintypes.LPCWSTR),
    ]


class MSG(ctypes.Structure):
    _fields_ = [
        ("hwnd", wintypes.HWND), ("message", wintypes.UINT),
        ("wParam", WPARAM), ("lParam", LPARAM), ("time", wintypes.DWORD),
        ("pt", wintypes.POINT), ("lPrivate", wintypes.DWORD),
    ]


def _window_class(instance_key: str) -> str:
    digest = hashlib.sha256(str(instance_key).encode("utf-8")).hexdigest()[:24]
    return "NearRoute.TrayWindow." + digest


def _restore_message_name(instance_key: str) -> str:
    return _window_class(instance_key) + ".Restore"


def _tooltip(title: str) -> str:
    # NOTIFYICONDATA's limit is 127 UTF-16 code units, not 127 Python code points.
    return str(title).replace("\0", " ").encode("utf-16-le", errors="replace")[:254].decode("utf-16-le", errors="ignore")


def _decode_event(wparam: int, lparam: int, version4: bool) -> tuple[int, int]:
    if version4:
        return lparam & 0xFFFF, (lparam >> 16) & 0xFFFF
    return lparam & 0xFFFF, wparam


class _NativeAPI:
    """Explicit pointer-width signatures prevent 64-bit HWND truncation."""

    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

        def bind(library, name, result, args):
            function = getattr(library, name)
            function.restype = result
            function.argtypes = args
            setattr(self, name, function)

        bind(self.kernel, "GetModuleHandleW", wintypes.HMODULE, [wintypes.LPCWSTR])
        bind(self.kernel, "GetCurrentThreadId", wintypes.DWORD, [])
        bind(self.user, "RegisterClassW", wintypes.ATOM, [ctypes.POINTER(WNDCLASSW)])
        bind(self.user, "UnregisterClassW", wintypes.BOOL, [wintypes.LPCWSTR, wintypes.HINSTANCE])
        bind(self.user, "CreateWindowExW", wintypes.HWND, [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
        ])
        bind(self.user, "DefWindowProcW", LRESULT, [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM])
        bind(self.user, "DestroyWindow", wintypes.BOOL, [wintypes.HWND])
        bind(self.user, "IsWindow", wintypes.BOOL, [wintypes.HWND])
        bind(self.user, "GetMessageW", wintypes.BOOL, [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT])
        bind(self.user, "TranslateMessage", wintypes.BOOL, [ctypes.POINTER(MSG)])
        bind(self.user, "DispatchMessageW", LRESULT, [ctypes.POINTER(MSG)])
        bind(self.user, "PostQuitMessage", None, [ctypes.c_int])
        bind(self.user, "PostMessageW", wintypes.BOOL, [wintypes.HWND, wintypes.UINT, WPARAM, LPARAM])
        bind(self.user, "PostThreadMessageW", wintypes.BOOL, [wintypes.DWORD, wintypes.UINT, WPARAM, LPARAM])
        bind(self.user, "RegisterWindowMessageW", wintypes.UINT, [wintypes.LPCWSTR])
        bind(self.user, "FindWindowW", wintypes.HWND, [wintypes.LPCWSTR, wintypes.LPCWSTR])
        bind(self.user, "LoadIconW", wintypes.HICON, [wintypes.HINSTANCE, wintypes.LPCWSTR])
        bind(self.user, "LoadImageW", wintypes.HANDLE, [
            wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
            ctypes.c_int, ctypes.c_int, wintypes.UINT,
        ])
        bind(self.user, "DestroyIcon", wintypes.BOOL, [wintypes.HICON])
        bind(self.user, "GetSystemMetrics", ctypes.c_int, [ctypes.c_int])
        for name, result, args in (
            ("GetDpiForWindow", wintypes.UINT, [wintypes.HWND]),
            ("GetSystemMetricsForDpi", ctypes.c_int, [ctypes.c_int, wintypes.UINT]),
            ("SetThreadDpiAwarenessContext", wintypes.HANDLE, [wintypes.HANDLE]),
        ):
            try:
                bind(self.user, name, result, args)
            except AttributeError:
                setattr(self, name, None)
        bind(self.user, "CreatePopupMenu", wintypes.HMENU, [])
        bind(self.user, "AppendMenuW", wintypes.BOOL, [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR])
        bind(self.user, "DestroyMenu", wintypes.BOOL, [wintypes.HMENU])
        bind(self.user, "SetForegroundWindow", wintypes.BOOL, [wintypes.HWND])
        bind(self.user, "GetCursorPos", wintypes.BOOL, [ctypes.POINTER(wintypes.POINT)])
        bind(self.user, "TrackPopupMenu", wintypes.BOOL, [
            wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
            ctypes.c_int, wintypes.HWND, ctypes.POINTER(wintypes.RECT),
        ])
        bind(self.user, "EndMenu", wintypes.BOOL, [])
        bind(self.shell, "Shell_NotifyIconW", wintypes.BOOL, [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATAW)])


def request_restore(instance_key: str) -> bool:
    """Ask the same installation's existing tray thread to emit ``open``."""
    if os.name != "nt":
        return False
    try:
        api = _NativeAPI()
        hwnd = api.FindWindowW(_window_class(instance_key), None)
        if not hwnd:
            return False
        message = api.RegisterWindowMessageW(_restore_message_name(instance_key))
        return bool(message and api.PostMessageW(hwnd, message, 0, 0))
    except (OSError, AttributeError):
        return False


class TrayIcon:
    def __init__(self, title: str, instance_key: str, on_action: Callable[[str], None]):
        self.title = str(title)
        self.instance_key = str(instance_key)
        self.on_action = on_action
        self._class_name = _window_class(self.instance_key)
        self._api = None
        self._thread = None
        self._native_thread_id = 0
        self._hwnd = None
        self._instance = None
        self._class_atom = 0
        self._wndproc_ref = None
        self._data = None
        self._owned_icon = 0
        self._icon_added = False
        self._version4 = False
        self._in_menu = False
        self._restore_message = 0
        self._taskbar_message = 0
        self._ready = threading.Event()
        self._available = threading.Event()
        self._stop_requested = threading.Event()
        self._lifecycle_lock = threading.Lock()
        self._notifications = queue.SimpleQueue()
        self.last_error = None

    @property
    def available(self) -> bool:
        return self._available.is_set()

    def start(self) -> bool:
        if os.name != "nt":
            return False
        with self._lifecycle_lock:
            if self._thread is None or not self._thread.is_alive():
                self._ready.clear()
                self._available.clear()
                self._stop_requested.clear()
                self.last_error = None
                self._thread = threading.Thread(target=self._run, name="NearRoute-tray", daemon=True)
                self._thread.start()
        if not self._ready.wait(5) or not self.available:
            self.stop()
            return False
        return True

    def stop(self) -> None:
        self._stop_requested.set()
        self._available.clear()
        api, hwnd, thread = self._api, self._hwnd, self._thread
        if api is not None:
            if hwnd:
                api.PostMessageW(hwnd, WM_CLOSE, 0, 0)
            elif self._native_thread_id:
                api.PostThreadMessageW(self._native_thread_id, WM_QUIT, 0, 0)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=5)

    def show_notification(self, title: str, text: str) -> bool:
        """Queue a shell notification without touching tray data from the GUI thread."""
        if not self.available or self._stop_requested.is_set():
            return False
        self._notifications.put((str(title), str(text)))
        return bool(self._api.PostMessageW(self._hwnd, WM_NOTIFICATION, 0, 0))

    def _show_notification(self):
        pending = None
        try:
            while True:
                pending = self._notifications.get_nowait()
        except queue.Empty:
            pass
        if pending is None or not self.available or self._stop_requested.is_set():
            return
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(data)
        data.hWnd = self._hwnd
        data.uID = ICON_ID
        data.uFlags = NIF_INFO
        title, text = pending
        data.szInfoTitle = title.replace("\0", " ").encode("utf-16-le", errors="replace")[:126].decode("utf-16-le", errors="ignore")
        data.szInfo = text.replace("\0", " ").encode("utf-16-le", errors="replace")[:510].decode("utf-16-le", errors="ignore")
        data.dwInfoFlags = 0x01 | 0x80  # NIIF_INFO | NIIF_RESPECT_QUIET_TIME
        self._api.Shell_NotifyIconW(NIM_MODIFY, ctypes.byref(data))

    def _emit(self, action: str) -> None:
        # All call sites are reached by the tray thread or its native window proc.
        try:
            self.on_action(action)
        except Exception as error:
            self.last_error = "callback:" + type(error).__name__

    def _small_icon_size(self) -> tuple[int, int]:
        dpi_for_window = self._api.GetDpiForWindow
        metrics_for_dpi = self._api.GetSystemMetricsForDpi
        dpi = dpi_for_window(self._hwnd) if dpi_for_window else 0
        if dpi and metrics_for_dpi:
            dimensions = tuple(metrics_for_dpi(index, dpi) for index in (SM_CXSMICON, SM_CYSMICON))
        else:
            dimensions = tuple(self._api.GetSystemMetrics(index) for index in (SM_CXSMICON, SM_CYSMICON))
        return tuple(value if value > 0 else 16 for value in dimensions)

    def _add_icon(self) -> bool:
        if self._stop_requested.is_set():
            return False
        data = NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(NOTIFYICONDATAW)
        data.hWnd = self._hwnd
        data.uID = ICON_ID
        data.uFlags = NIF_MESSAGE | NIF_ICON | NIF_TIP | NIF_SHOWTIP
        data.uCallbackMessage = WM_TRAY
        width, height = self._small_icon_size()
        # Keep this owned handle until the shell icon is removed. The ICO has
        # separate small sizes, avoiding Windows shrinking a generic 32px icon.
        self._owned_icon = self._api.LoadImageW(
            None, str(ICON_PATH), IMAGE_ICON, width, height, LR_LOADFROMFILE,
        ) or 0
        data.hIcon = self._owned_icon
        if not data.hIcon:
            self.last_error = "icon:load_failed"
            # A missing asset must not make a hidden application inaccessible.
            # This shared system fallback must never be passed to DestroyIcon.
            resource = ctypes.cast(ctypes.c_void_p(IDI_APPLICATION), wintypes.LPCWSTR)
            data.hIcon = self._api.LoadIconW(None, resource)
        data.szTip = _tooltip(self.title)
        self._data = data
        if not data.hIcon or not self._api.Shell_NotifyIconW(NIM_ADD, ctypes.byref(data)):
            self._delete_icon()
            return False
        self._icon_added = True
        data.uVersion = NOTIFYICON_VERSION_4
        self._version4 = bool(self._api.Shell_NotifyIconW(NIM_SETVERSION, ctypes.byref(data)))
        if self._stop_requested.is_set():
            self._delete_icon()
            return False
        self._available.set()
        return True

    def _delete_icon(self) -> None:
        self._available.clear()
        if self._icon_added and self._data is not None:
            self._api.Shell_NotifyIconW(NIM_DELETE, ctypes.byref(self._data))
        self._icon_added = False
        if self._owned_icon:
            self._api.DestroyIcon(self._owned_icon)
            self._owned_icon = 0
        if self._data is not None:
            self._data.hIcon = None

    def _show_menu(self, hwnd) -> None:
        if self._in_menu or self._stop_requested.is_set():
            return
        menu = self._api.CreatePopupMenu()
        if not menu:
            self._emit("open")
            return
        self._in_menu = True
        command = 0
        try:
            if not (self._api.AppendMenuW(menu, MF_STRING, OPEN_COMMAND, "打开近舟")
                    and self._api.AppendMenuW(menu, MF_SEPARATOR, 0, None)
                    and self._api.AppendMenuW(menu, MF_STRING, EXIT_COMMAND, "退出近舟（停止优选）")):
                self._emit("open")
                return
            point = wintypes.POINT()
            self._api.GetCursorPos(ctypes.byref(point))
            self._api.SetForegroundWindow(hwnd)
            command = self._api.TrackPopupMenu(
                menu, TPM_RIGHTBUTTON | TPM_NONOTIFY | TPM_RETURNCMD,
                point.x, point.y, 0, hwnd, None,
            )
        finally:
            self._api.DestroyMenu(menu)
            self._in_menu = False
            self._api.PostMessageW(hwnd, WM_NULL, 0, 0)
        if self._stop_requested.is_set():
            return
        if command == OPEN_COMMAND:
            self._emit("open")
        elif command == EXIT_COMMAND:
            self._emit("exit")
        elif self._icon_added:
            self._api.Shell_NotifyIconW(NIM_SETFOCUS, ctypes.byref(self._data))

    def _window_message(self, hwnd, message, wparam, lparam):
        if message == WM_NOTIFICATION:
            self._show_notification()
            return 0
        if message == WM_CLOSE:
            if self._in_menu:
                self._api.EndMenu()
            self._delete_icon()
            self._api.DestroyWindow(hwnd)
            return 0
        if message == WM_DESTROY:
            self._hwnd = None
            self._api.PostQuitMessage(0)
            return 0
        if self._restore_message and message == self._restore_message:
            if not self._stop_requested.is_set():
                self._emit("open")
            return 0
        if message == WM_DPICHANGED or (self._taskbar_message and message == self._taskbar_message):
            # Also handles DPI-change broadcasts, where the old icon may exist.
            self._delete_icon()
            if not self._add_icon() and not self._stop_requested.is_set():
                self._emit("tray_unavailable")
            return 0
        if message == WM_TRAY:
            event, icon_id = _decode_event(int(wparam), int(lparam), self._version4)
            if icon_id != ICON_ID or self._stop_requested.is_set():
                return 0
            if event in (WM_LBUTTONDBLCLK, NIN_SELECT, NIN_KEYSELECT, NIN_BALLOONUSERCLICK):
                self._emit("open")
            elif event == WM_CONTEXTMENU or (not self._version4 and event == WM_RBUTTONUP):
                self._show_menu(hwnd)
            return 0
        return self._api.DefWindowProcW(hwnd, message, wparam, lparam)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            return self._window_message(hwnd, message, wparam, lparam)
        except Exception as error:
            self.last_error = "window:" + type(error).__name__
            self._available.clear()
            if not self._stop_requested.is_set():
                self._emit("tray_unavailable")
            return self._api.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self) -> None:
        started = False
        previous_dpi_context = None
        try:
            self._api = _NativeAPI()
            # Set awareness only for this dedicated native thread. Changing the
            # process default at runtime would also change Tk's window layout.
            if self._api.SetThreadDpiAwarenessContext:
                previous_dpi_context = self._api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
                if not previous_dpi_context:  # Windows 10 before per-monitor v2.
                    previous_dpi_context = self._api.SetThreadDpiAwarenessContext(ctypes.c_void_p(-3))
            self._native_thread_id = self._api.GetCurrentThreadId()
            self._instance = self._api.GetModuleHandleW(None)
            self._restore_message = self._api.RegisterWindowMessageW(_restore_message_name(self.instance_key))
            self._taskbar_message = self._api.RegisterWindowMessageW("TaskbarCreated")
            if not self._instance or not self._restore_message or not self._taskbar_message:
                raise OSError("无法注册托盘窗口消息")
            self._wndproc_ref = WNDPROC(self._window_proc)
            window_class = WNDCLASSW()
            window_class.lpfnWndProc = self._wndproc_ref
            window_class.hInstance = self._instance
            window_class.lpszClassName = self._class_name
            self._class_atom = self._api.RegisterClassW(ctypes.byref(window_class))
            if not self._class_atom:
                raise OSError("无法注册托盘窗口类")
            # A hidden top-level window receives TaskbarCreated broadcasts;
            # HWND_MESSAGE would not. No WS_VISIBLE flag or ShowWindow is used.
            self._hwnd = self._api.CreateWindowExW(
                WS_EX_TOOLWINDOW, self._class_name, self._class_name, 0,
                0, 0, 0, 0, None, None, self._instance, None,
            )
            if not self._hwnd:
                raise OSError("无法创建托盘窗口")
            if not self._add_icon():
                return
            started = True
            self._ready.set()
            message = MSG()
            while not self._stop_requested.is_set():
                result = self._api.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise OSError("托盘消息循环失败")
                self._api.TranslateMessage(ctypes.byref(message))
                self._api.DispatchMessageW(ctypes.byref(message))
            if started and not self._stop_requested.is_set():
                self._emit("tray_unavailable")
        except Exception as error:
            self.last_error = type(error).__name__
            if started and not self._stop_requested.is_set():
                self._emit("tray_unavailable")
        finally:
            self._available.clear()
            try:
                if self._api is not None:
                    self._delete_icon()
                    hwnd = self._hwnd
                    if hwnd and self._api.IsWindow(hwnd):
                        self._api.DestroyWindow(hwnd)
                    self._hwnd = None
                    if self._class_atom:
                        if self._api.UnregisterClassW(self._class_name, self._instance):
                            self._class_atom = 0
                            self._wndproc_ref = None
            except Exception as error:
                self.last_error = "cleanup:" + type(error).__name__
            finally:
                if previous_dpi_context:
                    self._api.SetThreadDpiAwarenessContext(previous_dpi_context)
                self._native_thread_id = 0
                self._ready.set()
