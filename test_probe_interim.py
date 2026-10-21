"""Parse interim HTTP responses through real http.client without network I/O."""

import http.client
import io
import unittest
from unittest.mock import patch

import optimizer


class WireStream(io.BytesIO):
    def close(self):
        if not self.closed:
            self.consumed = self.tell()
        super().close()


class WireSocket:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.streams = []
        self.sent = []
        self.closed = False

    def sendall(self, data):
        self.sent.append(data)

    def makefile(self, *_args):
        stream = WireStream(next(self.responses))
        self.streams.append(stream)
        return stream

    def close(self):
        self.closed = True


class ProbeInterimTests(unittest.TestCase):
    def verify(self, responses, *, scheme="http", idle_seconds=0, tunnel_response=None):
        # Both production branches use the real HTTPConnection state machine;
        # only transport creation is replaced. HTTPS still sets its real tunnel.
        connection = http.client.HTTPConnection("127.0.0.1", 10090)
        self.addCleanup(connection.close)
        sock = WireSocket(([tunnel_response] if tunnel_response is not None else []) + responses)

        def connect():
            connection.sock = sock
            if tunnel_response is not None:
                connection._tunnel()

        connection.connect = connect
        clock = [0.0]

        def sleep(seconds):
            clock[0] = round(clock[0] + seconds, 6)

        with patch.object(optimizer.http.client, "HTTPConnection", return_value=connection) as plain, \
             patch.object(optimizer.http.client, "HTTPSConnection", return_value=connection) as secure, \
             patch.object(optimizer.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(optimizer.time, "sleep", side_effect=sleep):
            result = optimizer.verify_current_path(
                {"mixed-port": 10090}, idle_seconds=idle_seconds,
                probe_url=f"{scheme}://fixture.invalid/check", expected_statuses=(200,),
            )
        (secure if scheme == "https" else plain).assert_called_once()
        (plain if scheme == "https" else secure).assert_not_called()
        self.assertTrue(sock.closed)
        self.assertTrue(all(stream.closed for stream in sock.streams))
        return result, sock

    def test_early_hints_then_expected_final_status_succeeds_for_http_and_https(self):
        wire = (b"HTTP/1.1 103 Early Hints\r\nLink: </asset>; rel=preload\r\n\r\n"
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                result, sock = self.verify([wire], scheme=scheme)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["http_status"], 200)
                self.assertEqual(sock.streams[0].consumed, len(wire))

    def test_multiple_interim_responses_are_consumed_before_each_reused_request(self):
        wire = (b"HTTP/1.1 100 Continue\r\n\r\n"
                b"HTTP/1.1 102 Processing\r\nX-Progress: pending\r\n\r\n"
                b"HTTP/1.1 103 Early Hints\r\nLink: </asset>; rel=preload\r\n\r\n"
                b"HTTP/1.1 109 Unknown Interim\r\n\r\n"
                b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        for scheme in ("http", "https"):
            with self.subTest(scheme=scheme):
                result, sock = self.verify([wire] * 3, scheme=scheme, idle_seconds=1)
                self.assertTrue(result["ok"], result)
                self.assertEqual(result["http_status"], 200)
                self.assertTrue(result["same_connection"])
                self.assertEqual(result["same_tls_connection"], scheme == "https")
                self.assertEqual(len(result["request_ms"]), 3)
                self.assertEqual(len(sock.sent), 3)
                self.assertEqual([stream.consumed for stream in sock.streams], [len(wire)] * 3)

    def test_unexpected_final_status_after_interim_is_reported(self):
        wire = (b"HTTP/1.1 103 Early Hints\r\n\r\n"
                b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        result, _ = self.verify([wire])
        self.assertFalse(result["ok"])
        self.assertEqual(result["http_status"], 403)

    def test_truncated_final_body_after_interim_still_fails(self):
        for final in (
            b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\nshort",
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n14\r\nshort",
        ):
            with self.subTest(final_headers=final.split(b"\r\n\r\n")[0]):
                result, _ = self.verify([b"HTTP/1.1 103 Early Hints\r\n\r\n" + final])
                self.assertFalse(result["ok"])
                self.assertIn("IncompleteRead", result["reason"])

    def test_switching_protocols_is_final_and_does_not_wait_for_another_status(self):
        upgrade = (b"HTTP/1.1 101 Switching Protocols\r\n"
                   b"Connection: Upgrade\r\nUpgrade: websocket\r\n\r\n")
        for suffix in (b"", b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"):
            with self.subTest(has_later_status=bool(suffix)):
                result, sock = self.verify([upgrade + suffix])
                self.assertFalse(result["ok"])
                self.assertEqual(result["http_status"], 101)
                self.assertEqual(len(sock.sent), 1)
                self.assertEqual(sock.streams[0].consumed, len(upgrade))

    def test_connect_tunnel_uses_final_status_after_early_hints(self):
        final = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
        for status in (200, 403):
            with self.subTest(connect_status=status):
                tunnel = (b"HTTP/1.1 103 Early Hints\r\n\r\n"
                          + f"HTTP/1.1 {status} Tunnel Result\r\n\r\n".encode("ascii"))
                result, sock = self.verify([final], scheme="https", tunnel_response=tunnel)
                self.assertTrue(sock.sent[0].startswith(b"CONNECT fixture.invalid:443 HTTP/"))
                if status == 200:
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["http_status"], 200)
                    self.assertEqual(len(sock.sent), 2)
                else:
                    self.assertFalse(result["ok"])
                    self.assertIn("OSError", result["reason"])
                    self.assertEqual(len(sock.sent), 1)

    def test_interim_limit_allows_99_then_final_but_stops_after_100(self):
        for count in (99, 100):
            with self.subTest(interim_count=count):
                interim = b"".join(
                    f"HTTP/1.1 {100 if index % 2 == 0 else 103} Interim\r\n\r\n".encode("ascii")
                    for index in range(count)
                )
                final = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
                result, sock = self.verify([interim + final])
                if count == 99:
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(result["http_status"], 200)
                    self.assertEqual(sock.streams[0].consumed, len(interim + final))
                else:
                    self.assertFalse(result["ok"])
                    self.assertIn("HTTPException", result["reason"])
                    self.assertEqual(sock.streams[0].consumed, len(interim))


if __name__ == "__main__":
    unittest.main()
