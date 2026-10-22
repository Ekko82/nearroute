"""Offline release validation and transactional installation in temporary roots."""

from contextlib import ExitStack
from dataclasses import FrozenInstanceError, replace
import hashlib
import http.client
import io
import json
from pathlib import Path
import stat
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request
import zipfile

import updater


class Response(io.BytesIO):
    def __init__(self, data, url):
        super().__init__(data)
        self.url = url

    def geturl(self):
        return self.url


class UpdaterTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nearroute-updater-test-")
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.root = self.directory / "installation"
        self.root.mkdir()
        (self.root / "VERSION").write_text("0.1.0\n", encoding="utf-8")
        self.archive = self.directory / "update.zip"
        self.files = {}
        for name in sorted(updater.REQUIRED_FILES):
            if name == "VERSION":
                content = b"0.1.1\n"
            elif name.endswith(".py"):
                content = b"VALUE = 'new release'\n"
            elif name == "config.example.json":
                content = b'{"enabled": false}\n'
            else:
                content = b"new packaged file\n"
            self.files[name] = content
        self.release = self.package()
        self.real_open_url = updater._open_url
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(updater, "_open_url", side_effect=AssertionError("real network forbidden")))
        stack.enter_context(patch.object(updater.subprocess, "Popen", side_effect=AssertionError("real process forbidden")))
        stack.enter_context(patch.object(updater.subprocess, "run", side_effect=AssertionError("real process forbidden")))

    def release_for(self, archive):
        data = archive.read_bytes()
        return updater.Release(
            version="0.1.1", tag="v0.1.1",
            url=f"https://github.com/{updater.REPOSITORY}/releases/tag/v0.1.1", notes="Release notes",
            asset_url=f"https://github.com/{updater.REPOSITORY}/releases/download/v0.1.1/NearRoute-0.1.1-windows.zip",
            sha256=hashlib.sha256(data).hexdigest(), size=len(data),
        )

    def package(self, files=None, extras=()):
        with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as package:
            for name, data in (self.files if files is None else files).items():
                package.writestr(name, data)
            for name, data in extras:
                package.writestr(name, data)
        return self.release_for(self.archive)

    def metadata(self):
        manifest_url = f"https://github.com/{updater.REPOSITORY}/releases/download/v0.1.1/nearroute-update.json"
        metadata = {
            "tag_name": self.release.tag, "html_url": self.release.url, "body": self.release.notes,
            "draft": False, "prerelease": False,
            "assets": [
                {"name": "NearRoute-0.1.1-windows.zip", "browser_download_url": self.release.asset_url, "size": self.release.size},
                {"name": "nearroute-update.json", "browser_download_url": manifest_url, "size": 200},
            ],
        }
        manifest = {"schema": 1, "version": "0.1.1", "asset": "NearRoute-0.1.1-windows.zip",
                    "sha256": self.release.sha256, "size": self.release.size}
        return metadata, manifest, manifest_url

    def check(self, metadata, manifest):
        manifest_url = self.metadata()[2]

        def read(url, _limit):
            if url == updater.RELEASES_URL:
                return json.dumps(metadata).encode()
            if url == manifest_url:
                return json.dumps(manifest).encode()
            raise AssertionError("unexpected update URL")

        with patch.object(updater, "_read_url", side_effect=read):
            return updater.check_for_update(self.root)

    def truncated_chunked_response(self, url):
        stream = io.BytesIO(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nab")
        response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *_args: stream))
        response.begin()
        response.geturl = lambda: url
        self.addCleanup(response.close)
        return response

    def preserve_user_files(self):
        values = {
            "config.json": b'{"enabled": true, "custom_note": "keep"}\n',
            "state/history.json": b'{"private node": [123]}',
            "state/update-settings.json": b'{"auto_check":false,"mode":"notify"}',
            "state/install-state.json": b'{"python_exe":"saved-runtime"}',
            "personal-note.txt": b"not a packaged application file",
        }
        for name, data in values.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
        return values

    def assert_user_files(self, values):
        for name, expected in values.items():
            self.assertEqual((self.root / name).read_bytes(), expected, name)

    def symlink(self, link, target, directory=False):
        try:
            link.symlink_to(target, target_is_directory=directory)
        except OSError as error:
            self.skipTest(f"symlink creation unavailable (error {getattr(error, 'winerror', error.errno)})")

    def test_versions_are_stable_numeric_and_release_metadata_is_frozen(self):
        self.assertGreater(updater.version_tuple("0.10.0"), updater.version_tuple("0.9.99"))
        self.assertEqual(updater.current_version(self.root), "0.1.0")
        for invalid in ("v0.1.1", "0.1", "01.1.0", "0.1.1-beta", "0.1.1/path", None):
            with self.subTest(version=invalid), self.assertRaises(updater.UpdateError):
                updater.version_tuple(invalid)
        with self.assertRaises(FrozenInstanceError):
            self.release.version = "9.9.9"

    def test_check_returns_verified_release_and_throttles_automatic_checks(self):
        metadata, manifest, _url = self.metadata()
        now = updater.CHECK_INTERVAL_SECONDS * 2
        with patch.object(updater.time, "time", return_value=now):
            self.assertTrue(updater.check_due(self.root))
            self.assertEqual(self.check(metadata, manifest), self.release)
            self.assertEqual(updater.cached_release(self.root), self.release)
        self.assertFalse(updater.check_due(self.root, now=now + updater.CHECK_INTERVAL_SECONDS - 1))
        self.assertTrue(updater.check_due(self.root, now=now + updater.CHECK_INTERVAL_SECONDS))
        with patch.object(updater, "_read_url", side_effect=OSError("offline")):
            with self.assertRaises(updater.UpdateError):
                updater.check_for_update(self.root)
        self.assertEqual(updater.cached_release(self.root), self.release)
        (self.root / "VERSION").write_text(self.release.version, encoding="utf-8")
        self.assertIsNone(updater.cached_release(self.root))

    def test_equal_older_draft_and_prerelease_do_not_offer_installation(self):
        metadata, manifest, _url = self.metadata()
        for changes in ({"tag_name": "v0.1.0"}, {"tag_name": "v0.0.9"}, {"draft": True}, {"prerelease": True}):
            with self.subTest(changes=changes):
                self.assertIsNone(self.check(metadata | changes, manifest))

    def test_truncated_metadata_keeps_cached_release_and_allows_retry(self):
        metadata, manifest, manifest_url = self.metadata()
        self.assertEqual(self.check(metadata, manifest), self.release)
        response = self.truncated_chunked_response(updater.RELEASES_URL)
        with patch.object(updater, "_open_url", return_value=response):
            with self.assertRaises(updater.UpdateError) as failure:
                updater.check_for_update(self.root)
        self.assertIsInstance(failure.exception.__cause__, http.client.IncompleteRead)
        self.assertTrue(response.isclosed())
        self.assertEqual(updater.cached_release(self.root), self.release)
        state = self.root / "state/update-check.json"
        self.assertEqual(json.loads(state.read_text())["error"], "IncompleteRead")

        responses = [Response(json.dumps(metadata).encode(), updater.RELEASES_URL),
                     Response(json.dumps(manifest).encode(), manifest_url)]
        with patch.object(updater, "_open_url", side_effect=responses):
            self.assertEqual(updater.check_for_update(self.root), self.release)
        self.assertTrue(all(item.closed for item in responses))
        self.assertNotIn("error", json.loads(state.read_text()))

    def test_release_and_manifest_must_agree_on_version_origin_hash_and_size(self):
        metadata, manifest, _url = self.metadata()
        for changes in ({"schema": 2}, {"version": "0.2.0"}, {"asset": "other.zip"},
                        {"size": self.release.size + 1}, {"sha256": "invalid"}, {"size": True}):
            with self.subTest(manifest_changes=changes), self.assertRaises(updater.UpdateError):
                self.check(metadata, manifest | changes)
        for changes in ({"html_url": "https://example.org/release"}, {"tag_name": "main"}, {"assets": []}):
            with self.subTest(metadata_changes=changes), self.assertRaises(updater.UpdateError):
                self.check(metadata | changes, manifest)
        wrong_asset = dict(metadata)
        wrong_asset["assets"] = [dict(item) for item in metadata["assets"]]
        wrong_asset["assets"][0]["browser_download_url"] = "https://github.com/other/repo/releases/download/v0.1.1/package.zip"
        with self.assertRaises(updater.UpdateError):
            self.check(wrong_asset, manifest)

    def test_no_release_and_failed_check_are_throttled_without_changing_user_config(self):
        user_files = self.preserve_user_files()
        updater.save_preferences(True, "notify", self.root)
        user_files["state/update-settings.json"] = (self.root / "state/update-settings.json").read_bytes()
        now = updater.CHECK_INTERVAL_SECONDS * 2
        with patch.object(updater.time, "time", return_value=now), \
             patch.object(updater, "_read_url", side_effect=urllib.error.HTTPError(updater.RELEASES_URL, 404, "missing", {}, None)):
            self.assertIsNone(updater.check_for_update(self.root))
        self.assertFalse(updater.check_due(self.root, now=now + 1))
        with patch.object(updater.time, "time", return_value=now + 2), \
             patch.object(updater, "_read_url", return_value=b"not JSON"):
            with self.assertRaises(updater.UpdateError):
                updater.check_for_update(self.root)
        self.assertFalse(updater.check_due(self.root, now=now + 3))
        self.assert_user_files(user_files)

    def test_preferences_preserve_user_config_and_control_background_checks(self):
        user_files = self.preserve_user_files()
        for mode in ("notify", "download", "install"):
            saved = updater.save_preferences(False, mode, self.root)
            self.assertEqual(updater.load_preferences(self.root), saved)
            self.assertFalse(updater.check_due(self.root, now=10**10))
        del user_files["state/update-settings.json"]
        self.assert_user_files(user_files)
        for enabled, mode in ((1, "notify"), (True, "unknown")):
            with self.assertRaises(updater.UpdateError):
                updater.save_preferences(enabled, mode, self.root)

    def test_https_origin_and_every_redirect_are_validated(self):
        accepted = [self.release.asset_url, updater.RELEASES_URL,
                    "https://release-assets.githubusercontent.com/package?signature=placeholder"]
        for url in accepted:
            self.assertEqual(updater._https_url(url), url)
        rejected = ["http://github.com/file", "https://github.com.evil.invalid/file", "file:///tmp/update.zip",
                    "https://github.com@evil.invalid/file", "https://user:password@github.com/file", "https://github.com:444/file"]
        request = urllib.request.Request(self.release.asset_url)
        for url in rejected:
            with self.subTest(url=url), io.BytesIO(b"fixture redirect response") as response:
                with self.assertRaises(updater.UpdateError):
                    updater.SafeRedirect().redirect_request(request, response, 302, "Found", {}, url)
                self.assertTrue(response.closed)
        for url in accepted:
            with self.subTest(url=url), io.BytesIO(b"fixture redirect response") as response:
                redirected = updater.SafeRedirect().redirect_request(request, response, 302, "Found", {}, url)
                self.assertEqual(redirected.full_url, url)
                self.assertFalse(response.closed)  # urllib closes it when following the approved redirect.

    def test_metadata_reader_limits_bytes_and_closes_response(self):
        response = Response(b"12345", updater.RELEASES_URL)
        with patch.object(updater, "_open_url", return_value=response):
            with self.assertRaises(updater.UpdateError):
                updater._read_url(updater.RELEASES_URL, 4)
        self.assertTrue(response.closed)
        response = Response(b"{}", "https://example.org/redirect")
        with patch.object(updater, "_open_url", return_value=response):
            with self.assertRaises(updater.UpdateError):
                updater._read_url(updater.RELEASES_URL, 16)
        self.assertTrue(response.closed)

    def test_http_error_responses_close_and_allow_a_successful_retry(self):
        metadata, manifest, manifest_url = self.metadata()
        for stage, code in (("release", 404), ("release", 403), ("manifest", 403), ("package", 403)):
            with self.subTest(stage=stage, http_status=code):
                response = io.BytesIO(b"synthetic HTTP error")
                error_url = {"release": updater.RELEASES_URL, "manifest": manifest_url,
                             "package": self.release.asset_url}[stage]
                error = urllib.error.HTTPError(error_url, code, "fixture error", {}, response)
                self.addCleanup(error.close)
                opener = Mock()
                opener.open.side_effect = (
                    [Response(json.dumps(metadata).encode(), updater.RELEASES_URL), error]
                    if stage == "manifest" else [error]
                )
                with patch.object(updater, "_open_url", self.real_open_url), \
                     patch.object(updater.urllib.request, "build_opener", return_value=opener):
                    if code == 404:
                        self.assertIsNone(updater.check_for_update(self.root))
                    else:
                        with self.assertRaises(updater.UpdateError) as failure:
                            if stage == "package":
                                updater.prepare_update(self.release, self.root)
                            else:
                                updater.check_for_update(self.root)
                        self.assertIs(failure.exception.__cause__, error)
                    self.assertTrue(response.closed)

                    if stage == "package":
                        updates = self.root / "state" / "updates"
                        self.assertEqual(list(updates.iterdir()), [])
                        retry_responses = [Response(self.archive.read_bytes(), self.release.asset_url)]
                        opener.open.side_effect = retry_responses
                        prepared = updater.prepare_update(self.release, self.root)
                        self.assertEqual((prepared / "package.zip").read_bytes(), self.archive.read_bytes())
                        self.assertEqual(list(updates.iterdir()), [prepared])
                    else:
                        retry_responses = [Response(json.dumps(metadata).encode(), updater.RELEASES_URL),
                                           Response(json.dumps(manifest).encode(), manifest_url)]
                        opener.open.side_effect = retry_responses
                        self.assertEqual(updater.check_for_update(self.root), self.release)
                    self.assertTrue(all(item.closed for item in retry_responses))

    def test_prepare_downloads_validates_and_reuses_completed_cache(self):
        response = Response(self.archive.read_bytes(), self.release.asset_url)
        with patch.object(updater, "_open_url", return_value=response) as network:
            prepared = updater.prepare_update(self.release, self.root)
            self.assertEqual((prepared / "package.zip").read_bytes(), self.archive.read_bytes())
            self.assertEqual(json.loads((prepared / "release.json").read_text())["version"], "0.1.1")
            self.assertEqual(updater.prepare_update(self.release, self.root), prepared)
            network.assert_called_once_with(self.release.asset_url)
        self.assertTrue(response.closed)

    def test_oversized_download_is_removed_and_response_closed(self):
        response = Response(self.archive.read_bytes() + b"extra", self.release.asset_url)
        with patch.object(updater, "_open_url", return_value=response):
            with self.assertRaises(updater.UpdateError):
                updater.prepare_update(self.release, self.root)
        self.assertTrue(response.closed)
        self.assertEqual(list((self.root / "state/updates").iterdir()), [])

    def test_truncated_download_cleans_cache_and_allows_retry(self):
        response = self.truncated_chunked_response(self.release.asset_url)
        with patch.object(updater, "_open_url", return_value=response):
            with self.assertRaises(updater.UpdateError) as failure:
                updater.prepare_update(self.release, self.root)
        self.assertIsInstance(failure.exception.__cause__, http.client.IncompleteRead)
        self.assertTrue(response.isclosed())
        updates = self.root / "state/updates"
        self.assertEqual(list(updates.iterdir()), [])

        response = Response(self.archive.read_bytes(), self.release.asset_url)
        with patch.object(updater, "_open_url", return_value=response):
            prepared = updater.prepare_update(self.release, self.root)
        self.assertTrue(response.closed)
        self.assertEqual((prepared / "package.zip").read_bytes(), self.archive.read_bytes())
        self.assertEqual(list(updates.iterdir()), [prepared])

    def test_apply_replaces_program_files_and_preserves_all_user_data(self):
        user_files = self.preserve_user_files()
        (self.root / "optimizer.py").write_bytes(b"OLD = True\n")
        updater.apply_package(self.archive, self.release, self.root)
        for name, expected in self.files.items():
            self.assertEqual((self.root / name).read_bytes(), expected, name)
        self.assert_user_files(user_files)
        self.assertEqual(list(self.root.rglob("*.update-tmp")), [])

    def test_bad_hash_size_zip_version_and_python_never_change_installed_code(self):
        original = (self.root / "VERSION").read_bytes()
        for release in (replace(self.release, sha256="0" * 64), replace(self.release, size=self.release.size + 1)):
            with self.subTest(release=release), self.assertRaises(updater.UpdateError):
                updater.apply_package(self.archive, release, self.root)
        invalid_payloads = [self.files | {"VERSION": b"9.9.9"}, self.files | {"dashboard.py": b"def invalid(:\n"}]
        for payload in invalid_payloads:
            release = self.package(payload)
            with self.assertRaises(updater.UpdateError):
                updater.apply_package(self.archive, release, self.root)
        self.archive.write_bytes(b"not a ZIP")
        with self.assertRaises(updater.UpdateError):
            updater.apply_package(self.archive, self.release_for(self.archive), self.root)
        self.assertEqual((self.root / "VERSION").read_bytes(), original)
        self.assertFalse((self.root / "dashboard.py").exists())

    def test_archive_rejects_escape_runtime_data_device_names_and_case_collisions(self):
        unsafe = ["../escape.py", "/escape.py", "C:/escape.py", "assets\\..\\escape.py", "config.json",
                  "state/private.json", ".git/config", "module.py:alternate", "assets/CON.ico", "assets/NUL.png"]
        for name in unsafe:
            with self.subTest(name=name):
                release = self.package(extras=[(name, b"VALUE = 1\n")])
                with self.assertRaises(updater.UpdateError):
                    updater.validate_package(self.archive, release)
        release = self.package(extras=[("assets/NearRoute.ico", b"case collision")])
        with self.assertRaises(updater.UpdateError):
            updater.validate_package(self.archive, release)
        self.assertFalse((self.directory / "escape.py").exists())

    def test_archive_symlink_missing_files_and_unpacked_limit_are_rejected(self):
        link = zipfile.ZipInfo("linked.py")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        release = self.package(extras=[(link, b"../config.json")])
        with self.assertRaises(updater.UpdateError):
            updater.validate_package(self.archive, release)
        release = self.package({name: value for name, value in self.files.items() if name != "optimizer.py"})
        with self.assertRaises(updater.UpdateError):
            updater.validate_package(self.archive, release)
        release = self.package()
        with patch.object(updater, "MAX_UNPACKED_BYTES", sum(map(len, self.files.values())) - 1):
            with self.assertRaises(updater.UpdateError):
                updater.validate_package(self.archive, release)
        with patch.object(updater, "MAX_PACKAGE_BYTES", release.size - 1):
            with self.assertRaises(updater.UpdateError):
                updater.validate_package(self.archive, release)

    def test_zip_apply_refuses_git_checkout_at_root_or_ancestor(self):
        metadata, manifest, _url = self.metadata()
        git = self.root / ".git"
        git.write_text("gitdir: elsewhere", encoding="utf-8")
        self.assertFalse(updater.can_install(self.root))
        self.assertEqual(self.check(metadata, manifest), self.release)
        with self.assertRaises(updater.UpdateError):
            updater.apply_package(self.archive, self.release, self.root)
        self.assertEqual(updater.current_version(self.root), "0.1.0")
        git.unlink()
        (self.directory / ".git").mkdir()
        self.assertFalse(updater.can_install(self.root))
        with self.assertRaises(updater.UpdateError):
            updater.apply_package(self.archive, self.release, self.root)

    def test_file_replace_failure_rolls_back_existing_and_removes_new_files(self):
        user_files = self.preserve_user_files()
        old = {"VERSION": b"0.1.0\n", "optimizer.py": b"OLD = 1\n", "dashboard.py": b"OLD = 2\n"}
        for name, value in old.items():
            (self.root / name).write_bytes(value)
        ordered = {name: self.files[name] for name in ("VERSION", "optimizer.py")}
        ordered["new_feature.py"] = b"NEW = True\n"
        ordered["dashboard.py"] = self.files["dashboard.py"]
        ordered.update(self.files)
        release = self.package(ordered)
        real_replace = updater.os.replace

        def fail_replacing_dashboard(source, target):
            if Path(target) == self.root / "dashboard.py":
                raise PermissionError("simulated file in use")
            return real_replace(source, target)

        with patch.object(updater.os, "replace", side_effect=fail_replacing_dashboard):
            with self.assertRaisesRegex(updater.UpdateError, "已还原"):
                updater.apply_package(self.archive, release, self.root)
        for name, expected in old.items():
            self.assertEqual((self.root / name).read_bytes(), expected, name)
        self.assertFalse((self.root / "new_feature.py").exists())
        self.assertEqual(list(self.root.rglob("*.update-tmp")), [])
        self.assert_user_files(user_files)

    def test_program_target_symlink_is_rejected_without_touching_external_file(self):
        outside = self.directory / "outside.py"
        outside.write_bytes(b"outside content")
        self.symlink(self.root / "optimizer.py", outside)
        with self.assertRaises(updater.UpdateError):
            updater.apply_package(self.archive, self.release, self.root)
        self.assertEqual(outside.read_bytes(), b"outside content")
        self.assertEqual(updater.current_version(self.root), "0.1.0")

    def test_state_symlink_cannot_redirect_backup_deletion_or_download_cache(self):
        outside = self.directory / "outside-state"
        backup = outside / "updates/backup"
        backup.mkdir(parents=True)
        sentinel = backup / "must-keep.txt"
        sentinel.write_bytes(b"external backup")
        self.symlink(self.root / "state", outside, directory=True)
        with self.assertRaises(updater.UpdateError):
            updater.apply_package(self.archive, self.release, self.root)
        with self.assertRaises(updater.UpdateError):
            updater.prepare_update(self.release, self.root)
        self.assertEqual(sentinel.read_bytes(), b"external backup")
        self.assertEqual(updater.current_version(self.root), "0.1.0")

    def test_reparse_point_attribute_blocks_program_and_state_targets(self):
        user_files = self.preserve_user_files()
        program = self.root / "optimizer.py"
        program.write_bytes(b"OLD = True\n")
        real_lstat = Path.lstat
        for reparse in (program, self.root / "state"):
            def lstat(path):
                result = real_lstat(path)
                if path == reparse:
                    return SimpleNamespace(st_mode=result.st_mode, st_file_attributes=0x400)
                return result

            with self.subTest(path=reparse.name), patch.object(Path, "lstat", lstat):
                with self.assertRaises(updater.UpdateError):
                    updater.apply_package(self.archive, self.release, self.root)
                if reparse.name == "state":
                    with self.assertRaises(updater.UpdateError):
                        updater.prepare_update(self.release, self.root)
            self.assertEqual(program.read_bytes(), b"OLD = True\n")
            self.assert_user_files(user_files)


if __name__ == "__main__":
    unittest.main()
