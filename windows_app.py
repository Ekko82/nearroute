"""Windows taskbar identity for the Python-hosted NearRoute window.

Window properties must be cleared before destroying the Tk window:
https://learn.microsoft.com/windows/win32/api/shellapi/nf-shellapi-shgetpropertystoreforwindow
"""
from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import ntpath
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.parse
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
LAUNCH_PREFIX = ("-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File")


def _read_user_environment(name):
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        value, kind = winreg.QueryValueEx(key, name)
    if kind != winreg.REG_SZ or not isinstance(value, str):
        raise ValueError("Invalid controller credential")
    return value


def import_maomao_auth(controller):
    """Import the selected local client's credential after a user clicks Import.

    The elevated helper validates the client and stores only a user environment
    variable. Its name is returned; the credential never enters process arguments,
    stdout, diagnostic messages, or the application configuration.
    """
    if not IS_WINDOWS:
        raise RuntimeError("猫猫云认证导入仅支持 Windows")
    try:
        url = urllib.parse.urlsplit(controller)
        if (url.scheme != "http" or url.hostname not in ("127.0.0.1", "::1", "localhost")
                or url.username is not None or url.password is not None
                or url.path not in ("", "/") or "?" in controller or "#" in controller
                or "\\" in controller or any(ord(char) <= 32 or ord(char) >= 127 for char in controller)
                or url.netloc.endswith(":") or not 1 <= (80 if url.port is None else url.port) <= 65535):
            raise ValueError
        host, port = url.hostname, url.port or 80
    except (AttributeError, TypeError, ValueError):
        raise ValueError("认证导入仅支持本机 HTTP 控制地址，不能含账号密码、路径或查询参数") from None
    script = ROOT / "controller_auth.ps1"
    if not script.is_file():
        raise RuntimeError("缺少认证导入组件，请更新或重新安装近舟")
    powershell = str(Path(os.environ.get("SystemRoot", r"C:\Windows"))
                     / "System32/WindowsPowerShell/v1.0/powershell.exe")
    # Quote PowerShell literals separately from the executable's argument list.
    # Neither constructed command contains a controller credential.
    quote = lambda value: "'" + value.replace("'", "''") + "'"
    program = """
$ErrorActionPreference = 'Stop'
try {
    $sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $expires = [DateTime]::UtcNow.AddSeconds(60).Ticks
    $arguments = @('-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File',
        %s, '-ControllerHost', %s, '-ControllerPort', '%d', '-ExpectedSid', $sid,
        '-RequestExpiresUtcTicks', $expires)
    $process = Start-Process -FilePath %s -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru
    try {
        if (-not $process.WaitForExit(60000)) { exit 124 }
        exit $process.ExitCode
    } finally { $process.Dispose() }
} catch {
    if ($_.Exception.NativeErrorCode -eq 1223 -or $_.Exception.InnerException.NativeErrorCode -eq 1223) {
        exit 1223
    }
    exit 15
}
""" % (quote('"' + str(script) + '"'), quote(host), port, quote(powershell))
    try:
        result = subprocess.run(
            [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(program.encode("utf-16-le")).decode("ascii")],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=150, check=False, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("管理员确认或认证导入超时，请确认猫猫云仍在运行后重试") from None
    except OSError:
        raise RuntimeError("无法启动管理员认证导入，请确认 Windows PowerShell 可用") from None
    errors = {
        10: "所选控制端口没有唯一的猫猫云内核，无法导入认证",
        11: "无法核对猫猫云进程的当前用户身份",
        12: "所选端口不属于支持的猫猫云进程，无法导入认证",
        13: "猫猫云控制认证验证失败，请重新检测客户端后重试",
        14: "无法保存当前用户的认证变量",
        15: "无法启动管理员认证导入",
        124: "认证导入超时，请确认猫猫云仍在运行后重试",
        1223: "已取消管理员确认，未导入猫猫云认证",
    }
    if result.returncode:
        raise RuntimeError(errors.get(result.returncode, "猫猫云认证导入未完成，请重新检测客户端后重试"))
    name = f"NEARROUTE_MAOMAO_SECRET_{port}"
    try:
        credential = _read_user_environment(name)
        if not re.fullmatch(r"[0-9a-fA-F]{48}", credential):
            raise ValueError
        os.environ[name] = credential
    except (OSError, ValueError, TypeError, ImportError):
        raise RuntimeError("认证已导入，但当前窗口未能读取认证变量，请重新打开近舟后测试连接") from None
    return name


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
# Microsoft Propkey.h definitions for the actual link target and arguments:
# https://learn.microsoft.com/windows/win32/properties/props-system-link-targetparsingpath
# https://learn.microsoft.com/windows/win32/properties/props-system-link-arguments
LINK_TARGET = PROPERTYKEY(GUID.from_text("b9b4b3fc-2b51-4a42-b5d8-324146afcf25"), 2)
LINK_ARGUMENTS = PROPERTYKEY(GUID.from_text("436f2667-14e2-4feb-b30a-146c53b5b674"), 100)
# Microsoft Windows SDK ShObjIdl_core.h / ObjIdl.h interface identifiers.
CLSID_SHELL_LINK = GUID.from_text("00021401-0000-0000-c000-000000000046")
IID_SHELL_LINK_W = GUID.from_text("000214f9-0000-0000-c000-000000000046")
IID_PERSIST_FILE = GUID.from_text("0000010b-0000-0000-c000-000000000046")


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
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)

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
        bind(self.ole, "CoCreateInstance", HRESULT,
             [ctypes.POINTER(GUID), ctypes.c_void_p, ctypes.c_uint32,
              ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)])
        bind(self.ole, "PropVariantClear", HRESULT, [ctypes.POINTER(PROPVARIANT)])
        bind(self.user, "GetAncestor", wintypes.HWND, [wintypes.HWND, wintypes.UINT])
        bind(self.shell, "CommandLineToArgvW", ctypes.POINTER(wintypes.LPWSTR),
             [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)])
        bind(self.kernel, "LocalFree", ctypes.c_void_p, [ctypes.c_void_p])


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


