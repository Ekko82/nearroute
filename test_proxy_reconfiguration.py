"""Runtime proxy configuration changes do not become node health results."""

from contextlib import contextmanager
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import optimizer
import test_runtime


class ProxyReconfigurationTests(unittest.TestCase):
    @contextmanager
    def runtime(self):
        fixture = test_runtime.RuntimeTests()
        connections = []
        try:
            fixture.setUp()
            fixture.cfg.update(concurrency=1, verification_idle_seconds=1,
                               probe_url="http://fixture.invalid/check", probe_expected_statuses=[204])
            fixture.helper.reload_config()
            fixture.verify.side_effect = test_runtime.REAL_VERIFY
            fixture.helper.write_status(controller_available=True, mode="rule", group="Main", selected="JP old",
                                        health={"ok": True, "http_status": 204}, last_health_at=optimizer.iso(),
                                        verification={"ok": True, "http_status": 204}, consecutive_health_failures=0)
            run = SimpleNamespace(fixture=fixture, requests=[], status=204, on_response=None)
            clock = [1000.0]

            def connection(host, port, **_kwargs):
                conn = Mock()
                conn.sock = object()
                connections.append(conn)
                conn.request.side_effect = lambda *_args, **_kwargs: run.requests.append((host, port))

                def response():
                    if run.on_response:
                        run.on_response()
                    return SimpleNamespace(status=run.status, length=0, read=lambda _limit: b"")

                conn.getresponse.side_effect = response
                return conn

            def sleep(seconds):
                clock[0] = round(clock[0] + seconds, 6)

            with patch.object(optimizer.http.client, "HTTPConnection", side_effect=connection), \
                 patch.object(optimizer.time, "monotonic", side_effect=lambda: clock[0]), \
                 patch.object(optimizer.time, "sleep", side_effect=sleep):
                yield run
        finally:
            fixture.doCleanups()
            for conn in connections:
                conn.close.assert_called_once()

    def invoke(self, fixture, operation):
        try:
            if operation == "scan":
                return fixture.helper.scan(forced=True)
            if operation == "health":
                return fixture.helper.tick_health()
            return fixture.helper.change_candidate(fixture.api.general, "Main", "JP old", "JP fast")
        except optimizer.ControllerError:
            # The public boundary may report a configuration change via an
            # exception; its visible state and selection side effects are fixed.
            return None

    def assert_unverified(self, fixture):
        status = optimizer.read_json(fixture.state / "status.json", {})
        self.assertIsNone(status.get("health"))
        self.assertIsNone(status.get("last_health_at"))
        self.assertIsNone(status.get("verification"))
        self.assertEqual(status.get("consecutive_health_failures"), 0)
        self.assertEqual(fixture.helper.failures, 0)
        self.assertTrue(status["controller_available"])
        self.assertEqual(fixture.helper.quarantine, {})

    def test_unusable_proxy_config_does_not_count_failures_or_try_candidates(self):
        for change in ({"mixed-port": 0, "port": 0}, {"allow-lan": True, "bind-address": "192.0.2.10"}):
            for operation in ("scan", "health", "candidate"):
                with self.subTest(config=change, operation=operation), self.runtime() as run:
                    fixture = run.fixture
                    fixture.api.general.update(change)

                    self.invoke(fixture, operation)

                    self.assertEqual(fixture.api.select_calls, [])
                    self.assertEqual(run.requests, [])
                    self.assertEqual(fixture.api.proxies["Main"]["now"], "JP old")
                    self.assert_unverified(fixture)
                    self.assertFalse((fixture.state / "pending-selection.json").exists())
                    self.assertFalse((fixture.state / "selection.json").exists())

    def test_candidate_endpoint_change_discards_success_and_failure_then_rolls_back(self):
        for change in ({"mixed-port": 10091}, {"allow-lan": True, "bind-address": "[::1]"}):
            for status in (204, 403):
                with self.subTest(config=change, status=status), self.runtime() as run:
                    fixture = run.fixture
                    run.status = status
                    run.on_response = lambda: fixture.api.general.update(change)

                    self.assertFalse(self.invoke(fixture, "candidate"))

                    self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
                    self.assertEqual(fixture.api.proxies["Main"]["now"], "JP old")
                    self.assert_unverified(fixture)
                    self.assertFalse((fixture.state / "pending-selection.json").exists())
                    self.assertFalse((fixture.state / "selection.json").exists())

    def test_health_endpoint_change_does_not_publish_result_or_inherit_failures(self):
        for status in (204, 403):
            with self.subTest(status=status), self.runtime() as run:
                fixture = run.fixture
                fixture.helper.failures = 1
                fixture.helper.write_status(health={"ok": False, "http_status": 403}, consecutive_health_failures=1)
                run.status = status
                run.on_response = lambda: fixture.api.general.update({"mixed-port": 10091})

                self.invoke(fixture, "health")

                self.assertEqual(run.requests, [("127.0.0.1", 10090)])
                self.assertEqual(fixture.api.select_calls, [])
                self.assert_unverified(fixture)

    def test_scan_discards_short_health_after_endpoint_change_before_retry_or_put(self):
        for status in (204, 403):
            with self.subTest(status=status), self.runtime() as run:
                fixture = run.fixture
                run.status = status
                run.on_response = lambda: fixture.api.general.update({"mixed-port": 10091})

                self.invoke(fixture, "scan")

                self.assertEqual(run.requests, [("127.0.0.1", 10090)])
                self.assertEqual(fixture.api.select_calls, [])
                self.assert_unverified(fixture)
                self.assertFalse((fixture.state / "selection.json").exists())

    def test_equivalent_endpoint_representations_allow_normal_candidate_commit(self):
        for before, after, expected_host in (
            ({"allow-lan": False, "bind-address": "[::1]"}, {"allow-lan": True, "bind-address": "*"}, "127.0.0.1"),
            ({"allow-lan": True, "bind-address": "::"}, {"allow-lan": True, "bind-address": "[::]"}, "::1"),
        ):
            with self.subTest(before=before, after=after), self.runtime() as run:
                fixture = run.fixture
                fixture.api.general.update(before)
                run.on_response = lambda: fixture.api.general.update(after)

                self.assertTrue(self.invoke(fixture, "candidate"))

                self.assertEqual(run.requests, [(expected_host, 10090)] * 3)
                self.assertEqual(fixture.api.select_calls, [("Main", "JP fast")])
                self.assertTrue(fixture.helper.status["health"]["ok"])
                self.assertEqual(fixture.helper.quarantine, {})
                self.assertEqual(optimizer.current_selection(fixture.cfg)["name"], "JP fast")

    def test_fixed_proxy_config_can_be_checked_again_without_old_failure_count(self):
        with self.runtime() as run:
            fixture = run.fixture
            fixture.api.general.update({"mixed-port": 0, "port": 0})
            self.invoke(fixture, "health")
            self.assert_unverified(fixture)

            fixture.api.general.update({"mixed-port": 10091, "allow-lan": True, "bind-address": "[::1]"})
            self.invoke(fixture, "health")

            self.assertEqual(run.requests, [("::1", 10091)])
            self.assertTrue(fixture.helper.status["health"]["ok"])
            self.assertEqual(fixture.helper.status["consecutive_health_failures"], 0)
            self.assertEqual(fixture.helper.failures, 0)
            self.assertEqual(fixture.api.select_calls, [])
            self.assertEqual(fixture.helper.quarantine, {})

    def test_measurement_only_scan_does_not_require_a_local_proxy_port(self):
        for dry_run in (False, True):
            with self.subTest(dry_run=dry_run), self.runtime() as run:
                fixture = run.fixture
                fixture.cfg["enabled"] = dry_run
                fixture.helper.dry_run = dry_run
                fixture.api.general.update({"mixed-port": 0, "port": 0})
                with patch.object(fixture.api, "delay", wraps=fixture.api.delay) as delay:
                    self.invoke(fixture, "scan")
                self.assertGreater(delay.call_count, 0)
                self.assertEqual(run.requests, [])
                self.assertEqual(fixture.api.select_calls, [])
                self.assertIn("仅测量", fixture.helper.status["message"])


if __name__ == "__main__":
    unittest.main()
