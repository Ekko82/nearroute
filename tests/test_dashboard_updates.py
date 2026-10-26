"""Offline update UI behavior; no network, real helper, or installation runs."""

from contextlib import ExitStack
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
import http.client
import hashlib
import io
import queue
import json
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
import webbrowser
import zipfile
from unittest.mock import Mock, patch

import dashboard
import updater


RELEASE = updater.Release("1.1.0", "v1.1.0", "https://github.com/Ekko82/nearroute/releases/tag/v1.1.0",
                          "更新说明", "https://github.com/Ekko82/nearroute/releases/download/v1.1.0/package.zip", "a" * 64, 123)


class UpdateBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.app = dashboard.Dashboard.__new__(dashboard.Dashboard)
        self.app.root = Mock()
        self.app.active_tab = "overview"
        self.app.show_tab = Mock(side_effect=lambda key: setattr(self.app, "active_tab", key))
        self.app.closed = self.app.exiting = self.app.restore_busy = self.app.manual_busy = self.app.client_busy = False
        self.app.client_dialog = None
        self.app.settings_dialog = self.app.update_dialog = None
        self.app.tray = None
        self.app.updates_enabled = self.app.update_can_install = True
        self.app.update_busy = self.app.update_release = self.app.update_prepared = None
        self.app.update_requested_install = False
        self.app.update_auto_install_attempted = None
        self.app.update_results = queue.SimpleQueue()
        self.app.update_preferences = {"auto_check": True, "mode": "notify"}
        self.app.update_next_check = float("inf")
        self.app.update_text = Mock()
        self.app.started_helpers = []
        self.app.cancel_service_start = Mock()
        self.app.notify = Mock()
        self.app.close = Mock()
        self.workers = []
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(dashboard, "ROOT", self.root))

        def deferred_thread(**kwargs):
            self.workers.append(kwargs["target"])
            return Mock()

        self.patches.enter_context(patch.object(dashboard.threading, "Thread", side_effect=deferred_thread))
        self.due = self.patches.enter_context(patch.object(updater, "check_due", return_value=True))
        self.real_check = updater.check_for_update
        self.check = self.patches.enter_context(patch.object(updater, "check_for_update", return_value=RELEASE))
        self.real_prepare = updater.prepare_update
        self.real_start_installer = updater.start_installer
        self.prepare = self.patches.enter_context(patch.object(updater, "prepare_update", return_value=self.root / "prepared"))
        self.install = self.patches.enter_context(patch.object(updater, "start_installer"))
        self.patches.enter_context(patch.object(updater, "current_version", return_value="1.0.0"))
        self.confirm = self.patches.enter_context(patch.object(dashboard.messagebox, "askyesno", return_value=True))
        self.showerror = self.patches.enter_context(patch.object(dashboard.messagebox, "showerror"))
        self.browser = self.patches.enter_context(patch.object(webbrowser, "open"))
        self.patches.enter_context(patch.object(dashboard, "python_executable", return_value="mock-python"))
        self.wait = self.patches.enter_context(patch.object(dashboard, "wait_for_helper_startup", return_value=True))

    def fresh_status(self, **fields):
        return {"phase": "monitoring", "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat(), **fields}

    def finish(self, status=None):
        self.workers.pop(0)()
        self.app.poll_updates(self.fresh_status() if status is None else status)

    def test_default_notify_check_is_async_and_never_downloads_or_opens_dialog(self):
        self.app.tray = Mock()
        self.app.update_next_check = 0
        self.app.poll_updates({"phase": "monitoring"})
        self.check.assert_not_called()
        self.assertEqual(self.app.update_busy, "check")
        self.finish()
        self.assertEqual(self.app.update_release, RELEASE)
        self.assertIn("1.1.0", self.app.update_text.set.call_args.args[0])
        self.prepare.assert_not_called()
        self.install.assert_not_called()
        self.confirm.assert_not_called()
        self.showerror.assert_not_called()
        self.app.tray.show_notification.assert_called_once()
        self.app.check_updates(manual=True)
        self.finish()
        self.app.tray.show_notification.assert_called_once()  # No repeated balloon for the same release.

    def test_disabling_automatic_checks_keeps_manual_check_available(self):
        self.app.update_preferences = {"auto_check": False, "mode": "install"}
        self.app.update_release = RELEASE
        self.app.update_prepared = self.root / "prepared"
        self.app.update_next_check = 0
        self.app.poll_updates({})
        self.due.assert_not_called()
        self.assertEqual(self.workers, [])
        self.app.check_updates(manual=True)
        self.finish()
        self.check.assert_called_once_with(root=self.root)
        self.prepare.assert_not_called()
        self.install.assert_not_called()

    def test_network_failure_becomes_status_without_popup_or_immediate_retry(self):
        self.check.side_effect = updater.UpdateError("网络暂不可用")
        self.app.check_updates(manual=True)
        self.finish()
        self.assertIsNone(self.app.update_busy)
        self.assertIn("网络暂不可用", self.app.update_text.set.call_args.args[0])
        self.app.poll_updates({})
        self.assertEqual(self.workers, [])
        self.showerror.assert_not_called()

    def test_download_mode_stages_once_and_leaves_installation_to_user(self):
        self.app.update_release = RELEASE
        self.app.update_preferences["mode"] = "download"
        self.app.poll_updates({"phase": "monitoring"})
        self.assertEqual(self.app.update_busy, "prepare")
        self.prepare.assert_not_called()
        self.finish()
        self.app.poll_updates({"phase": "monitoring"})
        self.prepare.assert_called_once_with(RELEASE, root=self.root)
        self.assertEqual(self.app.update_prepared, self.root / "prepared")
        self.assertFalse(self.app.exiting)
        self.install.assert_not_called()

    def test_truncated_chunked_metadata_recovers_worker_and_allows_manual_recheck(self):
        asset_name = "NearRoute-1.1.0-windows.zip"
        download = f"https://github.com/{updater.REPOSITORY}/releases/download/v1.1.0/"
        metadata = {
            "tag_name": RELEASE.tag, "html_url": RELEASE.url, "body": RELEASE.notes,
            "assets": [{"name": asset_name, "browser_download_url": download + asset_name, "size": 123},
                       {"name": "nearroute-update.json", "browser_download_url": download + "nearroute-update.json", "size": 200}],
        }
        manifest = {"schema": 1, "version": RELEASE.version, "asset": asset_name, "size": 123, "sha256": "a" * 64}
        responses = []

        def open_response(url):
            if not responses:
                payload = b'20\r\n{"tag_name":"v1.'  # The first chunk ends before its declared size.
            else:
                document = metadata if url == updater.RELEASES_URL else manifest
                body = json.dumps(document).encode()
                payload = f"{len(body):x}\r\n".encode() + body + b"\r\n0\r\n\r\n"
            wire = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + payload
            response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *_: io.BytesIO(wire)))
            response.begin()
            response.url = url
            responses.append(response)
            return response

        self.check.side_effect = self.real_check
        with patch.object(updater, "_open_url", side_effect=open_response):
            self.app.check_updates(manual=True)
            self.assertEqual(self.app.update_busy, "check")
            self.workers.pop(0)()
            self.assertFalse(self.app.update_results.empty())
            self.app.poll_updates({})
            self.assertIsNone(self.app.update_busy)
            self.assertIn("更新未完成", self.app.update_text.set.call_args.args[0])
            self.app.check_updates(manual=True)
            self.finish()
        self.assertIsNone(self.app.update_busy)
        self.assertEqual(self.app.update_release.version, RELEASE.version)
        self.assertEqual(len(responses), 3)
        self.assertTrue(all(response.isclosed() for response in responses))
        self.showerror.assert_not_called()

    def test_failed_auto_download_retries_only_after_a_later_successful_check(self):
        for mode in ("download", "install"):
            with self.subTest(mode=mode):
                self.app.update_preferences = {"auto_check": True, "mode": mode}
                self.app.update_release = RELEASE
                self.app.update_prepared = None
                self.app.update_auto_prepare_attempted = None
                self.app.update_next_check = float("inf")
                self.prepare.reset_mock()
                self.prepare.side_effect = [updater.UpdateError("下载中断"), self.root / "prepared"]
                self.app.poll_updates({"phase": "verifying"})
                self.finish({"phase": "verifying"})
                self.assertIsNone(self.app.update_busy)
                for _ in range(3):
                    self.app.poll_updates({"phase": "verifying"})
                self.assertEqual(self.prepare.call_count, 1)
                self.assertEqual(self.workers, [])
                self.app.check_updates(manual=True)
                self.finish({"phase": "verifying"})
                self.assertEqual(self.app.update_busy, "prepare")
                self.assertEqual(len(self.workers), 1)
                self.finish({"phase": "verifying"})
                self.assertEqual(self.prepare.call_count, 2)
                self.app.poll_updates({"phase": "verifying"})
                self.assertEqual(self.workers, [])
        for preferences in ({"auto_check": True, "mode": "notify"},
                            {"auto_check": False, "mode": "download"},
                            {"auto_check": False, "mode": "install"}):
            with self.subTest(preferences=preferences):
                self.app.update_preferences = preferences
                self.app.update_prepared = None
                self.app.update_auto_prepare_attempted = RELEASE.tag
                self.prepare.reset_mock()
                self.app.check_updates(manual=True)
                self.finish({"phase": "verifying"})
                self.assertEqual(self.workers, [])
                self.prepare.assert_not_called()
        self.install.assert_not_called()

    def test_auto_install_waits_for_node_work_and_transport_recovery(self):
        self.app.update_release = RELEASE
        self.app.update_prepared = self.root / "prepared"
        self.app.update_preferences["mode"] = "install"
        self.app.poll_updates(self.fresh_status(phase="verifying"))
        until = (datetime.now(dashboard.CHINA_TIME) + timedelta(seconds=60)).isoformat()
        self.app.poll_updates(self.fresh_status(transport_recovery_until=until))
        self.app.manual_busy = True
        self.app.poll_updates(self.fresh_status())
        self.app.manual_busy = False
        self.app.restore_busy = True
        self.app.poll_updates(self.fresh_status())
        self.app.restore_busy = False
        self.app.settings_dialog = SimpleNamespace(closed=False, has_unsaved_changes=Mock(return_value=True))
        self.app.poll_updates(self.fresh_status())
        self.assertEqual(self.workers, [])
        self.assertEqual(self.app.active_tab, "overview")
        self.app.settings_dialog.has_unsaved_changes.return_value = False
        self.app.poll_updates(self.fresh_status())
        self.assertFalse(self.app.settings_dialog.closed)
        self.assertTrue(self.app.exiting)
        self.assertEqual(self.app.update_busy, "install")
        self.confirm.assert_not_called()  # This mode was explicitly enabled in preferences.

    def test_auto_install_waits_for_fresh_idle_status(self):
        self.app.update_release = RELEASE
        self.app.update_prepared = self.root / "prepared"
        self.app.update_preferences["mode"] = "install"
        expired = (datetime.now(dashboard.CHINA_TIME) - timedelta(seconds=dashboard.STATUS_STALE_SECONDS + 1)).isoformat()
        for status in ({"phase": "monitoring", "updated_at": expired},
                       {"phase": "monitoring"},
                       {"phase": "monitoring", "updated_at": "invalid"}):
            self.app.poll_updates(status)
            self.assertEqual(self.workers, [], msg=str(status))
            self.assertIsNone(self.app.update_busy)
            self.assertFalse(self.app.exiting)
        self.app.poll_updates(self.fresh_status())
        self.assertEqual(self.app.update_busy, "install")
        self.assertEqual(len(self.workers), 1)
        self.assertTrue(self.app.exiting)
        self.confirm.assert_not_called()

    def test_download_completion_waits_for_active_updates_but_allows_clean_hidden_page(self):
        self.app.update_release = RELEASE
        self.app.update_preferences["mode"] = "install"
        self.app.poll_updates(self.fresh_status())
        self.assertEqual(self.app.update_busy, "prepare")
        self.workers.pop(0)()  # Download completes before the UI receives its result.
        dialog = dashboard.UpdateDialog.__new__(dashboard.UpdateDialog)
        dialog.owner, dialog.window, dialog.closed = self.app, Mock(), False
        dialog.refresh = Mock()
        dialog.has_unsaved_changes = Mock(return_value=False)
        self.app.update_dialog = dialog
        self.app.active_tab = "updates"

        self.app.poll_updates(self.fresh_status())
        self.app.poll_updates(self.fresh_status())

        self.assertEqual(self.app.update_prepared, self.root / "prepared")
        self.assertIsNone(self.app.update_busy)
        self.assertIsNone(self.app.update_auto_install_attempted)
        self.assertFalse(self.app.exiting)
        self.assertEqual(self.workers, [])
        self.install.assert_not_called()
        self.app.show_tab("overview")
        self.assertIs(self.app.update_dialog, dialog)
        self.assertFalse(dialog.closed)
        dialog.window.destroy.assert_not_called()
        self.app.poll_updates(self.fresh_status())
        self.assertEqual(self.app.update_busy, "install")
        self.assertTrue(self.app.exiting)
        self.confirm.assert_not_called()
        self.finish()
        self.install.assert_called_once()
        self.app.close.assert_called_once()

    def test_explicit_install_from_open_update_dialog_remains_available(self):
        self.app.update_release = RELEASE
        self.app.update_prepared = self.root / "prepared"
        self.app.update_dialog = SimpleNamespace(closed=False, refresh=Mock(), has_unsaved_changes=Mock(return_value=False))
        self.app.active_tab = "updates"

        self.app.request_update_install()

        self.confirm.assert_called_once()
        self.assertEqual(self.app.update_busy, "install")
        self.assertTrue(self.app.exiting)
        self.finish()
        self.install.assert_called_once()
        self.app.close.assert_called_once()

    def test_confirmed_manual_install_ignores_stale_status_but_respects_local_node_work(self):
        expired = (datetime.now(dashboard.CHINA_TIME) - timedelta(minutes=10)).isoformat()
        for status, busy in (({}, "manual_busy"),
                             ({"phase": "monitoring", "updated_at": expired}, "restore_busy")):
            for prepared in (False, True):
                with self.subTest(status=status, prepared=prepared):
                    self.app.update_release = RELEASE
                    self.app.update_prepared = self.root / "prepared" if prepared else None
                    self.app.update_requested_install = False
                    self.app.update_busy = None
                    self.app.exiting = False
                    self.workers.clear()
                    self.confirm.reset_mock()
                    self.app.poll_updates(status)
                    if prepared:
                        setattr(self.app, busy, True)
                        self.app.request_update_install()
                        self.assertEqual(self.workers, [])
                        self.confirm.assert_not_called()
                        setattr(self.app, busy, False)
                        self.app.request_update_install()
                    else:
                        self.app.request_update_install()
                        self.assertEqual(self.app.update_busy, "prepare")
                        setattr(self.app, busy, True)
                        self.finish(status)
                        self.assertEqual(self.workers, [])
                        self.assertIsNone(self.app.update_busy)
                        self.assertFalse(self.app.exiting)
                        setattr(self.app, busy, False)
                        self.app.poll_updates(status)
                    self.confirm.assert_called_once()
                    self.assertEqual(self.app.update_busy, "install")
                    self.assertEqual(len(self.workers), 1)
                    self.assertTrue(self.app.exiting)
                    self.install.assert_not_called()

    def test_click_install_requires_confirmation_before_download(self):
        self.app.update_release = RELEASE
        self.confirm.return_value = False
        self.app.request_update_install()
        self.assertEqual(self.workers, [])
        self.prepare.assert_not_called()
        self.confirm.return_value = True
        self.app.request_update_install()
        self.assertTrue(self.app.update_requested_install)
        self.assertEqual(self.app.update_busy, "prepare")

    def test_source_checkout_downloads_and_installs_inside_the_app(self):
        with patch.object(dashboard, "ttk"), \
             patch.object(dashboard.tk, "StringVar"), \
             patch.object(dashboard.tk, "Menu"), \
             patch.object(updater, "cached_release", return_value=RELEASE), \
             patch.object(updater, "can_install", return_value=False), \
             patch.object(updater, "can_update", return_value=True) as can_update, \
             patch.object(dashboard.Dashboard, "poll_tray"), \
             patch.object(dashboard.Dashboard, "refresh"):
            # A source checkout is eligible for preparation, although ZIP installation
            # remains unavailable. All Tk widgets and service starts stay mocked.
            self.app = dashboard.Dashboard(Mock(), start_service=False, enable_tray=False)
        can_update.assert_called_once_with(root=self.root)
        self.assertTrue(self.app.update_can_install)
        self.app.updates_enabled = True
        self.app.update_next_check = float("inf")
        self.app.close = Mock()
        self.app.request_update_install()
        self.confirm.assert_called_once()
        self.assertEqual(self.app.update_busy, "prepare")
        self.finish()
        self.prepare.assert_called_once_with(RELEASE, root=self.root)
        self.assertEqual(self.app.update_busy, "install")
        self.finish()
        self.install.assert_called_once()
        self.app.close.assert_called_once()
        self.browser.assert_not_called()

    def test_unsupported_directory_reports_reason_without_opening_browser(self):
        self.app.update_release = RELEASE
        self.app.update_can_install = False
        self.app.update_preferences["mode"] = "install"
        self.app.poll_updates({"phase": "monitoring"})
        self.app.request_update_install()
        self.browser.assert_not_called()
        self.assertIn("不能安全", self.app.update_text.set.call_args.args[0])
        self.assertEqual(self.workers, [])
        self.prepare.assert_not_called()
        self.install.assert_not_called()
        self.confirm.assert_not_called()

    def test_source_safety_failure_is_visible_and_leaves_app_open_for_retry(self):
        self.app.update_release = RELEASE
        self.prepare.side_effect = updater.UpdateError("本地源码有改动，已保留现有文件，无法安全更新。")
        self.app.request_update_install()
        self.finish()
        self.assertIsNone(self.app.update_busy)
        self.assertFalse(self.app.exiting)
        self.assertFalse(self.app.update_requested_install)
        self.assertIn("本地源码有改动", self.app.update_text.set.call_args.args[0])
        self.assertEqual(self.workers, [])
        self.install.assert_not_called()
        self.app.close.assert_not_called()
        self.browser.assert_not_called()
        self.prepare.side_effect = None
        self.app.request_update_install()
        self.assertEqual(self.app.update_busy, "prepare")

    def test_installer_handoff_waits_for_startup_then_closes_ui_from_queue(self):
        self.app.update_prepared = self.root / "prepared"
        self.app.started_helpers = [Mock()]
        self.app.begin_update_install()
        self.app.cancel_service_start.assert_called_once()
        self.wait.assert_not_called()
        self.app.close.assert_not_called()
        self.workers.pop(0)()
        self.wait.assert_called_once_with(self.app.started_helpers)
        self.install.assert_called_once_with(self.root / "prepared", parent_pid=dashboard.os.getpid(),
                                             python_exe="mock-python", root=self.root)
        self.app.close.assert_not_called()
        self.app.poll_updates({})
        self.app.close.assert_called_once()

    def test_failed_handoff_keeps_existing_gui_and_releases_busy_state(self):
        self.app.update_prepared = self.root / "prepared"
        self.wait.return_value = False
        self.app.begin_update_install()
        self.finish()
        self.assertFalse(self.app.exiting)
        self.assertIsNone(self.app.update_busy)
        self.install.assert_not_called()
        self.app.close.assert_not_called()

    def test_missing_staged_metadata_releases_busy_and_manual_retry_repairs_cache(self):
        prepared = self.root / "state" / "updates" / RELEASE.tag
        prepared.mkdir(parents=True)
        archive = prepared / "package.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as package:
            for name in sorted(updater.REQUIRED_FILES):
                content = (RELEASE.version + "\n").encode() if name == "VERSION" else b"VALUE = 1\n"
                package.writestr(name, content)
        release = replace(RELEASE, asset_url=f"https://github.com/{updater.REPOSITORY}/releases/download/{RELEASE.tag}/NearRoute-{RELEASE.version}-windows.zip",
                          size=archive.stat().st_size, sha256=hashlib.sha256(archive.read_bytes()).hexdigest())
        metadata = prepared / "release.json"
        metadata.write_text(json.dumps(asdict(release)), encoding="utf-8")
        metadata.unlink()  # Cache cleanup after a successful download.
        self.app.update_release = release
        self.app.update_prepared = prepared
        self.install.side_effect = self.real_start_installer
        with patch.object(updater.subprocess, "Popen", side_effect=AssertionError("real installer forbidden")) as popen, \
             patch.object(updater, "_open_url", side_effect=AssertionError("valid cache needs no network")):
            self.app.request_update_install()
            self.finish()
            self.assertIsNone(self.app.update_busy)
            self.assertFalse(self.app.exiting)
            self.assertIsNone(self.app.update_prepared)
            self.assertFalse(self.app.update_requested_install)
            self.app.close.assert_not_called()
            self.assertIn("更新未完成", self.app.update_text.set.call_args.args[0])
            self.prepare.side_effect = self.real_prepare
            self.install.side_effect = None
            self.app.request_update_install()
            self.assertEqual(self.app.update_busy, "prepare")
            self.finish()
            self.prepare.assert_called_once_with(release, root=self.root)
            self.assertEqual(json.loads(metadata.read_text(encoding="utf-8")), asdict(release))
            self.assertEqual(self.app.update_busy, "install")
            self.finish()
            self.app.close.assert_called_once()
            popen.assert_not_called()

    def test_reopened_failed_version_requires_manual_retry_but_new_release_can_auto_install(self):
        state = self.root / "state"
        state.mkdir()
        (state / "update-settings.json").write_text(json.dumps({"auto_check": True, "mode": "install"}), encoding="utf-8")
        for retry in ("manual", "new-release", "previous-success"):
            with self.subTest(retry=retry):
                (state / "update-result.json").write_text(json.dumps({
                    "success": retry == "previous-success", "version": RELEASE.version, "message": "上次安装结果",
                }), encoding="utf-8")
                self.check.return_value = RELEASE
                self.confirm.reset_mock()
                self.prepare.reset_mock()
                with patch.object(dashboard, "ttk"), \
                     patch.object(dashboard.tk, "StringVar"), \
                     patch.object(dashboard.tk, "Menu"), \
                     patch.object(updater, "cached_release", return_value=RELEASE), \
                     patch.object(dashboard.Dashboard, "poll_tray"), \
                     patch.object(dashboard.Dashboard, "refresh"):
                    # Exercise production initialization and the real persisted result reader.
                    self.app = dashboard.Dashboard(Mock(), enable_tray=False)
                try:
                    self.app.update_next_check = float("inf")
                    self.assertIsNone(self.app.update_prepared)
                    self.app.check_updates(manual=True)
                    self.finish({"phase": "verifying"})
                    if retry == "previous-success":
                        self.assertEqual(self.app.update_busy, "prepare")
                        self.finish()
                        self.confirm.assert_not_called()
                    else:
                        self.app.poll_updates({"phase": "monitoring"})
                        self.assertEqual(self.workers, [])
                        self.assertIsNone(self.app.update_busy)
                        self.assertFalse(self.app.exiting)
                        self.prepare.assert_not_called()
                        self.assertIn("手动重试", self.app.update_text.set.call_args.args[0])
                        if retry == "manual":
                            self.app.request_update_install()
                            self.confirm.assert_called_once()
                        else:
                            self.check.return_value = replace(RELEASE, version="1.2.0", tag="v1.2.0")
                            self.app.check_updates(manual=True)
                            self.finish({"phase": "verifying"})
                            self.confirm.assert_not_called()
                        self.assertEqual(self.app.update_busy, "prepare")
                        self.finish()
                    self.assertEqual(self.app.update_busy, "install")
                    self.assertTrue(self.app.exiting)
                    self.assertEqual(len(self.workers), 1)
                    self.install.assert_not_called()  # The installer handoff remains deferred.
                finally:
                    self.app.close()
                    self.workers.clear()

    def test_installing_guard_prevents_second_window_creation(self):
        with patch.object(updater, "is_installing", return_value=True), \
             patch.object(dashboard, "WindowInstance") as instance, \
             patch.object(dashboard.tk, "Tk") as window:
            dashboard.main()
        instance.assert_not_called()
        window.assert_not_called()


