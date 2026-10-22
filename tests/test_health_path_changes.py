"""Offline health-result ownership tests for mode, selector, and leaf changes."""

from contextlib import contextmanager
import time
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


REAL_SNAPSHOT = optimizer.Controller.snapshot


class HealthPathChangeTests(unittest.TestCase):
    @contextmanager
    def runtime(self, mode="rule", nested=False):
        fixture = test_runtime.RuntimeTests(
            "test_paused_manual_switch_is_confirmed_and_survives_helper_restart"
        )
        try:
            fixture.setUp()
            fixture.cfg["concurrency"] = 1
            fixture.api.general["mode"] = mode
            fixture.api.proxies["GLOBAL"] = {
                "type": "Selector", "all": ["JP global", "JP fast", "JP manual"],
                "now": "JP global",
            }
            fixture.api.proxies["JP global"] = {"type": "Trojan"}
            fixture.api.delays.update({"JP global": 700, "JP old": 600, "JP fast": 100, "JP manual": 800})
            if nested:
                fixture.api.proxies["Nested"] = {
                    "type": "Selector", "all": ["JP old", "JP manual"], "now": "JP old",
                }
                fixture.api.proxies["Main"]["all"].append("Nested")
                fixture.api.proxies["Main"]["now"] = "Nested"
            fixture.api.snapshot = REAL_SNAPSHOT.__get__(fixture.api, type(fixture.api))
            yield fixture
        finally:
            fixture.doCleanups()

    def assert_current_status(self, fixture, mode, group, selected):
        status = optimizer.read_json(fixture.state / "status.json", {})
        self.assertEqual(status["mode"], mode)
        self.assertEqual(status["group"], group)
        self.assertEqual(status["selected"], selected)
        return status

    def test_health_result_is_discarded_when_active_path_changes_during_verification(self):
        cases = (
            ("selection", "rule", "rule", "Main", "JP manual"),
            ("rule_to_global", "rule", "global", "GLOBAL", "JP global"),
            ("global_to_rule", "global", "rule", "Main", "JP old"),
            ("rule_to_direct", "rule", "direct", None, "DIRECT"),
            ("global_to_direct", "global", "direct", None, "DIRECT"),
            ("nested_leaf", "rule", "rule", "Main", "JP manual"),
        )
        for change, source, target, group, selected in cases:
            with self.subTest(change=change), self.runtime(source, nested=change == "nested_leaf") as fixture:
                fixture.helper.tick_health()
                fixture.helper.add_sample("JP fast", 180)
                existing_rows = fixture.helper.statistics(["JP old", "JP fast", "JP manual"])
                fixture.helper.write_status(nodes=existing_rows)
                fixture.helper.failures = 1
                previous_deadline = time.time() + 300
                fixture.helper.manual_until = previous_deadline

                def change_path(*args, **kwargs):
                    if change == "selection":
                        fixture.api.proxies["Main"]["now"] = "JP manual"
                    elif change == "nested_leaf":
                        fixture.api.proxies["Nested"]["now"] = "JP manual"
                    else:
                        fixture.api.general["mode"] = target
                    return {"ok": False, "http_status": 503, "reason": "old path failed"}

                fixture.verify.side_effect = change_path
                fixture.helper.tick_health()

                status = self.assert_current_status(fixture, target, group, selected)
                self.assertIsNone(status["health"])
                self.assertEqual(fixture.helper.failures, 0)
                self.assertEqual(status["consecutive_health_failures"], 0)
                self.assertEqual(status["nodes"], existing_rows if change in ("selection", "nested_leaf") else [])
                if target != "direct":
                    self.assertGreater(fixture.helper.manual_until, previous_deadline)
                self.assertEqual(fixture.api.select_calls, [])

                fixture.verify.side_effect = None
                fixture.verify.return_value = {"ok": True, "http_status": 405}
                fixture.helper.scan(forced=True)
                self.assertEqual(fixture.api.select_calls, [])
                self.assert_current_status(fixture, target, group, selected)

    def test_scan_detects_nested_leaf_change_before_scan_and_respects_new_choice(self):
        with self.runtime(nested=True) as fixture:
            fixture.helper.tick_health()
            fixture.helper.failures = 2
            fixture.helper.manual_until = 0
            fixture.api.proxies["Nested"]["now"] = "JP manual"

            fixture.helper.scan(forced=True)

            self.assertEqual(fixture.api.proxies["Main"]["now"], "Nested")
            self.assertEqual(fixture.api.proxies["Nested"]["now"], "JP manual")
            self.assertEqual(fixture.api.select_calls, [])
            self.assertEqual(fixture.helper.failures, 0)
            self.assertGreater(fixture.helper.manual_until, time.time())
            self.assert_current_status(fixture, "rule", "Main", "JP manual")

    def test_unchanged_path_preserves_health_result_and_failure_accumulation(self):
        with self.runtime() as fixture:
            fixture.helper.tick_health()
            fixture.helper.failures = 1
            deadline = time.time() + 300
            fixture.helper.manual_until = deadline
            failure = {"ok": False, "http_status": 503, "reason": "same path failed"}
            fixture.verify.return_value = failure

            fixture.helper.tick_health()

            status = self.assert_current_status(fixture, "rule", "Main", "JP old")
            self.assertEqual(status["health"], failure)
            self.assertEqual(status["consecutive_health_failures"], 2)
            self.assertEqual(fixture.helper.failures, 2)
            self.assertEqual(fixture.helper.manual_until, deadline)
            self.assertEqual(fixture.api.select_calls, [])

    def test_scan_detects_nested_leaf_change_during_measurement_and_abandons_old_scan(self):
        with self.runtime(nested=True) as fixture:
            fixture.helper.tick_health()
            fixture.helper.failures = 2
            fixture.helper.manual_until = 0
            delay = fixture.api.delay
            changed = False

            def change_leaf_once(name):
                nonlocal changed
                if not changed:
                    changed = True
                    fixture.api.proxies["Nested"]["now"] = "JP manual"
                return delay(name)

            with patch.object(fixture.api, "delay", side_effect=change_leaf_once):
                fixture.helper.scan(forced=True)

            self.assertTrue(changed)
            self.assertEqual(fixture.api.proxies["Main"]["now"], "Nested")
            self.assertEqual(fixture.api.proxies["Nested"]["now"], "JP manual")
            self.assertEqual(fixture.api.select_calls, [])
            self.assertEqual(fixture.helper.next_scan, 0)
            self.assertEqual(fixture.helper.failures, 0)
            self.assertGreater(fixture.helper.manual_until, time.time())
            status = self.assert_current_status(fixture, "rule", "Main", "JP manual")
            self.assertEqual({row["name"] for row in status["nodes"]}, {"JP old", "JP fast", "JP manual"})

    def test_own_successful_selections_do_not_look_like_external_path_changes(self):
        for mode in ("rule", "global"):
            for source in ("automatic", "manual"):
                with self.subTest(mode=mode, source=source), self.runtime(mode) as fixture:
                    fixture.helper.tick_health()
                    general, proxies, group = fixture.api.snapshot(fixture.cfg)
                    previous = proxies[group]["now"]
                    if source == "automatic":
                        self.assertTrue(fixture.helper.change_candidate(general, group, previous, "JP fast"))
                        self.assertEqual(fixture.helper.manual_until, 0)
                    else:
                        optimizer.request_manual_selection("JP fast")
                        self.assertTrue(fixture.helper.process_manual_selection())
                        self.assertGreater(fixture.helper.manual_until, time.time())
                    deadline = fixture.helper.manual_until

                    fixture.helper.tick_health()

                    self.assertEqual(fixture.helper.manual_until, deadline)
                    self.assertEqual(fixture.api.select_calls, [(group, "JP fast")])
                    self.assertFalse(any(event["event"] == "external_selection_change"
                                         for event in fixture.events()))
                    self.assert_current_status(fixture, mode, group, "JP fast")


if __name__ == "__main__":
    unittest.main()