class _ComInterface:
    def __init__(self, pointer):
        self.pointer = pointer

    def _method(self, index, result, *args):
        table = ctypes.cast(self.pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        return WINFUNCTYPE(result, ctypes.c_void_p, *args)(table[index])

    def close(self):
        if self.pointer:
            self._method(2, ctypes.c_uint32)(self.pointer)
            self.pointer = None


class _PropertyStore(_ComInterface):

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

    def get_text(self, key, api):
        value = PROPVARIANT()
        try:
            _check(self._method(5, HRESULT, ctypes.POINTER(PROPERTYKEY), ctypes.POINTER(PROPVARIANT))(
                self.pointer, ctypes.byref(key), ctypes.byref(value)), "读取快捷方式属性")
            if value.vt == VT_EMPTY:
                return ""
            if value.vt != VT_LPWSTR:
                raise OSError("快捷方式属性不是字符串")
            return ctypes.wstring_at(value.value.pointer) if value.value.pointer else ""
        finally:
            api.PropVariantClear(ctypes.byref(value))

    def commit(self):
        _check(self._method(7, HRESULT)(self.pointer), "保存快捷方式属性")

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


@contextmanager
def _shortcut_store(api, path, flags=0):
    path = Path(path).resolve(strict=True)
    if path.suffix.lower() != ".lnk":
        raise ValueError("只支持 Windows .lnk 快捷方式")
    pointer = ctypes.c_void_p()
    _check(api.SHGetPropertyStoreFromParsingName(str(path), None, flags,
           ctypes.byref(IID_PROPERTY_STORE), ctypes.byref(pointer)), "读取快捷方式属性")
    store = _PropertyStore(pointer)
    try:
        yield store
    finally:
        store.close()


def read_shortcut(path):
    """Read the .lnk in place through the Unicode Shell property API.

    WScript.Shell may return empty fields for paths outside the system ANSI
    code page. These properties describe the executable and arguments, not the
    taskbar RelaunchCommand, which can intentionally be a different command.
    """
    if not IS_WINDOWS:
        raise OSError("快捷方式属性仅支持 Windows")
    api = _NativeAPI()
    with _com(api), _shortcut_store(api, path) as store:
        return {"target": store.get_text(LINK_TARGET, api),
                "arguments": store.get_text(LINK_ARGUMENTS, api)}


def _windows_arguments(arguments):
    api = _NativeAPI()
    count = ctypes.c_int()
    # CommandLineToArgvW expects argv[0]; the stored Arguments omit it.
    pointer = api.CommandLineToArgvW("powershell.exe " + arguments, ctypes.byref(count))
    if not pointer:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return [pointer[index] for index in range(1, count.value)]
    finally:
        api.LocalFree(pointer)


def _powershell_script(arguments):
    """Recognize our launch dispatcher and return its actual -File argument."""
    tokens = _windows_arguments(arguments)
    count = len(LAUNCH_PREFIX)
    if len(tokens) <= count or [value.casefold() for value in tokens[:count]] != [
            value.casefold() for value in LAUNCH_PREFIX]:
        return None
    return tokens[count]


def owns_shortcut(path, target, script):
    """Require the intended executable and its actual -File script to match."""
    try:
        values = read_shortcut(path)
        actual_script = _powershell_script(values["arguments"])
        # Normalize strings only: checking an unrelated link must not access its
        # target, which could be an unavailable network share.
        def same_path(actual, expected):
            return (bool(actual) and ntpath.isabs(actual)
                    and ntpath.normcase(ntpath.normpath(actual))
                    == ntpath.normcase(ntpath.normpath(str(expected))))
        return same_path(values["target"], target) and same_path(actual_script, script)
    except (OSError, ValueError):
        return False


def create_shortcut(path, target, arguments, working_directory, icon):
    """Create an installer-owned new link using IShellLinkW and IPersistFile."""
    if not IS_WINDOWS:
        raise OSError("快捷方式仅支持 Windows")
    path = Path(path).absolute()
    if path.suffix.lower() != ".lnk":
        raise ValueError("只支持 Windows .lnk 快捷方式")
    if path.exists():
        raise FileExistsError(path)
    api = _NativeAPI()
    with _com(api):
        pointer = ctypes.c_void_p()
        _check(api.CoCreateInstance(ctypes.byref(CLSID_SHELL_LINK), None, 1,
               ctypes.byref(IID_SHELL_LINK_W), ctypes.byref(pointer)), "创建快捷方式")
        link = _ComInterface(pointer)
        try:
            # IShellLinkW vtable methods from the SDK, after IUnknown's 3 slots:
            # SetPath=20, SetArguments=11, SetWorkingDirectory=9, SetDescription=7.
            for slot, value in ((20, str(target)), (11, arguments),
                                (9, str(working_directory)),
                                (7, "近舟 NearRoute：线路探测、范围设置和自动优选")):
                _check(link._method(slot, HRESULT, wintypes.LPCWSTR)(link.pointer, value), "写入快捷方式")
            _check(link._method(15, HRESULT, ctypes.c_int)(link.pointer, 7), "设置快捷方式窗口样式")
            _check(link._method(17, HRESULT, wintypes.LPCWSTR, ctypes.c_int)(
                link.pointer, str(icon), 0), "设置快捷方式图标")
            persist_pointer = ctypes.c_void_p()
            _check(link._method(0, HRESULT, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p))(
                link.pointer, ctypes.byref(IID_PERSIST_FILE), ctypes.byref(persist_pointer)), "保存快捷方式")
            persist = _ComInterface(persist_pointer)
            try:
                # IPersistFile::Save takes LPCOLESTR (Unicode) and BOOL.
                _check(persist._method(6, HRESULT, wintypes.LPCWSTR, wintypes.BOOL)(
                    persist.pointer, str(path), True), "保存快捷方式")
            finally:
                persist.close()
        finally:
            link.close()


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
    api = _NativeAPI()
    with _com(api), _shortcut_store(api, path, GPS_READWRITE) as store:
        for property_id, value in relaunch_properties().items():
            store.set(property_id, value)
        store.commit()


