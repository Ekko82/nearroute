"""Taskbar identity tests; no live application windows are opened or changed."""
import ctypes
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import windows_app as app


class TaskbarIdentityTests(unittest.TestCase):
    def test_app_identity_has_its_own_stable_namespace_and_relaunch_pair(self):
        props = app.relaunch_properties()
        self.assertEqual(props[5], "Ekko82.NearRoute.Desktop")
        self.assertEqual(props[4], "近舟 NearRoute")
        self.assertEqual(props[3], str(app.ICON_PATH) + ",0")
        self.assertIn(str(app.ROOT / "dashboard.py"), props[2])
        self.assertNotIn("optimizer.py", props[2])
        self.assertEqual(list(props)[-1], 5)

    def test_relaunch_command_quotes_paths_with_spaces(self):
        with patch.object(app, "ROOT", Path("C:/An App/NearRoute")), \
             patch.object(app.sys, "executable", "C:/Python Runtime/python.exe"), \
             patch.object(Path, "is_file", return_value=True):
            command = app.relaunch_properties()[2]
        self.assertTrue(command.startswith('"'))
        self.assertIn('pythonw.exe" -B "', command)
        self.assertTrue(command.endswith('dashboard.py"'))

    def test_non_windows_public_window_functions_are_no_ops(self):
        with patch.object(app, "IS_WINDOWS", False), patch.object(app, "_NativeAPI") as native:
            app.configure_taskbar()
            app.configure_window(Mock())
            app.clear_window(Mock())
        native.assert_not_called()

    def test_process_identity_failure_is_reported(self):
        api = Mock()
        api.SetCurrentProcessExplicitAppUserModelID.return_value = -1
        with patch.object(app, "IS_WINDOWS", True), patch.object(app, "_NativeAPI", return_value=api):
            with self.assertRaisesRegex(OSError, "HRESULT"):
                app.configure_taskbar()
        api.SetCurrentProcessExplicitAppUserModelID.assert_called_once_with(app.APP_ID)

    def test_tk_wrapper_hwnd_receives_properties_and_interface_is_released(self):
        api = Mock()
        api.CoInitializeEx.return_value = 0
        api.GetAncestor.return_value = 456
        root = Mock()
        root.winfo_id.return_value = 123
        store = Mock()
        @contextmanager
        def window_store(_api, hwnd):
            self.assertEqual(hwnd, 456)
            try:
                yield store
            finally:
                store.close()
        with patch.object(app, "IS_WINDOWS", True), patch.object(app, "_NativeAPI", return_value=api), \
             patch.object(app, "_window_store", side_effect=window_store):
            app.configure_window(root)
        self.assertEqual([call.args for call in store.set.call_args_list], list(app.relaunch_properties().items()))
        root.update_idletasks.assert_called_once()
        api.GetAncestor.assert_called_once_with(123, app.GA_ROOT)
        api.CoUninitialize.assert_called_once()
        store.close.assert_called_once()

    def test_failed_property_write_clears_partial_window_state(self):
        api = Mock()
        api.CoInitializeEx.return_value = 0
        api.GetAncestor.return_value = 456
        store = Mock()
        store.set.side_effect = [None, OSError("write failed"), None, None, None, None]
        @contextmanager
        def window_store(*args):
            yield store
        with patch.object(app, "IS_WINDOWS", True), patch.object(app, "_NativeAPI", return_value=api), \
             patch.object(app, "_window_store", side_effect=window_store):
            with self.assertRaisesRegex(OSError, "write failed"):
                app.configure_window(Mock())
        self.assertEqual([call.args for call in store.set.call_args_list[-4:]],
                         [(key, None) for key in app.PROPERTY_IDS])
        api.CoUninitialize.assert_called_once()

    def test_cleanup_attempts_all_properties_even_if_one_fails(self):
        store = Mock()
        store.set.side_effect = [OSError("failure"), None, None, None]
        with self.assertRaises(OSError):
            app._clear_properties(store)
        self.assertEqual(store.set.call_count, len(app.PROPERTY_IDS))

    def test_existing_different_com_apartment_is_not_uninitialized(self):
        api = Mock()
        api.CoInitializeEx.return_value = app.RPC_E_CHANGED_MODE
        with app._com(api):
            pass
        api.CoUninitialize.assert_not_called()

    def test_com_s_false_still_requires_matching_uninitialize(self):
        api = Mock()
        api.CoInitializeEx.return_value = 1
        with app._com(api):
            pass
        api.CoUninitialize.assert_called_once()

    def test_com_abi_sizes_are_pointer_correct(self):
        self.assertEqual(ctypes.sizeof(app.GUID), 16)
        self.assertEqual(ctypes.sizeof(app.PROPERTYKEY), 20)
        self.assertEqual(ctypes.sizeof(app.PROPVARIANT), 24 if ctypes.sizeof(ctypes.c_void_p) == 8 else 16)


