"""Discovery uses synthetic inspectors and never probes user-owned clients."""

import os
import subprocess
import threading
import time
import unittest
from unittest.mock import patch

import client_detection as detection


class AdaptiveScopeTests(unittest.TestCase):
    def test_nearby_regions_tracks_current_nodes_without_worldwide_fallback(self):
        region_of = lambda name: name.split()[0] if " " in name else None
        self.assertEqual(detection.nearby_regions(["PH 1", "JP 2", "US 1", "JP 3", "TW 1"], region_of), ["JP", "TW", "PH"])
        self.assertEqual(detection.nearby_regions(["US 1", "unknown"], region_of), [])


class GroupRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.proxies = {
            "JP 1": {"type": "Shadowsocks"}, "TW 1": {"type": "VMess"},
            "DIRECT": {"type": "DIRECT"}, "REJECT-DROP": {"type": "REJECT-DROP"},
            "Main": {"type": "Selector", "all": ["JP 1", "DIRECT"]},
            "Other": {"type": "Selector", "all": ["TW 1"]},
            "Nested": {"type": "Selector", "all": ["Main"]},
            "Builtins": {"type": "Selector", "all": ["DIRECT", "REJECT-DROP"]},
            "GLOBAL": {"type": "Selector", "all": ["JP 1", "TW 1"]},
        }

    def recommend(self, configured="Missing", **kwargs):
        return detection.recommend_group(self.proxies, configured, **kwargs)["recommended_group"]

    def test_keeps_valid_manual_group_before_route_suggestion(self):
        self.assertEqual(self.recommend("Main", connection_chains=[["TW 1", "Other"]]), "Main")

    def test_current_route_can_disambiguate_safe_groups(self):
        self.assertEqual(self.recommend(connection_chains=[["JP 1", "Main", "Nested"]]), "Main")
        self.assertIsNone(self.recommend(connection_chains=[["Main", "Other"]]))

    def test_nested_and_builtin_only_groups_are_not_suggested(self):
        self.proxies.pop("Other")
        self.assertEqual(self.recommend("Nested"), "Main")
        self.proxies.pop("Main")
        self.assertIsNone(self.recommend("Builtins"))

    def test_multiple_groups_require_choice_and_global_is_mode_specific(self):
        self.assertIsNone(self.recommend())
        self.assertIsNone(self.recommend("GLOBAL"))
        self.assertEqual(self.recommend(mode="global"), "GLOBAL")
        self.assertIsNone(self.recommend("Main", mode="direct"))


