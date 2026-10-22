"""A saved pause drains the active probe without starting more automatic work."""

import concurrent.futures
from contextlib import contextmanager, ExitStack
import copy
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


REAL_LOAD_CONFIG = optimizer.load_config


class PauseMeasurementTests(unittest.TestCase):
    @contextmanager
    def running_scan(self, *, gate_at=1, forced=False):
        fixture = test_runtime.RuntimeTests()
        clock = [1000.0]
        run = SimpleNamespace(fixture=fixture, calls=[], rounds=[], completed=[], errors=[], workers=[], probe_threads=[],
                              entered=threading.Event(), release=threading.Event(),
                              pause_seen=threading.Event(), draining=threading.Event(),
                              stopped=threading.Event(), deadline=1200.0)
        try:
            with ExitStack() as patches:
                patches.enter_context(patch.object(optimizer.time, "time", side_effect=lambda: clock[0]))
                fixture.setUp()
                fixture.cfg.update(concurrency=1, probe_timeout_ms=15000)
                fixture.cfg = optimizer.validate_config(fixture.cfg)
                optimizer.atomic_json(optimizer.CONFIG, fixture.cfg)
                fixture.api.delays.update({"JP old": 100, "JP fast": 300, "JP manual": 400})
                fixture.helper.last_scan = 900.0
                fixture.helper.write_status(nodes=[], last_scan_at=optimizer.iso(900))
                if forced:
                    (fixture.state / "scan.request").touch()
                reload_config, scan = fixture.helper.reload_config, fixture.helper.scan
                measure = fixture.helper.measure_round
                executor_class = concurrent.futures.ThreadPoolExecutor

                def observe_reload():
                    reload_config()
                    if not fixture.helper.cfg["enabled"]:
                        run.pause_seen.set()

                def delay(name):
                    run.calls.append(name)
                    run.probe_threads.append(threading.current_thread())
                    if len(run.calls) == gate_at:
                        run.entered.set()
                        if not run.release.wait(6):
                            raise AssertionError("probe gate was not released")
                    return fixture.api.delays[name]

                def executor(*args, **kwargs):
                    pool = executor_class(*args, **kwargs)
                    shutdown = pool.shutdown

                    def drain(*args, **kwargs):
                        if run.entered.is_set() and not run.release.is_set():
                            run.draining.set()
                            run.release.set()
                        return shutdown(*args, **kwargs)

                    pool.shutdown = drain
                    return pool

                def record_round(names, title, **kwargs):
                    run.rounds.append(title)
                    return measure(names, title, **kwargs)

                def complete_scan(*args, **kwargs):
                    scan(*args, **kwargs)
                    run.completed.append(copy.deepcopy(fixture.helper.status))
                    run.stopped.set()

                def advance(seconds):
                    clock[0] = round(clock[0] + seconds, 6)

                def worker():
                    try:
                        fixture.helper.run()
                    except BaseException as error:
                        run.errors.append(error)

                def start():
                    run.stopped.clear()
                    run.deadline = clock[0] + 20
                    thread = threading.Thread(target=worker, name="nearroute-pause-test")
                    run.workers.append(thread)
                    thread.start()
                    return thread

                run.start = start
                patches.enter_context(patch.object(optimizer, "load_config", side_effect=REAL_LOAD_CONFIG))
                patches.enter_context(patch.object(fixture.helper, "reload_config", side_effect=observe_reload))
                patches.enter_context(patch.object(fixture.api, "delay", side_effect=delay))
                patches.enter_context(patch.object(optimizer.concurrent.futures, "ThreadPoolExecutor", side_effect=executor))
                patches.enter_context(patch.object(fixture.helper, "measure_round", side_effect=record_round))
                patches.enter_context(patch.object(fixture.helper, "scan", side_effect=complete_scan))
                patches.enter_context(patch.object(fixture.helper, "stopping", side_effect=lambda: run.stopped.is_set() or clock[0] >= run.deadline))
                patches.enter_context(patch.object(optimizer.time, "sleep", side_effect=advance))
                try:
                    start()
                    self.assertTrue(run.entered.wait(3), "first gated probe did not start")
                    optimizer.atomic_json(optimizer.CONFIG, dict(REAL_LOAD_CONFIG(), enabled=False))
                    self.assertTrue(run.pause_seen.wait(3), "scan did not load the saved pause")
                    yield run
                finally:
                    run.release.set()
                    run.stopped.set()
                    for thread in run.workers:
                        thread.join(8)
                    self.assertFalse(any(thread.is_alive() for thread in run.workers))
                    self.assertFalse(any(thread.is_alive() for thread in run.probe_threads), "probe thread was leaked")
        finally:
            fixture.doCleanups()

    def finish_paused_scan(self, run, *, forced=False):
        if forced:
            run.release.set()
        elif not run.draining.wait(1.5):
            # Old implementations need this release to finish and expose their
            # unwanted new probes instead of leaving a blocked test worker.
            run.release.set()
        run.workers[-1].join(8)
        self.assertFalse(run.workers[-1].is_alive())
        self.assertEqual(run.errors, [])
        self.assertEqual(len(run.completed), 1)

    def test_pause_during_initial_or_confirmation_round_only_finishes_inflight_probe(self):
        for gate_at, expected_rounds, retained_samples in ((1, 1, 0), (4, 2, 3)):
            with self.subTest(gate_at=gate_at), self.running_scan(gate_at=gate_at) as run:
                self.finish_paused_scan(run)

                self.assertEqual(len(run.calls), gate_at, "automatic probes started after pause was loaded")
                self.assertEqual(len(run.rounds), expected_rounds)
                self.assertEqual(run.completed[0]["phase"], "paused")
                self.assertEqual(run.fixture.helper.last_scan, 900)
                self.assertEqual(run.completed[0]["last_scan_at"], optimizer.iso(900))
                self.assertEqual(run.fixture.helper.next_scan, 0)
                history = optimizer.read_json(run.fixture.state / "history.json", {})
                self.assertEqual(sum(map(len, history.values())), retained_samples)
                self.assertEqual(run.fixture.api.select_calls, [])

    def test_forced_scan_still_finishes_all_three_rounds_after_pause(self):
        with self.running_scan(forced=True) as run:
            self.finish_paused_scan(run, forced=True)

            self.assertEqual(len(run.calls), 9)
            self.assertEqual(len(run.rounds), 3)
            self.assertEqual(run.completed[0]["phase"], "paused")
            self.assertGreater(run.fixture.helper.last_scan, 900)
            self.assertEqual(run.completed[0]["last_scan_at"], optimizer.iso(run.fixture.helper.last_scan))
            self.assertEqual(run.fixture.api.select_calls, [])

    def test_reenabling_runs_a_new_scan_after_the_automatic_round_was_cancelled(self):
        with self.running_scan() as run:
            self.finish_paused_scan(run)
            self.assertEqual(len(run.calls), 1)
            optimizer.atomic_json(optimizer.CONFIG, dict(REAL_LOAD_CONFIG(), enabled=True))

            resumed = run.start()
            resumed.join(8)

            self.assertFalse(resumed.is_alive())
            self.assertEqual(run.errors, [])
            self.assertEqual(len(run.completed), 2, "resume lost the cancelled scan until the regular interval")
            self.assertEqual(len(run.calls), 10)
            self.assertEqual(len(run.rounds), 4)
            self.assertGreater(run.fixture.helper.last_scan, 900)
            self.assertEqual(run.completed[-1]["last_scan_at"], optimizer.iso(run.fixture.helper.last_scan))
            self.assertTrue(all(row["samples"] == 3 for row in run.completed[-1]["nodes"]))


if __name__ == "__main__":
    unittest.main()