class UpdateDialogLayoutTests(unittest.TestCase):
    def test_source_update_controls_and_diagnostics_fit_inline_page(self):
        try:
            root = tk.Tk()
            root.withdraw()
            dashboard.apply_theme(root)
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        dialog = None
        try:
            root.attributes("-alpha", 0)
            notebook = dashboard.ttk.Notebook(root)
            notebook.pack(fill="both", expand=True)
            parent = dashboard.ttk.Frame(notebook)
            notebook.add(parent, text="版本与更新")
            with tempfile.TemporaryDirectory() as directory:
                source = Path(directory)
                (source / ".git").mkdir()
                owner = SimpleNamespace(
                    root=root, update_preferences=updater.load_preferences(root=source),
                    update_can_install=updater.can_update(root=source), update_text=Mock(get=Mock(return_value="发现新版本")),
                    update_release=RELEASE, update_busy=None, update_prepared=None, exiting=False,
                    check_updates=Mock(), request_update_install=Mock(), update_dialog=None,
                    show_tab=Mock(), open_diagnostics=Mock(), notify=Mock(),
                )
                with patch.object(dashboard.tk, "Toplevel", side_effect=AssertionError("inline page must not create windows")):
                    dialog = dashboard.UpdateDialog(owner, parent=parent)
                owner.update_dialog = dialog
                self.assertIs(dialog.window.master, parent)
                self.assertEqual(dialog.window.winfo_class(), "TFrame")
                self.assertTrue(dialog.auto_check.get())
                self.assertEqual(dialog.mode.get(), "notify")
                self.assertEqual(dialog.install_button.cget("text"), "下载并安装")
                widgets = []

                def collect(parent):
                    for child in parent.winfo_children():
                        widgets.append(child)
                        collect(child)

                collect(dialog.window)
                buttons = [widget for widget in widgets if widget.winfo_class() == "TButton"]
                diagnostic = next(button for button in buttons if button.cget("text") == "诊断信息")
                self.assertEqual(diagnostic.master, dialog.install_button.master)
                for widget in widgets:
                    if widget.winfo_class() == "TRadiobutton":
                        self.assertFalse(widget.instate(["disabled"]))
                for width, height in ((1040, 620), (920, 540)):
                    root.geometry(f"{width}x{height}")
                    root.deiconify()
                    root.update()
                    for button in buttons:
                        self.assertTrue(button.winfo_ismapped())
                        x = button.winfo_rootx() - dialog.window.winfo_rootx()
                        y = button.winfo_rooty() - dialog.window.winfo_rooty()
                        self.assertGreaterEqual(x, 0)
                        self.assertGreaterEqual(y, 0)
                        self.assertLessEqual(x + button.winfo_width(), dialog.window.winfo_width())
                        self.assertLessEqual(y + button.winfo_height(), dialog.window.winfo_height())
                    self.assertGreaterEqual(dialog.notes.winfo_height(), 64)
                self.assertFalse(dialog.has_unsaved_changes())
                dialog.auto_check.set(False)
                dialog.mode.set("download")
                self.assertTrue(dialog.has_unsaved_changes())
                with patch.object(dashboard, "ROOT", source):
                    dialog.save()
                self.assertFalse(dialog.has_unsaved_changes())
                self.assertFalse(dialog.closed)
                self.assertIs(owner.update_dialog, dialog)
                self.assertEqual(dialog.message.get(), "更新设置已保存。")
                owner.show_tab.assert_not_called()
                diagnostic.invoke()
                owner.open_diagnostics.assert_called_once()
                dialog.mode.set("install")
                self.assertTrue(dialog.has_unsaved_changes())
                dialog.close()
                owner.show_tab.assert_called_once_with("overview")
                self.assertIs(owner.update_dialog, dialog)
                self.assertFalse(dialog.closed)
                self.assertEqual(dialog.mode.get(), "install")
                self.assertEqual(owner.update_preferences["mode"], "download")
                owner.show_tab.reset_mock()
                discard = next(button for button in buttons if button.cget("text") == "放弃修改")
                discard.invoke()
                self.assertTrue(dialog.closed)
                self.assertIsNone(owner.update_dialog)
                self.assertEqual(owner.update_preferences["mode"], "download")
                owner.show_tab.assert_called_once_with("overview")
        finally:
            if dialog is not None:
                dialog.dispose()
            root.destroy()


