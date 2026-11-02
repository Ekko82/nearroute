"""No-Git source updates verify the old release before switching to ZIP updates."""

import hashlib
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
import zipfile

import updater
from tests.test_git_updates import Download, FAKE_CORE, FAKE_DASHBOARD
from tools.build_release import RELEASE_FILES


class SourcePackageFixture:
    def __init__(self, legacy=False):
        self.temporary = tempfile.TemporaryDirectory(prefix="nearroute-source-package-")
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "installed"
        self.root.mkdir()
        self.files = {}
        for name in RELEASE_FILES:
            if legacy and name in {"codex_transport.py", "codex_history.py", "toml_compat.py"}:
                continue
            self.files[name] = b"official release file\n"
            if name.endswith(".py"):
                self.files[name] = b"OFFICIAL = True\n"
            elif name == "config.example.json":
                self.files[name] = b'{"enabled":false}\n'
            elif name == "assets/nearroute.ico":
                self.files[name] = b"\x00official icon\xff"
        self.files["optimizer.py"] = FAKE_CORE.encode()
        self.files["dashboard.py"] = FAKE_DASHBOARD.encode()
        self.files["VERSION"] = b"0.0.1\n"
        self.old, self.old_archive = self.package("0.0.1", self.files)
        for name, content in self.files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        self.target_files = self.files | {
            "VERSION": b"0.0.2\n", "tray_icon.py": b"OFFICIAL = 'new'\n",
            "codex_transport.py": b"OFFICIAL = True\n", "codex_history.py": b"OFFICIAL = True\n",
            "toml_compat.py": b"OFFICIAL = True\n", "new_feature.py": b"NEW_FEATURE = True\n",
        }
        self.release, self.archive = self.package("0.0.2", self.target_files)
        self.git = self.root / ".git"
        self.git.mkdir()
        (self.git / "HEAD").write_bytes(b"ref: refs/heads/main\n")
        (self.git / "config").write_bytes(b"preserved source metadata\n")
        self.user_files = {
            "config.json": b'{"enabled":false,"user_note":"keep"}',
            "state/history.json": b'{"keep":true}',
            "state/update-settings.json": b'{"auto_check":false,"mode":"notify"}',
            "tests/local_note.py": b"LOCAL_SOURCE = True\n",
            ".gitignore": b"/state/\n/config.json\n",
        }
        for name, content in self.user_files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)

    def close(self):
        self.temporary.cleanup()

    def package(self, version, files):
        archive = self.base / f"NearRoute-{version}-windows.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            for name, content in files.items():
                package.writestr(name, content)
        content = archive.read_bytes()
        tag = "v" + version
        release = updater.Release(version, tag, f"https://github.com/{updater.REPOSITORY}/releases/tag/{tag}",
                                  "offline fixture", f"https://github.com/{updater.REPOSITORY}/releases/download/{tag}/{archive.name}",
                                  hashlib.sha256(content).hexdigest(), len(content))
        return release, archive

    def open(self, url):
        tag_url = f"https://api.github.com/repos/{updater.REPOSITORY}/releases/tags/{self.old.tag}"
        manifest_url = self.old.asset_url.rsplit("/", 1)[0] + "/nearroute-update.json"
        if url == tag_url:
            data = {"tag_name": self.old.tag, "html_url": self.old.url, "assets": [
                {"name": self.old_archive.name, "browser_download_url": self.old.asset_url, "size": self.old.size},
                {"name": "nearroute-update.json", "browser_download_url": manifest_url, "size": 200},
            ]}
            return Download(json.dumps(data).encode(), url)
        if url == manifest_url:
            data = {"schema": 1, "version": self.old.version, "asset": self.old_archive.name,
                    "sha256": self.old.sha256, "size": self.old.size}
            return Download(json.dumps(data).encode(), url)
        for release, archive in ((self.old, self.old_archive), (self.release, self.archive)):
            if url == release.asset_url:
                return Download(archive.read_bytes(), url)
        raise AssertionError("unexpected external request: " + url)

    def prepare(self):
        with patch.object(updater, "_open_url", side_effect=self.open):
            return updater.prepare_update(self.release, self.root)

    def assert_preserved(self, case):
        for name, content in self.user_files.items():
            case.assertEqual((self.root / name).read_bytes(), content, name)


class SourcePackageUpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = SourcePackageFixture()
        self.addCleanup(self.fixture.close)
        environment = patch.dict(os.environ, {"PATH": ""})
        environment.start()
        self.addCleanup(environment.stop)
        self.assertIsNone(shutil.which("git"), "this fixture must exercise a machine without Git")

    def test_verified_source_transitions_then_accepts_another_package_update_without_git(self):
        f = self.fixture
        self.assertTrue(updater.requires_package_migration(f.root))
        self.assertTrue(updater.can_update(f.root))
        prepared = f.prepare()
        self.assertTrue((prepared / "source-package.json").is_file())
        self.assertTrue(f.git.is_dir(), "preparation must retain active source metadata")
        self.assertEqual(updater.current_version(f.root), f.old.version)
        with patch.object(updater, "_open_url", side_effect=AssertionError("apply must be offline")), \
             patch.object(updater.subprocess, "run", side_effect=AssertionError("no Git or helper process here")):
            metadata = updater.apply_source_package_update(prepared / "package.zip", f.release, f.root)
        self.assertFalse(f.git.exists())
        self.assertEqual((metadata / ".git" / "config").read_bytes(), b"preserved source metadata\n")
        self.assertTrue(metadata.is_relative_to(f.root / "state" / "updates"))
        self.assertTrue(updater.can_install(f.root))
        self.assertFalse(updater.requires_package_migration(f.root))
        for name, content in f.target_files.items():
            self.assertEqual((f.root / name).read_bytes(), content, name)
        f.assert_preserved(self)
        next_files = f.target_files | {"VERSION": b"0.0.3\n"}
        next_release, next_archive = f.package("0.0.3", next_files)
        with patch.object(updater, "_open_url", return_value=Download(next_archive.read_bytes(), next_release.asset_url)):
            next_prepared = updater.prepare_update(next_release, f.root)
        updater.apply_package(next_prepared / "package.zip", next_release, f.root)
        self.assertEqual(updater.current_version(f.root), "0.0.3")
        self.assertEqual((metadata / ".git" / "HEAD").read_bytes(), b"ref: refs/heads/main\n")
        f.assert_preserved(self)

    def test_completed_baseline_and_target_cache_need_no_repeat_download(self):
        f = self.fixture
        prepared = f.prepare()
        with patch.object(updater, "_open_url", side_effect=AssertionError("verified cache needs no network")):
            self.assertEqual(updater.prepare_update(f.release, f.root), prepared)
        f.assert_preserved(self)

    def test_legacy_baseline_can_omit_modules_added_by_the_new_release(self):
        f = SourcePackageFixture(legacy=True)
        self.addCleanup(f.close)
        with self.assertRaises(updater.UpdateError):
            updater.validate_package(f.old_archive, f.old)
        prepared = f.prepare()
        updater.apply_source_package_update(prepared / "package.zip", f.release, f.root)
        self.assertTrue((f.root / "codex_transport.py").is_file())
        self.assertEqual(updater.current_version(f.root), f.release.version)
        f.assert_preserved(self)

    def test_local_changes_are_preserved_and_not_mistaken_for_a_clean_source(self):
        f = self.fixture
        target = f.root / "tray_icon.py"
        local = b"LOCAL_CHANGE = True\n"
        target.write_bytes(local)
        with self.assertRaisesRegex(updater.UpdateError, "本地改动"):
            f.prepare()
        self.assertEqual(target.read_bytes(), local)
        self.assertTrue(f.git.is_dir())
        self.assertEqual(updater.current_version(f.root), f.old.version)
        f.assert_preserved(self)

    def test_apply_and_installer_handoff_recheck_changes_after_prepare(self):
        f = self.fixture
        prepared = f.prepare()
        target = f.root / "tray_icon.py"
        target.write_bytes(b"CHANGED_AFTER_DOWNLOAD = True\n")
        with patch.object(updater.subprocess, "Popen", side_effect=AssertionError("must not start installer")):
            with self.assertRaisesRegex(updater.UpdateError, "本地改动"):
                updater.start_installer(prepared, parent_pid=12345, python_exe=sys.executable, root=f.root)
        with self.assertRaisesRegex(updater.UpdateError, "本地改动"):
            updater.apply_source_package_update(prepared / "package.zip", f.release, f.root)
        self.assertTrue(f.git.is_dir())
        self.assertEqual(list((f.root / "state/updates").glob("source-metadata-*")), [])
        f.assert_preserved(self)

    def test_new_target_file_collisions_are_preserved_before_and_after_preparation(self):
        f = self.fixture
        collision = f.root / "new_feature.py"
        for stage in ("prepare", "apply"):
            with self.subTest(stage=stage):
                collision.unlink(missing_ok=True)
                if stage == "apply":
                    prepared = f.prepare()
                collision.write_bytes(b"MY_LOCAL_FILE = True\n")
                with self.assertRaisesRegex(updater.UpdateError, "冲突"):
                    if stage == "prepare":
                        f.prepare()
                    else:
                        updater.apply_source_package_update(prepared / "package.zip", f.release, f.root)
                self.assertEqual(collision.read_bytes(), b"MY_LOCAL_FILE = True\n")
                self.assertTrue(f.git.is_dir())
                self.assertEqual(updater.current_version(f.root), f.old.version)
                f.assert_preserved(self)

    def test_failed_replacement_rolls_back_runtime_and_restores_active_git_metadata(self):
        f = self.fixture
        prepared = f.prepare()
        replace = updater.os.replace

        def fail_dashboard(source, target):
            if Path(target) == f.root / "dashboard.py":
                raise PermissionError("fixture file in use")
            return replace(source, target)

        with patch.object(updater.os, "replace", side_effect=fail_dashboard):
            with self.assertRaisesRegex(updater.UpdateError, "已还原"):
                updater.apply_source_package_update(prepared / "package.zip", f.release, f.root)
        for name, content in f.files.items():
            self.assertEqual((f.root / name).read_bytes(), content, name)
        self.assertFalse((f.root / "new_feature.py").exists())
        self.assertEqual((f.git / "config").read_bytes(), b"preserved source metadata\n")
        self.assertEqual(list((f.root / "state/updates").glob("source-metadata-*")), [])
        f.assert_preserved(self)

    def test_windows_text_checkout_equivalence_does_not_hide_binary_or_content_changes(self):
        f = self.fixture
        for name, original in f.files.items():
            if name != "assets/nearroute.ico":
                (f.root / name).write_bytes(b"\xef\xbb\xbf" + original.replace(b"\n", b"\r\n"))
        prepared = f.prepare()
        (f.root / "assets/nearroute.ico").write_bytes(b"\x00changed icon\xff")
        with self.assertRaisesRegex(updater.UpdateError, "本地改动"):
            updater.apply_source_package_update(prepared / "package.zip", f.release, f.root)
        self.assertTrue(f.git.is_dir())
        (f.root / "assets/nearroute.ico").write_bytes(f.files["assets/nearroute.ico"])
        updater.apply_source_package_update(prepared / "package.zip", f.release, f.root)
        f.assert_preserved(self)

    def test_corrupt_cached_baseline_cannot_authorize_an_install(self):
        f = self.fixture
        prepared = f.prepare()
        baseline = f.root / "state/updates" / f.old.tag / "package.zip"
        baseline.write_bytes(b"not the verified baseline")
        with self.assertRaisesRegex(updater.UpdateError, "校验失败"):
            updater.apply_source_package_update(prepared / "package.zip", f.release, f.root)
        self.assertTrue(f.git.is_dir())
        self.assertEqual(updater.current_version(f.root), f.old.version)
        f.assert_preserved(self)

    def test_worktree_shared_metadata_and_ancestor_repositories_are_rejected(self):
        for kind in ("worktree-file", "shared", "linked-worktrees", "ancestor"):
            with self.subTest(kind=kind):
                f = SourcePackageFixture()
                try:
                    if kind == "worktree-file":
                        shutil.rmtree(f.git)
                        f.git.write_text("gitdir: ../shared-repository", encoding="utf-8")
                    elif kind == "shared":
                        (f.git / "commondir").write_text("../shared-repository", encoding="utf-8")
                    elif kind == "linked-worktrees":
                        (f.git / "worktrees").mkdir()
                    else:
                        (f.base / ".git").mkdir()
                    with patch.object(updater, "_open_url", side_effect=AssertionError("reject before network")):
                        with self.assertRaises(updater.UpdateError):
                            updater.prepare_update(f.release, f.root)
                    self.assertTrue(f.git.exists())
                    self.assertEqual(updater.current_version(f.root), f.old.version)
                    f.assert_preserved(self)
                finally:
                    f.close()

    @unittest.skipUnless(os.name == "nt", "Detached installer handoff targets Windows")
    def test_detached_worker_waits_stops_updates_archives_and_restarts_without_git(self):
        f = self.fixture
        prepared = f.prepare()
        gate = f.base / "parent-can-exit"
        parent = subprocess.Popen(
            [sys.executable, "-B", "-c", "import pathlib,sys,time\np=pathlib.Path(sys.argv[1])\nwhile not p.exists(): time.sleep(.02)\n", str(gate)],
            creationflags=updater.NO_WINDOW)
        child = None
        try:
            child = updater.start_installer(prepared, parent_pid=parent.pid, python_exe=sys.executable, root=f.root)
            deadline = time.monotonic() + 8
            while not updater.is_installing(f.root) and child.poll() is None and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(updater.is_installing(f.root))
            self.assertIsNone(parent.poll())
            self.assertTrue(f.git.is_dir())
            self.assertEqual(updater.current_version(f.root), f.old.version)
            gate.touch()
            parent.wait(timeout=5)
            child.wait(timeout=20)
            self.assertEqual(child.returncode, 0)
            result = updater.read_update_result(f.root)
            self.assertTrue(result.get("success"), result)
            self.assertEqual(updater.current_version(f.root), f.release.version)
            self.assertTrue((f.root / "state/fixture-stopped.txt").is_file())
            self.assertFalse(f.git.exists())
            archived = list((f.root / "state/updates").glob("source-metadata-*/.git/config"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(archived[0].read_bytes(), b"preserved source metadata\n")
            restarted = f.root / "state/restarted.txt"
            deadline = time.monotonic() + 5
            while not restarted.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertTrue(restarted.is_file())
            restarted_pid = int(restarted.read_text())
            updater._wait_for_parent(restarted_pid, timeout=5)
            self.assertFalse(updater.is_installing(f.root))
            self.assertFalse((f.root / "state/fixture-helper.lock").exists())
            self.assertFalse(Path(child.args[2]).parent.exists())
            f.assert_preserved(self)
        finally:
            gate.touch()
            for process in (child, parent):
                if process is not None:
                    if process.poll() is None:
                        process.terminate()
                    process.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
