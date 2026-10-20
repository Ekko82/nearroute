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
from unittest.mock import patch

import updater
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
        self.assertFalse(updater.read_update_result(self.root)["success"])
        self.assertEqual(stop.call_args.args[0][-1], "stop")
        self.assertEqual(Path(restart.call_args.args[0][-1]).resolve(), (self.root / "dashboard.py").resolve())
        self.assertFalse(updater.is_installing(self.root))


if __name__ == "__main__":
    unittest.main()
