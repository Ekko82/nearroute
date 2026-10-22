"""Successful selections publish health belonging to the new active leaf."""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


class SelectionHealthTests(unittest.TestCase):
    @contextmanager
    def runtime(self):
        fixture = test_runtime.RuntimeTests()
        fixture.clock = [1000.0]
        try:
            with patch.object(optimizer.time, "time", side_effect=lambda: fixture.clock[0]):
                fixture.setUp()
                fixture.cfg["concurrency"] = 1
                yield fixture
        finally:
            fixture.doCleanups()

    def test_complete_scan_publishes_candidate_health_for_healthy_and_failed_old_paths(self):
        for old_healthy in (True, False):
            with self.subTest(old_healthy=old_healthy), self.runtime() as fixture:
                old_result = ({"ok": True, "http_status": 405, "request_ms": [600]}
                              if old_healthy else {"ok": False, "http_status": 403, "reason": "old path failed"})
                candidate_result = {"ok": True, "http_status": 405, "request_ms": [100, 101, 102]}
                fixture.verify.side_effect = lambda *_args, **_kwargs: (
                    old_result if fixture.api.proxies["Main"]["now"] == "JP old" else candidate_result)
                fixture.helper.tick_health()
                fixture.helper.tick_health()
                before = optimizer.read_json(fixture.state / "status.json", {})
                self.assertEqual(before["consecutive_health_failures"], 0 if old_healthy else 2)
                calls_before = fixture.verify.call_count
                fixture.clock[0] = 1010.0

                fixture.helper.scan(forced=True)

                status = optimizer.read_json(fixture.state / "status.json", {})
                self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
                self.assertEqual(status["selected"], "JP fast")
                self.assertEqual(status["health"], candidate_result)
                self.assertEqual(status["verification"], candidate_result)
                self.assertEqual(status["last_health_at"], optimizer.iso(fixture.clock[0]))
                self.assertNotEqual(status["last_health_at"], before["last_health_at"])
                self.assertEqual(status["consecutive_health_failures"], 0)
                self.assertEqual(fixture.helper.failures, 0)
                self.assertEqual(fixture.helper.last_health, fixture.clock[0])
                self.assertEqual(fixture.verify.call_count - calls_before, 2 if old_healthy else 3)

    def test_manual_success_clears_old_health_and_verification_without_an_extra_probe(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled), self.runtime() as fixture:
                self.assertTrue(fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast"))
                fixture.helper.recover_pending()
                fixture.clock[0] = 1010.0
                fixture.verify.return_value = {"ok": False, "http_status": 403, "reason": "old path failed"}
                fixture.helper.tick_health()
                fixture.helper.tick_health()
                before = optimizer.read_json(fixture.state / "status.json", {})
                self.assertFalse(before["health"]["ok"])
                self.assertTrue(before["verification"]["ok"])
                self.assertEqual(before["consecutive_health_failures"], 2)
                fixture.cfg["enabled"] = enabled
                fixture.clock[0] = 1020.0
                calls_before = fixture.verify.call_count
                optimizer.request_manual_selection("JP manual", expected_context=optimizer.state_context(fixture.cfg),
                                                   expected_mode=("rule", "Main"))

                self.assertTrue(fixture.helper.process_manual_selection())

                status = optimizer.read_json(fixture.state / "status.json", {})
                self.assertEqual(status["selected"], "JP manual")
                self.assertEqual(status["phase"], "monitoring" if enabled else "paused")
                for field in ("health", "last_health_at", "verification"):
                    self.assertIsNone(status[field], field)
                self.assertEqual(status["consecutive_health_failures"], 0)
                self.assertEqual(fixture.helper.failures, 0)
                self.assertTrue(status["manual_selection_result"]["ok"])
                self.assertEqual(fixture.verify.call_count, calls_before)

    def test_final_snapshot_discards_candidate_health_after_external_path_change(self):
        for change in ("top", "nested_leaf"):
            with self.subTest(change=change), self.runtime() as fixture:
                fixture.verify.return_value = {"ok": True, "http_status": 405, "request_ms": [100, 101, 102]}
                select = fixture.helper.change_candidate
                requests_at_commit = 0

                def external_choice(*args, **kwargs):
                    nonlocal requests_at_commit
                    changed = select(*args, **kwargs)
                    if changed:
                        requests_at_commit = request.call_count
                        if change == "top":
                            fixture.api.proxies["Main"]["now"] = "JP manual"
                        else:
                            fixture.api.proxies["JP fast"] = {
                                "type": "Selector", "all": ["JP manual"], "now": "JP manual",
                            }
                    return changed

                with patch.object(fixture.api, "request", wraps=fixture.api.request) as request, \
                     patch.object(fixture.helper, "change_candidate", side_effect=external_choice):
                    fixture.helper.scan(forced=True)

                status = optimizer.read_json(fixture.state / "status.json", {})
                self.assertEqual(status["selected"], "JP manual")
                for field in ("health", "last_health_at", "verification"):
                    self.assertIsNone(status[field], field)
                self.assertEqual(status["consecutive_health_failures"], 0)
                self.assertEqual(fixture.helper.failures, 0)
                self.assertEqual(fixture.helper.last_health, 0)
                self.assertGreater(fixture.helper.manual_until, fixture.clock[0])
                self.assertIn("手动选点", status["message"])
                self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
                self.assertEqual(fixture.verify.call_count, 2)
                self.assertEqual([call.args[0] for call in request.call_args_list[requests_at_commit:]],
                                 ["/configs", "/proxies"])

        with self.subTest(change="unchanged_failed_path"), self.runtime() as fixture:
            fixture.verify.return_value = {"ok": False, "http_status": 403, "reason": "old path failed"}
            fixture.helper.scan(forced=True)
            self.assertEqual(fixture.helper.status["selected"], "JP old")
            self.assertFalse(fixture.helper.status["health"]["ok"])
            self.assertEqual(fixture.helper.failures, 2)


if __name__ == "__main__":
    unittest.main()
