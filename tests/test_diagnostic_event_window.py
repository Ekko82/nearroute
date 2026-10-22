"""Opt-in local event windows retain sanitized events around Codex errors."""

from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import dashboard
import optimizer


class DiagnosticEventWindowTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="nearroute-event-window-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "state").mkdir()
        self.error_at = datetime(2026, 10, 23, 12, 0, tzinfo=dashboard.CHINA_TIME)

    def event(self, kind="switched", *, seconds=0, **metadata):
        return {"event": kind, "at": (self.error_at + timedelta(seconds=seconds)).isoformat(), **metadata}

    def write_events(self, rows, name="events.jsonl"):
        (self.root / "state" / name).write_text(
            "\n".join(json.dumps(row) for row in rows), encoding="utf-8")

    def summarize(self, event_times):
        return json.loads(dashboard.diagnostic_summary(self.root, event_times=event_times))

    def test_matching_event_older_than_recent_eight_is_retained(self):
        self.write_events([self.event()] + [self.event("scan_completed", seconds=300 + i) for i in range(10)])

        summary = self.summarize([self.error_at.isoformat()])

        self.assertEqual(summary["events"]["near_error_events"], [
            {"type": "switched", "time": self.error_at.isoformat()},
        ])
        self.assertFalse(summary["events"]["near_error_events_truncated"])

    def test_rotated_log_is_read_without_changing_recent_eight(self):
        self.write_events([self.event("scan_completed", seconds=300 + i) for i in range(12)])
        self.write_events([self.event(), self.event("stopped", seconds=900)], "events.previous.jsonl")
        original = json.loads(dashboard.diagnostic_summary(self.root))["events"]

        events = self.summarize([self.error_at.isoformat()])["events"]

        self.assertEqual(original["files"]["events.previous.jsonl"], "not_needed")
        self.assertEqual(events["files"]["events.previous.jsonl"], "ok")
        self.assertEqual(events["installation_recent_events"], original["installation_recent_events"])
        self.assertEqual(len(events["installation_recent_events"]), 8)
        self.assertEqual(events["near_error_events"], [{"type": "switched", "time": self.error_at.isoformat()}])

    def test_inclusive_two_minute_boundaries_and_time_normalization(self):
        before = self.event("rollback", seconds=-120)
        before["at"] = (self.error_at - timedelta(seconds=120)).replace(tzinfo=None).isoformat()
        after = self.event("switched", seconds=120)
        after["at"] = (self.error_at + timedelta(seconds=120)).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        self.write_events([after, self.event(seconds=121), before, self.event(seconds=-121)])

        events = self.summarize([self.error_at.isoformat(), self.error_at.isoformat()])["events"]

        self.assertEqual(events["near_error_events"], [
            {"type": "rollback", "time": (self.error_at - timedelta(seconds=120)).isoformat()},
            {"type": "switched", "time": (self.error_at + timedelta(seconds=120)).astimezone(timezone.utc).isoformat()},
        ])
        self.assertFalse(events["near_error_events_truncated"])

    def test_unknown_malformed_and_secret_fields_are_excluded(self):
        markers = ("SECRET_PATH_MARKER", "SECRET_LABEL_MARKER", "SECRET_MESSAGE_MARKER", "SECRET_ID_MARKER")
        self.write_events([
            self.event(path=markers[0], label=markers[1], message=markers[2], id=markers[3]),
            {"event": markers[0], "at": self.error_at.isoformat()},
            {"event": "switched", "at": markers[1]},
            {"event": ["switched"], "at": self.error_at.isoformat()},
            None, [], 12, {"at": self.error_at.isoformat()},
        ])
        with (self.root / "state/events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write('\n{"event":')

        summary = self.summarize([self.error_at.isoformat(), markers[2], None, 1, float("inf"), float("nan")])

        self.assertEqual(summary["events"]["near_error_events"], [
            {"type": "switched", "time": self.error_at.isoformat()},
        ])
        raw = json.dumps(summary)
        for marker in markers:
            self.assertNotIn(marker, raw)
        self.assertNotIn(str(self.root), raw)
        self.assertTrue(all(set(row) == {"type", "time"} for row in summary["events"]["near_error_events"]))

    def test_event_window_keeps_latest_256_and_reports_truncation(self):
        for count in (256, 257):
            with self.subTest(count=count):
                rows = [self.event(seconds=(index - 128) / 2) for index in range(count)]
                self.write_events(list(reversed(rows)))

                events = self.summarize([self.error_at.isoformat()])["events"]

                self.assertEqual(events["near_error_events"], [
                    {"type": row["event"], "time": row["at"]} for row in rows[-256:]
                ])
                self.assertEqual(events["near_error_events_truncated"], count > 256)

    def test_empty_or_invalid_error_times_have_empty_opt_in_fields(self):
        self.write_events([self.event()])
        for event_times in ([], [None, False, 0, float("nan"), "invalid", "2026-99-99T12:00:00"], 10):
            with self.subTest(event_times=event_times):
                events = self.summarize(event_times)["events"]
                self.assertEqual(events["near_error_events"], [])
                self.assertFalse(events["near_error_events_truncated"])

    def test_default_and_empty_opt_in_skip_previous_and_stay_read_only(self):
        self.write_events([self.event("scan_completed", seconds=300 + i) for i in range(8)])
        self.write_events([self.event()], "events.previous.jsonl")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        allowed = {self.root / name for name in (
            "VERSION", "config.json", "state/status.json", "state/events.jsonl", "state/events.previous.jsonl",
        )}
        opened = []
        original_open = Path.open

        def read_only(path, *args, **kwargs):
            opened.append(path)
            self.assertIn(path, allowed)
            mode = args[0] if args else kwargs.get("mode", "r")
            self.assertFalse(any(flag in mode for flag in "wax+"))
            return original_open(path, *args, **kwargs)

        with ExitStack() as stack:
            stack.enter_context(patch.object(Path, "open", read_only))
            stack.enter_context(patch.object(Path, "mkdir", side_effect=AssertionError("must not create state")))
            for owner, name in (
                (dashboard.core, "Controller"), (dashboard.core, "load_config"),
                (dashboard.core.sqlite3, "connect"), (dashboard.core, "codex_connection_summary"),
                (dashboard.core.urllib.request, "build_opener"), (dashboard.core.urllib.request, "urlopen"),
                (dashboard.subprocess, "Popen"),
            ):
                stack.enter_context(patch.object(owner, name, side_effect=AssertionError("must stay local and read only")))
            default = json.loads(dashboard.diagnostic_summary(self.root))["events"]
            default_opened = list(opened)
            opened.clear()
            empty = self.summarize([])["events"]
            empty_opened = list(opened)
            opened.clear()
            window = self.summarize([self.error_at.isoformat()])["events"]

        self.assertEqual(empty_opened, default_opened)
        self.assertNotIn(self.root / "state/events.previous.jsonl", default_opened)
        self.assertIn(self.root / "state/events.previous.jsonl", opened)
        self.assertNotIn("near_error_events", default)
        self.assertNotIn("near_error_events_truncated", default)
        self.assertEqual(empty["installation_recent_events"], default["installation_recent_events"])
        self.assertEqual(empty["files"]["events.previous.jsonl"], "not_needed")
        self.assertEqual(window["near_error_events"], [{"type": "switched", "time": self.error_at.isoformat()}])
        self.assertEqual({path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}, before)

    def test_json_non_object_documents_are_invalid(self):
        for value in ([], None, "SECRET_CONFIG_MARKER", 1):
            with self.subTest(value=value):
                (self.root / "config.json").write_text(json.dumps(value), encoding="utf-8")
                (self.root / "state/status.json").write_text(json.dumps(value), encoding="utf-8")

                summary = self.summarize([])

                self.assertEqual(summary["config"]["file"], "invalid")
                self.assertEqual(summary["status"]["file"], "invalid")
                self.assertFalse(summary["status"]["current"])
                self.assertNotIn("SECRET_CONFIG_MARKER", json.dumps(summary))

    def test_rotation_between_file_reads_does_not_duplicate_window_or_claim_truncation(self):
        rows = [self.event(seconds=(index - 128) / 2) for index in range(256)]
        self.write_events(rows + [{"event": "unknown", "padding": "x" * (2 * 1024 * 1024)}])
        state = self.root / "state"
        current = state / "events.jsonl"
        self.assertGreater(current.stat().st_size, 2 * 1024 * 1024)
        original_open = Path.open
        rotated = False

        @contextmanager
        def rotate_after_close(stream):
            nonlocal rotated
            try:
                yield stream
            finally:
                stream.close()
                if not rotated:
                    rotated = True
                    optimizer.event("stopped")

        def open_with_rotation(path, *args, **kwargs):
            stream = original_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if path == current and mode == "r":
                return rotate_after_close(stream)
            return stream

        with patch.object(optimizer, "STATE", state), patch.object(Path, "open", open_with_rotation):
            events = self.summarize([self.error_at.isoformat()])["events"]

        self.assertTrue(rotated)
        self.assertFalse(events["near_error_events_truncated"])
        self.assertEqual(events["near_error_events"], [
            {"type": row["event"], "time": row["at"]} for row in rows
        ])
        self.assertEqual(events["installation_recent_events"], [
            {"type": row["event"], "time": row["at"]} for row in rows[-8:]
        ])


if __name__ == "__main__":
    unittest.main()
