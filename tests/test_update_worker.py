"""Exercise the detached installer using only an isolated, headless installation."""

from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import updater
from tests.test_git_updates import GitUpdateFixture
from tools.build_release import build_release, RELEASE_FILES


class UpdateWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nearroute-worker-test-")
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "source"
        self.root = self.base / "installed"
        for name in RELEASE_FILES:
            target = self.source / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(updater.ROOT / name, target)
        (self.source / "dashboard.py").write_text(
            "from pathlib import Path\nimport os\n"
            "(Path(__file__).parent / 'state' / 'restarted.txt').write_text(str(os.getpid()), encoding='utf-8')\n",
            encoding="utf-8")
        shutil.copytree(self.source, self.root)
        (self.root / "VERSION").write_text("0.0.0\n", encoding="utf-8")
        (self.root / "config.json").write_text('{"enabled":false,"user_note":"keep"}', encoding="utf-8")
        (self.root / "state").mkdir()
        (self.root / "state" / "history.json").write_text('{"keep":true}', encoding="utf-8")
        metadata = build_release(self.source, self.base / "dist")
        version = metadata["version"]
        self.release = updater.Release(version, "v" + version,
            f"https://github.com/Ekko82/nearroute/releases/tag/v{version}", "offline fixture",
            f"https://github.com/Ekko82/nearroute/releases/download/v{version}/{metadata['asset']}",
            metadata["sha256"], metadata["size"])
        self.prepared = self.root / "state" / "updates" / self.release.tag
        self.prepared.mkdir(parents=True)
        shutil.copy2(self.base / "dist" / metadata["asset"], self.prepared / "package.zip")
        (self.prepared / "release.json").write_text(json.dumps(asdict(self.release)), encoding="utf-8")

    @unittest.skipUnless(os.name == "nt", "Installer handoff uses the Windows helper mutex")
    def test_detached_worker_waits_for_parent_updates_and_restarts_without_a_gui(self):
        gate = self.base / "parent-can-exit"
        parent_code = (
            "import pathlib,sys,time\n"
            "p=pathlib.Path(sys.argv[1])\n"
            "while not p.exists(): time.sleep(.02)\n"
        )
        parent = subprocess.Popen([sys.executable, "-B", "-c", parent_code, str(gate)],
                                  creationflags=updater.NO_WINDOW)
        child = None
        try:
            child = updater.start_installer(self.prepared, parent_pid=parent.pid,
                                           python_exe=sys.executable, root=self.root)
            deadline = time.monotonic() + 8
            while not updater.is_installing(self.root) and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(updater.is_installing(self.root), "installer never entered its guarded handoff")
            self.assertIsNone(parent.poll())
            self.assertEqual(updater.current_version(self.root), "0.0.0")
            gate.touch()
            parent.wait(timeout=5)
            child.wait(timeout=15)
            self.assertEqual(child.returncode, 0)
            result = updater.read_update_result(self.root)
            self.assertTrue(result.get("success"), result)
            self.assertEqual(updater.current_version(self.root), self.release.version)
            self.assertEqual((self.root / "config.json").read_text(), '{"enabled":false,"user_note":"keep"}')
            self.assertEqual((self.root / "state" / "history.json").read_text(), '{"keep":true}')
            restarted = self.root / "state" / "restarted.txt"
            deadline = time.monotonic() + 5
            while not restarted.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(restarted.exists())
            updater._wait_for_parent(int(restarted.read_text()), timeout=5)
            self.assertFalse(updater.is_installing(self.root))
            self.assertFalse(Path(child.args[2]).parent.exists(), "detached worker left its temporary code behind")
        finally:
            gate.touch()
            for process in (child, parent):
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)

    def test_failed_stop_preserves_old_program_and_reopens_it_with_a_result(self):
        with patch.object(updater, "_wait_for_parent"), \
             patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 1)) as stop, \
             patch.object(updater.subprocess, "Popen") as restart:
            updater._run_installer(self.prepared, self.root, 12345, "test-python")
        self.assertEqual(updater.current_version(self.root), "0.0.0")
        result = updater.read_update_result(self.root)
        self.assertFalse(result["success"])
        self.assertEqual(result.get("version"), self.release.version)
        self.assertEqual(stop.call_args.args[0][-1], "stop")
        restart.assert_called_once()
        self.assertEqual(Path(restart.call_args.args[0][-1]).resolve(), (self.root / "dashboard.py").resolve())
        self.assertFalse(updater.is_installing(self.root))

    def test_failed_install_records_target_version_and_releases_locks(self):
        core = Mock()
        helper_lock = core.InstanceLock.return_value
        helper_lock.acquire.return_value = True
        with patch.object(updater, "_wait_for_parent"), \
             patch.object(updater.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)), \
             patch.object(updater.subprocess, "Popen") as restart, \
             patch.object(updater.importlib.util, "spec_from_file_location", return_value=Mock()), \
             patch.object(updater.importlib.util, "module_from_spec", return_value=core), \
             patch.object(updater, "apply_package", side_effect=updater.UpdateError("程序文件正被占用，已还原旧版程序")) as apply:
            updater._run_installer(self.prepared, self.root, 12345, "test-python")
        apply.assert_called_once_with(self.prepared / "package.zip", self.release, self.root.resolve())
        self.assertEqual(updater.current_version(self.root), "0.0.0")
        result = updater.read_update_result(self.root)
        self.assertFalse(result["success"])
        self.assertEqual(result.get("version"), self.release.version)
        self.assertIn("已还原旧版程序", result["message"])
        helper_lock.close.assert_called_once()
        self.assertFalse(updater.is_installing(self.root))
        restart.assert_called_once()
        self.assertEqual(Path(restart.call_args.args[0][-1]).resolve(), (self.root / "dashboard.py").resolve())