def main():
    parser = argparse.ArgumentParser(description="设置近舟快捷方式的 Windows 任务栏标识")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--shortcut", type=Path)
    action.add_argument("--check-shortcut", type=Path)
    action.add_argument("--read-shortcut", type=Path)
    action.add_argument("--create-shortcut", type=Path)
    parser.add_argument("--target", type=Path)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--python", type=Path)
    args = parser.parse_args()
    if args.check_shortcut:
        if args.target is None or args.script is None:
            parser.error("--check-shortcut 需要 --target 和 --script")
        raise SystemExit(0 if owns_shortcut(args.check_shortcut, args.target, args.script) else 1)
    if args.read_shortcut:
        print(json.dumps(read_shortcut(args.read_shortcut), ensure_ascii=True))
    elif args.create_shortcut:
        if args.target is None or args.script is None or args.python is None:
            parser.error("--create-shortcut 需要 --target、--script 和 --python")
        arguments = subprocess.list2cmdline([
            *LAUNCH_PREFIX, str(args.script), "-Mode", "dashboard", "-PythonExe", str(args.python)])
        create_shortcut(args.create_shortcut, args.target, arguments, args.script.parent, ICON_PATH)
        configure_shortcut(args.create_shortcut)
    else:
        configure_shortcut(args.shortcut)


if __name__ == "__main__":
    main()
