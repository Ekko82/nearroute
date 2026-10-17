"""Deterministic queue races using the real file lock and isolated state."""

from contextlib import contextmanager
from pathlib import Path
import threading
import unittest
from unittest.mock import patch

import optimizer
import test_runtime


class ManualQueueConcurrencyTests(unittest.TestCase):
    def setUp(self):
        # Reuse the existing offline controller/TLS fixture. Its state directory
        # is temporary and all real network constructors are prohibited.
        self.runtime = test_runtime.RuntimeTests("test_paused_manual_switch_is_confirmed_and_survives_helper_restart")
        self.runtime.setUp()
        self.addCleanup(self.runtime.doCleanups)
        self.runtime.cfg["enabled"] = False
        self.results = {}
        self.errors = []
        self.workers = []

    def start_worker(self, name, action, done=None):
        def run():
            try:
                self.results[name] = action()
            except BaseException as error:
                self.errors.append(error)
            finally:
                if done is not None:
                    done.set()
        thread = threading.Thread(target=run, name=name)
        self.workers.append(thread)
        thread.start()

    @contextmanager
    def pause_request_read(self, reader_name, waiter_name):
        """Hold a request read inside its real lock until the test releases it."""
        read_started = threading.Event()
        release = threading.Event()
        waiter_attempted = threading.Event()
        original_read = optimizer.read_json
        original_lock = optimizer._manual_request_lock

        def gated_read(path, default):
            result = original_read(path, default)
            if Path(path).name == "select.request.json" and threading.current_thread().name == reader_name:
                read_started.set()
                if not release.wait(5):
                    raise AssertionError("request-read gate was not released")
            return result

        @contextmanager
        def observed_lock():
            if threading.current_thread().name == waiter_name:
                waiter_attempted.set()
            with original_lock():
                yield

        with patch.object(optimizer, "read_json", side_effect=gated_read), \
             patch.object(optimizer, "_manual_request_lock", side_effect=observed_lock):
            try:
                yield read_started, release, waiter_attempted
            finally:
                release.set()
                for thread in self.workers:
                    thread.join(timeout=12)
                self.assertFalse(any(thread.is_alive() for thread in self.workers), "queue worker did not stop")
        self.assertEqual(self.errors, [])

    def test_cancel_cannot_report_success_after_consumer_claim_has_started(self):
        request_id = optimizer.request_manual_selection("JP manual")
        cancel_done = threading.Event()
        with self.pause_request_read("queue-consumer", "queue-cancel") as (read_started, release, attempted):
            self.start_worker("queue-consumer", self.runtime.helper.process_manual_selection)
            self.assertTrue(read_started.wait(3), "consumer did not reach request read")
            self.start_worker("queue-cancel", lambda: optimizer.cancel_manual_selection(request_id), cancel_done)
            self.assertTrue(attempted.wait(3), "cancel did not attempt the queue lock")
            self.assertFalse(cancel_done.wait(0.1), "cancel bypassed the consumer's claim lock")
            release.set()
        self.assertTrue(self.results["queue-consumer"])
        self.assertFalse(self.results["queue-cancel"])
        self.assertEqual(self.runtime.api.select_calls, [("Main", "JP manual")])
        self.assertFalse((self.runtime.state / "select.request.json").exists())

    def test_cancel_of_old_request_cannot_delete_concurrent_new_request(self):
        old_id = optimizer.request_manual_selection("JP manual")
        producer_done = threading.Event()
        with self.pause_request_read("queue-cancel", "queue-producer") as (read_started, release, attempted):
            self.start_worker("queue-cancel", lambda: optimizer.cancel_manual_selection(old_id))
            self.assertTrue(read_started.wait(3), "cancel did not read the old request")
            self.start_worker("queue-producer", lambda: optimizer.request_manual_selection("JP fast"), producer_done)
            self.assertTrue(attempted.wait(3), "producer did not attempt the queue lock")
            self.assertFalse(producer_done.wait(0.1), "producer replaced a request while cancel held its lock")
            release.set()
        self.assertTrue(self.results["queue-cancel"])
        new_id = self.results["queue-producer"]
        self.assertNotEqual(new_id, old_id)
        pending = optimizer.read_json(self.runtime.state / "select.request.json", {})
        self.assertEqual(pending["request_id"], new_id)
        self.assertEqual(pending["name"], "JP fast")
        self.assertEqual(self.runtime.api.select_calls, [])


if __name__ == "__main__":
    unittest.main()
