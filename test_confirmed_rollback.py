"""A confirmed rollback cannot reclaim a later external selection."""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
import test_runtime


class ConfirmedRollbackTests(unittest.TestCase):
    @contextmanager
    def runtime(self):
        fixture = test_runtime.RuntimeTests()
        try:
            fixture.setUp()
            fixture.cfg.update(concurrency=1, max_candidate_attempts=1)
            yield fixture
        finally:
            fixture.doCleanups()

    def test_confirmed_exception_rollback_does_not_revert_a_later_external_choice(self):
        with self.runtime() as fixture:
            snapshot = fixture.api.snapshot
            failed = False

            def fail_postverify_snapshot(*args, **kwargs):
                nonlocal failed
                if fixture.api.proxies["Main"]["now"] == "JP fast" and not failed:
                    failed = True
                    raise optimizer.ControllerError("fixture post-verification snapshot unavailable")
                return snapshot(*args, **kwargs)

            with patch.object(fixture.api, "snapshot", side_effect=fail_postverify_snapshot):
                with self.assertRaises(optimizer.ControllerError):
                    fixture.helper.scan(forced=True)
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
            self.assertEqual(fixture.api.proxies["Main"]["now"], "JP old")
            self.assertFalse((fixture.state / "selection.json").exists())

            # The user pauses, selects the same candidate in the client, then
            # asks NearRoute to measure it without changing that selection.
            fixture.cfg["enabled"] = False
            fixture.api.proxies["Main"]["now"] = "JP fast"
            fixture.api.select_calls.clear()
            fixture.helper.scan(forced=True)

            self.assertEqual(fixture.api.select_calls, [])
            self.assertEqual(fixture.api.proxies["Main"]["now"], "JP fast")
            self.assertEqual(fixture.helper.status["selected"], "JP fast")
            self.assertEqual(fixture.helper.status["phase"], "paused")
            self.assertFalse((fixture.state / "pending-selection.json").exists())

    def test_rejected_candidate_retires_its_confirmed_rollback_before_returning(self):
        with self.runtime() as fixture:
            fixture.verify.return_value = {"ok": False, "http_status": 403}

            changed = fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast")

            self.assertFalse(changed)
            self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
            self.assertEqual(fixture.api.proxies["Main"]["now"], "JP old")
            self.assertFalse((fixture.state / "pending-selection.json").exists())

    def test_offline_or_unconfirmed_rollback_keeps_the_transaction_for_retry(self):
        for failure in ("offline", "unconfirmed"):
            with self.subTest(failure=failure), self.runtime() as fixture:
                select = fixture.api.select

                def unconfirmed_select(group, name):
                    if name == "JP old":
                        fixture.api.select_calls.append((group, name))
                        return  # Simulates a successful PUT response that was not applied.
                    return select(group, name)

                def verify(*args, **kwargs):
                    fixture.api.offline = failure == "offline"
                    return {"ok": False, "http_status": 403}

                fixture.verify.side_effect = verify
                with patch.object(fixture.api, "select", side_effect=unconfirmed_select):
                    with self.assertRaises(optimizer.ControllerError):
                        fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast")

                pending_path = fixture.state / "pending-selection.json"
                self.assertTrue(pending_path.exists())
                self.assertEqual(optimizer.read_json(pending_path, {})["candidate"], "JP fast")
                self.assertEqual(fixture.api.proxies["Main"]["now"], "JP fast")
                self.assertFalse((fixture.state / "selection.json").exists())

                fixture.api.offline = False
                fixture.helper.recover_pending()
                self.assertEqual(fixture.api.proxies["Main"]["now"], "JP old")
                self.assertFalse(pending_path.exists())

    def test_confirmed_rollback_keeps_a_replacement_transaction(self):
        with self.runtime() as fixture:
            fixture.verify.return_value = {"ok": False, "http_status": 403}
            pending_path = fixture.state / "pending-selection.json"
            request = fixture.api.request
            replacement = None

            def replace_pending_at_confirmation(path, *args, **kwargs):
                nonlocal replacement
                response = request(path, *args, **kwargs)
                if (path == "/proxies" and replacement is None
                        and fixture.api.select_calls == [("Main", "JP fast"), ("Main", "JP old")]):
                    replacement = optimizer.read_json(pending_path, {}) | {"transaction_id": "replacement-transaction"}
                    optimizer.atomic_json(pending_path, replacement)
                return response

            with patch.object(fixture.api, "request", side_effect=replace_pending_at_confirmation):
                changed = fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast")

            self.assertFalse(changed)
            self.assertIsNotNone(replacement)
            self.assertEqual(optimizer.read_json(pending_path, {}), replacement)
            self.assertEqual(fixture.api.proxies["Main"]["now"], "JP old")


if __name__ == "__main__":
    unittest.main()
