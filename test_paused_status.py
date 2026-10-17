"""Paused metadata observation through the real loop, without proxy traffic."""

import copy
from contextlib import ExitStack
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import optimizer


class FakeController(optimizer.Controller):
    """Keep real snapshot interpretation while replacing every HTTP operation."""

    def __init__(self, clock):
        self.clock = clock
        self.general = {"mode": "rule", "mixed-port": 10090}
        self.proxies = {
            "Main": {"type": "Selector", "all": ["JP old", "JP new"], "now": "JP old"},
            "JP old": {"type": "Shadowsocks"},
            "JP new": {"type": "Trojan"},
        }
        self.requests = []
        self.offline = False
        self.behavior = lambda _api, _elapsed: None
        self.delay = Mock(side_effect=AssertionError("paused mode must not measure nodes"))
        self.select = Mock(side_effect=AssertionError("paused observation must not change nodes"))

    def request(self, path, method="GET", body=None, timeout=5):
        self.behavior(self, self.clock[0] - 1000)
        self.requests.append((self.clock[0], path, method))
        if self.offline:
            raise optimizer.ControllerError("fake controller offline")
        if method != "GET":
            raise AssertionError("paused observation must be read-only")
        if path == "/configs":
            return copy.deepcopy(self.general)
        if path == "/proxies":
            return {"proxies": copy.deepcopy(self.proxies)}
        raise AssertionError("unexpected controller path: " + path)


class PausedStatusTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nearroute-paused-test-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name) / "state"
        self.cfg = dict(optimizer.DEFAULTS, enabled=False, group="Main", codex_log_db="", health_interval_seconds=15)
        self.clock = [1000.0]
        self.api = FakeController(self.clock)
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(optimizer, "STATE", self.state))
        stack.enter_context(patch.object(optimizer, "CONFIG", Path(temporary.name) / "config.json"))
        stack.enter_context(patch.object(optimizer, "load_config", side_effect=lambda: dict(self.cfg)))
        stack.enter_context(patch.object(optimizer, "Controller", return_value=self.api))
        stack.enter_context(patch.object(optimizer.urllib.request, "build_opener", side_effect=AssertionError("real network forbidden")))
        stack.enter_context(patch.object(optimizer.http.client, "HTTPConnection", side_effect=AssertionError("real HTTP forbidden")))
        stack.enter_context(patch.object(optimizer.http.client, "HTTPSConnection", side_effect=AssertionError("real TLS forbidden")))
        self.verify = stack.enter_context(patch.object(optimizer, "verify_current_path", side_effect=AssertionError("paused observation must not probe")))
        optimizer.atomic_json(self.state / "status.json", {
            "selected": "JP old", "group": "Main", "controller_available": True,
        })
        self.helper = optimizer.Optimizer()

    def run_for(self, seconds):
        snapshots = []
        write_status = self.helper.write_status

        def capture(**updates):
            write_status(**updates)
            snapshots.append(copy.deepcopy(self.helper.status))

        def sleep(seconds):
            self.clock[0] = round(self.clock[0] + seconds, 3)

        with patch.object(optimizer.time, "time", side_effect=lambda: self.clock[0]), \
             patch.object(optimizer.time, "sleep", side_effect=sleep), \
             patch.object(self.helper, "stopping", side_effect=lambda: self.clock[0] >= 1000 + seconds), \
             patch.object(self.helper, "write_status", side_effect=capture):
            self.helper.run()
        self.api.delay.assert_not_called()
        self.api.select.assert_not_called()
        self.verify.assert_not_called()
        self.assertTrue(all(method == "GET" for _at, _path, method in self.api.requests))
        return [status for status in snapshots if status.get("phase") != "stopped"]

    def test_external_selection_change_is_reflected_while_paused(self):
        def change(api, elapsed):
            if elapsed >= 1:
                api.proxies["Main"]["now"] = "JP new"
        self.api.behavior = change
        snapshots = self.run_for(16)
        self.assertEqual(snapshots[0]["selected"], "JP old")
        self.assertEqual(snapshots[-1]["selected"], "JP new")
        self.assertEqual(snapshots[-1]["group"], "Main")
        self.assertTrue(snapshots[-1]["controller_available"])
        self.assertEqual(snapshots[-1]["phase"], "paused")

    def test_offline_controller_is_not_kept_connected_by_fresh_heartbeat(self):
        self.api.behavior = lambda api, elapsed: setattr(api, "offline", elapsed >= 1)
        snapshots = self.run_for(16)
        self.assertFalse(snapshots[-1]["controller_available"])
        self.assertTrue(snapshots[-1]["last_error"])
        self.assertEqual(snapshots[-1]["selected"], "JP old")
        self.assertEqual([at for at, path, _ in self.api.requests if path == "/configs"], [1000.0, 1015.0])

    def test_recovery_clears_previous_connection_error(self):
        self.api.behavior = lambda api, elapsed: setattr(api, "offline", elapsed < 16)
        snapshots = self.run_for(31)
        self.assertTrue(any(not status["controller_available"] for status in snapshots))
        self.assertTrue(snapshots[-1]["controller_available"])
        self.assertIsNone(snapshots[-1].get("last_error"))
        self.assertEqual(snapshots[-1]["selected"], "JP old")
        self.assertEqual(snapshots[-1]["phase"], "paused")
        self.assertEqual([at for at, path, _ in self.api.requests if path == "/configs"], [1000.0, 1015.0, 1030.0])

    def test_observation_obeys_health_interval_without_probes_or_selection(self):
        self.run_for(31)
        config_reads = [at for at, path, _method in self.api.requests if path == "/configs"]
        self.assertEqual(config_reads, [1000.0, 1015.0, 1030.0])

    def test_direct_mode_is_online_and_described_as_direct(self):
        self.api.general["mode"] = "direct"
        status = self.run_for(1)[-1]
        self.assertTrue(status["controller_available"])
        self.assertEqual(status["mode"], "direct")
        self.assertEqual(status["selected"], "DIRECT")
        self.assertIsNone(status["group"])
        self.assertIsNone(status.get("last_error"))


if __name__ == "__main__":
    unittest.main()
