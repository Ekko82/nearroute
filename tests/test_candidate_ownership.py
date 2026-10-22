"""A candidate becomes helper-owned only after its selection was attempted."""

import unittest
from unittest.mock import patch

from tests import test_runtime


class CandidateOwnershipTests(unittest.TestCase):
    def test_scan_preserves_client_choice_of_the_same_candidate_before_put(self):
        fixture = test_runtime.RuntimeTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.cfg["concurrency"] = 1
        change = fixture.helper.change_candidate
        chosen = []

        def choose_in_client(general, group, previous, candidate, **kwargs):
            self.assertEqual(fixture.api.select_calls, [])
            chosen.append(candidate)
            fixture.api.proxies[group]["now"] = candidate
            return change(general, group, previous, candidate, **kwargs)

        with patch.object(fixture.helper, "change_candidate", side_effect=choose_in_client):
            fixture.helper.scan(forced=True)

        self.assertEqual(chosen, ["JP fast"])
        self.assertEqual(fixture.api.select_calls, [])
        self.assertEqual(fixture.api.proxies["Main"]["now"], "JP fast")
        self.assertEqual(fixture.helper.status["selected"], "JP fast")
        self.assertNotIn("rollback", [event["event"] for event in fixture.events()])
        for name in ("pending-selection.json", "original-selection.json", "selection.json"):
            self.assertFalse((fixture.state / name).exists(), name)
        fixture.helper.recover_pending()
        self.assertEqual(fixture.api.select_calls, [])


if __name__ == "__main__":
    unittest.main()