@unittest.skipUnless(os.name == "nt" and shutil.which("git"), "Windows installer with Git")
class GitUpdateWorkerTests(unittest.TestCase):
    def test_detached_git_worker_waits_updates_and_restarts_the_fake_dashboard(self):
        fixture = GitUpdateFixture()
        self.addCleanup(fixture.close)
        prepared = fixture.prepare()
        gate = fixture.base / "parent-can-exit"
        parent = subprocess.Popen(
            [sys.executable, "-B", "-c",
             "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile not p.exists(): time.sleep(.02)\n", str(gate)],
            creationflags=updater.NO_WINDOW)
        child = None
        try:
            child = updater.start_installer(prepared, parent_pid=parent.pid,
                                           python_exe=sys.executable, root=fixture.root)
            deadline = time.monotonic() + 8
            while not updater.is_installing(fixture.root) and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(updater.is_installing(fixture.root), "Git worker did not acquire the install lock")
            self.assertIsNone(parent.poll())
            self.assertEqual(fixture.git(fixture.root, "rev-parse", "HEAD"), fixture.old)
            gate.touch()
            parent.wait(timeout=5)
            child.wait(timeout=20)
            self.assertEqual(child.returncode, 0)
            result = updater.read_update_result(fixture.root)
            self.assertTrue(result.get("success"), result)
            self.assertEqual(result.get("version"), fixture.release.version)
            self.assertEqual(fixture.git(fixture.root, "rev-parse", "HEAD"), fixture.target)
            fixture.assert_preserved(self)
            self.assertTrue((fixture.root / "state" / "fixture-stopped.txt").exists())
            restarted = fixture.root / "state" / "restarted.txt"
            deadline = time.monotonic() + 5
            while not restarted.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(restarted.exists(), "updated fake dashboard was not restarted")
            updater._wait_for_parent(int(restarted.read_text()), timeout=5)
            self.assertFalse(updater.is_installing(fixture.root))
            self.assertFalse((fixture.root / "state" / "fixture-helper.lock").exists())
            self.assertFalse(Path(child.args[2]).parent.exists(), "temporary updater was not removed")
        finally:
            gate.touch()
            for process in (child, parent):
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
