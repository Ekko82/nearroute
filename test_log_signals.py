"""Offline Codex transport signals using a temporary SQLite log schema."""

from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import optimizer


RETRY = "codex_core::responses_retry"
CLIENT = "codex_http_client::client"
ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
TURN_A = "00000000-0000-0000-0000-000000000001"
TURN_B = "00000000-0000-0000-0000-000000000002"
SEND = "error sending request"
DECODE = "error decoding response body"


def body(kind=SEND, turn=TURN_A, retry=1, endpoint=ENDPOINT):
    return f'method=POST url={endpoint} turn_id="{turn}" retries={retry} max_retries=5 {kind}'


class LogSignalsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nearroute-log-signals-")
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "logs.sqlite"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute("""CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, ts_nanos INTEGER NOT NULL DEFAULT 0,
            level TEXT NOT NULL DEFAULT 'WARN', target TEXT NOT NULL, feedback_log_body TEXT NOT NULL,
            module_path TEXT, file TEXT, line INTEGER, thread_id TEXT, process_uuid TEXT,
            estimated_bytes INTEGER NOT NULL DEFAULT 0)""")
        self.db.commit()
        self.signals = optimizer.LogSignals(str(self.path))
        clock = patch.object(optimizer.time, "time", return_value=1000.0)
        self.clock = clock.start()
        self.addCleanup(clock.stop)

    def insert(self, text=None, *, stamp=1000, target=RETRY, process="process-a", thread="thread-a"):
        self.db.execute("INSERT INTO logs(ts,target,feedback_log_body,process_uuid,thread_id) VALUES(?,?,?,?,?)",
                        (stamp, target, text if text is not None else body(DECODE), process, thread))
        self.db.commit()

    def prime(self):
        self.assertEqual(self.signals.poll(), 0)
        self.assertIsNone(self.signals.last_new_error_at)

    def test_initial_poll_only_establishes_cursor_and_quiet_poll_has_no_new_notice(self):
        self.insert()
        self.prime()
        self.insert(stamp=1001)
        self.clock.return_value = 1001
        self.assertEqual(self.signals.poll(), 1)
        self.assertEqual(self.signals.last_new_error_at, 1001)
        self.assertEqual(self.signals.poll(), 1)
        self.assertIsNone(self.signals.last_new_error_at)

    def test_retry_errors_and_known_responses_post_sends_are_counted(self):
        self.prime()
        self.insert(DECODE)
        self.insert(SEND)
        self.insert(f"method=POST url={ENDPOINT} {SEND}", target=CLIENT)
        self.insert(f"{SEND} for url ({ENDPOINT})", target=CLIENT)
        excluded = [
            f'method=GET url={ENDPOINT} {SEND}',
            body(endpoint="https://example.org/health"),
            body(endpoint=ENDPOINT + "/other"),
            body(endpoint="https://chatgpt.com.evil.invalid/backend-api/codex/responses"),
            f"{SEND} for url (https://example.org/health?next={ENDPOINT})",
            f"url={ENDPOINT} {SEND} for url (https://example.org/health)",
            body(DECODE),
        ]
        for text in excluded:
            self.insert(text, target=CLIENT)
        self.insert(body(), target="unrelated_http_client")
        self.assertEqual(self.signals.poll(), 4)
        self.assertEqual(self.signals.last_new_error_at, 1000)

    def test_complete_cross_logger_duplicates_pair_one_to_one_across_polls(self):
        self.prime()
        self.insert(body(), target=CLIENT)
        self.assertEqual(self.signals.poll(), 1)
        self.clock.return_value = 1005
        self.insert(body(), target=RETRY, stamp=1001)
        self.assertEqual(self.signals.poll(), 1)
        self.assertIsNone(self.signals.last_new_error_at)
        # The first pair cannot absorb another event with the same context.
        self.insert(body(), target=RETRY, stamp=1001)
        self.insert(body(), target=CLIENT, stamp=1002)
        self.assertEqual(self.signals.poll(), 2)
        self.assertEqual(self.signals.last_new_error_at, 1001)

    def test_same_logger_records_and_distinct_contexts_are_not_merged(self):
        self.prime()
        self.insert(body(), target=CLIENT)
        self.insert(body(), target=CLIENT)
        self.insert(body(), target=RETRY)
        self.insert(body(), target=RETRY)
        self.insert(body(turn=TURN_B), target=RETRY)
        self.insert(body(), target=RETRY, thread="thread-b")
        self.insert(body(), target=RETRY, process="process-b")
        self.insert(body(retry=2), target=RETRY)
        self.insert(body(DECODE), target=RETRY)
        self.assertEqual(self.signals.poll(), 7)

    def test_missing_metadata_and_outside_pair_window_are_not_guessed_duplicates(self):
        self.prime()
        self.insert(body(), target=CLIENT, stamp=995)
        self.insert(body(), target=RETRY, stamp=998)
        self.insert(body(), target=CLIENT, process=None)
        self.insert(body(), target=RETRY, process=None)
        self.insert(f"url={ENDPOINT} {SEND}", target=CLIENT)
        self.insert(SEND, target=RETRY)
        self.assertEqual(self.signals.poll(), 6)
        self.assertEqual(self.signals.last_new_error_at, 1000)

    def test_ansi_metadata_is_parsed_without_retaining_bodies(self):
        self.prime()
        text = body().replace("turn_id", "\x1b[3mturn_id\x1b[0m").replace("retries=1", "retries=\x1b[0m1")
        self.insert(text, target=CLIENT)
        self.insert(text, target=RETRY)
        self.assertEqual(self.signals.poll(), 1)
        self.assertNotIn(text, repr(vars(self.signals)))

    def test_180_second_window_expires_out_of_order_rows_and_skips_old_insertions(self):
        self.prime()
        self.insert(DECODE, stamp=1000)
        self.insert(DECODE, stamp=825)
        self.insert(DECODE, stamp=819)
        self.assertEqual(self.signals.poll(), 2)
        self.assertEqual(self.signals.last_new_error_at, 1000)
        self.clock.return_value = 1006
        self.assertEqual(self.signals.poll(), 1)
        self.assertIsNone(self.signals.last_new_error_at)
        self.clock.return_value = 1180
        self.assertEqual(self.signals.poll(), 1)  # Exactly 180 seconds is retained.
        self.clock.return_value = 1181
        self.assertEqual(self.signals.poll(), 0)

    def test_newly_inserted_old_error_reports_its_timestamp_without_increasing_count(self):
        self.prime()
        self.insert(DECODE, stamp=819)
        self.assertEqual(self.signals.poll(), 0)
        self.assertEqual(self.signals.last_new_error_at, 819)
        self.assertEqual(self.signals.poll(), 0)
        self.assertIsNone(self.signals.last_new_error_at)

    def test_rows_inserted_after_max_id_are_read_once_on_the_next_poll(self):
        self.prime()
        self.insert(DECODE)
        real_connect = sqlite3.connect
        connection_closed = []
        fixture = self

        class Reader:
            def __init__(self, *args, **kwargs):
                fixture.assertTrue(kwargs.get("uri"))
                fixture.assertIn("mode=ro", args[0])
                self.connection = real_connect(*args, **kwargs)

            def execute(self, sql, parameters=()):
                cursor = self.connection.execute(sql, parameters)
                if sql == "SELECT MAX(id) FROM logs":
                    row = cursor.fetchone()
                    cursor.close()
                    fixture.insert(DECODE)

                    class Maximum:
                        def fetchone(self):
                            return row
                    return Maximum()
                return cursor

            def close(self):
                self.connection.close()
                connection_closed.append(True)

        with patch.object(optimizer.sqlite3, "connect", side_effect=Reader):
            self.assertEqual(self.signals.poll(), 1)
        self.assertEqual(connection_closed, [True])
        self.assertEqual(self.signals.poll(), 2)
        self.assertEqual(self.signals.poll(), 2)
        self.assertIsNone(self.signals.last_new_error_at)

    def test_locked_database_preserves_count_and_does_not_leave_new_notice(self):
        self.prime()
        self.insert()
        self.assertEqual(self.signals.poll(), 1)
        self.db.execute("BEGIN EXCLUSIVE")
        try:
            self.assertEqual(self.signals.poll(), 1)
            self.assertIsNone(self.signals.last_new_error_at)
        finally:
            self.db.rollback()
        self.insert(stamp=1001)
        self.clock.return_value = 1001
        self.assertEqual(self.signals.poll(), 2)

    def test_bad_missing_and_disabled_databases_do_not_crash_or_create_files(self):
        bad = self.path.parent / "bad.sqlite"
        bad.write_bytes(b"not a SQLite database")
        missing = self.path.parent / "missing.sqlite"
        for path in (bad, missing, ""):
            with self.subTest(path=Path(path).name if path else "disabled"):
                signals = optimizer.LogSignals(str(path))
                self.assertEqual(signals.poll(), 0)
                self.assertIsNone(signals.last_new_error_at)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
