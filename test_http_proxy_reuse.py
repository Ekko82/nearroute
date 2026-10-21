"""Mihomo's ordinary HTTP proxy requires Proxy-Connection for reuse."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import http.client
import socket
import threading
import unittest
from unittest.mock import Mock, patch

import optimizer
import test_probe_interim as interim
import test_runtime as runtime


@contextmanager
def virtual_idle():
    clock = [0.0]

    def sleep(seconds):
        clock[0] = round(clock[0] + seconds, 6)

    with patch.object(optimizer.time, "monotonic", side_effect=lambda: clock[0]), \
         patch.object(optimizer.time, "sleep", side_effect=sleep):
        yield


class HTTPProxyReuseTests(unittest.TestCase):
    def test_single_health_and_three_checks_use_mihomo_proxy_keep_alive(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self):
                # Match listener/http/proxy.go: Connection alone does not keep
                # an ordinary HTTP proxy connection alive.
                keep = self.headers.get("Proxy-Connection", "").strip().lower() == "keep-alive"
                requests.append((self.connection, self.path, keep))
                self.send_response(200)
                self.send_header("Content-Length", "2")
                self.send_header("Connection", "keep-alive" if keep else "close")
                self.end_headers()
                self.wfile.write(b"ok")
                self.wfile.flush()
                self.close_connection = not keep

            def log_message(self, *_args):
                pass

        server = HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                                  name="nearroute-http-reuse-test")
        create_connection = socket.create_connection

        def only_fixture(address, *args, **kwargs):
            self.assertEqual(address, ("127.0.0.1", server.server_port))
            return create_connection(address, *args, **kwargs)

        thread.start()
        try:
            with patch.object(socket, "create_connection", side_effect=only_fixture), virtual_idle():
                for idle in (0, 1):
                    result = optimizer.verify_current_path(
                        {"port": server.server_port}, idle_seconds=idle,
                        probe_url="http://fixture.invalid/check", expected_statuses=(200,),
                    )
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(len(result["request_ms"]), 3 if idle else 1)
                    self.assertEqual(result["same_connection"], bool(idle))
            self.assertEqual(len(requests), 4)
            self.assertTrue(all(path == "http://fixture.invalid/check" and keep for _, path, keep in requests))
            self.assertTrue(all(conn is requests[1][0] for conn, _, _ in requests[1:]))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive(), "fixture HTTP thread did not stop")
            self.assertEqual(server.socket.fileno(), -1)

    def test_https_origin_request_does_not_receive_proxy_connection_header(self):
        connection = Mock()
        response = Mock(status=200, length=0)
        response.read.return_value = b""
        connection.getresponse.return_value = response
        with patch.object(optimizer.http.client, "HTTPSConnection", return_value=connection), \
             patch.object(optimizer.http.client, "HTTPConnection", side_effect=AssertionError("plain HTTP unexpected")):
            result = optimizer.verify_current_path(
                {"port": 10090}, probe_url="https://fixture.invalid/check", expected_statuses=(200,),
            )
        self.assertTrue(result["ok"], result)
        connection.set_tunnel.assert_called_once_with("fixture.invalid", 443)
        self.assertNotIn("proxy-connection", {name.lower() for name in connection.request.call_args.kwargs["headers"]})
        connection.close.assert_called_once()

    def test_healthy_http_candidate_commits_without_rollback_or_quarantine(self):
        fixture = runtime.RuntimeTests("test_scan_keeps_nodes_visible_and_publishes_progress_samples")
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.cfg.update(probe_url="http://fixture.invalid/check", probe_expected_statuses=[200])
        fixture.helper.reload_config()

        class ProxyWireSocket(interim.WireSocket):
            def makefile(self, *_args):
                keep = b"\r\nproxy-connection: keep-alive\r\n" in self.sent[-1].lower()
                header = b"keep-alive" if keep else b"close"
                stream = interim.WireStream(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: " + header + b"\r\n\r\nok",
                )
                self.streams.append(stream)
                return stream

        sock = ProxyWireSocket([])
        connection = http.client.HTTPConnection("127.0.0.1", 10090)
        self.addCleanup(connection.close)
        connect = Mock(side_effect=lambda: setattr(connection, "sock", sock))
        connection.connect = connect
        with patch.object(optimizer, "verify_current_path", side_effect=runtime.REAL_VERIFY), \
             patch.object(optimizer.http.client, "HTTPConnection", return_value=connection), virtual_idle():
            changed = fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast")
        self.assertTrue(changed)
        self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
        self.assertEqual(fixture.api.proxies["Main"]["now"], "JP fast")
        self.assertNotIn("JP fast", fixture.helper.quarantine)
        self.assertEqual(fixture.helper.record["name"], "JP fast")
        self.assertTrue(fixture.helper.record["verification"]["same_connection"])
        self.assertNotIn("candidate_rejected", [event["event"] for event in fixture.events()])
        connect.assert_called_once()
        self.assertEqual(len(sock.sent), 3)
        self.assertTrue(sock.closed)
        self.assertTrue(all(stream.closed for stream in sock.streams))


if __name__ == "__main__":
    unittest.main()
