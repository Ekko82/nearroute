"""Subscription changes during a scan use only current selectable members."""

import unittest
from unittest.mock import patch

import test_runtime


class ScanMembershipTests(unittest.TestCase):
    def test_removed_candidate_does_not_interrupt_recovery_or_select_an_unmeasured_addition(self):
        for boundary in ("after_initial_measurement", "before_candidate_put"):
            with self.subTest(boundary=boundary):
                f = test_runtime.RuntimeTests()
                try:
                    f.setUp()
                    f.cfg.update(concurrency=1, max_candidate_attempts=1)
                    f.api.delays["JP manual"] = 200
                    f.verify.side_effect = lambda *_args, **_kwargs: {
                        "ok": f.api.proxies["Main"]["now"] != "JP old",
                        "reason": "fixture current path failure",
                    }
                    measure, change = f.helper.measure_round, f.helper.change_candidate
                    initial_measurement = True
                    updated = False

                    def update_subscription():
                        nonlocal updated
                        updated = True
                        # The proxy still exists for other groups, but Main can
                        # no longer select it. The active path is unchanged.
                        f.api.proxies["Main"]["all"].remove("JP fast")
                        f.api.proxies["Main"]["all"].append("JP new")
                        f.api.proxies["JP new"] = {"type": "Trojan"}
                        f.api.delays["JP new"] = 50

                    def measure_with_update(*args, **kwargs):
                        nonlocal initial_measurement
                        result = measure(*args, **kwargs)
                        if boundary == "after_initial_measurement" and initial_measurement:
                            update_subscription()
                        initial_measurement = False
                        return result

                    def change_with_update(general, group, previous, candidate, **kwargs):
                        if boundary == "before_candidate_put" and not updated:
                            update_subscription()
                        return change(general, group, previous, candidate, **kwargs)

                    with patch.object(f.helper, "measure_round", side_effect=measure_with_update), \
                         patch.object(f.helper, "change_candidate", side_effect=change_with_update) as attempt, \
                         patch.object(f.api, "delay", wraps=f.api.delay) as delay:
                        f.helper.scan()

                    self.assertTrue(updated)
                    self.assertEqual(f.api.select_calls, [("Main", "JP manual")])
                    self.assertEqual(f.helper.status["selected"], "JP manual")
                    self.assertEqual(f.helper.record["name"], "JP manual")
                    self.assertEqual(f.helper.failures, 0)
                    self.assertEqual(f.helper.quarantine, {})
                    self.assertFalse((f.state / "pending-selection.json").exists())
                    row = next(row for row in f.helper.status["nodes"] if row["name"] == "JP fast")
                    self.assertFalse(row["eligible"])
                    self.assertNotIn("JP new", f.helper.history)
                    self.assertNotIn("JP new", [call.args[0] for call in delay.call_args_list])
                    if boundary == "after_initial_measurement":
                        self.assertEqual([call.args[3] for call in attempt.call_args_list], ["JP manual"])
                finally:
                    f.doCleanups()


if __name__ == "__main__":
    unittest.main()
