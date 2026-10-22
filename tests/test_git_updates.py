"""Release-tag updates against disposable Git repositories; no external network."""

from dataclasses import replace
import hashlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import updater
from tools.build_release import build_release, RELEASE_FILES


OFFICIAL = "https://github.com/Ekko82/nearroute.git"
FAKE_CORE = """from pathlib import Path
import os
import sys
ROOT = Path(__file__).resolve().parent
class InstanceLock:
    def __init__(self):
        self.path = ROOT / 'state' / 'fixture-helper.lock'
        self.fd = None
    def acquire(self):
        try:
            self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            return True
        except FileExistsError:
            return False
    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            self.path.unlink()
if __name__ == '__main__':
    assert sys.argv[1:] == ['stop']
    (ROOT / 'state' / 'fixture-stopped.txt').write_text('stopped')
"""
FAKE_DASHBOARD = """from pathlib import Path
import os
root = Path(__file__).resolve().parent
(root / 'state' / 'restarted.txt').write_text(str(os.getpid()))
"""


class Download(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


class GitUpdateFixture:
    def __init__(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="nearroute-git-update-")
        self.base = Path(self.temporary.name).resolve()
        self.environment = patch.dict(os.environ, {
            **{k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
            "GIT_CONFIG_GLOBAL": str(self.base / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "never",
        }, clear=True)
        self.environment.start()
        try:
            self.source, self.root, self.remote = (self.base / name for name in ("source", "installed", "remote.git"))
            self.source.mkdir()
            self.git(self.base, "config", "--global", "user.name", "NearRoute fixture")
            self.git(self.base, "config", "--global", "user.email", "fixture@example.invalid")
            self.git(self.base, "config", "--global", "core.autocrlf", "true")
            self.git(self.base, "config", "--global", "url." + self.remote.as_uri() + ".insteadOf", OFFICIAL)
            self.git(self.source, "init", "-b", "main")
            self.git(self.source, "config", "core.autocrlf", "false")
            for name in RELEASE_FILES:
                target = self.source / name
                target.parent.mkdir(parents=True, exist_ok=True)
                content = b"fixture release file\n"
                if name.endswith(".py"):
                    content = b"RELEASE_MARKER = 'fixture'\n"
                elif name == "config.example.json":
                    content = b'{"enabled":false}\n'
                target.write_bytes(content)
            (self.source / "optimizer.py").write_bytes(FAKE_CORE.encode())
            (self.source / "dashboard.py").write_bytes(FAKE_DASHBOARD.encode())
            (self.source / ".gitignore").write_bytes(b"/state/\n/config.json\n/ignored-note.txt\n")
            (self.source / "VERSION").write_bytes(b"0.0.1\n")
            self.commit(self.source, "old release")
            self.old = self.git(self.source, "rev-parse", "HEAD")
            self.git(self.source, "tag", "-a", "v0.0.1", "-m", "old release")
            (self.source / "VERSION").write_bytes(b"0.0.2\n")
            (self.source / "tray_icon.py").write_bytes(b"RELEASE_MARKER = 'new'\n")
            (self.source / "new-note.txt").write_bytes(b"new tracked file\n")
            self.commit(self.source, "release target")
            self.target = self.git(self.source, "rev-parse", "HEAD")
            self.git(self.source, "tag", "-a", "v0.0.2", "-m", "release target")
            metadata = build_release(self.source, self.base / "dist")
            self.archive = self.base / "dist" / metadata["asset"]
            self.release = updater.Release("0.0.2", "v0.0.2",
                "https://github.com/Ekko82/nearroute/releases/tag/v0.0.2", "offline fixture",
                "https://github.com/Ekko82/nearroute/releases/download/v0.0.2/" + metadata["asset"],
                metadata["sha256"], metadata["size"])
            (self.source / "VERSION").write_bytes(b"0.0.3\n")
            self.commit(self.source, "unreleased main")
            self.latest = self.git(self.source, "rev-parse", "HEAD")
            self.git(self.base, "clone", "--bare", str(self.source), str(self.remote))
            self.git(self.base, "clone", "--branch", "v0.0.1", str(self.remote), str(self.root))
            self.git(self.root, "checkout", "-b", "main")
            self.git(self.root, "remote", "set-url", "origin", OFFICIAL)
            (self.root / "config.json").write_bytes(b'{"user_note":"preserve"}\n')
            (self.root / "state").mkdir()
            (self.root / "state" / "private.json").write_bytes(b'{"private":"preserve"}\n')
        except BaseException:
            self.close()
            raise

    def close(self):
        self.environment.stop()
        self.temporary.cleanup()

    def git(self, directory, *args):
        directory = Path(directory).resolve()
        if not directory.is_relative_to(self.base):
            raise AssertionError("Git fixture command escaped its temporary directory")
        result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True,
                                text=True, encoding="utf-8", timeout=15, creationflags=updater.NO_WINDOW)
        if result.returncode:
            raise AssertionError(f"fixture git {args[0]} failed: {result.stderr}")
        return result.stdout.strip()

    def commit(self, directory, message):
        self.git(directory, "add", ".")
        self.git(directory, "commit", "-m", message)

    def prepare(self):
        def download(url):
            if url != self.release.asset_url:
                raise AssertionError("unexpected update URL")
            return Download(self.archive.read_bytes(), url)
        with patch.object(updater, "_open_url", side_effect=download), \
             patch.object(updater, "_read_url", side_effect=AssertionError("external network forbidden")):
            return updater.prepare_update(self.release, self.root)

    def assert_preserved(self, case):
        case.assertEqual((self.root / "config.json").read_bytes(), b'{"user_note":"preserve"}\n')
        case.assertEqual((self.root / "state" / "private.json").read_bytes(), b'{"private":"preserve"}\n')


@unittest.skipUnless(shutil.which("git"), "Git is required for source update fixtures")
class GitUpdateTests(unittest.TestCase):
    def setUp(self):
        self.fixture = GitUpdateFixture()
        self.addCleanup(self.fixture.close)

    def test_clean_source_fast_forwards_to_release_tag_and_preserves_user_data(self):
        f = self.fixture
        self.assertTrue(updater.can_update(f.root))
        self.assertFalse(updater.can_install(f.root))
        with self.assertRaises(updater.UpdateError):
            updater.apply_package(f.archive, f.release, f.root)
        prepared = f.prepare()
        self.assertTrue((prepared / "git-update.json").is_file())
        self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), f.old, "prepare must not change running code")
        updater.apply_git_update(prepared / "package.zip", f.release, f.root)
        self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), f.target)
        self.assertNotEqual(f.target, f.latest)
        self.assertEqual(f.git(f.root, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(f.git(f.root, "status", "--porcelain"), "")
        self.assertEqual(updater.current_version(f.root), f.release.version)
        f.assert_preserved(self)

    def test_prepare_rejects_tracked_and_staged_changes(self):
        f = self.fixture
        file = f.root / "tray_icon.py"
        file.write_bytes(b"LOCAL_CHANGE = True\n")
        for staged in (False, True):
            with self.subTest(staged=staged):
                if staged:
                    f.git(f.root, "add", "tray_icon.py")
                with self.assertRaises(updater.UpdateError):
                    f.prepare()
                self.assertEqual(file.read_bytes(), b"LOCAL_CHANGE = True\n")
                self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), f.old)
                f.assert_preserved(self)

    def test_prepare_rejects_diverged_or_ahead_history(self):
        f = self.fixture
        (f.root / "local-commit.txt").write_text("local work", encoding="utf-8")
        f.commit(f.root, "local work")
        local = f.git(f.root, "rev-parse", "HEAD")
        with self.assertRaises(updater.UpdateError):
            f.prepare()
        self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), local)
        f.git(f.root, "checkout", "-b", "already-ahead", f.latest)
        with self.assertRaises(updater.UpdateError):
            f.prepare()
        self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), f.latest)

    def test_apply_rechecks_changes_and_branch_after_preparation(self):
        f = self.fixture
        prepared = f.prepare()
        file = f.root / "tray_icon.py"
        original = file.read_bytes()
        file.write_bytes(b"LOCAL_AFTER_DOWNLOAD = True\n")
        with self.assertRaises(updater.UpdateError):
            updater.apply_git_update(prepared / "package.zip", f.release, f.root)
        self.assertEqual(file.read_bytes(), b"LOCAL_AFTER_DOWNLOAD = True\n")
        file.write_bytes(original)
        f.git(f.root, "checkout", "-b", "user-switched-branch")
        with self.assertRaises(updater.UpdateError):
            updater.apply_git_update(prepared / "package.zip", f.release, f.root)
        self.assertEqual(f.git(f.root, "symbolic-ref", "--short", "HEAD"), "user-switched-branch")
        self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), f.old)
        f.assert_preserved(self)

    def test_apply_preserves_untracked_and_ignored_collision(self):
        f = self.fixture
        prepared = f.prepare()
        collision = f.root / "new-note.txt"
        collision.write_bytes(b"user content\n")
        for ignored in (False, True):
            with self.subTest(ignored=ignored):
                if ignored:
                    with (f.root / ".git" / "info" / "exclude").open("a", encoding="utf-8") as stream:
                        stream.write("\n/new-note.txt\n")
                with self.assertRaises(updater.UpdateError):
                    updater.apply_git_update(prepared / "package.zip", f.release, f.root)
                self.assertEqual(collision.read_bytes(), b"user content\n")
                self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), f.old)
                f.assert_preserved(self)

    def test_foreign_origin_and_ancestor_repository_are_not_update_targets(self):
        f = self.fixture
        f.git(f.root, "remote", "set-url", "origin", "https://example.invalid/foreign.git")
        with self.assertRaises(updater.UpdateError):
            f.prepare()
        nested = f.root / "nested-package"
        shutil.copytree(f.source, nested, ignore=shutil.ignore_patterns(".git"))
        self.assertFalse(updater.can_update(nested))
        self.assertFalse(updater.can_install(nested))
        with self.assertRaises(updater.UpdateError):
            updater.apply_package(f.archive, f.release, nested)
        self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), f.old)
        f.assert_preserved(self)

    def test_apply_rejects_valid_zip_whose_runtime_differs_from_the_tag(self):
        f = self.fixture
        prepared = f.prepare()
        with zipfile.ZipFile(prepared / "package.zip") as package:
            files = {name: package.read(name) for name in package.namelist()}
        files["tray_icon.py"] = b"OTHER_RELEASE_CONTENT = True\n"
        archive = prepared / "package.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            for name, data in files.items():
                package.writestr(name, data)
        data = archive.read_bytes()
        release = replace(f.release, sha256=hashlib.sha256(data).hexdigest(), size=len(data))
        updater.validate_package(archive, release)
        with self.assertRaises(updater.UpdateError):
            updater.apply_git_update(archive, release, f.root)
        self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), f.old)
        f.assert_preserved(self)

    def test_release_tree_cannot_take_ownership_of_user_data(self):
        f = self.fixture
        f.git(f.source, "checkout", "-b", "unsafe-release", f.target)
        (f.source / "config.json").write_bytes(b"{}\n")
        (f.source / "state").mkdir()
        (f.source / "state" / "private.json").write_bytes(b"{}\n")
        f.git(f.source, "add", "-f", "config.json", "state/private.json")
        f.git(f.source, "commit", "-m", "fixture tracks user data")
        f.git(f.source, "tag", "-fa", f.release.tag, "-m", "unsafe fixture tag")
        f.git(f.source, "push", "--force", str(f.remote), "refs/tags/" + f.release.tag)
        with self.assertRaises(updater.UpdateError):
            prepared = f.prepare()
            updater.apply_git_update(prepared / "package.zip", f.release, f.root)
        self.assertEqual(f.git(f.root, "rev-parse", "HEAD"), f.old)
        f.assert_preserved(self)


if __name__ == "__main__":
    unittest.main()
