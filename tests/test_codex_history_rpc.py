"""Exercise metadata RPC framing without Codex, user state, or network access."""

import io
import itertools
import json
import queue
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import codex_history as history


class HistoryRpcTests(unittest.TestCase):
    def client(self, proc):
        # Keep the actual JSONL reader, writer and request loop; startup is not
        # under test and must never launch the user's Codex engine.
        client = object.__new__(history.Client)
        client.proc = proc
        client.messages = queue.Queue()
        client.next_id = 1
        return client

    def stream_client(self, *messages):
        incoming = "".join(json.dumps(message) + "\n" for message in messages)
        proc = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(incoming.encode("utf-8")))
        self.addCleanup(proc.stdin.close)
        self.addCleanup(proc.stdout.close)
        client = self.client(proc)
        client.read()
        return client

    def scripted_client(self, script):
        proc = subprocess.Popen([sys.executable, "-u", "-c", textwrap.dedent(script)],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=history.FLAGS)
        client = self.client(proc)
        reader = threading.Thread(target=client.read, name="nearroute-rpc-fixture", daemon=True)
        self.addCleanup(self.stop_script, proc, reader)
        reader.start()
        return client, proc

    def stop_script(self, proc, reader):
        try:
            try:
                proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                # This is exclusively the scripted fixture started above.
                proc.kill()
                proc.wait(timeout=2)
        finally:
            reader.join(timeout=2)
            proc.stdout.close()
            proc.stderr.close()
        self.assertFalse(reader.is_alive(), "fixture JSONL reader did not stop")

    def test_colliding_server_request_is_rejected_before_real_result(self):
        client, proc = self.scripted_client(r'''
            import json
            import sys

            def send(message):
                print(json.dumps(message), flush=True)

            request = json.loads(sys.stdin.readline())
            assert request["method"] == "thread/resume"
            request_id = request["id"]
            send({"method": "thread/fixtureNotification", "params": {}})
            send({"id": request_id, "method": "fixture/private-server-method",
                  "params": {"private": "fixture-private-parameter"}})

            # Do not release the result until the client answers the server's
            # same-ID request. Silently ignoring it must fail this regression.
            rejection = json.loads(sys.stdin.readline())
            assert rejection["id"] == request_id
            assert rejection["error"]["code"] == -32601
            assert isinstance(rejection["error"]["message"], str)
            assert "method" not in rejection and "result" not in rejection
            assert "fixture/private-server-method" not in json.dumps(rejection)
            assert "fixture-private-parameter" not in json.dumps(rejection)
            send({"id": request_id, "result": {"fixture_response": "actual-result"}})
        ''')

        result = client.request("thread/resume", {"threadId": "fixture-thread"})

        self.assertEqual(result, {"fixture_response": "actual-result"})
        self.assertEqual(proc.wait(timeout=2), 0, proc.stderr.read().decode("utf-8"))

    def test_notifications_are_ignored_and_null_is_a_valid_result(self):
        client = self.stream_client(
            {"method": "thread/fixtureNotification", "params": {}},
            {"id": 1, "result": None},
        )

        self.assertIsNone(client.request("thread/resume", {"threadId": "fixture-thread"}))
        sent = [json.loads(line) for line in client.proc.stdin.getvalue().splitlines()]
        self.assertEqual(sent, [{"id": 1, "method": "thread/resume",
                                 "params": {"threadId": "fixture-thread"}}])

    def test_notifications_do_not_bypass_request_deadline(self):
        client = self.stream_client(
            {"method": "thread/fixtureNotification", "params": {}},
            {"method": "thread/fixtureNotification", "params": {}},
            {"id": 1, "result": {"must_not_arrive_after_deadline": True}},
        )

        with patch.object(history.time, "monotonic", side_effect=itertools.count(0, 50)):
            with self.assertRaises(history.Refused) as caught:
                client.request("thread/resume", {"threadId": "fixture-thread"})
        self.assertIn("timed out", str(caught.exception))

    def test_actual_rpc_error_is_sanitized(self):
        private = "fixture-private-error-detail"
        client = self.stream_client({"id": 1, "error": {"code": -32603, "message": private}})

        with self.assertRaises(history.Refused) as caught:
            client.request("thread/resume", {"threadId": "fixture-thread"})

        self.assertNotIn(private, str(caught.exception))

    def test_matching_message_without_result_or_error_is_rejected(self):
        private = "fixture-private-malformed-detail"
        client = self.stream_client({"id": 1, "private": private})

        with self.assertRaises(history.Refused) as caught:
            client.request("thread/resume", {"threadId": "fixture-thread"})

        self.assertNotIn(private, str(caught.exception))


if __name__ == "__main__":
    unittest.main()
