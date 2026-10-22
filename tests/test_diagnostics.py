"""Shareable diagnostics use local, sanitized snapshots without side effects."""

from contextlib import ExitStack
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import threading
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dashboard
import optimizer


class DiagnosticSummaryTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="nearroute-diagnostics-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name) / "ABS_USER_MARKER" / "NearRoute"
        self.root.mkdir(parents=True)
        self.now = datetime.now(dashboard.CHINA_TIME).replace(microsecond=0)
        self.markers = (
            "ENV_TOKEN_MARKER", "SECRET_ENV_MARKER", "QUERY_TOKEN_MARKER", "USERINFO_MARKER",
            "ABS_USER_MARKER", "NODE_NAME_MARKER", "GROUP_NAME_MARKER", "ERROR_BODY_MARKER",
            "MESSAGE_BODY_MARKER", "HEALTH_REASON_MARKER", "EVENT_METADATA_MARKER",
        )
        self.cfg = dict(optimizer.DEFAULTS, group="GROUP_NAME_MARKER",
                        controller_secret_env="SECRET_ENV_MARKER", scan_regions=["JP", "TW"],
                        probe_url="https://fixture.invalid/check?access_token=QUERY_TOKEN_MARKER",
                        codex_log_db=r"C:\Users\ABS_USER_MARKER\.codex\logs_2.sqlite")

    def write(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")

    def populate(self):
        self.write("VERSION", "0.2.223\n")
        self.write("config.json", self.cfg)
        self.status = {
            "controller_context": list(optimizer.state_context(self.cfg)),
            "updated_at": self.now.isoformat(), "phase": "monitoring", "controller_available": True,
            "runtime_version": "0.2.223", "started_at": (self.now - timedelta(hours=1)).isoformat(),
            "health": {"ok": True, "reason": "HEALTH_REASON_MARKER"}, "network_errors_recent": 3,
            "last_switch_at": (self.now - timedelta(seconds=60)).isoformat(),
            "transport_recovery_until": (self.now + timedelta(seconds=40)).isoformat(),
            "last_error": "ERROR_BODY_MARKER", "message": "MESSAGE_BODY_MARKER",
            "selected": "NODE_NAME_MARKER", "nodes": [{"name": "NODE_NAME_MARKER"}],
        }
        self.write("state/status.json", self.status)
        self.events = [
            {"event": "switched", "at": self.status["last_switch_at"], "selected": "NODE_NAME_MARKER",
             "metadata": {"path": r"C:\Users\ABS_USER_MARKER", "secret": "EVENT_METADATA_MARKER"}},
            {"event": "rollback", "at": (self.now - timedelta(seconds=30)).isoformat(),
             "previous": "NODE_NAME_MARKER"},
        ]
        self.write("state/events.jsonl", "\n".join(json.dumps(row) for row in self.events)
                   + '\n{"event":"EVENT_METADATA_MARKER","at":"invalid"}\n{partial')

    def files(self):
        return {str(path.relative_to(self.root)): path.read_bytes()
                for path in self.root.rglob("*") if path.is_file()}

    def summarize(self, *, unreadable=None):
        before = self.files()
        allowed = {self.root / name for name in (
            "VERSION", "config.json", "state/status.json", "state/events.jsonl", "state/events.previous.jsonl",
        )}
        original_open = Path.open

        def read_only(path, *args, **kwargs):
            self.assertIn(path, allowed, "diagnostics must not inspect credentials or unrelated files")
            mode = args[0] if args else kwargs.get("mode", "r")
            self.assertFalse(any(flag in mode for flag in "wax+"), "diagnostics must not write files")
            if path == unreadable:
                raise PermissionError("ABS_USER_MARKER ERROR_BODY_MARKER")
            return original_open(path, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, "open", read_only))
            stack.enter_context(patch.object(Path, "mkdir", side_effect=AssertionError("must not create state")))
            stack.enter_context(patch.dict(os.environ, {"SECRET_ENV_MARKER": "ENV_TOKEN_MARKER"}))
            for owner, name in (
                (optimizer, "Controller"), (optimizer, "load_config"), (optimizer.sqlite3, "connect"),
                (optimizer, "codex_connection_summary"),
                (optimizer.urllib.request, "build_opener"), (optimizer.urllib.request, "urlopen"),
                (dashboard.subprocess, "Popen"),
            ):
                stack.enter_context(patch.object(owner, name, side_effect=AssertionError("diagnostics must stay local and read only")))
            raw = dashboard.diagnostic_summary(self.root)
        self.assertEqual(self.files(), before)
        for marker in self.markers:
            self.assertNotIn(marker, raw)
        self.assertNotIn(str(self.root), raw)
        return json.loads(raw)

    def test_fresh_summary_contains_safe_facts_and_installation_event_times_only(self):
        self.populate()
        previous = {"event": "started", "at": (self.now - timedelta(seconds=120)).isoformat()}
        self.write("state/events.previous.jsonl", json.dumps(previous) + "\n")
        self.events.append(previous)
        summary = self.summarize()

        self.assertEqual(summary["version"], "0.2.223")
        self.assertEqual(summary["installed_version"], "0.2.223")
        self.assertEqual(summary["runtime"]["gui_runtime_version"], optimizer.RUNTIME_VERSION)
        self.assertTrue(summary["runtime"]["helper_matches_installed"])
        self.assertEqual(summary["version_file"], "ok")
        self.assertEqual(summary["config"]["file"], "ok")
        self.assertTrue(summary["config"]["codex_log_configured"])
        self.assertEqual(summary["config"]["scan_regions"], ["JP", "TW"])
        status = summary["status"]
        self.assertTrue(status["current"])
        self.assertTrue(status["context_matches"])
        self.assertFalse(status["stale"])
        self.assertTrue(status["controller_available"])
        self.assertTrue(status["health_ok"])
        self.assertEqual(status["network_errors_recent"], 3)
        self.assertEqual(status["helper_runtime_version"], "0.2.223")
        self.assertEqual(status["started_at"], self.status["started_at"])
        self.assertEqual(datetime.fromisoformat(status["recorded_last_switch_at"]), datetime.fromisoformat(self.status["last_switch_at"]))
        self.assertNotIn("last_switch_at", status)
        recent = summary["events"]["installation_recent_events"]
        self.assertEqual({row["type"] for row in recent}, {"started", "switched", "rollback"})
        self.assertEqual(summary["events"]["files"]["events.previous.jsonl"], "ok")
        self.assertTrue(all(set(row) == {"type", "time"} for row in recent))
        self.assertEqual({datetime.fromisoformat(row["time"]) for row in recent},
                         {datetime.fromisoformat(row["at"]) for row in self.events})

        self.cfg["probe_url"] = "https://USERINFO_MARKER:ENV_TOKEN_MARKER@fixture.invalid/check?token=QUERY_TOKEN_MARKER"
        self.write("config.json", self.cfg)
        invalid = self.summarize()
        self.assertEqual(invalid["config"]["file"], "invalid")
        self.assertFalse(invalid["status"]["current"])

    def test_old_other_context_and_inactive_snapshots_never_claim_current_health(self):
        for reason in ("stale", "other_context", "missing_context", "starting", "stopping", "stopped"):
            with self.subTest(reason=reason):
                self.populate()
                if reason == "stale":
                    self.status["updated_at"] = (self.now - timedelta(seconds=600)).isoformat()
                elif reason == "other_context":
                    self.status["controller_context"][0] = "http://127.0.0.1:9799"
                elif reason == "missing_context":
                    self.status.pop("controller_context")
                else:
                    self.status["phase"] = reason
                self.write("state/status.json", self.status)
                summary = self.summarize()
                status = summary["status"]
                self.assertIsNone(summary["runtime"]["helper_matches_installed"])
                self.assertFalse(status["current"])
                self.assertEqual(status["stale"], reason == "stale")
                if reason == "missing_context":
                    self.assertIsNone(status["context_matches"])
                else:
                    self.assertEqual(status["context_matches"], reason != "other_context")
                for field in ("controller_available", "health_ok", "network_errors_recent", "transport_recovery_until"):
                    self.assertIsNone(status[field], field)
                self.assertEqual(datetime.fromisoformat(status["recorded_last_switch_at"]),
                                 datetime.fromisoformat(self.status["last_switch_at"]))

        self.populate()
        self.status.update(phase="waiting", controller_available=False)
        self.write("state/status.json", self.status)
        unavailable = self.summarize()["status"]
        self.assertFalse(unavailable["controller_available"])
        self.assertIsNone(unavailable["health_ok"])
        self.assertEqual(unavailable["network_errors_recent"], 3)

    def test_runtime_versions_distinguish_replaced_files_and_loaded_processes(self):
        self.populate()
        self.status["runtime_version"] = "0.2.222"
        self.write("state/status.json", self.status)
        with patch.object(optimizer, "RUNTIME_VERSION", "0.2.221"):
            summary = self.summarize()
        self.assertEqual(summary["installed_version"], "0.2.223")
        self.assertEqual(summary["runtime"]["gui_runtime_version"], "0.2.221")
        self.assertFalse(summary["runtime"]["gui_matches_installed"])
        self.assertEqual(summary["status"]["helper_runtime_version"], "0.2.222")
        self.assertFalse(summary["runtime"]["helper_matches_installed"])
        note = dashboard.DiagnosticDialog.runtime_note(summary)
        self.assertIn("窗口与安装文件版本不同", note)
        self.assertIn("后台与安装文件版本不同", note)
        self.assertIn("退出近舟", note)

        for reported in (None, "ENV_TOKEN_MARKER", "v0.2.223", "1.2.3-beta"):
            with self.subTest(reported=reported):
                self.status["runtime_version"] = reported
                self.status["started_at"] = "ABS_USER_MARKER"
                self.write("state/status.json", self.status)
                summary = self.summarize()
                self.assertIsNone(summary["status"]["helper_runtime_version"])
                self.assertIsNone(summary["status"]["started_at"])
                self.assertIsNone(summary["runtime"]["helper_matches_installed"])
                self.assertIn("后台快照 未知", dashboard.DiagnosticDialog.runtime_note(summary))
                self.assertIn("退出近舟", dashboard.DiagnosticDialog.runtime_note(summary))

        self.status["phase"] = "stopped"
        self.write("state/status.json", self.status)
        self.assertNotIn("后台版本未知", dashboard.DiagnosticDialog.runtime_note(self.summarize()))

    def test_absent_log_option_reports_effective_defaults_without_home_lookup(self):
        self.populate()
        self.cfg.pop("codex_log_db")
        self.write("config.json", self.cfg)
        with patch.object(Path, "home", side_effect=AssertionError("no default lookup before opt-in")):
            self.assertEqual(self.summarize()["config"]["codex_log_configured"], bool(optimizer.DEFAULTS["codex_log_db"]))

    def test_missing_partial_and_unreadable_files_have_explicit_status_without_mutation(self):
        missing = self.summarize()
        self.assertEqual(missing["version_file"], "missing")
        self.assertEqual(missing["config"]["file"], "missing")
        self.assertEqual(missing["status"]["file"], "missing")
        self.assertIsNone(missing["status"]["context_matches"])
        self.assertEqual(missing["events"]["files"]["events.jsonl"], "missing")
        self.assertFalse((self.root / "state").exists())

        self.write("VERSION", "ABS_USER_MARKER")
        self.write("config.json", '{"probe_url":"USERINFO_MARKER')
        self.write("state/status.json", '{"last_error":"ERROR_BODY_MARKER')
        self.write("state/events.jsonl", '{"event":"EVENT_METADATA_MARKER')
        partial = self.summarize()
        self.assertEqual(partial["version_file"], "invalid")
        self.assertEqual(partial["config"]["file"], "invalid")
        self.assertEqual(partial["status"]["file"], "invalid")
        self.assertEqual(partial["events"]["installation_recent_events"], [])
        self.assertFalse(partial["status"]["current"])

        self.populate()
        blocked = self.summarize(unreadable=self.root / "state/status.json")
        self.assertEqual(blocked["status"]["file"], "unreadable")
        self.assertFalse(blocked["status"]["current"])


