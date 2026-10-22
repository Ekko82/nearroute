"""Idle disk writes are bounded without delaying observable runtime changes."""

import copy
from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


class IdleStatusTests(unittest.TestCase):
    @contextmanager
    def runtime(self, *, paused=False, health_interval=15):
        fixture = test_runtime.RuntimeTests("test_paused_manual_switch_is_confirmed_and_survives_helper_restart")
        fixture.clock = [1000.0]
        try:
            with patch.object(optimizer.time, "time", side_effect=lambda: fixture.clock[0]):
                fixture.setUp()
                fixture.cfg.update(health_interval_seconds=health_interval, switch_cooldown_seconds=1800)
                fixture.api.delays.update({"JP old": 100, "JP fast": 300, "JP manual": 400})
                fixture.helper.record = {
                    **optimizer.controller_identity(fixture.cfg), "group": "Main", "name": "JP old",
                    "last_switch_at": 1000, "verification": {"ok": True},
                }
                fixture.helper.scan()
                if paused:
                    fixture.cfg["enabled"] = False
                    fixture.helper.reload_config()
                    fixture.helper.observe_paused_path()
                yield fixture
        finally:
            fixture.doCleanups()

    def run_for(self, fixture, seconds, action=None):
        writes, checks, observations = [], [], []
        atomic, tick, observe = optimizer.atomic_json, fixture.helper.tick_health, fixture.helper.observe_paused_path
        path = fixture.state / "status.json"

        def write(destination, value):
            atomic(destination, value)
            if destination == path:
                writes.append((fixture.clock[0], copy.deepcopy(value)))

        def health():
            checks.append(fixture.clock[0])
            return tick()

        def metadata():
            observations.append(fixture.clock[0])
            return observe()

        def advance(delta):
            fixture.clock[0] = round(fixture.clock[0] + delta, 3)
            if action:
                action(fixture.clock[0])

        with patch.object(optimizer, "atomic_json", side_effect=write), \
             patch.object(fixture.helper, "tick_health", side_effect=health), \
             patch.object(fixture.helper, "observe_paused_path", side_effect=metadata), \
             patch.object(fixture.helper, "scan", side_effect=AssertionError("unexpected idle scan")), \
             patch.object(optimizer.time, "sleep", side_effect=advance), \
             patch.object(fixture.helper, "stopping", side_effect=lambda: fixture.clock[0] >= 1000 + seconds):
            fixture.helper.run()
        return writes, checks, observations

    def test_300_second_idle_run_reduces_writes_and_retains_health_and_metadata_checks(self):
        for paused in (False, True):
            with self.subTest(paused=paused), self.runtime(paused=paused) as f:
                f.helper.last_health = 985
                writes, checks, observations = self.run_for(f, 300)
                self.assertLessEqual(len(writes), 25)  # Previously 301 snapshots, including shutdown.
                scheduled = observations if paused else checks
                self.assertEqual(scheduled, list(range(1000, 1300, 15)))
                self.assertEqual(checks if paused else observations, [])
                self.assertEqual(writes[-1][1]["phase"], "stopped")
                self.assertEqual(f.api.select_calls, [])

    def test_long_health_interval_still_publishes_a_heartbeat_within_15_seconds(self):
        with self.runtime(health_interval=300) as f:
            f.helper.last_health = 1000
            writes, checks, _observations = self.run_for(f, 300)
            self.assertEqual(checks, [])
            times = [1000] + [at for at, _status in writes]
            self.assertLessEqual(max(b - a for a, b in zip(times, times[1:])), 15)
            self.assertLessEqual(len(writes), 25)
            self.assertEqual(writes[-1][1]["phase"], "stopped")

    def test_new_transport_signal_is_published_at_the_5_second_poll(self):
        with self.runtime() as f:
            f.helper.last_health = 1000
            emitted = False

            def signal():
                nonlocal emitted
                fresh = not emitted and f.clock[0] >= 1005
                f.signals.last_new_error_at = f.clock[0] if fresh else None
                emitted = emitted or fresh
                return 1 if emitted else 0

            f.signals.poll.side_effect = signal
            writes, _checks, _observations = self.run_for(f, 11)
            changed = [(at, status) for at, status in writes if status.get("transport_recovery_until")]
            self.assertTrue(changed)
            self.assertEqual(changed[0][0], 1005)
            self.assertEqual(changed[0][1]["network_errors_recent"], 1)
            self.assertEqual(changed[0][1]["transport_recovery_until"], optimizer.iso(1065))

    def test_pause_manual_ack_and_stop_are_published_without_heartbeat_delay(self):
        with self.runtime() as f:
            f.helper.last_health = 1000
            paused, request_id = False, None

            def action(now):
                nonlocal paused, request_id
                if now >= 1003 and not paused:
                    f.cfg["enabled"] = False
                    paused = True
                if now >= 1005 and request_id is None:
                    request_id = optimizer.request_manual_selection("JP manual")

            writes, _checks, _observations = self.run_for(f, 11, action)
            paused_writes = [(at, status) for at, status in writes if status.get("phase") == "paused"]
            self.assertEqual(paused_writes[0][0], 1003)
            self.assertFalse(paused_writes[0][1]["enabled"])
            acknowledgements = [(at, status) for at, status in writes
                                if status.get("manual_selection_result", {}).get("request_id") == request_id]
            self.assertTrue(acknowledgements)
            self.assertEqual(acknowledgements[0][0], 1005)
            result = acknowledgements[0][1]["manual_selection_result"]
            self.assertTrue(result["ok"])
            self.assertEqual(result["name"], "JP manual")
            self.assertEqual(writes[-1][0], 1011)
            self.assertEqual(writes[-1][1]["phase"], "stopped")


if __name__ == "__main__":
    unittest.main()
