"""Restoring a superseded transaction permits a safe client configuration change."""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


class RestoreClientHandoffTests(unittest.TestCase):
    @contextmanager
    def pending_runtime(self):
        fixture = test_runtime.RuntimeTests()
        try:
            fixture.setUp()
            optimizer.atomic_json(optimizer.CONFIG, fixture.cfg)
            self.assertTrue(fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP manual"))
            fixture.helper.recover_pending()
            fixture.api.lose_next_put_response = True
            with self.assertRaises(optimizer.ControllerError):
                fixture.helper.change_candidate(fixture.api.general, "Main", "JP manual", "JP fast")
            fixture.api.offline = False
            fixture.api.select_calls.clear()
            fixture.api.proxies["JP external"] = {"type": "Trojan"}
            fixture.api.proxies["Main"]["all"].append("JP external")
            with patch.object(optimizer, "stop_daemon", return_value=True):
                yield fixture
        finally:
            fixture.doCleanups()

    def test_restore_retires_superseded_pending_without_put_then_can_change_client(self):
        for change in ("external_leaf", "external_owned_leaf", "same_name_group", "external_direct"):
            with self.subTest(change=change), self.pending_runtime() as fixture:
                baseline = (fixture.state / "original-selection.json").read_bytes()
                other_records = [
                    {**optimizer.controller_identity(fixture.cfg), "group": "Other", "name": "JP external"},
                    {**optimizer.controller_identity(fixture.cfg), "controller": "http://127.0.0.1:9793",
                     "group": "Main", "name": "JP external"},
                ]
                fixture.api.proxies["Other"] = {
                    "type": "Selector", "all": ["JP external"], "now": "JP external",
                }
                for record in other_records:
                    optimizer.save_owned_record(fixture.state / "selection.json", record)
                selected = "JP external"
                if change == "same_name_group":
                    fixture.api.proxies["JP fast"] = {
                        "type": "Selector", "all": ["JP external"], "now": "JP external",
                    }
                elif change == "external_direct":
                    fixture.api.proxies["DIRECT"] = {"type": "Direct"}
                    fixture.api.proxies["Main"]["all"].append("DIRECT")
                    fixture.api.proxies["Main"]["now"] = selected = "DIRECT"
                else:
                    if change == "external_owned_leaf":
                        selected = "JP manual"
                    fixture.api.proxies["Main"]["now"] = selected

                self.assertEqual(optimizer.restore_original(), [])
                self.assertEqual(optimizer.restore_original(), [])

                self.assertEqual(fixture.api.select_calls, [])
                self.assertFalse((fixture.state / "pending-selection.json").exists())
                status = optimizer.read_json(fixture.state / "status.json", {})
                self.assertEqual(status["selected"], selected)
                self.assertIsNone(status["health"])
                self.assertEqual((fixture.state / "original-selection.json").read_bytes(), baseline)
                self.assertEqual(optimizer.owned_records(optimizer.read_json(fixture.state / "selection.json", {})),
                                 other_records)
                self.assertEqual(optimizer.current_selection(fixture.cfg), {})
                new_endpoint = "http://127.0.0.1:9791"
                result = optimizer.configure_client({"controller": new_endpoint},
                                                    expected_context=optimizer.state_context(fixture.cfg))
                self.assertEqual(result["controller"], new_endpoint)
                self.assertFalse(result["enabled"])
                self.assertEqual(optimizer.read_json(optimizer.CONFIG, {})["controller"], new_endpoint)
                self.assertEqual(fixture.api.select_calls, [])

    def test_failed_ownership_retirement_keeps_pending_for_retry(self):
        with self.pending_runtime() as fixture:
            fixture.api.proxies["Main"]["now"] = "JP manual"
            pending_path = fixture.state / "pending-selection.json"
            selection_path = fixture.state / "selection.json"
            pending_before, selection_before = pending_path.read_bytes(), selection_path.read_bytes()
            write = optimizer.atomic_json

            def fail_retirement(path, value):
                if path == selection_path:
                    raise PermissionError("fixture ownership retirement unavailable")
                return write(path, value)

            with patch.object(optimizer, "atomic_json", side_effect=fail_retirement):
                with self.assertRaises(PermissionError):
                    optimizer.restore_original()
            self.assertEqual(pending_path.read_bytes(), pending_before)
            self.assertEqual(selection_path.read_bytes(), selection_before)
            self.assertEqual(optimizer.restore_original(), [])
            self.assertEqual(optimizer.restore_original(), [])
            self.assertFalse(pending_path.exists())
            self.assertEqual(fixture.api.select_calls, [])

    def test_unresolved_or_unbound_pending_still_blocks_client_change(self):
        for boundary in ("offline", "unconfirmed", "other_controller", "other_auth", "legacy"):
            with self.subTest(boundary=boundary), self.pending_runtime() as fixture:
                pending_path = fixture.state / "pending-selection.json"
                pending = optimizer.read_json(pending_path, {})
                if boundary in ("other_controller", "other_auth", "legacy"):
                    fixture.api.proxies["Main"]["now"] = "JP external"
                    if boundary == "other_controller":
                        pending["controller"] = "http://127.0.0.1:9792"
                    elif boundary == "other_auth":
                        pending["controller_secret_env"] = "OTHER_FIXTURE_AUTH"
                    else:
                        pending.pop("controller")
                        pending.pop("controller_secret_env")
                    optimizer.atomic_json(pending_path, pending)
                pending_bytes = pending_path.read_bytes()
                if boundary == "offline":
                    fixture.api.offline = True
                    with self.assertRaises(optimizer.ControllerError):
                        optimizer.restore_original()
                elif boundary == "unconfirmed":
                    with patch.object(fixture.api, "select") as select:
                        with self.assertRaises(optimizer.ControllerError):
                            optimizer.restore_original()
                    select.assert_called_once_with("Main", "JP old")
                else:
                    self.assertEqual(optimizer.restore_original(), [])

                self.assertEqual(pending_path.read_bytes(), pending_bytes)
                with self.assertRaisesRegex(RuntimeError, "未完成的回滚记录"):
                    optimizer.configure_client({"controller": "http://127.0.0.1:9791"})
                self.assertEqual(optimizer.read_json(optimizer.CONFIG, {})["controller"], fixture.cfg["controller"])
                self.assertEqual(pending_path.read_bytes(), pending_bytes)


if __name__ == "__main__":
    unittest.main()