@unittest.skipUnless(os.name == "nt", "Windows shell properties only")
class ShortcutPropertyIntegrationTests(unittest.TestCase):
    def test_create_and_read_unicode_shortcut_outside_system_code_page(self):
        # WScript.Shell cannot open this emoji name on ordinary ANSI code pages.
        # Exercise both Unicode directories and the actual link file name.
        with tempfile.TemporaryDirectory(prefix="nearroute-unicode-link-") as directory:
            folder = Path(directory) / "近舟 🚣"
            folder.mkdir()
            script = folder / "launch.ps1"
            script.write_text("# fixture\n", encoding="utf-8")
            target = Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe"
            arguments = subprocess.list2cmdline([
                "-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden",
                "-File", str(script), "-Mode", "dashboard"])
            link = folder / "近舟 NearRoute 🚣.lnk"
            app.create_shortcut(link, target, arguments, folder, app.ICON_PATH)
            app.configure_shortcut(link)
            before = link.read_bytes()
            values = app.read_shortcut(link)
            self.assertEqual(values["target"].casefold(), str(target).casefold())
            self.assertEqual(values["arguments"], arguments)
            self.assertTrue(app.owns_shortcut(link, target, script))
            self.assertEqual(link.read_bytes(), before, "ownership checks must not rewrite the link")
            with self.assertRaises(FileExistsError):
                app.create_shortcut(link, target, arguments, folder, app.ICON_PATH)

    def test_ownership_checks_executable_and_actual_file_not_quoted_mentions(self):
        target = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
        script = r"C:\NearRoute app\launch.ps1"
        prefix = ["-NoProfile", "-ExecutionPolicy", "Bypass", "-WindowStyle", "Hidden", "-File"]
        examples = [
            ([*prefix, script, "-Mode", "dashboard"], True),
            ([*[value.upper() for value in prefix], script.upper()], True),
            (["-Command", "Write-Output", script], False),
            ([*prefix, r"C:\Other app\launch.ps1", script], False),
            (["-ExecutionPolicy", "-File", script], False),
            (["-File"], False),
        ]
        for tokens, expected in examples:
            with self.subTest(tokens=tokens), patch.object(app, "read_shortcut", return_value={
                    "target": target, "arguments": subprocess.list2cmdline(tokens)}):
                self.assertEqual(app.owns_shortcut("fixture.lnk", target, script), expected)
        with patch.object(app, "read_shortcut", return_value={
                "target": r"\\unrelated-server\share\powershell.exe",
                "arguments": subprocess.list2cmdline([*prefix, script])}), \
             patch.object(app.os.path, "samefile", side_effect=AssertionError("must not resolve target")):
            self.assertFalse(app.owns_shortcut("fixture.lnk", target, script))

    def test_isolated_process_and_hidden_tk_window_have_matching_identity(self):
        # AppID is process-global, so verify it in a child that owns only a
        # temporary withdrawn Tk window and exits after releasing all resources.
        code = '''
import ctypes
import tkinter as tk
import windows_app as a
a.configure_taskbar()
api = a._NativeAPI()
api.shell.GetCurrentProcessExplicitAppUserModelID.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
api.shell.GetCurrentProcessExplicitAppUserModelID.restype = a.HRESULT
api.ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
api.ole.CoTaskMemFree.restype = None
identity = ctypes.c_void_p()
a._check(api.shell.GetCurrentProcessExplicitAppUserModelID(ctypes.byref(identity)), "get identity")
try:
    assert ctypes.wstring_at(identity) == a.APP_ID
finally:
    api.ole.CoTaskMemFree(identity)
root = tk.Tk()
root.withdraw()
try:
    a.configure_window(root)
    def values():
        result = {}
        with a._com(api), a._window_store(api, a._window_handle(root, api)) as store:
            for key_id in a.PROPERTY_IDS:
                key = a.PROPERTYKEY(a._APP_FMTID, key_id)
                value = a.PROPVARIANT()
                try:
                    a._check(store._method(5, a.HRESULT, ctypes.POINTER(a.PROPERTYKEY),
                             ctypes.POINTER(a.PROPVARIANT))(store.pointer, ctypes.byref(key), ctypes.byref(value)), "get")
                    result[key_id] = ctypes.wstring_at(value.value.pointer) if value.vt == a.VT_LPWSTR else None
                finally:
                    api.PropVariantClear(ctypes.byref(value))
        return result
    assert values() == a.relaunch_properties()
    a.clear_window(root)
    assert values() == {key_id: None for key_id in a.PROPERTY_IDS}
finally:
    try:
        a.clear_window(root)
    finally:
        root.destroy()
'''
        result = subprocess.run([app.sys.executable, "-B", "-c", code], cwd=app.ROOT,
                                capture_output=True, text=True, timeout=20,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_temporary_shortcut_retains_own_identity_icon_and_relaunch_properties(self):
        # Create only a temporary test .lnk, never the user's desktop/pinned link.
        with tempfile.TemporaryDirectory(prefix="nearroute-taskbar-") as directory:
            shortcut = Path(directory) / "test.lnk"
            script = "$w=New-Object -ComObject WScript.Shell; $s=$w.CreateShortcut($env:NEARROUTE_TEST_LINK); $s.TargetPath=$env:ComSpec; $s.Save(); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($s); [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($w)"
            subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                           env=dict(os.environ, NEARROUTE_TEST_LINK=str(shortcut)), check=True,
                           capture_output=True, timeout=20,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            app.configure_shortcut(shortcut)
            api = app._NativeAPI()
            with app._com(api):
                pointer = ctypes.c_void_p()
                app._check(api.SHGetPropertyStoreFromParsingName(str(shortcut), None, 0,
                           ctypes.byref(app.IID_PROPERTY_STORE), ctypes.byref(pointer)), "test read")
                store = app._PropertyStore(pointer)
                try:
                    for key_id, expected in app.relaunch_properties().items():
                        key = app.PROPERTYKEY(app._APP_FMTID, key_id)
                        value = app.PROPVARIANT()
                        try:
                            app._check(store._method(5, app.HRESULT, ctypes.POINTER(app.PROPERTYKEY),
                                       ctypes.POINTER(app.PROPVARIANT))(store.pointer, ctypes.byref(key), ctypes.byref(value)), "test get")
                            self.assertEqual(value.vt, app.VT_LPWSTR)
                            self.assertEqual(ctypes.wstring_at(value.value.pointer), expected)
                        finally:
                            api.PropVariantClear(ctypes.byref(value))
                finally:
                    store.close()


if __name__ == "__main__":
    unittest.main()