class ControllerAddressTests(unittest.TestCase):
    def test_current_address_and_identified_custom_ports_are_in_bounded_loopback_list(self):
        listeners = [{"pid": 100, "port": port, "host": "127.0.0.1"} for port in (32123, *range(40000, 40050))]
        with patch.object(detection, "controller_listeners", return_value=listeners):
            addresses = detection.controller_addresses("http://localhost:23456")
        self.assertEqual(addresses[0], "http://localhost:23456")
        self.assertIn("http://127.0.0.1:32123", addresses)
        self.assertLessEqual(len(addresses), 24)
        self.assertEqual(len(addresses), len(set(addresses)))

    def test_dual_stack_listener_is_one_candidate_but_separate_pids_stay_separate(self):
        listeners = [
            {"pid": 100, "port": 9090, "host": "127.0.0.1"},
            {"pid": 100, "port": 9090, "host": "::1"},
            {"pid": 200, "port": 32123, "host": "127.0.0.1"},
            {"pid": 300, "port": 32123, "host": "::1"},
        ]
        with patch.object(detection, "controller_listeners", return_value=listeners):
            addresses = detection.controller_addresses("http://127.0.0.1:9790")
        self.assertEqual([address for address in addresses if address.endswith(":9090")], ["http://127.0.0.1:9090"])
        self.assertIn("http://127.0.0.1:32123", addresses)
        self.assertIn("http://[::1]:32123", addresses)

    def test_ipv6_only_listener_uses_ipv6_and_current_family_stays_first(self):
        listeners = [{"pid": 100, "port": 9090, "host": "::1"}]
        with patch.object(detection, "controller_listeners", return_value=listeners):
            addresses = detection.controller_addresses("http://127.0.0.1:9790")
        self.assertIn("http://[::1]:9090", addresses)
        self.assertNotIn("http://127.0.0.1:9090", addresses)
        listeners.append({"pid": 100, "port": 9090, "host": "127.0.0.1"})
        with patch.object(detection, "controller_listeners", return_value=listeners):
            addresses = detection.controller_addresses("http://[::1]:9090")
        self.assertEqual(addresses[0], "http://[::1]:9090")
        self.assertNotIn("http://127.0.0.1:9090", addresses)

    def test_remote_and_credential_bearing_addresses_are_not_probed(self):
        with patch.object(detection, "controller_listeners", return_value=[]):
            for configured in ("http://192.168.1.1:9090", "http://user:secret@localhost:9090", "http://localhost:9090?secret=x", "http://localhost:99999", "https://localhost:9090", "http://localhost:\\9090"):
                with self.subTest(configured=configured):
                    self.assertNotIn(configured, detection.controller_addresses(configured))

    def test_windows_port_enumeration_is_hidden_bounded_and_filters_output(self):
        result = subprocess.CompletedProcess([], 0, stdout=b'[{"OwningProcess":100,"LocalPort":32123,"LocalAddress":"0.0.0.0"},{"OwningProcess":100,"LocalPort":32123,"LocalAddress":"::"},{"OwningProcess":true,"LocalPort":9090,"LocalAddress":"127.0.0.1"},{"OwningProcess":100,"LocalPort":65536,"LocalAddress":"127.0.0.1"},{"OwningProcess":100,"LocalPort":9090,"LocalAddress":"192.168.1.1"}]')
        with patch.object(detection.os, "name", "nt"), patch.object(detection.subprocess, "run", return_value=result) as run:
            self.assertEqual(detection.controller_listeners(), [{"pid": 100, "port": 32123, "host": "127.0.0.1"}, {"pid": 100, "port": 32123, "host": "::1"}])
        self.assertEqual(run.call_args.kwargs["timeout"], 3)
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertEqual(run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertIn("-NoProfile", run.call_args.args[0])
        self.assertIn("creationflags", run.call_args.kwargs)

    def test_unavailable_process_enumeration_retains_common_addresses(self):
        with patch.object(detection.os, "name", "nt"), patch.object(detection.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell.exe", 3)):
            addresses = detection.controller_addresses("http://127.0.0.1:12345")
        self.assertEqual(addresses[0], "http://127.0.0.1:12345")
        self.assertIn("http://127.0.0.1:9090", addresses)


class ClientDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"controller": "http://127.0.0.1:9790", "controller_secret_env": "SYNTHETIC_SECRET", "group": "Old"}
        self.addresses = [self.cfg["controller"], "http://127.0.0.1:9090", "http://127.0.0.1:32123"]
        self.patch_addresses = patch.object(detection, "controller_addresses", return_value=self.addresses)
        self.patch_addresses.start()
        self.addCleanup(self.patch_addresses.stop)
        environment = patch.dict(os.environ, {"SYNTHETIC_SECRET": "synthetic-test-value"})
        environment.start()
        self.addCleanup(environment.stop)

    @staticmethod
    def information():
        return {"groups": ["Main"], "active_group": "Main", "mode": "rule", "proxy_port": 7890}

    def test_keeps_valid_configured_client_and_never_broadcasts_secret(self):
        seen = {}
        def inspector(fields, *, timeout, adaptive):
            self.assertEqual(timeout, 0.6)
            self.assertTrue(adaptive)
            seen[fields["controller"]] = fields["controller_secret_env"]
            return self.information()
        result = detection.discover_clients(self.cfg, inspector)
        self.assertEqual(result["selected_controller"], self.cfg["controller"])
        self.assertEqual(seen[self.cfg["controller"]], "SYNTHETIC_SECRET")
        self.assertEqual([seen[address] for address in self.addresses[1:]], ["", ""])
        self.assertEqual(self.cfg["controller_secret_env"], "SYNTHETIC_SECRET")

    def test_single_verified_alternative_is_selected(self):
        def inspector(fields, **_kwargs):
            if fields["controller"] != self.addresses[1]:
                raise RuntimeError("无法连接代理客户端控制接口")
            return self.information()
        result = detection.discover_clients(self.cfg, inspector)
        self.assertEqual(result["selected_controller"], self.addresses[1])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertTrue(result["candidates"][0]["verified"])

    def test_missing_configured_secret_does_not_silently_migrate(self):
        def inspector(fields, **_kwargs):
            if fields["controller"] == self.cfg["controller"]:
                raise ValueError("configured environment variable is missing")
            if fields["controller"] == self.addresses[2]:
                raise RuntimeError("closed")
            return self.information()
        with patch.dict(os.environ, {}, clear=True):
            result = detection.discover_clients(self.cfg, inspector)
        self.assertIsNone(result["selected_controller"])
        current = next(item for item in result["candidates"] if item["controller"] == self.cfg["controller"])
        self.assertTrue(current["needs_auth"])
        self.assertFalse(current["verified"])

    def test_one_dual_stack_kernel_remains_a_unique_automatic_choice(self):
        self.patch_addresses.stop()
        listeners = [{"pid": 100, "port": 9090, "host": host} for host in ("127.0.0.1", "::1")]
        def inspector(fields, **_kwargs):
            if fields["controller"].endswith(":9090"):
                return self.information()
            raise RuntimeError("unavailable")
        with patch.object(detection, "controller_listeners", return_value=listeners):
            result = detection.discover_clients(self.cfg, inspector)
        self.assertEqual(result["selected_controller"], "http://127.0.0.1:9090")
        self.assertEqual(len(result["candidates"]), 1)

    def test_auth_required_alternative_prevents_silent_selection(self):
        def inspector(fields, **_kwargs):
            if fields["controller"] == self.addresses[0]:
                raise OSError("closed")
            if fields["controller"] == self.addresses[1]:
                raise RuntimeError("控制接口 HTTP 401")
            return self.information()
        result = detection.discover_clients(self.cfg, inspector)
        self.assertIsNone(result["selected_controller"])
        self.assertTrue(result["candidates"][0]["needs_auth"])
        self.assertFalse(result["candidates"][0]["verified"])

    def test_multiple_verified_clients_require_choice(self):
        def inspector(fields, **_kwargs):
            if fields["controller"] == self.cfg["controller"]:
                raise ValueError("not a controller")
            return self.information()
        result = detection.discover_clients(self.cfg, inspector)
        self.assertIsNone(result["selected_controller"])
        self.assertEqual(len(result["candidates"]), 2)

    def test_discovery_concurrency_is_bounded(self):
        self.patch_addresses.stop()
        addresses = [f"http://127.0.0.1:{10000 + index}" for index in range(24)]
        lock, running, peak = threading.Lock(), 0, 0
        def inspector(_fields, **_kwargs):
            nonlocal running, peak
            with lock:
                running += 1
                peak = max(peak, running)
            time.sleep(0.005)
            with lock:
                running -= 1
            raise RuntimeError("unavailable")
        with patch.object(detection, "controller_addresses", return_value=addresses):
            result = detection.discover_clients(self.cfg, inspector)
        self.assertEqual(result["candidates"], [])
        self.assertLessEqual(peak, 6)
        self.assertEqual(running, 0)


if __name__ == "__main__":
    unittest.main()
