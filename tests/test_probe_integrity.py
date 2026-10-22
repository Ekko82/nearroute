"""Real HTTP response parsing without sockets or a live proxy."""

import http.client
import io
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import optimizer


class ProbeIntegrityTests(unittest.TestCase):
    def response(self, wire):
        response = http.client.HTTPResponse(SimpleNamespace(makefile=lambda *_: io.BytesIO(wire)))
        self.addCleanup(response.close)
        response.begin()
        return response

    def test_declared_and_chunked_bodies_must_be_complete(self):
        cases = (
            (b"Content-Length: 5\r\n", b"short", True),
            (b"Content-Length: 20\r\n", b"short", False),
            (b"Transfer-Encoding: chunked\r\n", b"5\r\nshort\r\n0\r\n\r\n", True),
            (b"Transfer-Encoding: chunked\r\n", b"14\r\nshort", False),
            (b"Connection: close\r\n", b"short", True),
        )
        for scheme in ("http", "https"):
            for headers, body, complete in cases:
                with self.subTest(scheme=scheme, headers=headers, complete=complete):
                    response = self.response(b"HTTP/1.1 405 Method Not Allowed\r\n" + headers + b"\r\n" + body)
                    connection = Mock()
                    connection.getresponse.return_value = response
                    with patch.object(optimizer.http.client, "HTTPConnection", return_value=connection), \
                         patch.object(optimizer.http.client, "HTTPSConnection", return_value=connection):
                        result = optimizer.verify_current_path(
                            {"mixed-port": 10090}, probe_url=f"{scheme}://fixture.invalid/check",
                        )
                    self.assertEqual(result["ok"], complete)
                    if not complete:
                        self.assertIn("IncompleteRead", result["reason"])
                    connection.close.assert_called_once()

    def test_cancellation_at_the_end_of_idle_prevents_the_next_request(self):
        clock = [0.0]
        cancelled = [False]
        connection = Mock()
        connection.sock = Mock()
        connection.getresponse.side_effect = lambda: self.response(
            b"HTTP/1.1 405 Method Not Allowed\r\nContent-Length: 0\r\n\r\n",
        )

        def sleep(seconds):
            clock[0] += seconds
            if clock[0] >= 1:
                cancelled[0] = True

        with patch.object(optimizer.http.client, "HTTPConnection", return_value=connection), \
             patch.object(optimizer.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(optimizer.time, "sleep", side_effect=sleep):
            result = optimizer.verify_current_path(
                {"mixed-port": 10090}, idle_seconds=1, cancel=lambda: cancelled[0],
                probe_url="http://fixture.invalid/check",
            )
        self.assertTrue(result.get("cancelled"))
        self.assertEqual(connection.request.call_count, 1)
        connection.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
