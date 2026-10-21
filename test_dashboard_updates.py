"""Offline update UI behavior; no network, real helper, or installation runs."""

from contextlib import ExitStack
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
import http.client
import io
import queue
import json
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
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
        self.app.closed = self.app.exiting = self.app.restore_busy = self.app.manual_busy = False
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
        self.prepare = self.patches.enter_context(patch.object(updater, "prepare_update", return_value=self.root / "prepared"))
        self.install = self.patches.enter_context(patch.object(updater, "start_installer"))
        self.patches.enter_context(patch.object(updater, "current_version", return_value="1.0.0"))
        self.confirm = self.patches.enter_context(patch.object(dashboard.messagebox, "askyesno", return_value=True))
        self.showerror = self.patches.enter_context(patch.object(dashboard.messagebox, "showerror"))
        self.browser = self.patches.enter_context(patch.object(dashboard.webbrowser, "open"))
        self.patches.enter_context(patch.object(dashboard, "python_executable", return_value="mock-python"))
        self.wait = self.patches.enter_context(patch.object(dashboard, "wait_for_helper_startup", return_value=True))

    def finish(self, status=None):
        self.workers.pop(0)()
        self.app.poll_updates(status or {"phase": "monitoring"})

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
        self.app.poll_updates({"phase": "verifying"})
        until = (datetime.now(dashboard.CHINA_TIME) + timedelta(seconds=60)).isoformat()
        self.app.poll_updates({"phase": "monitoring", "transport_recovery_until": until})
        self.app.manual_busy = True
        self.app.poll_updates({"phase": "monitoring"})
        self.app.manual_busy = False
        self.app.restore_busy = True
        self.app.poll_updates({"phase": "monitoring"})
        self.assertEqual(self.workers, [])
        self.app.restore_busy = False
        self.app.poll_updates({"phase": "monitoring"})
        self.assertTrue(self.app.exiting)
        self.assertEqual(self.app.update_busy, "install")
        self.confirm.assert_not_called()  # This mode was explicitly enabled in preferences.

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

    def test_source_checkout_only_opens_release_page_even_in_auto_install_mode(self):
        self.app.update_release = RELEASE
        self.app.update_can_install = False
        self.app.update_preferences["mode"] = "install"
        self.app.poll_updates({"phase": "monitoring"})
        self.app.request_update_install()
        self.browser.assert_called_once_with(RELEASE.url)
        self.assertEqual(self.workers, [])
        self.prepare.assert_not_called()
        self.install.assert_not_called()
        self.confirm.assert_not_called()

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
                    self.assertIsNone(app.update_dialog)
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