class DisabledStartupTests(unittest.TestCase):
    def test_offline_dashboard_does_not_start_update_or_daemon_workers(self):
        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        with tempfile.TemporaryDirectory() as directory:
            location = Path(directory)
            (location / "VERSION").write_text("1.0.0", encoding="utf-8")
            with patch.object(dashboard, "ROOT", location), \
                 patch.object(dashboard, "STATE", location / "state"), \
                 patch.object(dashboard, "CONFIG", location / "config.json"), \
                 patch.object(dashboard, "STATUS", location / "state" / "status.json"), \
                 patch.object(dashboard, "start_daemon") as daemon, \
                 patch.object(dashboard.threading, "Thread") as thread, \
                 patch.object(updater, "check_for_update") as check:
                app = dashboard.Dashboard(root, start_service=False, enable_tray=False)
                try:
                    root.update_idletasks()
                    self.assertFalse(app.updates_enabled)
                    daemon.assert_not_called()
                    thread.assert_not_called()
                    check.assert_not_called()
                    app.open_updates()
                    app.update_dialog.auto_check.set(False)
                    app.update_dialog.mode.set("notify")
                    app.update_dialog.save()
                    saved = json.loads((location / "state" / "update-settings.json").read_text(encoding="utf-8"))
                    self.assertEqual(saved, {"auto_check": False, "mode": "notify"})
                    self.assertFalse(app.update_dialog.closed)
                    self.assertEqual(app.update_dialog.message.get(), "更新设置已保存。")
                finally:
                    app.close()
                # A restart inside the check interval keeps the cached release visible,
                # even when a previous installation left a result message behind.
                (location / "state" / "update-result.json").write_text(json.dumps({"message": "旧版更新结果"}), encoding="utf-8")
                reopened = tk.Tk()
                reopened.withdraw()
                with patch.object(updater, "cached_release", return_value=RELEASE):
                    app = dashboard.Dashboard(reopened, enable_tray=False)
                try:
                    reopened.update_idletasks()
                    self.assertIn(RELEASE.version, app.update_text.get())
                    self.assertNotIn("旧版更新结果", app.update_text.get())
                    app.open_updates()
                    self.assertIn(RELEASE.notes, app.update_dialog.notes.get("1.0", tk.END))
                    daemon.assert_not_called()
                    check.assert_not_called()
                    thread.assert_not_called()
                finally:
                    app.close()


if __name__ == "__main__":
    unittest.main()
