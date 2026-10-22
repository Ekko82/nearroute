"""Local proxy binding selection; the socket fixture never forwards traffic."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, HTTPServer
import socket
import threading
import unittest
from unittest.mock import Mock, call, patch

import optimizer


class ProxyBindingTests(unittest.TestCase):
    @contextmanager
    def connections(self):
        connection = Mock()
        response = Mock(status=204, length=0)
        response.read.return_value = b""
        connection.getresponse.return_value = response
        with patch.object(optimizer.http.client, "HTTPConnection", return_value=connection) as plain, \
             patch.object(optimizer.http.client, "HTTPSConnection", return_value=connection) as secure:
            yield connection, plain, secure

    def test_ipv6_only_http_listener_is_reachable_from_reported_configs(self):
        calls = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                calls.append(self.path)
                self.send_response(204)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *_args):
                pass

        class IPv6Server(HTTPServer):
            address_family = socket.AF_INET6

            def server_bind(self):
                self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 1)
                super().server_bind()

        try:
            server = IPv6Server(("::1", 0), Handler)
        except OSError as error:
            self.skipTest("IPv6 loopback listener unavailable: " + type(error).__name__)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01},
                                  name="nearroute-ipv6-probe-test")
        create_connection = socket.create_connection

        def local_connection(address, *args, **kwargs):
            self.assertIn(address[0], ("127.0.0.1", "::1"))
            self.assertEqual(address[1], server.server_port)
            return create_connection(address, *args, **kwargs)

        thread.start()
        try:
            with patch.object(socket, "create_connection", side_effect=local_connection):
                result = optimizer.verify_current_path(
                    {"allow-lan": True, "bind-address": "[::1]", "mixed-port": 0, "port": server.server_port},
                    probe_url="http://fixture.invalid/check", expected_statuses=(204,),
                )
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["http_status"], 204)
            self.assertEqual(calls, ["http://fixture.invalid/check"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(2)
            self.assertFalse(thread.is_alive(), "fixture HTTP thread did not stop")
            self.assertEqual(server.socket.fileno(), -1)

    def test_http_and_https_use_the_loopback_for_the_reported_binding(self):
        cases = (
            (False, "[::1]", "127.0.0.1"),
            (False, "192.0.2.10", "127.0.0.1"),
            (True, "*", "127.0.0.1"),
            (True, "", "127.0.0.1"),
            (True, "0.0.0.0", "127.0.0.1"),
            (True, "::", "::1"),
            (True, "[::]", "::1"),
            (True, "::1", "::1"),
            (True, "[::1]", "::1"),
            (True, "127.0.0.1", "127.0.0.1"),
            (True, "127.0.0.2", "127.0.0.2"),
            (True, "localhost", "127.0.0.1"),
        )
        for scheme in ("http", "https"):
            for allow_lan, binding, host in cases:
                with self.subTest(scheme=scheme, allow_lan=allow_lan, binding=binding), self.connections() as (connection, plain, secure):
                    result = optimizer.verify_current_path(
                        {"allow-lan": allow_lan, "bind-address": binding, "mixed-port": 7890, "port": 8090},
                        probe_url=f"{scheme}://fixture.invalid/check", expected_statuses=(204,),
                    )
                    self.assertTrue(result["ok"], result)
                    selected, unused = (secure, plain) if scheme == "https" else (plain, secure)
                    self.assertEqual(selected.call_args.args[:2], (host, 7890))
                    unused.assert_not_called()
                    connection.close.assert_called_once()

    def test_lan_only_address_or_hostname_is_rejected_without_connecting(self):
        for scheme in ("http", "https"):
            for binding in ("192.0.2.10", "proxy.fixture.invalid"):
                with self.subTest(scheme=scheme, binding=binding), self.connections() as (_connection, plain, secure):
                    result = optimizer.verify_current_path(
                        {"allow-lan": True, "bind-address": binding, "mixed-port": 7890},
                        probe_url=f"{scheme}://fixture.invalid/check", expected_statuses=(204,),
                    )
                    self.assertFalse(result["ok"])
                    self.assertTrue(result.get("reason"))
                    plain.assert_not_called()
                    secure.assert_not_called()

    def test_discovery_accepts_ipv6_and_rejects_bindings_that_cannot_be_probed(self):
        proxies = {"Main": {"type": "Selector", "all": ["JP fixture"], "now": "JP fixture"}}
        for binding in ("[::1]", "192.0.2.10", "proxy.fixture.invalid"):
            with self.subTest(binding=binding):
                api = Mock()
                api.request.side_effect = [
                    {"allow-lan": True, "bind-address": binding, "mixed-port": 7890, "mode": "rule"},
                    {"proxies": proxies},
                ]
                with patch.object(optimizer, "Controller", return_value=api):
                    if binding == "[::1]":
                        info = optimizer.inspect_controller({"group": "Main"})
                        self.assertEqual(info["proxy_port"], 7890)
                        self.assertEqual(info["groups"], ["Main"])
                    else:
                        with self.assertRaises((optimizer.ControllerError, ValueError)):
                            optimizer.inspect_controller({"group": "Main"})
                self.assertEqual(api.request.call_args_list, [call("/configs"), call("/proxies")])
                api.select.assert_not_called()

    def test_plain_http_port_fallback_and_missing_port(self):
        for general in ({"port": 8090}, {"mixed-port": 0, "port": 8090}, {"mixed-port": 0, "port": 0}):
            with self.subTest(configs=general), self.connections() as (connection, plain, secure):
                result = optimizer.verify_current_path(
                    general, probe_url="http://fixture.invalid/check", expected_statuses=(204,),
                )
                secure.assert_not_called()
                if general["port"]:
                    self.assertTrue(result["ok"], result)
                    self.assertEqual(plain.call_args.args[:2], ("127.0.0.1", 8090))
                    connection.close.assert_called_once()
                else:
                    self.assertFalse(result["ok"])
                    self.assertTrue(result.get("reason"))
                    plain.assert_not_called()


if __name__ == "__main__":
    unittest.main()
