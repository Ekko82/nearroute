"""Supported in-flight probes finish before exit/update stop deadlines."""

import subprocess
import unittest
from unittest.mock import Mock, patch

import optimizer
import test_dashboard_tray
import test_runtime
import test_update_worker
import updater


class StopTimingTests(unittest.TestCase):
    def setUp(self):
        self.runtime = test_runtime.RuntimeTests()
        self.addCleanup(self.runtime.doCleanups)
        self.runtime.setUp()
        self.clock = [0.0]
        self.stop_timeouts = []
        # The supported 15-second probe timeout permits a 33-second controller
        # delay request. Its worker keeps the helper lock until it finishes.
        self.runtime.instance_lock.acquire.side_effect = lambda: self.clock[0] >= 33
        self.runtime.stack.enter_context(patch.object(optimizer.time, "monotonic", side_effect=lambda: self.clock[0]))
        self.runtime.stack.enter_context(patch.object(optimizer.time, "sleep", side_effect=self.advance))

    def advance(self, seconds):
        self.clock[0] += seconds

    def stop_process(self, command, *, timeout, **_kwargs):
        self.stop_timeouts.append(timeout)
        return subprocess.CompletedProcess(command, 0 if optimizer.stop_daemon() else 1)

    def test_exit_waits_for_a_supported_slow_probe_before_closing(self):
        ui = test_dashboard_tray.DashboardTrayTests()
        self.addCleanup(ui.doCleanups)
        ui.setUp()
        ui.run.side_effect = self.stop_process

        ui.app.request_exit()
        ui.finish_worker()
        ui.app.poll_tray()

        self.assertTrue(ui.app.closed)
        self.assertEqual(self.clock[0], 33)
        self.assertEqual(self.stop_timeouts, [45])
        self.runtime.instance_lock.close.assert_called_once()

    def test_installer_waits_for_a_supported_slow_probe_then_updates_and_reopens(self):
        worker = test_update_worker.UpdateWorkerTests()
        self.addCleanup(worker.doCleanups)
        worker.setUp()
        core = Mock()
        helper_lock = core.InstanceLock.return_value
        helper_lock.acquire.return_value = True
        with patch.object(updater, "_wait_for_parent"), \
             patch.object(updater.subprocess, "run", side_effect=self.stop_process), \
             patch.object(updater.subprocess, "Popen") as restart, \
             patch.object(updater.importlib.util, "spec_from_file_location", return_value=Mock()), \
             patch.object(updater.importlib.util, "module_from_spec", return_value=core):
            updater._run_installer(worker.prepared, worker.root, 12345, "test-python")

        result = updater.read_update_result(worker.root)
        self.assertTrue(result["success"], result)
        self.assertEqual(self.clock[0], 33)
        self.assertEqual(self.stop_timeouts, [45])
        self.assertEqual(updater.current_version(worker.root), worker.release.version)
        self.assertEqual((worker.root / "config.json").read_text(), '{"enabled":false,"user_note":"keep"}')
        self.assertEqual((worker.root / "state" / "history.json").read_text(), '{"keep":true}')
        helper_lock.close.assert_called_once()
        restart.assert_called_once()
        self.assertFalse(updater.is_installing(worker.root))


if __name__ == "__main__":
    unittest.main()
