"""A diagnostic snapshot retains recent events when the live log rotates."""

from contextlib import contextmanager, ExitStack
from datetime import datetime, timedelta
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import dashboard
import optimizer
import test_diagnostics


class DiagnosticRotationTests(unittest.TestCase):
    def test_rotation_after_first_event_file_keeps_recent_rollback(self):
        fixture = test_diagnostics.DiagnosticSummaryTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.populate()
        state = fixture.root / "state"
        current = state / "events.jsonl"
        previous = state / "events.previous.jsonl"
        old = [{"event": "scan_completed", "at": (fixture.now - timedelta(hours=1, seconds=i)).isoformat()}
               for i in range(8)]
        recent = [{"event": "rollback" if i == 7 else "scan_completed",
                   "at": (fixture.now - timedelta(seconds=40 - i)).isoformat()}
                  for i in range(8)]
        previous.write_text("\n".join(json.dumps(item) for item in old) + "\n", encoding="utf-8")
        padding = json.dumps({"event": "scan_completed", "at": (fixture.now - timedelta(minutes=2)).isoformat()}) + "\n"
        current.write_text(padding * ((2 * 1024 * 1024) // len(padding) + 1)
                           + "\n".join(json.dumps(item) for item in recent) + "\n", encoding="utf-8")
        self.assertGreater(current.stat().st_size, 2 * 1024 * 1024)
        real_open = Path.open
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
                    # This is a concurrent event after the first file's capture.
                    optimizer.event("stopped")

        def open_with_rotation(path, *args, **kwargs):
            stream = real_open(path, *args, **kwargs)
            mode = args[0] if args else kwargs.get("mode", "r")
            if path in (current, previous) and mode == "r":
                return rotate_after_close(stream)
            return stream

        with ExitStack() as stack:
            stack.enter_context(patch.object(optimizer, "STATE", state))
            stack.enter_context(patch.object(Path, "open", open_with_rotation))
            for owner, name in ((optimizer, "Controller"), (optimizer.sqlite3, "connect"),
                                (optimizer.urllib.request, "urlopen"), (dashboard.subprocess, "Popen")):
                stack.enter_context(patch.object(owner, name, side_effect=AssertionError("diagnostics must stay local")))
            summary = json.loads(dashboard.diagnostic_summary(fixture.root))

        self.assertTrue(rotated)
        events = summary["events"]["installation_recent_events"]
        self.assertIn({"type": "rollback", "time": recent[-1]["at"]}, events)
        self.assertEqual(len(events), 8)
        self.assertTrue(all(datetime.fromisoformat(item["time"]) >= fixture.now - timedelta(minutes=1)
                            for item in events), "the recent snapshot must not fall back to hour-old events")


if __name__ == "__main__":
    unittest.main()
