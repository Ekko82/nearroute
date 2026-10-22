"""Real local HTTP reads must obey the whole-request budget and cancellation."""

import http.client
import socket
import threading
import time
import unittest
from unittest.mock import patch

import optimizer


class ProbeDeadlineTests(unittest.TestCase):
    def probe(self, prefix, tail=b"", *, cancel_after=None, quiet_after_cancel=False):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.5)
        port = listener.getsockname()[1]
        stop = threading.Event()
        cancelled = threading.Event()
        accepted, client_sockets, connections, responses = [], [], [], []
        requests = []

        def serve():
            try:
                conn, _ = listener.accept()
                accepted.append(conn)
                with conn:
                    conn.settimeout(0.5)
                    request = b""
                    while b"\r\n\r\n" not in request:
                        data = conn.recv(4096)
                        if not data:
                            return
                        request += data
                    requests.append(request)
                    if prefix:
                        conn.sendall(prefix)
                    for index, byte in enumerate(tail, 1):
                        if stop.wait(0.01):
                            return
                        conn.sendall(bytes((byte,)))
                        if index == cancel_after:
                            # Let the reader block awaiting the next byte before
                            # cancelling, then remain silent until its budget ends.
                            if quiet_after_cancel and stop.wait(0.05):
                                return
                            cancelled.set()
                            if quiet_after_cancel:
                                stop.wait(1)
                                return
            except OSError:
                # A deadline or cancellation closes the client mid-response.
                pass

        real_connection = http.client.HTTPConnection
        real_response = optimizer.ProbeHTTPResponse
        real_create = socket.create_connection

        def connection_factory(host, selected_port, **kwargs):
            self.assertEqual((host, selected_port), ("127.0.0.1", port))
            # Also shorten the old implementation's socket timeout: arriving
            # bytes must not restart this budget indefinitely.
            kwargs["timeout"] = 0.2
            connection = real_connection(host, selected_port, **kwargs)
            connections.append(connection)
            return connection

        def local_connection(address, *args, **kwargs):
            self.assertEqual(address, ("127.0.0.1", port))
            conn = real_create(address, *args, **kwargs)
            client_sockets.append(conn)
            return conn

        class TrackedResponse(real_response):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                responses.append(self)

        thread = threading.Thread(target=serve, name="nearroute-probe-deadline-test")
        thread.start()
        try:
            started = time.monotonic()
            with patch.object(optimizer, "PROBE_REQUEST_TIMEOUT", 0.2, create=True), \
                 patch.object(optimizer.http.client, "HTTPConnection", side_effect=connection_factory), \
                 patch.object(optimizer, "ProbeHTTPResponse", TrackedResponse), \
                 patch.object(socket, "create_connection", side_effect=local_connection):
                result = optimizer.verify_current_path(
                    {"port": port}, probe_url="http://fixture.invalid/check", expected_statuses=(200,),
                    cancel=cancelled.is_set if cancel_after is not None else None,
                )
            elapsed = time.monotonic() - started
        finally:
            stop.set()
            listener.close()
            for conn in accepted:
                try:
                    conn.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                conn.close()
            thread.join(2)
            for connection in connections:
                self.addCleanup(connection.close)
            self.assertFalse(thread.is_alive(), "fixture server thread did not stop")
            self.assertEqual(listener.fileno(), -1)
            self.assertTrue(all(conn.fileno() == -1 for conn in accepted))
        self.assertEqual(len(requests), 1)
        self.assertTrue(responses, "fixture must exercise the real HTTP response parser")
        self.assertTrue(all(response.isclosed() for response in responses))
        self.assertTrue(all(connection.sock is None for connection in connections))
        self.assertTrue(all(conn.fileno() == -1 for conn in client_sockets))
        return result, elapsed, cancelled.is_set()

    def assert_budget_expires(self, prefix, tail):
        result, elapsed, _ = self.probe(prefix, tail)
        self.assertFalse(result["ok"], result)
        self.assertFalse(result.get("cancelled", False), result)
        self.assertLess(elapsed, 1.0, "a 0.2-second budget must bound a continuously arriving response")

    def test_deadline_bounds_status_line_and_response_headers(self):
        cases = (
            (b"", b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"),
            (b"HTTP/1.1 200 OK\r\nX-Note: ", b"x" * 48 + b"\r\nContent-Length: 0\r\n\r\n"),
        )
        for index, (prefix, tail) in enumerate(cases):
            with self.subTest(phase=("status", "headers")[index]):
                self.assert_budget_expires(prefix, tail)

    def test_deadline_bounds_body_chunk_data_chunk_size_and_trailer(self):
        chunked = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        cases = (
            ("content_length", b"HTTP/1.1 200 OK\r\nContent-Length: 48\r\n\r\n", b"x" * 48),
            ("connection_close", b"HTTP/1.1 200 OK\r\nConnection: close\r\nContent-Length: 48\r\n\r\n", b"x" * 48),
            ("chunk_data", chunked + b"30\r\n", b"x" * 48 + b"\r\n0\r\n\r\n"),
            ("chunk_size", chunked, b"1;" + b"x" * 48 + b"\r\nx\r\n0\r\n\r\n"),
            ("trailer", chunked + b"1\r\nx\r\n0\r\nX-Note: ", b"x" * 48 + b"\r\n\r\n"),
        )
        for name, prefix, tail in cases:
            with self.subTest(phase=name):
                self.assert_budget_expires(prefix, tail)

    def test_cancel_during_body_read_is_cancelled_instead_of_bad_health(self):
        for quiet in (False, True):
            with self.subTest(quiet_after_cancel=quiet):
                result, elapsed, cancelled = self.probe(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 48\r\n\r\n", b"x" * 48,
                    cancel_after=2, quiet_after_cancel=quiet,
                )
                self.assertTrue(cancelled)
                self.assertFalse(result["ok"], result)
                self.assertTrue(result.get("cancelled"), result)
                self.assertLess(elapsed, 1.0)

    def test_small_complete_response_still_succeeds_with_request_budget(self):
        result, _, _ = self.probe(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["http_status"], 200)


if __name__ == "__main__":
    unittest.main()