class DiagnosticDialogTests(unittest.TestCase):
    def make_dialog(self, *, configured=True):
        fixture = DiagnosticSummaryTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.populate()
        if not configured:
            fixture.cfg["codex_log_db"] = ""
            fixture.write("config.json", fixture.cfg)
        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        self.addCleanup(root.destroy)
        root_patch = patch.object(dashboard, "ROOT", fixture.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)
        owner = SimpleNamespace(window=root, diagnostic_dialog=None)
        dialog = dashboard.DiagnosticDialog(owner, visible=False)
        owner.diagnostic_dialog = dialog
        self.addCleanup(dialog.close)
        return fixture, dialog

    def receive_summary(self, dialog):
        if dialog.after_id is not None:
            dialog.window.after_cancel(dialog.after_id)
        dialog.poll_codex_summary()
        self.assertIsNone(dialog.after_id)
        self.assertFalse(dialog.reading_codex)
        self.assertEqual(dialog.text.get("1.0", "end-1c"), dialog.summary)
        self.assertEqual(str(dialog.text.cget("state")), "disabled")

    @staticmethod
    def connection_result(errors=None):
        errors = [] if errors is None else errors
        return {"state": "ok", "window_seconds": 7200, "error_count": len(errors),
                "errors": errors, "transports": [], "truncated": False}

    def test_explicit_read_runs_off_tk_and_merges_safe_timing_preview_before_copy(self):
        fixture, dialog = self.make_dialog()
        before_files, before_summary = fixture.files(), dialog.summary
        parent_thread, workers, read_threads = threading.get_ident(), [], []
        real_thread = threading.Thread
        errors = [{"at": (fixture.now - timedelta(seconds=59)).isoformat(), "kind": "decode",
                   "retry": 1, "max_retries": 5, "retry_wait_ms": 200},
                  {"at": (fixture.now - timedelta(minutes=10)).isoformat(), "kind": "websocket",
                   "retry": None, "max_retries": None, "retry_wait_ms": None}]

        def thread(**kwargs):
            self.assertTrue(kwargs["daemon"])
            worker = real_thread(**kwargs)
            workers.append(worker)
            return worker

        def read(path):
            read_threads.append(threading.get_ident())
            return self.connection_result(errors)

        with patch.object(dashboard.threading, "Thread", side_effect=thread), \
             patch.object(optimizer, "codex_connection_summary", side_effect=read) as connection:
            self.assertNotIn("codex_connection", json.loads(dialog.summary))
            self.assertIsNone(dialog.after_id)
            connection.assert_not_called()
            dialog.codex_button.invoke()
            self.assertTrue(dialog.reading_codex)
            workers[0].join(timeout=1)
            self.assertFalse(workers[0].is_alive())
            self.assertEqual(dialog.summary, before_summary, "worker must leave Tk preview changes to polling")
            self.assertNotEqual(read_threads, [parent_thread])
            connection.assert_called_once_with(fixture.cfg["codex_log_db"])
            self.receive_summary(dialog)

        report = json.loads(dialog.summary)["codex_connection"]
        self.assertEqual(report["source"], "configured")
        self.assertEqual(report["error_count"], 2)
        nearby = report["errors"][0]["nearby_events"]
        switched = next(event for event in nearby if event["type"] == "switched")
        self.assertEqual(switched["seconds_from_event"], 1)
        self.assertEqual(set(switched), {"type", "time", "seconds_from_event"})
        self.assertEqual(report["errors"][1]["nearby_events"], [])
        self.assertIn("不能据此判断原因或重试结果", dialog.codex_message.get())
        self.assertEqual(fixture.files(), before_files)
        self.assertNotIn("ABS_USER_MARKER", dialog.summary)
        with patch.object(dialog.window, "clipboard_clear"), \
             patch.object(dialog.window, "clipboard_append") as append:
            dialog.copy_summary()
        append.assert_called_once_with(dialog.summary)

    def test_default_path_is_resolved_only_after_click_and_respects_codex_home(self):
        for codex_home in ("", r"C:\ENV_USER_MARKER\codex-private"):
            with self.subTest(codex_home=codex_home), \
                 patch.dict(os.environ, {"CODEX_HOME": codex_home}), \
                 patch.object(Path, "home", side_effect=AssertionError("no home lookup before click")) as home, \
                 patch.object(optimizer, "codex_connection_summary", return_value=self.connection_result()) as connection:
                fixture, dialog = self.make_dialog(configured=False)
                before = fixture.files()
                self.assertFalse(json.loads(dialog.summary)["config"]["codex_log_configured"])
                home.assert_not_called()
                connection.assert_not_called()
                home.side_effect = None
                home.return_value = fixture.root / "DEFAULT_USER_MARKER"
                workers = []

                def thread(**kwargs):
                    workers.append(kwargs["target"])
                    return Mock()

                with patch.object(dashboard.threading, "Thread", side_effect=thread):
                    dialog.codex_button.invoke()
                    self.assertEqual(len(workers), 1)
                    connection.assert_not_called()
                    workers.pop()()
                    self.receive_summary(dialog)
                expected_home = Path(codex_home) if codex_home else home.return_value / ".codex"
                connection.assert_called_once_with(str(expected_home / "logs_2.sqlite"))
                if codex_home:
                    home.assert_not_called()
                else:
                    home.assert_called_once()
                self.assertEqual(json.loads(dialog.summary)["codex_connection"]["source"], "default")
                for marker in ("ENV_USER_MARKER", "DEFAULT_USER_MARKER", "ABS_USER_MARKER"):
                    self.assertNotIn(marker, dialog.summary)
                self.assertEqual(fixture.files(), before)
                dialog.close()

    def test_close_cancels_pending_poll_and_worker_failure_contains_no_exception_details(self):
        fixture, dialog = self.make_dialog()
        workers = []
        with patch.object(dashboard.threading, "Thread", side_effect=lambda **kw: workers.append(kw["target"]) or Mock()), \
             patch.object(optimizer, "codex_connection_summary", side_effect=PermissionError("ENV_TOKEN_MARKER")):
            dialog.read_codex_summary()
            dialog.read_codex_summary()
            self.assertEqual(len(workers), 1)
            workers.pop()()
            self.receive_summary(dialog)
            self.assertEqual(json.loads(dialog.summary)["codex_connection"]["state"], "unreadable")
            self.assertNotIn("ENV_TOKEN_MARKER", dialog.summary)
            dialog.read_codex_summary()
            callback = dialog.after_id
            with patch.object(dialog.window, "after_cancel", wraps=dialog.window.after_cancel) as cancel:
                dialog.close()
            cancel.assert_called_once_with(callback)
            self.assertIsNone(dialog.after_id)
            workers.pop()()
            dialog.poll_codex_summary()
            self.assertTrue(dialog.closed)
            self.assertFalse(dialog.window.winfo_exists())

    def test_thread_start_failure_releases_button_and_truncation_is_visible(self):
        fixture, dialog = self.make_dialog()
        with patch.object(dashboard.threading, "Thread", return_value=Mock(start=Mock(side_effect=RuntimeError("ENV_TOKEN_MARKER")))):
            dialog.read_codex_summary()
            self.receive_summary(dialog)
        self.assertEqual(json.loads(dialog.summary)["codex_connection"]["state"], "unreadable")
        self.assertNotIn("ENV_TOKEN_MARKER", dialog.summary)
        self.assertNotIn("disabled", dialog.codex_button.state())
        result = self.connection_result()
        result.update(truncated=True, source="configured")
        dialog.results.put(result)
        self.receive_summary(dialog)
        self.assertIn("摘要已截断", dialog.codex_message.get())
        self.assertIn("已读取范围内的观察数", dialog.codex_message.get())
        self.assertIn("不代表近 2 小时完整错误数或最终失败数", dialog.codex_message.get())

    def test_preview_copy_failure_and_parent_close_remain_local(self):
        fixture = DiagnosticSummaryTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.populate()
        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        owner = SimpleNamespace(
            root=root, update_preferences={"auto_check": False, "mode": "notify"},
            update_can_install=False, update_text=Mock(get=Mock(return_value="fixture")),
            update_release=None, update_busy=None, update_prepared=None, exiting=False,
            check_updates=Mock(), request_update_install=Mock(), update_dialog=None,
        )
        update = None
        dialog_class = dashboard.DiagnosticDialog
        try:
            with patch.object(dashboard, "ROOT", fixture.root), \
                 patch.object(dashboard, "start_daemon", side_effect=AssertionError("must not start helper")), \
                 patch.object(dashboard.threading, "Thread", side_effect=AssertionError("must not start worker")), \
                 patch.object(dashboard, "DiagnosticDialog", side_effect=lambda parent: dialog_class(parent, visible=False)) as create, \
                 patch.object(dashboard.messagebox, "showerror") as showerror:
                update = dashboard.UpdateDialog(owner)
                update.window.withdraw()
                owner.update_dialog = update
                update.open_diagnostics()
                diagnostic = update.diagnostic_dialog
                with patch.object(diagnostic.window, "lift"):
                    update.open_diagnostics()
                create.assert_called_once()
                self.assertEqual(diagnostic.text.get("1.0", "end-1c"), diagnostic.summary)
                self.assertEqual(json.loads(diagnostic.summary)["version"], "0.2.223")
                self.assertEqual(str(diagnostic.text.cget("state")), "disabled")
                self.assertEqual(diagnostic.window.state(), "withdrawn")

                with patch.object(diagnostic.window, "clipboard_clear") as clear, \
                     patch.object(diagnostic.window, "clipboard_append") as append:
                    diagnostic.copy_summary()
                    clear.assert_called_once()
                    append.assert_called_once_with(diagnostic.summary)
                    showerror.assert_not_called()
                    append.side_effect = tk.TclError("ENV_TOKEN_MARKER")
                    diagnostic.copy_summary()
                    showerror.assert_called_once()
                    self.assertNotIn("ENV_TOKEN_MARKER", str(showerror.call_args))
                    self.assertEqual(diagnostic.copy_message.get(), "")

                update.close()
                self.assertTrue(diagnostic.closed)
                self.assertFalse(diagnostic.window.winfo_exists())
                self.assertIsNone(update.diagnostic_dialog)
                self.assertIsNone(owner.update_dialog)
                diagnostic.close()
        finally:
            if update is not None and not update.closed:
                update.close()
            root.destroy()


if __name__ == "__main__":
    unittest.main()
