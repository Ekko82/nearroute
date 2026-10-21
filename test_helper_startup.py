"""Private startup acknowledgements are independent of best-effort snapshots."""

import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import dashboard
import optimizer
import test_dashboard_tray
import test_runtime
import updater


class HelperStartupTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.RuntimeTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.state = self.fixture.state
        self.status = self.state / "status.json"
        self.fixture.stack.enter_context(patch.object(dashboard, "STATE", self.state))
        self.fixture.stack.enter_context(patch.object(dashboard, "STATUS", self.status))
        self.fixture.stack.enter_context(patch.object(updater, "is_installing", return_value=False))

    def child(self, *, exited=False):
        token = uuid.uuid4().hex
        return SimpleNamespace(
            pid=os.getpid(), poll=lambda: 0 if exited else None,
            nearroute_ready_file=self.state / f"startup-{token}.ready",
            nearroute_started=False,
        )

    def run_main(self, child):
        token = child.nearroute_ready_file.stem.removeprefix("startup-")
        with patch.object(optimizer.sys, "argv", ["optimizer.py", "run", "--ready-token", token]), \
             patch.object(optimizer.sys, "stdout", None):
            optimizer.main()

    def ui(self, child):
        fixture = test_dashboard_tray.DashboardTrayTests()
        self.addCleanup(fixture.doCleanups)
        fixture.setUp()
        fixture.app.started_helpers = [child]
        wait = dashboard.wait_for_helper_startup
        fixture.patches.enter_context(patch.object(
            dashboard, "wait_for_helper_startup",
            side_effect=lambda processes: wait(processes, timeout=0),
        ))
        return fixture

    def test_start_uses_a_fresh_private_acknowledgement_and_spawn_failure_leaves_none(self):
        children = [SimpleNamespace(pid=101), SimpleNamespace(pid=102)]
        with patch.object(dashboard.subprocess, "Popen", side_effect=children) as popen, \
             patch.object(dashboard, "python_executable", return_value="mock-python"):
            first = dashboard.start_daemon()
            second = dashboard.start_daemon()
        self.assertNotEqual(first.nearroute_ready_file, second.nearroute_ready_file)
        for child, spawned in zip(children, popen.call_args_list):
            command = spawned.args[0]
            token = command[command.index("--ready-token") + 1]
            self.assertEqual(child.nearroute_ready_file, self.state / f"startup-{uuid.UUID(token).hex}.ready")
            self.assertFalse(child.nearroute_ready_file.exists())
            self.assertFalse(child.nearroute_started)
        with patch.object(dashboard.subprocess, "Popen", side_effect=OSError("spawn failed")), \
             patch.object(dashboard, "python_executable", return_value="mock-python"):
            with self.assertRaisesRegex(OSError, "spawn failed"):
                dashboard.start_daemon()
        self.assertEqual(list(self.state.glob("*.ready")), [])

    def test_unready_child_blocks_stop_even_if_snapshot_pid_matches(self):
        child = self.child()
        optimizer.atomic_json(self.status, {"pid": child.pid})
        ui = self.ui(child)
        ui.app.request_exit()
        ui.finish_worker()
        ui.app.poll_tray()
        ui.run.assert_not_called()
        self.assertFalse(ui.app.closed)
        self.assertFalse(child.nearroute_started)

    def test_consumed_acknowledgement_is_remembered_and_exited_child_is_cleaned(self):
        child = self.child()
        child.nearroute_ready_file.touch()
        self.assertTrue(dashboard.wait_for_helper_startup([child], timeout=0))
        self.assertTrue(child.nearroute_started)
        self.assertFalse(child.nearroute_ready_file.exists())
        self.assertTrue(dashboard.wait_for_helper_startup([child], timeout=0))
        exited = self.child(exited=True)
        exited.nearroute_ready_file.touch()
        self.assertTrue(dashboard.wait_for_helper_startup([exited], timeout=0))
        self.assertFalse(exited.nearroute_ready_file.exists())

    def test_main_signals_only_after_lock_and_stop_reset_then_cleans_up(self):
        child = self.child()
        stop = self.state / "stop.request"
        stop.touch()

        def initialize(*args):
            self.fixture.instance_lock.acquire.assert_called_once()
            self.assertFalse(stop.exists())
            self.assertTrue(child.nearroute_ready_file.is_file())
            return SimpleNamespace(run=lambda: None)

        with patch.object(optimizer, "Optimizer", side_effect=initialize):
            self.run_main(child)
        self.assertFalse(child.nearroute_ready_file.exists())
        self.fixture.instance_lock.close.assert_called_once()

    def test_initialization_error_and_existing_instance_leave_no_ready_resource(self):
        child = self.child()
        with patch.object(optimizer, "Optimizer", side_effect=RuntimeError("initialization failed")):
            with self.assertRaisesRegex(RuntimeError, "initialization failed"):
                self.run_main(child)
        self.assertFalse(child.nearroute_ready_file.exists())
        self.fixture.instance_lock.close.assert_called_once()

        self.fixture.instance_lock.reset_mock()
        self.fixture.instance_lock.acquire.return_value = False
        with patch.object(optimizer, "Optimizer") as initialize:
            self.run_main(child)
        initialize.assert_not_called()
        self.assertEqual(list(self.state.glob("*.ready")), [])

    @unittest.skipUnless(os.name == "nt", "requires Windows reader sharing semantics")
    def test_exit_completes_when_startup_snapshot_is_blocked_by_a_real_reader(self):
        child = self.child()
        optimizer.atomic_json(self.status, {"pid": None, "phase": "stopped"})
        ui = self.ui(child)
        real_optimizer = optimizer.Optimizer

        def initialize(*args):
            helper = real_optimizer(*args)
            self.assertTrue(helper.status.get("storage_error"))
            self.assertIsNone(optimizer.read_json(self.status, {})["pid"])

            def run():
                ui.app.request_exit()
                ui.finish_worker()
                ui.app.poll_tray()
                self.assertTrue(ui.app.closed)
                self.assertEqual(ui.run.call_args.args[0][-1], "stop")
                self.assertTrue(child.nearroute_started)
                self.assertFalse(child.nearroute_ready_file.exists())

            return SimpleNamespace(run=run)

        with self.status.open("r", encoding="utf-8"), \
             patch.object(optimizer, "Optimizer", side_effect=initialize):
            self.run_main(child)
        self.fixture.instance_lock.close.assert_called_once()
        self.assertEqual(list(self.state.glob("*.ready")), [])
        self.assertEqual(list(self.state.glob("*.tmp")), [])
        self.assertEqual(self.fixture.api.select_calls, [])
        self.fixture.verify.assert_not_called()


if __name__ == "__main__":
    unittest.main()
