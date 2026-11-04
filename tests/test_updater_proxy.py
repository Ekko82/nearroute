"""Updater proxy selection with isolated environments and loopback-only traffic."""

from contextlib import contextmanager
import http.server
import os
from pathlib import Path
import socket
import subprocess
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import updater


SYSTEM_PROXIES = {"http": "http://127.0.0.1:7890", "https": "http://127.0.0.1:7890"}
LOCAL_BYPASS = "localhost,127.0.0.1"


class UpdaterProxyTests(unittest.TestCase):
    @contextmanager
    def windows_environment(self, environment, proxies=None):
        with patch.dict(os.environ, environment, clear=True), \
             patch.object(updater.sys, "platform", "win32"), \
             patch.object(urllib.request, "getproxies_registry",
                          return_value=SYSTEM_PROXIES if proxies is None else proxies, create=True) as registry:
            yield registry

    def test_real_request_uses_system_proxy_when_environment_only_sets_no_proxy(self):
        connections = []

        class Proxy(http.server.BaseHTTPRequestHandler):
            def do_CONNECT(self):
                connections.append(self.path)
                self.send_error(502, "loopback fixture")

            def log_message(self, *_args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Proxy)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connect = socket.create_connection

        def loopback_only(address, *args, **kwargs):
            if address != server.server_address:
                raise AssertionError("the updater attempted to bypass the loopback proxy")
            return connect(address, *args, **kwargs)

        proxy = "http://127.0.0.1:" + str(server.server_port)
        try:
            with self.windows_environment({"NO_PROXY": LOCAL_BYPASS}, {"https": proxy}), \
                 patch.object(socket, "create_connection", side_effect=loopback_only):
                with self.assertRaises(urllib.error.URLError):
                    updater._open_url(updater.RELEASES_URL)
            self.assertEqual(connections, ["api.github.com:443"])
        finally:
            server.shutdown()
            server.server_close()
            worker.join(timeout=2)
        self.assertFalse(worker.is_alive())

    def test_only_bypass_environment_merges_system_routes_without_changing_environment(self):
        with self.windows_environment({"NO_PROXY": LOCAL_BYPASS}) as registry:
            before = dict(os.environ)
            self.assertEqual(updater._update_proxies(), SYSTEM_PROXIES | {"no": LOCAL_BYPASS})
            self.assertEqual(dict(os.environ), before)
            registry.assert_called_once_with()

    def test_explicit_proxy_environment_including_empty_values_keeps_existing_selection(self):
        for name in ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            for value in ("http://explicit.invalid:1234", ""):
                with self.subTest(name=name, value=value), \
                     self.windows_environment({"NO_PROXY": LOCAL_BYPASS, name: value}) as registry:
                    expected = urllib.request.getproxies()
                    self.assertEqual(updater._update_proxies(), expected)
                    registry.assert_not_called()

    def test_non_windows_keeps_existing_proxy_selection(self):
        with self.windows_environment({"NO_PROXY": LOCAL_BYPASS}) as registry, \
             patch.object(updater.sys, "platform", "linux"):
            self.assertEqual(updater._update_proxies(), urllib.request.getproxies())
            registry.assert_not_called()

    def test_metadata_manifest_and_package_keep_proxy_handler_and_safe_redirect(self):
        urls = [updater.RELEASES_URL,
                "https://github.com/Ekko82/nearroute/releases/download/v0.2.50/nearroute-update.json",
                "https://github.com/Ekko82/nearroute/releases/download/v0.2.50/NearRoute-0.2.50-windows.zip"]
        for url in urls:
            with self.subTest(url=url), self.windows_environment({"NO_PROXY": LOCAL_BYPASS}), \
                 patch.object(urllib.request, "build_opener") as build:
                response = updater._open_url(url)
                self.assertIs(response, build.return_value.open.return_value)
                handlers = build.call_args.args
                proxy_handlers = [handler for handler in handlers if isinstance(handler, urllib.request.ProxyHandler)]
                self.assertEqual(len(proxy_handlers), 1)
                self.assertEqual(proxy_handlers[0].proxies, SYSTEM_PROXIES | {"no": LOCAL_BYPASS})
                self.assertEqual(sum(isinstance(handler, updater.SafeRedirect) for handler in handlers), 1)
                request = build.return_value.open.call_args.args[0]
                self.assertEqual(request.full_url, url)
                self.assertEqual(build.return_value.open.call_args.kwargs, {"timeout": 15})

    def test_no_proxy_bypass_still_applies_to_the_explicit_handler(self):
        with self.windows_environment({"NO_PROXY": "api.github.com"}), \
             patch.object(urllib.request, "build_opener") as build:
            updater._open_url(updater.RELEASES_URL)
            handler = next(handler for handler in build.call_args.args
                           if isinstance(handler, urllib.request.ProxyHandler))
            bypassed = urllib.request.Request(updater.RELEASES_URL)
            routed = urllib.request.Request("https://github.com/Ekko82/nearroute")
            handler.https_open(bypassed)
            handler.https_open(routed)
            self.assertEqual(bypassed.host, "api.github.com")
            self.assertEqual(routed.host, "127.0.0.1:7890")
            self.assertEqual(routed._tunnel_host, "github.com")

    def test_git_fetch_gets_system_proxy_only_in_child_environment(self):
        result = subprocess.CompletedProcess([], 0, stdout=b"fixture", stderr=b"")
        with self.windows_environment({"NO_PROXY": LOCAL_BYPASS}), \
             patch.object(updater.subprocess, "run", return_value=result) as run:
            before = dict(os.environ)
            self.assertEqual(updater._git(Path("fixture"), "status", "--porcelain"), b"fixture")
            updater._git(Path("fixture"), "fetch", updater.SOURCE_URL, "refs/tags/v0.2.50")
            local, fetch = run.call_args_list
            self.assertNotIn("HTTPS_PROXY", local.kwargs["env"])
            self.assertEqual(fetch.kwargs["env"]["HTTPS_PROXY"], SYSTEM_PROXIES["https"])
            self.assertEqual(fetch.kwargs["env"]["NO_PROXY"], LOCAL_BYPASS)
            self.assertEqual(fetch.args[0], ["git", "-C", "fixture", "fetch", updater.SOURCE_URL,
                                             "refs/tags/v0.2.50"])
            self.assertEqual(dict(os.environ), before)

    def test_git_fetch_preserves_explicit_routes_and_bypass(self):
        environments = [{"NO_PROXY": "github.com"},
                        {"NO_PROXY": LOCAL_BYPASS, "HTTPS_PROXY": "http://explicit.invalid:1234"},
                        {"NO_PROXY": LOCAL_BYPASS, "https_proxy": ""},
                        {"NO_PROXY": LOCAL_BYPASS, "HTTP_PROXY": "http://explicit.invalid:1234"},
                        {"NO_PROXY": LOCAL_BYPASS, "ALL_PROXY": "socks5://explicit.invalid:1234"}]
        result = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        for environment in environments:
            with self.subTest(environment=environment), self.windows_environment(environment), \
                 patch.object(updater.subprocess, "run", return_value=result) as run:
                updater._git(Path("fixture"), "fetch", updater.SOURCE_URL)
                child = run.call_args.kwargs["env"]
                self.assertEqual({key: value for key, value in child.items() if key.lower().endswith("_proxy")},
                                 {key: value for key, value in os.environ.items() if key.lower().endswith("_proxy")})

    def test_git_fetch_preserves_explicit_git_configuration_and_non_windows_environment(self):
        environment = {"NO_PROXY": LOCAL_BYPASS, "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "http.proxy",
                       "GIT_CONFIG_VALUE_0": "http://git-explicit.invalid:4321"}
        result = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with self.windows_environment(environment), \
             patch.object(updater.subprocess, "run", return_value=result) as run:
            updater._git(Path("fixture"), "fetch", updater.SOURCE_URL)
            child = run.call_args.kwargs["env"]
            for name, value in environment.items():
                self.assertEqual(child[name], value)
            self.assertNotIn("-c", run.call_args.args[0])
            with patch.object(updater.sys, "platform", "linux"):
                updater._git(Path("fixture"), "fetch", updater.SOURCE_URL)
            self.assertNotIn("HTTPS_PROXY", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
