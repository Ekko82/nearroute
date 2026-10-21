"""Candidate verification belongs to the leaf that the helper selected."""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
import test_runtime


class CandidateValidationTests(unittest.TestCase):
    @contextmanager
    def runtime(self):
        fixture = test_runtime.RuntimeTests()
        try:
            fixture.setUp()
            fixture.cfg["concurrency"] = 1
            yield fixture
        finally:
            fixture.doCleanups()

    def assert_uncommitted(self, fixture):
        self.assertFalse((fixture.state / "selection.json").exists())
        self.assertEqual(fixture.helper.quarantine, {})
        self.assertIsNone(fixture.helper.status.get("health"))
        self.assertEqual(fixture.helper.next_scan, 0)

    def test_candidate_replaced_by_group_preserves_new_leaf_and_discards_verification(self):
        for verified in (True, False):
            with self.subTest(verified=verified), self.runtime() as fixture:
                def verify(*_args, **_kwargs):
                    if fixture.api.proxies["Main"]["now"] == "JP fast":
                        fixture.api.proxies["JP fast"] = {
                            "type": "Selector", "all": ["JP manual"], "now": "JP manual",
                        }
                        return {"ok": verified, "http_status": 405 if verified else 403}
                    return {"ok": True, "http_status": 405}

                fixture.verify.side_effect = verify
                fixture.helper.scan(forced=True)

                self.assert_uncommitted(fixture)
                self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
                self.assertEqual(fixture.api.proxies["Main"]["now"], "JP fast")
                self.assertEqual(fixture.helper.status["selected"], "JP manual")
                self.assertEqual(optimizer.resolve_leaf(fixture.api.proxies, "JP fast"), "JP manual")
                fixture.helper.recover_pending()
                self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
                self.assertFalse((fixture.state / "pending-selection.json").exists())

    def test_removed_direct_membership_cancels_but_still_rolls_back_the_owned_leaf(self):
        for verified in (True, False):
            with self.subTest(verified=verified), self.runtime() as fixture:
                def verify(*_args, **_kwargs):
                    if fixture.api.proxies["Main"]["now"] == "JP fast":
                        fixture.api.proxies["Main"]["all"].remove("JP fast")
                        return {"ok": verified, "http_status": 405 if verified else 403}
                    return {"ok": True, "http_status": 405}

                fixture.verify.side_effect = verify
                fixture.helper.scan(forced=True)

                self.assert_uncommitted(fixture)
                self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
                self.assertEqual(fixture.api.proxies["Main"]["now"], "JP old")
                self.assertEqual(fixture.helper.status["selected"], "JP old")
                fixture.helper.recover_pending()
                self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
                self.assertFalse((fixture.state / "pending-selection.json").exists())

    def test_restore_preserves_same_name_group_replacing_an_owned_or_pending_leaf(self):
        for source in ("owned", "pending"):
            with self.subTest(source=source), self.runtime() as fixture:
                if source == "owned":
                    self.assertTrue(fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast"))
                    fixture.helper.recover_pending()
                else:
                    fixture.api.lose_next_put_response = True
                    with self.assertRaises(optimizer.ControllerError):
                        fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast")
                    fixture.api.offline = False
                fixture.api.select_calls.clear()
                saved = {name: (fixture.state / name).read_bytes()
                         for name in ("original-selection.json", "selection.json", "pending-selection.json")
                         if (fixture.state / name).exists()}
                fixture.api.proxies["JP fast"] = {
                    "type": "Selector", "all": ["JP manual"], "now": "JP manual",
                }

                with patch.object(optimizer, "stop_daemon", return_value=True):
                    self.assertEqual(optimizer.restore_original(), [])

                self.assertEqual(fixture.api.select_calls, [])
                self.assertEqual(fixture.api.proxies["Main"]["now"], "JP fast")
                status = optimizer.read_json(fixture.state / "status.json", {})
                self.assertEqual(status["selected"], "JP manual")
                self.assertIsNone(status["health"])
                for name, contents in saved.items():
                    self.assertEqual((fixture.state / name).read_bytes(), contents)


if __name__ == "__main__":
    unittest.main()
