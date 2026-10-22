"""Opt-in Codex diagnostics read a bounded, sanitized SQLite snapshot."""

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import optimizer


NOW = 2000000000
RETRY = "codex_core::responses_retry"
CLIENT = "codex_http_client::client"
REQUEST = "codex_http_client::request"
ENDPOINT = "https://chatgpt.com/backend-api/codex/responses"
TURN = "00000000-0000-0000-0000-000000000001"
DECODE = "stream disconnected before completion: Transport error: network error: error decoding response body"


def at(stamp):
    return datetime.fromtimestamp(stamp, optimizer.TZ).isoformat(timespec="milliseconds")


def retry(error=DECODE, *, number=1):
    return (f"stream disconnected - retrying sampling request ({number}/5 in 204ms)... "
            f"turn_id={TURN} retries={number} max_retries=5 sampling_error={error}")


class ConnectionDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="nearroute-connection-diagnostics-")
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "logs.sqlite"
        self.db = sqlite3.connect(self.path)
        self.addCleanup(self.db.close)
        self.db.execute("""CREATE TABLE logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, ts_nanos INTEGER NOT NULL DEFAULT 0,
            level TEXT NOT NULL DEFAULT 'WARN', target TEXT NOT NULL, feedback_log_body TEXT NOT NULL,
            module_path TEXT, file TEXT, line INTEGER, thread_id TEXT, process_uuid TEXT,
            estimated_bytes INTEGER NOT NULL DEFAULT 0)""")
        self.db.execute("CREATE INDEX idx_logs_ts ON logs(ts DESC,ts_nanos DESC,id DESC)")
        self.db.commit()

    def insert(self, body=None, *, stamp=NOW, nanos=0, target=RETRY, level="WARN",
               process="PROCESS_SECRET", thread="THREAD_SECRET"):
        self.db.execute("""INSERT INTO logs(ts,ts_nanos,level,target,feedback_log_body,process_uuid,thread_id)
            VALUES(?,?,?,?,?,?,?)""", (stamp, nanos, level, target, retry() if body is None else body, process, thread))
        self.db.commit()

    def summarize(self, **kwargs):
        return optimizer.codex_connection_summary(self.path, now=kwargs.pop("now", NOW), **kwargs)

    def test_states_are_fixed_codes_and_never_create_missing_files(self):
        missing = self.path.parent / "PRIVATE_PATH.sqlite"
        for value in (None, ""):
            self.assertEqual(optimizer.codex_connection_summary(value, now=NOW)["state"], "not_configured")
        self.assertEqual(optimizer.codex_connection_summary(missing, now=NOW)["state"], "missing")
        self.assertFalse(missing.exists())
        bad = self.path.parent / "bad.sqlite"
        bad.write_bytes(b"PRIVATE_ERROR not a database")
        result = optimizer.codex_connection_summary(bad, now=NOW)
        self.assertEqual(result["state"], "unreadable")
        self.assertEqual(set(result), {"state", "window_seconds", "error_count", "errors", "transports", "truncated"})
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_unsupported_schema_or_missing_time_index_is_not_scanned(self):
        self.db.execute("DROP INDEX idx_logs_ts")
        self.db.commit()
        self.assertEqual(self.summarize()["state"], "unsupported")
        self.db.execute("DROP TABLE logs")
        self.db.execute("CREATE TABLE logs(ts INTEGER)")
        self.db.commit()
        self.assertEqual(self.summarize()["state"], "unsupported")

    def test_locked_database_is_busy_and_released(self):
        self.db.execute("BEGIN EXCLUSIVE")
        try:
            result = self.summarize()
        finally:
            self.db.rollback()
        self.assertEqual(result["state"], "busy")
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.summarize()["state"], "ok")

    def test_python_310_sqlite_errors_without_error_codes_are_classified(self):
        for message in ("database is locked", "database table is locked: main", "interrupted"):
            with self.subTest(message=message):
                with patch.object(optimizer.sqlite3, "connect", side_effect=sqlite3.OperationalError(message)):
                    result = self.summarize()
                self.assertEqual(result["state"], "busy")
                self.assertEqual(result["errors"], [])
                self.assertNotIn(message, json.dumps(result))

    def test_read_only_connection_includes_committed_wal_and_closes(self):
        self.db.execute("PRAGMA journal_mode=WAL")
        self.insert()
        original = sqlite3.connect
        closed = []
        fixture = self

        class Reader:
            def __init__(self, database, **kwargs):
                fixture.assertEqual(database, fixture.path.as_uri() + "?mode=ro")
                fixture.assertTrue(kwargs["uri"])
                fixture.assertEqual(kwargs["timeout"], 0.15)
                self.connection = original(database, **kwargs)

            def set_progress_handler(self, callback, steps):
                self.connection.set_progress_handler(callback, steps)

            def execute(self, sql, parameters=()):
                fixture.assertTrue(sql.lstrip().upper().startswith(("SELECT", "PRAGMA")))
                return self.connection.execute(sql, parameters)

            def close(self):
                self.connection.close()
                closed.append(True)

        before = self.path.read_bytes()
        wal = self.path.with_name(self.path.name + "-wal")
        wal_before = wal.read_bytes()
        with patch.object(optimizer.sqlite3, "connect", side_effect=Reader):
            result = self.summarize()
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(closed, [True])
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual(wal.read_bytes(), wal_before)

    def test_safe_retry_metadata_and_transport_enums_only(self):
        self.insert(retry() + ' secret="AUTH_SECRET" model="MODEL_SECRET" cwd="PATH_SECRET"')
        self.insert('request feedback_tags transport="responses_http" url=URL_SECRET', stamp=NOW-2, target=REQUEST, level="INFO")
        self.insert('transport=responses_websocket model=MODEL_SECRET', stamp=NOW-1,
                    target="codex_http_client::feedback_tags", level="DEBUG")
        self.insert('transport="responses_http"', stamp=NOW-1, target=REQUEST, level="INFO")
        result = self.summarize(now=datetime.fromtimestamp(NOW, optimizer.TZ))
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["window_seconds"], 7200)
        self.assertEqual(result["errors"], [{"at": at(NOW), "kind": "decode", "retry": 1,
                                             "max_retries": 5, "retry_wait_ms": 204.0}])
        self.assertEqual({row["transport"]: row["last_seen"] for row in result["transports"]},
                         {"responses_http": at(NOW-1), "responses_websocket": at(NOW-1)})
        raw = json.dumps(result)
        for secret in ("AUTH_SECRET", "MODEL_SECRET", "PATH_SECRET", "URL_SECRET", "PROCESS_SECRET", "THREAD_SECRET", TURN, ENDPOINT):
            self.assertNotIn(secret, raw)
        self.assertFalse(result["truncated"])

    def test_transcripts_similar_targets_and_configured_transports_are_excluded(self):
        for target in ("user_transcript", "unrelated_responses_retry", "codex_core::responses_retry::user", "codex_http_client::request_extra"):
            self.insert(retry() + ' transport="responses_websocket"', target=target)
        self.insert('transport="responses_websocket"', target=RETRY)
        self.insert('transport="responses_http_configured" transport="other"', target=REQUEST)
        self.insert(retry(), level="DEBUG")
        self.insert(retry('authentication failed 401: "error decoding response body"'))
        self.insert('sampling_error=' + DECODE)
        self.insert('method=GET url=' + ENDPOINT + ' error sending request', target=CLIENT)
        self.insert('method=POST url=https://example.invalid error sending request', target=CLIENT)
        result = self.summarize()
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["transports"], [])

    def test_known_error_kinds_require_trusted_retry_context(self):
        errors = (
            ("error sending request", "send"),
            ("stream disconnected before completion: websocket closed by server before response.completed", "websocket"),
            ("stream disconnected before completion: IO error: closed (os error 10054)", "reset"),
            ("stream disconnected before completion: tls handshake eof", "tls"),
        )
        for offset, (error, _) in enumerate(errors):
            self.insert(retry(error), stamp=NOW-offset)
        self.insert("stream disconnected before completion: tls handshake eof")
        result = self.summarize()
        self.assertEqual([row["kind"] for row in result["errors"]], [kind for _, kind in errors])
        self.assertEqual(result["error_count"], 4)

    def test_exact_two_hour_bounds_future_and_malformed_timestamps(self):
        self.insert(stamp=NOW-7200)
        self.insert(stamp=NOW-7201)
        self.insert(stamp=NOW+1)
        self.insert(stamp=NOW, nanos=1)
        self.insert(stamp=NOW-1, nanos=-1)
        self.insert(stamp=NOW-1, nanos="not-a-number")
        self.insert(stamp="not-a-timestamp")
        self.insert(stamp=NOW-2.5)
        result = self.summarize()
        self.assertEqual(result["error_count"], 1)
        self.assertEqual(result["errors"][0]["at"], at(NOW-7200))

    def test_milliseconds_distinguish_same_second_retry_observations(self):
        self.insert(stamp=NOW-1, nanos=123456789)
        self.insert(stamp=NOW-1, nanos=456789123)
        result = self.summarize()
        self.assertEqual(result["error_count"], 2)
        self.assertEqual([row["at"] for row in result["errors"]],
                         [at(NOW-1).replace(".000", ".456"), at(NOW-1).replace(".000", ".123")])

    def test_fractional_cutoff_includes_only_in_window_nanosecond_timestamps(self):
        self.insert(stamp=NOW-7200, nanos=750000000)
        self.insert(stamp=NOW-7200, nanos=250000000)
        result = self.summarize(now=NOW+0.5)
        self.assertEqual(result["error_count"], 1)
        self.assertTrue(result["errors"][0]["at"].endswith(".750+08:00"))

    def test_query_deadline_interrupts_as_busy_and_closes_connection(self):
        self.db.executemany("INSERT INTO logs(ts,target,feedback_log_body) VALUES(?,?,?)",
                            ((NOW, "user_transcript", "PRIVATE_BODY") for _ in range(2000)))
        self.db.commit()
        with patch.object(optimizer.time, "monotonic", side_effect=[0.0, 1.0]):
            result = self.summarize()
        self.assertEqual(result["state"], "busy")
        self.assertEqual(result["errors"], [])
        self.assertEqual(self.summarize()["state"], "ok")

    def test_cross_logger_pairs_are_deduplicated_but_same_logger_rows_are_not(self):
        body = (f'method=POST url={ENDPOINT} turn_id="{TURN}" retries=1 max_retries=5 '
                'error sending request: error decoding response body')
        self.insert(body, stamp=NOW, target=CLIENT)
        self.insert(retry(), stamp=NOW-1)
        self.insert(retry(), stamp=NOW-2)
        self.insert(body, stamp=NOW-3, target=CLIENT)
        self.insert(body, stamp=NOW-6, target=CLIENT)
        self.insert(retry(), stamp=NOW-9)
        self.insert(retry(), stamp=NOW-1, process=None)
        result = self.summarize()
        self.assertEqual(result["error_count"], 5)
        self.assertEqual(result["errors"][0]["at"], at(NOW))
        self.assertEqual(result["errors"][0]["retry_wait_ms"], 204.0)

    def test_display_is_latest_twenty_with_count_and_honest_truncation(self):
        for offset in range(21):
            self.insert(stamp=NOW-offset)
        result = self.summarize()
        self.assertEqual(result["error_count"], 21)
        self.assertEqual(len(result["errors"]), 20)
        self.assertEqual(result["errors"][0]["at"], at(NOW))
        self.assertEqual(result["errors"][-1]["at"], at(NOW-19))
        self.assertTrue(result["truncated"])

    def test_query_limit_is_reported_even_when_no_error_was_observed(self):
        self.db.executemany("INSERT INTO logs(ts,target,feedback_log_body) VALUES(?,?,?)",
                            ((NOW-offset % 7200, REQUEST, "unrecognized record") for offset in range(5001)))
        self.db.commit()
        result = self.summarize()
        self.assertEqual(result["state"], "ok")
        self.assertEqual(result["error_count"], 0)
        self.assertEqual(result["errors"], [])
        self.assertTrue(result["truncated"])


if __name__ == "__main__":
    unittest.main()
