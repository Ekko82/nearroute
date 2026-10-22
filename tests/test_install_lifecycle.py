"""Opt-in Windows installer smoke tests in a temporary app and registry key.

Run with NEARROUTE_INSTALL_SMOKE=1. No actual Run key, desktop shortcut, daemon,
or controller is touched; the optimizer in the fixture only records commands.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
import uuid

if os.name == "nt":
    import winreg


ROOT = Path(__file__).resolve().parents[1]
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


@unittest.skipUnless(os.name == "nt" and os.environ.get("NEARROUTE_INSTALL_SMOKE") == "1",
                     "set NEARROUTE_INSTALL_SMOKE=1 for isolated Windows installer smoke")
class InstallLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="nearroute-install-test-")
        self.addCleanup(self.temp.cleanup)
        # Emoji is outside legacy ANSI code pages on both Chinese and English
        # Windows, covering creation as well as reopening of Unicode .lnk paths.
        self.app = Path(self.temp.name).resolve() / "NearRoute app 🚣"
        self.app.mkdir()
        self.desktop = Path(self.temp.name).resolve() / "Desktop fixture 🚣"
        self.desktop.mkdir()
        self.reg_key = r"Software\NearRouteInstallTest_" + uuid.uuid4().hex
        self.addCleanup(self.remove_registry_fixture)
        self.powershell = str(Path(os.environ["SystemRoot"]) / "System32/WindowsPowerShell/v1.0/powershell.exe")
        self.python = Path(sys.executable)
        self.pythonw = self.python.with_name("pythonw.exe")
        if not self.pythonw.is_file():
            self.skipTest("the interpreter needs its pythonw.exe sibling")
        for name in ("install.ps1", "launch.ps1", "uninstall.ps1"):
            text = (ROOT / name).read_text(encoding="utf-8-sig")
            if name in ("install.ps1", "uninstall.ps1"):
                self.assertIn(RUN_KEY, text, "installer registry isolation needs updating")
            if name == "install.ps1":
                self.assertIn("[Environment]::GetFolderPath('Desktop')", text,
                              "installer desktop isolation needs updating")
            text = text.replace(RUN_KEY, self.reg_key)
            text = text.replace("[Environment]::GetFolderPath('Desktop')",
                                "'" + str(self.desktop).replace("'", "''") + "'")
            self.assertNotIn(RUN_KEY, text, "the real startup key must never enter the fixture")
            (self.app / name).write_text(text, encoding="utf-8-sig")
        for name in ("windows_app.py", "updater.py", "VERSION", "config.example.json"):
            shutil.copy2(ROOT / name, self.app / name)
        (self.app / "assets").mkdir()
        shutil.copy2(ROOT / "assets/nearroute.ico", self.app / "assets/nearroute.ico")
        (self.app / "dashboard.py").write_text("# Test fixture, never launched.\n", encoding="utf-8")
        (self.app / "tray_icon.py").write_text("# Installer presence check only.\n", encoding="utf-8")
        (self.app / "optimizer.py").write_text(
            "from pathlib import Path\nimport sys\n"
            "with (Path(__file__).parent / 'commands.txt').open('a') as f:\n"
            "    f.write(sys.argv[1] + '\\n')\n", encoding="utf-8")
        self.shortcut = self.desktop / "近舟 NearRoute.lnk"
        self.state = self.app / "state/install-state.json"

    def remove_registry_fixture(self):
        # The fixture creates a single flat key, never the Windows Run key.
        assert self.reg_key.startswith("Software\\NearRouteInstallTest_")
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, self.reg_key)
        except FileNotFoundError:
            pass

    def run_script(self, name, *args, success=True):
        command = [self.powershell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                   str(self.app / name), *map(str, args)]
        # Python may inherit PowerShell 7's module paths from the test runner.
        # Let Windows PowerShell initialize its own inbox module paths.
        environment = {key: value for key, value in os.environ.items()
                       if key.lower() != "psmodulepath"}
        result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                timeout=45, creationflags=subprocess.CREATE_NO_WINDOW,
                                env=environment)
        expected_result = result.returncode == 0 if success else result.returncode != 0
        if not expected_result:
            self.fail(result.stdout + result.stderr + "\nFixture shortcut: " +
                      json.dumps(self.shortcut_diagnostics(), ensure_ascii=True))
        return result

    def shortcut_diagnostics(self):
        """Read this test's .lnk through the same Unicode API as the installer."""
        result = subprocess.run([str(self.python), "-B", str(self.app / "windows_app.py"),
                                 "--read-shortcut", str(self.shortcut)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=15, creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            values = json.loads(result.stdout)
            return values | {"expected_target": self.powershell,
                             "expected_file": str(self.app / "launch.ps1"),
                             "shortcut": str(self.shortcut)}
        except ValueError:
            return {"diagnostic_exit_code": result.returncode, "fixture": str(self.app)}

    def assert_owned_shortcut(self):
        details = self.shortcut_diagnostics()
        diagnostic = json.dumps(details, ensure_ascii=True)
        self.assertEqual(details.get("target", "").casefold(), self.powershell.casefold(), diagnostic)
        self.assertIn('"' + str(self.app / "launch.ps1") + '"', details.get("arguments", ""), diagnostic)
        result = subprocess.run([str(self.python), "-B", str(self.app / "windows_app.py"),
                                 "--check-shortcut", str(self.shortcut), "--target", self.powershell,
                                 "--script", str(self.app / "launch.ps1")],
                                capture_output=True, timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        self.assertEqual(result.returncode, 0, diagnostic)

    def run_value(self):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.reg_key) as key:
                return winreg.QueryValueEx(key, "CodexNodeOptimizer")[0]
        except FileNotFoundError:
            return None

    def move_application(self):
        base = Path(self.temp.name).resolve()
        source = self.app.resolve()
        destination = (base / "Moved NearRoute app 🚣").resolve()
        for path in (source, destination):
            self.assertNotEqual(path, base)
            self.assertTrue(path.is_relative_to(base), "fixture move must stay inside its temporary directory")
        source.rename(destination)
        self.app = destination
        self.state = destination / "state/install-state.json"
        return source

    @contextmanager
    def lock_shortcut_against_replacement(self):
        api = ctypes.WinDLL("kernel32", use_last_error=True)
        api.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                   ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        api.CreateFileW.restype = wintypes.HANDLE
        api.CloseHandle.argtypes = [wintypes.HANDLE]
        api.CloseHandle.restype = wintypes.BOOL
        handle = api.CreateFileW(str(self.shortcut), 0x80000000, 3, None, 3, 0, None)
        if handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not api.CloseHandle(handle):
                raise ctypes.WinError(ctypes.get_last_error())

    def test_failed_reinstall_preserves_prior_record_and_can_be_retried(self):
        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python)
        before_state = self.state.read_bytes()
        before_run = self.run_value()
        before_shortcut = self.shortcut.read_bytes()
        with self.lock_shortcut_against_replacement():
            self.run_script("install.ps1", "-NoStart", "-PythonExe", self.pythonw, success=False)
        self.assertEqual(self.state.read_bytes(), before_state)
        self.assertEqual(self.run_value(), before_run)
        self.assertEqual(self.shortcut.read_bytes(), before_shortcut)
        self.assertEqual(list((self.app / "state").glob(".nearroute-*.lnk")), [])
        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.pythonw)
        self.assertEqual(json.loads(self.state.read_text(encoding="utf-8-sig"))["python_exe"], str(self.pythonw))

    def test_failed_install_without_record_leaves_no_startup_or_false_record(self):
        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python)
        # Establish that the installer recognizes the metadata it just wrote.
        # Otherwise a rejection while locked would not exercise rollback at all.
        self.assert_owned_shortcut()
        # An installer-owned shortcut may remain after an interrupted old setup.
        self.state.unlink()
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.reg_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, "CodexNodeOptimizer")
        before_shortcut = self.shortcut.read_bytes()
        with self.lock_shortcut_against_replacement():
            failure = self.run_script("install.ps1", "-NoStart", "-PythonExe", self.pythonw, success=False)
        self.assertIn("MoveItemCommand", failure.stderr, failure.stdout + failure.stderr + "\n" +
                      json.dumps(self.shortcut_diagnostics(), ensure_ascii=True))
        self.assertFalse(self.state.exists())
        self.assertIsNone(self.run_value())
        self.assertEqual(self.shortcut.read_bytes(), before_shortcut)
        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.pythonw)

    def test_install_and_uninstall_are_repeatable(self):
        for _ in range(2):
            self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python)
        self.assertTrue(self.shortcut.is_file())
        self.assertIsNotNone(self.run_value())
        for _ in range(2):
            self.run_script("uninstall.ps1", "-PythonExe", self.python)
            self.assertFalse(self.shortcut.exists())
            self.assertIsNone(self.run_value())
            self.assertFalse(json.loads(self.state.read_text(encoding="utf-8-sig"))["installed"])
        self.assertEqual((self.app / "commands.txt").read_text().splitlines(),
                         ["stop", "restore", "stop", "restore"])

    def test_uninstall_without_python_still_removes_owned_startup(self):
        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python)
        self.run_script("uninstall.ps1", "-PythonExe", self.app / "missing-python.exe")
        self.assertFalse(self.shortcut.exists())
        self.assertIsNone(self.run_value())
        self.assertTrue((self.app / "state/stop.request").is_file())
        self.assertFalse(json.loads(self.state.read_text(encoding="utf-8-sig"))["installed"])

    def test_uninstalled_application_can_move_and_install_without_losing_configuration(self):
        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python)
        config = self.app / "config.json"
        original_config = config.read_bytes() + b"\n"
        config.write_bytes(original_config)
        old_root = self.app
        self.assertIn(str(old_root / "launch.ps1"), self.run_value())

        self.run_script("uninstall.ps1", "-NoRestore", "-PythonExe", self.python)
        self.assertIsNone(self.run_value())
        self.assertFalse(self.shortcut.exists())
        self.assertFalse(json.loads(self.state.read_text(encoding="utf-8-sig"))["installed"])
        self.move_application()

        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python)

        state = json.loads(self.state.read_text(encoding="utf-8-sig"))
        self.assertTrue(state["installed"])
        self.assertEqual(state["root"], str(self.app))
        self.assertEqual((self.app / "config.json").read_bytes(), original_config)
        self.assertIn(str(self.app / "launch.ps1"), self.run_value())
        self.assertNotIn(str(old_root / "launch.ps1"), self.run_value())
        self.assert_owned_shortcut()
        self.run_script("uninstall.ps1", "-NoRestore", "-PythonExe", self.python)
        self.assertIsNone(self.run_value())
        self.assertFalse(self.shortcut.exists())
        self.assertEqual((self.app / "config.json").read_bytes(), original_config)

    def test_moved_active_install_cannot_claim_old_startup_or_shortcut(self):
        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python)
        state = json.loads(self.state.read_text(encoding="utf-8-sig"))
        before_run = self.run_value()
        before_shortcut = self.shortcut.read_bytes()
        self.move_application()

        for installed in (True, None, "false", "missing"):
            with self.subTest(installed=installed):
                candidate = state | {"installed": installed}
                if installed == "missing":
                    candidate.pop("installed")
                self.state.write_text(json.dumps(candidate), encoding="utf-8")
                before_state = self.state.read_bytes()
                self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python, success=False)
                self.assertEqual(self.state.read_bytes(), before_state)
                self.assertEqual(self.run_value(), before_run)
                self.assertEqual(self.shortcut.read_bytes(), before_shortcut)

    def test_uninstall_preserves_user_modified_startup_and_shortcut(self):
        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python)
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, self.reg_key, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "CodexNodeOptimizer", 0, winreg.REG_SZ, "user-owned-command")
        with self.shortcut.open("ab") as out:
            out.write(b"user edit")
        modified_shortcut = self.shortcut.read_bytes()
        self.run_script("uninstall.ps1", "-NoRestore", "-PythonExe", self.python)
        self.assertEqual(self.run_value(), "user-owned-command")
        self.assertEqual(self.shortcut.read_bytes(), modified_shortcut)

    def test_failed_uninstall_can_be_retried_after_shortcut_is_released(self):
        self.run_script("install.ps1", "-NoStart", "-PythonExe", self.python)
        with self.lock_shortcut_against_replacement():
            self.run_script("uninstall.ps1", "-NoRestore", "-PythonExe", self.python, success=False)
        self.assertTrue(json.loads(self.state.read_text(encoding="utf-8-sig"))["installed"])
        self.run_script("uninstall.ps1", "-NoRestore", "-PythonExe", self.python)
        self.assertFalse(self.shortcut.exists())
        self.assertIsNone(self.run_value())
        self.assertFalse(json.loads(self.state.read_text(encoding="utf-8-sig"))["installed"])


if __name__ == "__main__":
    unittest.main()
