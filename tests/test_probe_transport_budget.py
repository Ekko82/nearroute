"""The real HTTPS transport must share one request budget across its stages."""

import http.client
import unittest
from unittest.mock import Mock, patch

import optimizer
from tests.test_probe_interim import WireSocket, WireStream


class ProbeTransportBudgetTests(unittest.TestCase):
    def verify(self, *, tcp_seconds=2, tunnel_seconds=2, tls_seconds=1, idle_seconds=0):
        clock = [0.0]
        timeouts = {"tcp": [], "tls": [], "gets": []}
        case = self

        class TimedStream(WireStream):
            delay = tunnel_seconds

            def readline(self, size=-1):
                clock[0] += self.delay
                self.delay = 0
                return super().readline(size)

        class TransportSocket(WireSocket):
            def setsockopt(self, *_args):
                pass

            def sendall(self, data):
                if data.startswith(b"GET "):
                    timeouts["gets"].append(self.timeout)
                super().sendall(data)

            def makefile(self, *_args):
                stream = (TimedStream if not self.streams else WireStream)(next(self.responses))
                self.streams.append(stream)
                return stream

        sock = TransportSocket([
            b"HTTP/1.1 200 Connection established\r\n\r\n",
            *([b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"] * 3),
        ])

        def create_connection(address, timeout, _source_address):
            self.assertEqual(address, ("127.0.0.1", 10090))
            timeouts["tcp"].append(timeout)
            sock.settimeout(timeout)
            clock[0] += tcp_seconds
            return sock

        def wrap_socket(transport, *, server_hostname):
            case.assertIs(transport, sock)
            case.assertEqual(server_hostname, "fixture.invalid")
            case.assertFalse(sock.closed, "closing the CONNECT response must retain the transport")
            case.assertTrue(sock.streams[0].closed, "the CONNECT response file must be released")
            timeouts["tls"].append(sock.timeout)
            clock[0] += tls_seconds
            return sock

        context = Mock(wrap_socket=wrap_socket)
        connection = http.client.HTTPSConnection("127.0.0.1", 10090, context=context)
        connection._create_connection = create_connection
        self.addCleanup(connection.close)

        def sleep(seconds):
            clock[0] = round(clock[0] + seconds, 6)

        with patch.object(optimizer.http.client, "HTTPSConnection", return_value=connection), \
             patch.object(optimizer.ssl, "create_default_context", return_value=context), \
             patch.object(optimizer.time, "monotonic", side_effect=lambda: clock[0]), \
             patch.object(optimizer.time, "sleep", side_effect=sleep):
            result = optimizer.verify_current_path(
                {"mixed-port": 10090}, probe_url="https://fixture.invalid/check",
                expected_statuses=(200,), idle_seconds=idle_seconds,
            )
        self.assertTrue(sock.closed)
        self.assertIsNone(connection.sock)
        self.assertTrue(all(stream.closed for stream in sock.streams))
        return result, timeouts, clock[0]

    def test_tcp_connect_and_tls_share_budget_then_reuse_excludes_idle(self):
        result, timeouts, elapsed = self.verify(idle_seconds=2)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["same_tls_connection"])
        self.assertEqual(timeouts["tcp"], [7])
        self.assertEqual(timeouts["tls"], [3])
        self.assertEqual(timeouts["gets"], [2, 7, 7])
        self.assertEqual(elapsed, 9)

    def test_exhausted_transport_budget_never_sends_get(self):
        cases = (
            ("tcp", {"tcp_seconds": 8}, []),
            ("connect", {"tunnel_seconds": 6}, []),
            ("tls", {"tls_seconds": 4}, [3]),
        )
        for stage, timing, expected_tls in cases:
            with self.subTest(stage=stage):
                result, timeouts, _ = self.verify(**timing)
                self.assertFalse(result["ok"], result)
                self.assertIn("TimeoutError", result["reason"])
                self.assertEqual(timeouts["gets"], [])
                self.assertEqual(timeouts["tls"], expected_tls)


if __name__ == "__main__":
    unittest.main()
