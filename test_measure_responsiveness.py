"""Event-gated probe waits; no live controller or proxy is used."""

import concurrent.futures
from contextlib import contextmanager, ExitStack
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.parse

import optimizer
import test_runtime


REAL_CONTROLLER = optimizer.Controller


class MeasureResponsivenessTests(unittest.TestCase):
    @contextmanager
    def measurement(self):
        fixture = test_runtime.RuntimeTests("test_manual_request_during_scan_takes_priority_over_auto_switch")
        fixture.setUp()
        fixture.cfg.update(concurrency=1, enabled=False)
        fixture.helper.cfg.update(concurrency=1, enabled=False)
        run = SimpleNamespace(fixture=fixture, release=threading.Event(), started=threading.Event(),
                              manual_done=threading.Event(), shutdown=threading.Event(),
                              recovery=threading.Event(), calls=[], probe_threads=[], result=None, errors=[])
        names = ["JP old", "JP fast", "JP manual"]
        delays = {"JP old": 100, "JP fast": None, "JP manual": 200}
        real_executor = concurrent.futures.ThreadPoolExecutor
        write_status = fixture.helper.write_status

        def delay(name):
            run.calls.append(name)
            run.probe_threads.append(threading.current_thread())
            run.started.set()
            if not run.release.wait(5):
                raise AssertionError("fixture probe was not released")
            return delays[name]

        def executor(*args, **kwargs):
            pool = real_executor(*args, **kwargs)
            shutdown = pool.shutdown

            def observed_shutdown(*args, **kwargs):
                run.shutdown.set()
                return shutdown(*args, **kwargs)

            pool.shutdown = observed_shutdown
            return pool

        def publish(**updates):
            result = write_status(**updates)
            if "manual_selection_result" in updates:
                run.manual_done.set()
            return result

        def measure():
            try:
                run.result = fixture.helper.measure_round(names, "fixture")
            except BaseException as error:
                run.errors.append(error)

        with ExitStack() as patches:
            patches.enter_context(patch.object(fixture.api, "delay", side_effect=delay))
            patches.enter_context(patch.object(fixture.helper, "write_status", side_effect=publish))
            patches.enter_context(patch.object(fixture.helper, "transport_recovery_active", side_effect=run.recovery.is_set))
            patches.enter_context(patch.object(optimizer.concurrent.futures, "ThreadPoolExecutor", side_effect=executor))
            worker = threading.Thread(target=measure, name="fixture-measure-round")
            run.worker = worker
            worker.start()
            try:
                self.assertTrue(run.started.wait(1), "first probe did not start")
                yield run
            finally:
                run.release.set()
                worker.join(3)
                fixture.doCleanups()
                self.assertFalse(worker.is_alive(), "measurement worker did not stop")
                self.assertFalse(any(thread.is_alive() for thread in run.probe_threads), "probe thread was leaked")
                self.assertEqual(run.errors, [])

    def test_manual_choice_is_acknowledged_before_probe_finishes_even_during_recovery(self):
        for recovering in (False, True):
            with self.subTest(recovery=recovering), self.measurement() as run:
                optimizer.request_manual_selection("JP manual")
                if recovering:
                    run.recovery.set()
                self.assertTrue(run.manual_done.wait(1), "manual choice waited for the probe")
                self.assertTrue(run.fixture.helper.status["manual_selection_result"]["ok"])
                self.assertTrue(run.shutdown.wait(1), "remaining probes were not cancelled")
                self.assertFalse(run.release.is_set())
            self.assertEqual(run.calls, ["JP old"])
            self.assertEqual(run.result, {})
            self.assertEqual(run.fixture.api.select_calls, [("Main", "JP manual")])

    def test_stop_and_recovery_cancel_unstarted_probes_while_waiting(self):
        for reason in ("stop", "recovery"):
            with self.subTest(reason=reason), self.measurement() as run:
                if reason == "stop":
                    (run.fixture.state / "stop.request").touch()
                else:
                    run.recovery.set()
                self.assertTrue(run.shutdown.wait(1), "cancellation waited for the probe")
                self.assertFalse(run.release.is_set())
            self.assertEqual(run.calls, ["JP old"])
            self.assertEqual(run.result, {})
            self.assertEqual(run.fixture.api.select_calls, [])

    def test_policy_reload_does_not_record_a_result_from_the_previous_probe(self):
        with self.measurement() as run:
            run.fixture.cfg["probe_url"] = "https://example.invalid/new-probe"
            optimizer.request_manual_selection("missing node")
            self.assertTrue(run.manual_done.wait(1), "manual request did not reload policy during the wait")
            self.assertFalse(run.fixture.helper.status["manual_selection_result"]["ok"])
            self.assertTrue(run.shutdown.wait(1))
        self.assertEqual(run.result, {})
        self.assertEqual(run.fixture.helper.history, {})
        self.assertEqual(run.calls, ["JP old"])

    def test_failed_manual_choice_keeps_the_normal_round_and_failed_probe_sample(self):
        with self.measurement() as run:
            optimizer.request_manual_selection("missing node")
            self.assertTrue(run.manual_done.wait(1), "manual error waited for the probe")
            self.assertFalse(run.fixture.helper.status["manual_selection_result"]["ok"])
            self.assertFalse(run.shutdown.is_set())
        self.assertEqual(run.result, {"JP old": 100, "JP fast": None, "JP manual": 200})
        self.assertEqual(run.calls, ["JP old", "JP fast", "JP manual"])
        self.assertEqual({name: items[-1]["delay"] for name, items in run.fixture.helper.history.items()}, run.result)

    def test_saved_probe_and_scope_change_interrupts_then_new_scan_uses_new_settings(self):
        with self.measurement() as run:
            requests = []

            def request(path, *args, **kwargs):
                if path.startswith("/proxies/") and "/delay?" in path:
                    query = urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)
                    requests.append((urllib.parse.unquote(path.split("/")[2]), query["url"][0]))
                    return {"delay": 125}
                return run.fixture.api.request(path, *args, **kwargs)

            def controller(cfg):
                api = REAL_CONTROLLER(cfg)
                api.request = Mock(side_effect=request)
                return api

            with patch.object(optimizer, "Controller", side_effect=controller):
                run.fixture.cfg.update(probe_url="https://fixture.invalid/new-probe",
                                       scan_mode="nodes", scan_nodes=["JP manual"])
                (run.fixture.state / "scan.request").touch()
                self.assertTrue(run.shutdown.wait(2.5), "saved settings waited for the old probe")
                self.assertFalse(run.release.is_set())
                self.assertEqual(run.fixture.helper.cfg["probe_url"], run.fixture.cfg["probe_url"])
                run.release.set()
                run.worker.join(3)
                self.assertFalse(run.worker.is_alive())
                self.assertEqual(run.result, {})
                self.assertEqual(run.calls, ["JP old"])
                self.assertEqual(run.fixture.helper.history, {})

                run.fixture.helper.scan(forced=True)

            self.assertEqual(requests, [("JP manual", run.fixture.cfg["probe_url"])] *
                             (1 + run.fixture.cfg["confirmation_rounds"]))
            self.assertEqual(set(run.fixture.helper.history), {"JP manual"})
            self.assertEqual(run.fixture.api.select_calls, [])

    def test_scope_only_change_preserves_history_but_discards_the_old_round(self):
        with self.measurement() as run:
            run.fixture.helper.add_sample("JP old", 99)
            run.fixture.cfg.update(scan_mode="nodes", scan_nodes=["JP manual"])
            self.assertTrue(run.shutdown.wait(2.5), "new scope waited for the old probe")
            self.assertFalse(run.release.is_set())
        self.assertEqual(run.calls, ["JP old"])
        self.assertEqual(run.result, {})
        self.assertEqual([item["delay"] for item in run.fixture.helper.history["JP old"]], [99])

    def test_saved_policy_is_checked_before_submitting_a_round(self):
        fixture = test_runtime.RuntimeTests("test_manual_request_during_scan_takes_priority_over_auto_switch")
        try:
            fixture.setUp()
            fixture.cfg["probe_url"] = "https://fixture.invalid/new-probe"
            with patch.object(fixture.api, "delay", wraps=fixture.api.delay) as delay:
                self.assertEqual(fixture.helper.measure_round(["JP old"], "fixture"), {})
            delay.assert_not_called()
            self.assertEqual(fixture.helper.cfg["probe_url"], fixture.cfg["probe_url"])
        finally:
            fixture.doCleanups()

    def test_waiting_reloads_configuration_about_once_a_second(self):
        with self.measurement() as run:
            optimizer.load_config.reset_mock()
            started = time.monotonic()
            threading.Event().wait(1.3)
            elapsed = time.monotonic() - started
            self.assertGreaterEqual(optimizer.load_config.call_count, 1)
            self.assertLessEqual(optimizer.load_config.call_count, int(elapsed) + 1)
            self.assertFalse(run.shutdown.is_set())


if __name__ == "__main__":
    unittest.main()
