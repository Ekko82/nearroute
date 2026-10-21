"""Health-check cancellation hands control back without recording a node failure."""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
import test_runtime


class HealthCancellationTests(unittest.TestCase):
    @contextmanager
    def runtime(self):
        fixture = test_runtime.RuntimeTests()
        fixture.setUp()
        try:
            fixture.helper.write_status(
                controller_available=True, mode="rule", group="Main", selected="JP old",
                health={"ok": True, "http_status": 405}, consecutive_health_failures=0)
            yield fixture
        finally:
            fixture.doCleanups()

    def cancel_check(self, fixture, signal, clock):
        def verify(_general, _idle=0, cancel=None, **_kwargs):
            self.assertTrue(callable(cancel), "health verification must receive a cancellation callback")
            self.assertFalse(cancel())
            if signal == "stop":
                (fixture.state / "stop.request").touch()
            elif signal == "manual":
                optimizer.request_manual_selection("JP manual")
            else:
                fixture.cfg["enabled"] = False
            clock[0] += 1  # The pause check uses the existing one-second config-read cadence.
            self.assertTrue(cancel(), f"{signal} was not noticed during health verification")
            return {"ok": False, "cancelled": True, "reason": "验证已取消"}
        return verify

    def assert_no_failure_or_selection(self, fixture, signal):
        self.assertEqual(fixture.helper.failures, 0)
        self.assertEqual(fixture.helper.status["consecutive_health_failures"], 0)
        self.assertEqual(fixture.helper.status["health"], {"ok": True, "http_status": 405})
        self.assertEqual(fixture.helper.quarantine, {})
        self.assertEqual(fixture.api.select_calls, [])
        self.assertFalse((fixture.state / "pending-selection.json").exists())
        if signal == "manual":
            request = optimizer.read_json(fixture.state / "select.request.json", {})
            self.assertEqual(request["name"], "JP manual")

    def test_periodic_health_cancellation_preserves_health_and_pending_manual_choice(self):
        for signal in ("stop", "manual", "pause"):
            with self.subTest(signal=signal), self.runtime() as fixture:
                clock = [100.0]
                fixture.verify.side_effect = self.cancel_check(fixture, signal, clock)
                with patch.object(optimizer.time, "monotonic", side_effect=lambda: clock[0]):
                    fixture.helper.tick_health()
                self.assertEqual(fixture.verify.call_count, 1)
                self.assert_no_failure_or_selection(fixture, signal)

    def test_scan_short_health_cancellation_does_not_retry_or_choose_a_candidate(self):
        for signal in ("stop", "manual", "pause"):
            with self.subTest(signal=signal), self.runtime() as fixture:
                clock = [100.0]
                fixture.verify.side_effect = self.cancel_check(fixture, signal, clock)
                with patch.object(optimizer.time, "monotonic", side_effect=lambda: clock[0]):
                    fixture.helper.scan(forced=True)
                self.assertEqual(fixture.verify.call_count, 1)
                self.assert_no_failure_or_selection(fixture, signal)

    def test_scan_retry_cancellation_preserves_only_the_first_real_failure(self):
        with self.runtime() as fixture:
            clock = [100.0]
            first_failure = {"ok": False, "http_status": 403, "reason": "HTTP 403"}
            cancel = self.cancel_check(fixture, "manual", clock)

            def verify(*args, **kwargs):
                if fixture.verify.call_count == 1:
                    return first_failure
                return cancel(*args, **kwargs)

            fixture.verify.side_effect = verify
            with patch.object(optimizer.time, "monotonic", side_effect=lambda: clock[0]):
                fixture.helper.scan(forced=True)

            self.assertEqual(fixture.verify.call_count, 2)
            self.assertEqual(fixture.helper.failures, 1)
            self.assertEqual(fixture.helper.status["health"], first_failure)
            self.assertEqual(fixture.helper.quarantine, {})
            self.assertEqual(fixture.api.select_calls, [])
            self.assertFalse((fixture.state / "pending-selection.json").exists())
            self.assertEqual(optimizer.read_json(fixture.state / "select.request.json", {})["name"], "JP manual")


if __name__ == "__main__":
    unittest.main()
