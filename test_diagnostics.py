"""Shareable diagnostics use local, sanitized snapshots without side effects."""

from contextlib import ExitStack
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
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
        summary = self.summarize()

        self.assertEqual(summary["version"], "0.2.223")
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
        self.assertEqual(datetime.fromisoformat(status["recorded_last_switch_at"]), datetime.fromisoformat(self.status["last_switch_at"]))
        self.assertNotIn("last_switch_at", status)
        recent = summary["events"]["installation_recent_events"]
        self.assertEqual({row["type"] for row in recent}, {"switched", "rollback"})
        self.assertTrue(all(set(row) == {"type", "time"} for row in recent))
        self.assertEqual({datetime.fromisoformat(row["time"]) for row in recent},
                         {datetime.fromisoformat(row["at"]) for row in self.events})

        self.cfg["probe_url"] = "https://USERINFO_MARKER:ENV_TOKEN_MARKER@fixture.invalid/check?token=QUERY_TOKEN_MARKER"
        self.write("config.json", self.cfg)
        invalid = self.summarize()
        self.assertEqual(invalid["config"]["file"], "invalid")
        self.assertFalse(invalid["status"]["current"])

    def test_old_other_context_and_inactive_snapshots_never_claim_current_health(self):
        for reason in ("stale", "other_context", "starting", "stopping", "stopped"):
            with self.subTest(reason=reason):
                self.populate()
                if reason == "stale":
                    self.status["updated_at"] = (self.now - timedelta(seconds=600)).isoformat()
                elif reason == "other_context":
                    self.status["controller_context"][0] = "http://127.0.0.1:9799"
                else:
                    self.status["phase"] = reason
                self.write("state/status.json", self.status)
                status = self.summarize()["status"]
                self.assertFalse(status["current"])
                self.assertEqual(status["stale"], reason == "stale")
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

    def test_missing_partial_and_unreadable_files_have_explicit_status_without_mutation(self):
        missing = self.summarize()
        self.assertEqual(missing["version_file"], "missing")
        self.assertEqual(missing["config"]["file"], "missing")
        self.assertEqual(missing["status"]["file"], "missing")
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
