"""Expired GUI requests cannot survive exit and execute on helper restart."""

from contextlib import ExitStack
import queue
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

import dashboard
import optimizer
import test_dashboard_manual
from test_dashboard_tray import DeferredThread
import test_runtime
import updater


class PublicationGate(queue.SimpleQueue):
    def __init__(self, before_publish):
        super().__init__()
        self.before_publish = before_publish

    def put(self, item):
        if item[1] and self.before_publish is not None:
            callback, self.before_publish = self.before_publish, None
            callback()
        super().put(item)


class ManualRequestLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.RuntimeTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.fixture.cfg["enabled"] = False
        self.app = test_dashboard_manual.dashboard_state()
        self.app.node_context = optimizer.state_context(self.fixture.cfg)
        self.app.node_mode = ("rule", "Main")
        self.app.started_helpers = []
        self.app.tray_actions = queue.SimpleQueue()
        self.app.show_window = Mock()
        self.workers = []
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)

        def thread(**kwargs):
            worker = DeferredThread(**kwargs)
            self.workers.append(worker)
            return worker

        def command(args, **_kwargs):
            self.assertEqual(args[-1], "stop")
            return subprocess.CompletedProcess(args, 0 if optimizer.stop_daemon() else 1)

        self.patches.enter_context(patch.object(dashboard.threading, "Thread", side_effect=thread))
        self.clock = self.patches.enter_context(patch.object(dashboard.time, "monotonic", return_value=100.0))
        self.patches.enter_context(patch.object(dashboard.subprocess, "run", side_effect=command))
        self.patches.enter_context(patch.object(dashboard.subprocess, "Popen", side_effect=AssertionError("real app forbidden")))
        self.patches.enter_context(patch.object(dashboard, "python_executable", return_value="mock-python"))

    def expire_attempt(self):
        self.clock.return_value = 131.0
        self.app.poll_manual_selection({})
        self.assertFalse(self.app.manual_busy)
        self.assertIn("超时", self.app.notify.call_args.args[0])

    def exit_gui(self, before_close=None):
        self.app.request_exit()
        self.assertTrue(self.app.exiting)
        self.workers[-1].target()
        self.assertTrue((self.fixture.state / "stop.request").exists())
        if before_close:
            before_close()
        self.app.poll_tray()
        self.assertTrue(self.app.closed)

    def restart_helper(self):
        # Run the real CLI startup and queue consumer in this process. The first
        # offline snapshot requests shutdown so no background service is left.
        snapshot = self.fixture.api.snapshot

        def snapshot_and_stop(*args, **kwargs):
            result = snapshot(*args, **kwargs)
            (self.fixture.state / "stop.request").touch()
            return result

        with patch.object(self.fixture.api, "snapshot", side_effect=snapshot_and_stop), \
             patch.object(sys, "argv", ["optimizer.py", "run"]), \
             patch.object(updater, "is_installing", return_value=False):
            optimizer.main()

    def test_late_request_after_timeout_is_cancelled_even_when_exit_stops_result_polling(self):
        self.app.switch_node("JP manual")
        producer = self.workers[0]
        self.expire_attempt()

        self.exit_gui(before_close=producer.target)
        self.assertEqual(self.fixture.api.select_calls, [])
        self.restart_helper()

        self.assertEqual(self.fixture.api.select_calls, [], "expired GUI request executed after restart")
        self.assertEqual(self.fixture.api.proxies["Main"]["now"], "JP old")
        self.assertFalse((self.fixture.state / "select.request.json").exists())

    def test_timeout_between_producer_validity_check_and_result_publication_cancels_request(self):
        self.app.manual_results = PublicationGate(self.expire_attempt)
        self.app.switch_node("JP manual")
        self.workers[0].target()

        self.exit_gui()
        self.restart_helper()

        self.assertEqual(self.fixture.api.select_calls, [], "request survived publication-time cancellation")
        self.assertEqual(self.fixture.api.proxies["Main"]["now"], "JP old")
        self.assertFalse((self.fixture.state / "select.request.json").exists())

    def test_late_old_result_cleanup_cannot_remove_a_new_request(self):
        def timeout_and_request_again():
            self.expire_attempt()
            self.app.switch_node("JP fast")
            self.workers[-1].target()

        self.app.manual_results = PublicationGate(timeout_and_request_again)
        self.app.switch_node("JP manual")
        self.workers[0].target()
        # The new result was published before the old producer finishes its put.
        self.app.poll_manual_selection({})
        pending = optimizer.read_json(self.fixture.state / "select.request.json", {})
        self.assertEqual(pending["name"], "JP fast")
        self.assertEqual(pending["request_id"], self.app.manual_request_id)
        self.assertTrue(self.app.manual_busy)

        self.assertTrue(self.fixture.helper.process_manual_selection())
        self.app.poll_manual_selection(self.fixture.helper.status)

        self.assertFalse(self.app.manual_busy)
        self.assertEqual(self.fixture.api.select_calls, [("Main", "JP fast")])
        self.assertEqual(self.fixture.api.proxies["Main"]["now"], "JP fast")
        self.assertIn("已手动切换到“JP fast”", self.app.notify.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
