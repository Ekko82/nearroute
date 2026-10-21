"""Automatic journal recovery must preserve a later choice in the client."""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
import test_runtime


class PendingSupersessionTests(unittest.TestCase):
    @contextmanager
    def runtime(self, *, pending=False):
        fixture = test_runtime.RuntimeTests()
        try:
            fixture.setUp()
            fixture.cfg["concurrency"] = 1
            self.assertTrue(fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP manual"))
            if pending:
                fixture.helper.recover_pending()
                fixture.api.lose_next_put_response = True
                with self.assertRaises(optimizer.ControllerError):
                    fixture.helper.change_candidate(fixture.api.general, "Main", "JP manual", "JP fast")
                fixture.api.offline = False
            fixture.api.select_calls.clear()
            fixture.cfg["enabled"] = False
            with patch.object(optimizer, "stop_daemon", return_value=True):
                yield fixture
        finally:
            fixture.doCleanups()

    def test_auto_recovery_then_restore_preserves_external_leaf_or_replacement_group(self):
        for change in ("external_leaf", "same_name_group"):
            with self.subTest(change=change), self.runtime(pending=True) as fixture:
                if change == "external_leaf":
                    fixture.api.proxies["Main"]["now"] = "JP manual"
                else:
                    fixture.api.proxies["JP fast"] = {
                        "type": "Selector", "all": ["JP manual"], "now": "JP manual",
                    }
                baseline = (fixture.state / "original-selection.json").read_bytes()

                fixture.helper.scan(forced=True)

                self.assertEqual(fixture.api.select_calls, [])
                self.assertEqual(fixture.helper.status["selected"], "JP manual")
                self.assertFalse((fixture.state / "pending-selection.json").exists())
                owned_after_scan = optimizer.current_selection(fixture.cfg)
                restored = optimizer.restore_original()

                self.assertEqual(fixture.api.select_calls, [], "restore must not reclaim the external choice")
                self.assertEqual(restored, [])
                self.assertEqual(owned_after_scan, {})
                self.assertEqual(optimizer.resolve_leaf(fixture.api.proxies, fixture.api.proxies["Main"]["now"]),
                                 "JP manual")
                self.assertEqual((fixture.state / "original-selection.json").read_bytes(), baseline)

    def test_confirmed_pending_rollback_keeps_previous_ownership_for_explicit_restore(self):
        with self.runtime(pending=True) as fixture:
            fixture.helper.scan(forced=True)

            self.assertEqual(fixture.api.select_calls, [("Main", "JP manual")])
            self.assertFalse((fixture.state / "pending-selection.json").exists())
            self.assertEqual(optimizer.current_selection(fixture.cfg)["name"], "JP manual")
            self.assertEqual(fixture.helper.manual_until, 0)
            self.assertEqual(optimizer.restore_original(), ["Main"])
            self.assertEqual(fixture.api.select_calls, [("Main", "JP manual"), ("Main", "JP old")])

    def test_matching_completed_transaction_is_cleaned_without_retiring_ownership(self):
        with self.runtime() as fixture:
            owned = optimizer.current_selection(fixture.cfg)
            journal = optimizer.read_json(fixture.state / "pending-selection.json", {})
            self.assertEqual(journal["transaction_id"], owned["transaction_id"])

            fixture.helper.scan(forced=True)

            self.assertEqual(fixture.api.select_calls, [])
            self.assertFalse((fixture.state / "pending-selection.json").exists())
            self.assertEqual(optimizer.current_selection(fixture.cfg), owned)
            self.assertEqual(optimizer.restore_original(), ["Main"])
            self.assertEqual(fixture.api.select_calls, [("Main", "JP old")])

    def test_retired_ownership_does_not_break_external_grace_after_restart(self):
        for external_choice in ("JP old", "JP manual"):
            with self.subTest(external_choice=external_choice):
                clock = [1000.0]
                with patch.object(optimizer.time, "time", side_effect=lambda: clock[0]), self.runtime(pending=True) as fixture:
                    clock[0] = 3000.0
                    fixture.api.proxies["Main"]["now"] = external_choice
                    fixture.helper.scan(forced=True)
                    self.assertEqual(fixture.api.select_calls, [])
                    self.assertEqual(optimizer.current_selection(fixture.cfg), {})
                    grace_after_recovery = fixture.helper.manual_until

                    clock[0] = 3010.0
                    fixture.cfg["enabled"] = True
                    fixture.helper.scan(forced=True)

                    self.assertEqual(fixture.api.select_calls, [], "enabling must retain the external choice for ten minutes")
                    self.assertEqual(fixture.api.proxies["Main"]["now"], external_choice)
                    self.assertEqual(grace_after_recovery, 3600)
                    self.assertEqual(fixture.helper.manual_until, 3600)

                    clock[0] = 3020.0
                    restarted = optimizer.Optimizer()
                    restarted.scan(forced=True)

                    self.assertEqual(fixture.api.select_calls, [], "restarting must retain the external choice for ten minutes")
                    self.assertEqual(fixture.api.proxies["Main"]["now"], external_choice)
                    self.assertEqual(restarted.manual_until, 3600)
                    self.assertEqual(restarted.status["manual_until"], optimizer.iso(3600))

    def test_retirement_write_failure_preserves_pending_then_retry_keeps_other_owners(self):
        with self.runtime(pending=True) as fixture:
            fixture.api.proxies["Main"]["now"] = "JP manual"
            other_records = [
                {**optimizer.controller_identity(fixture.cfg), "group": "Other", "name": "JP fast"},
                {**optimizer.controller_identity(fixture.cfg), "controller": "http://127.0.0.1:9793",
                 "group": "Main", "name": "JP fast"},
            ]
            selection_path = fixture.state / "selection.json"
            pending_path = fixture.state / "pending-selection.json"
            for record in other_records:
                optimizer.save_owned_record(selection_path, record)
            selection_before = selection_path.read_bytes()
            pending_before = pending_path.read_bytes()
            baseline_before = (fixture.state / "original-selection.json").read_bytes()
            write = optimizer.atomic_json

            def fail_retirement(path, value):
                if path == selection_path:
                    raise PermissionError("fixture ownership file unavailable")
                return write(path, value)

            with patch.object(optimizer, "atomic_json", side_effect=fail_retirement):
                with self.assertRaises(PermissionError):
                    fixture.helper.scan(forced=True)
            self.assertEqual(pending_path.read_bytes(), pending_before)
            self.assertEqual(selection_path.read_bytes(), selection_before)
            self.assertEqual(fixture.api.select_calls, [])

            fixture.helper.scan(forced=True)

            self.assertFalse(pending_path.exists())
            self.assertEqual(optimizer.owned_records(optimizer.read_json(selection_path, {})), other_records)
            self.assertEqual((fixture.state / "original-selection.json").read_bytes(), baseline_before)
            self.assertEqual(fixture.api.select_calls, [])


if __name__ == "__main__":
    unittest.main()
