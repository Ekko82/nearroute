"""Windows taskbar identity for the Python-hosted NearRoute window.

Window properties must be cleared before destroying the Tk window:
https://learn.microsoft.com/windows/win32/api/shellapi/nf-shellapi-shgetpropertystoreforwindow
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
import subprocess
import sys
import uuid


APP_ID = "Ekko82.NearRoute.Desktop"
APP_NAME = "近舟 NearRoute"
IS_WINDOWS = os.name == "nt"
ROOT = Path(__file__).resolve().parent
ICON_PATH = ROOT / "assets" / "nearroute.ico"
HRESULT = ctypes.c_int32
WINFUNCTYPE = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
VT_EMPTY, VT_LPWSTR = 0, 31
GPS_READWRITE = 2
GA_ROOT = 2
RPC_E_CHANGED_MODE = -2147417850


class GUID(ctypes.Structure):
    _fields_ = [("data1", ctypes.c_uint32), ("data2", ctypes.c_uint16),
                ("data3", ctypes.c_uint16), ("data4", ctypes.c_ubyte * 8)]

    @classmethod
    def from_text(cls, value):
        return cls.from_buffer_copy(uuid.UUID(value).bytes_le)


class PROPERTYKEY(ctypes.Structure):
    _fields_ = [("fmtid", GUID), ("pid", ctypes.c_uint32)]


class _VariantValue(ctypes.Union):
    # PROPVARIANT's largest value is two pointers on Win64, 8 bytes on Win32.
    _fields_ = [("pointer", ctypes.c_void_p), ("integer", ctypes.c_uint64),
                ("storage", ctypes.c_byte * (2 * ctypes.sizeof(ctypes.c_void_p)))]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_uint16), ("reserved1", ctypes.c_uint16),
                ("reserved2", ctypes.c_uint16), ("reserved3", ctypes.c_uint16),
                ("value", _VariantValue)]


_APP_FMTID = GUID.from_text("9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3")
IID_PROPERTY_STORE = GUID.from_text("886d8eeb-8cf2-4446-8d02-cdba1dbdcf99")
PROPERTY_IDS = (2, 4, 3, 5)  # RelaunchCommand, display name, icon, then AppID.


def _check(result, operation):
    if result < 0:
        raise OSError(f"{operation} 失败 (HRESULT 0x{result & 0xffffffff:08X})")


def relaunch_properties():
    executable = Path(sys.executable)
    windowless = executable.with_name("pythonw.exe")
    if windowless.is_file():
        executable = windowless
    command = subprocess.list2cmdline([str(executable), "-B", str(ROOT / "dashboard.py")])
    return {2: command, 4: APP_NAME, 3: str(ICON_PATH) + ",0", 5: APP_ID}


class _NativeAPI:
    def __init__(self):
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.ole = ctypes.WinDLL("ole32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)

        def bind(library, name, result, args):
            function = getattr(library, name)
            function.restype, function.argtypes = result, args
            setattr(self, name, function)

        bind(self.shell, "SetCurrentProcessExplicitAppUserModelID", HRESULT, [wintypes.LPCWSTR])
        bind(self.shell, "SHGetPropertyStoreForWindow", HRESULT,
             [wintypes.HWND, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)])
        bind(self.shell, "SHGetPropertyStoreFromParsingName", HRESULT,
             [wintypes.LPCWSTR, ctypes.c_void_p, ctypes.c_uint32,
              ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)])
        bind(self.ole, "CoInitializeEx", HRESULT, [ctypes.c_void_p, ctypes.c_uint32])
        bind(self.ole, "CoUninitialize", None, [])
        bind(self.ole, "PropVariantClear", HRESULT, [ctypes.POINTER(PROPVARIANT)])
        bind(self.user, "GetAncestor", wintypes.HWND, [wintypes.HWND, wintypes.UINT])


@contextmanager
def _com(api):
    result = api.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    if result != RPC_E_CHANGED_MODE:
        _check(result, "初始化任务栏属性")
    try:
        yield
    finally:
        if result >= 0:
            api.CoUninitialize()


class _PropertyStore:
    def __init__(self, pointer):
        self.pointer = pointer

    def _method(self, index, result, *args):
        table = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return WINFUNCTYPE(result, ctypes.c_void_p, *args)(table[index])

    def set(self, property_id, text):
        key = PROPERTYKEY(_APP_FMTID, property_id)
        value = PROPVARIANT()
        # SetValue copies the string into its store. The Python buffer stays
        # alive through the call; it must not be freed with PropVariantClear.
        buffer = None
        if text is not None:
            buffer = ctypes.create_unicode_buffer(text)
            value.vt = VT_LPWSTR
            value.value.pointer = ctypes.cast(buffer, ctypes.c_void_p).value
        _check(self._method(6, HRESULT, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(
            self.pointer, ctypes.byref(key), ctypes.byref(value)), "写入任务栏属性")

    def commit(self):
        _check(self._method(7, HRESULT)(self.pointer), "保存快捷方式属性")

    def close(self):
        if self.pointer:
            self._method(2, ctypes.c_uint32)(self.pointer)
            self.pointer = None


@contextmanager
def _window_store(api, hwnd):
    pointer = ctypes.c_void_p()
    _check(api.SHGetPropertyStoreForWindow(hwnd, ctypes.byref(IID_PROPERTY_STORE), ctypes.byref(pointer)),
           "读取窗口任务栏属性")
    store = _PropertyStore(pointer)
    try:
        yield store
    finally:
        store.close()


def _window_handle(root, api):
    # Tk's winfo_id identifies a child; the taskbar represents its wrapper HWND.
    return api.GetAncestor(root.winfo_id(), GA_ROOT) or root.winfo_id()


def _clear_properties(store):
    first_error = None
    for property_id in PROPERTY_IDS:
        try:
            store.set(property_id, None)
        except OSError as error:
            first_error = first_error or error
    if first_error is not None:
        raise first_error


def configure_taskbar():
    """Call before creating Tk, so Python-hosted apps are not grouped together."""
    if IS_WINDOWS:
        _check(_NativeAPI().SetCurrentProcessExplicitAppUserModelID(APP_ID), "设置近舟应用标识")


def configure_window(root):
    """Give an existing Tk wrapper its own identity, icon and relaunch command."""
    if not IS_WINDOWS:
        return
    api = _NativeAPI()
    root.update_idletasks()
    with _com(api), _window_store(api, _window_handle(root, api)) as store:
        try:
            for property_id, value in relaunch_properties().items():
                store.set(property_id, value)
        except OSError:
            try:
                _clear_properties(store)
            except OSError:
                pass  # Preserve the original failure after attempting all keys.
            raise


def clear_window(root):
    """Release window-property strings before root.destroy(), as Win32 requires."""
    if not IS_WINDOWS:
        return
    api = _NativeAPI()
    with _com(api), _window_store(api, _window_handle(root, api)) as store:
        _clear_properties(store)


def configure_shortcut(path):
    """Stamp an installer-owned .lnk; callers must establish file ownership."""
    if not IS_WINDOWS:
        raise OSError("快捷方式任务栏属性仅支持 Windows")
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() != ".lnk":
        raise ValueError("只支持 Windows .lnk 快捷方式")
    api = _NativeAPI()
    with _com(api):
        pointer = ctypes.c_void_p()
        _check(api.SHGetPropertyStoreFromParsingName(str(path), None, GPS_READWRITE,
               ctypes.byref(IID_PROPERTY_STORE), ctypes.byref(pointer)), "读取快捷方式属性")
        store = _PropertyStore(pointer)
        try:
            for property_id, value in relaunch_properties().items():
                store.set(property_id, value)
            store.commit()
        finally:
            store.close()


def main():
    parser = argparse.ArgumentParser(description="设置近舟快捷方式的 Windows 任务栏标识")
    parser.add_argument("--shortcut", type=Path, required=True)
    args = parser.parse_args()
    configure_shortcut(args.shortcut)


if __name__ == "__main__":
    main()
