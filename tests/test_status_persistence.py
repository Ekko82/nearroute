"""Status snapshots survive Windows readers; transaction write errors stay fatal."""

import os
import threading
import time
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


class StatusPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.RuntimeTests(
            "test_paused_manual_switch_is_confirmed_and_survives_helper_restart"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.helper = self.fixture.helper
        self.path = self.fixture.state / "status.json"

    @unittest.skipUnless(os.name == "nt", "requires Windows reader sharing semantics")
    def test_atomic_snapshot_retries_after_a_short_read_and_removes_temporary_file(self):
        old = optimizer.read_json(self.path, {})
        reader = self.path.open("r", encoding="utf-8")
        blocked, released = threading.Event(), threading.Event()
        errors = []
        replace = os.replace

        def replace_after_reader_gate(source, destination):
            try:
                return replace(source, destination)
            except PermissionError:
                blocked.set()
                if not released.wait(3):
                    raise AssertionError("reader release gate timed out")
                raise

        def write_snapshot():
            try:
                optimizer.atomic_json(self.path, {"phase": "monitoring", "complete": True})
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=write_snapshot, name="nearroute-snapshot-retry-test")
        with patch.object(optimizer.os, "replace", side_effect=replace_after_reader_gate) as attempts:
            try:
                worker.start()
                self.assertTrue(blocked.wait(3), "the real reader did not block replacement")
                self.assertEqual(optimizer.read_json(self.path, {}), old)
            finally:
                reader.close()
                released.set()
                worker.join(3)
                self.assertFalse(worker.is_alive(), "snapshot writer did not stop")
            self.assertGreaterEqual(attempts.call_count, 2)
            self.assertLessEqual(attempts.call_count, 5)
        self.assertEqual(errors, [])
        self.assertEqual(optimizer.read_json(self.path, {}), {"phase": "monitoring", "complete": True})
        self.assertEqual(list(self.fixture.state.glob("*.tmp")), [])

    @unittest.skipUnless(os.name == "nt", "requires Windows reader sharing semantics")
    def test_run_survives_a_persistent_reader_and_publishes_after_release(self):
        self.fixture.cfg["enabled"] = False
        self.helper.last_health = time.time()
        old = self.path.read_bytes()
        reader = self.path.open("r", encoding="utf-8")
        blocked, released, recovered = (threading.Event() for _ in range(3))
        errors, recovery_status = [], []
        write_status = self.helper.write_status

        def observe_write(**updates):
            result = write_status(**updates)
            if self.helper.status.get("storage_error"):
                blocked.set()
            elif released.is_set() and updates.get("phase") != "stopped":
                recovery_status.append(optimizer.read_json(self.path, {}))
                recovered.set()
                (self.fixture.state / "stop.request").touch()
            return result

        def run_helper():
            try:
                self.helper.run()
            except Exception as error:
                errors.append(error)
            finally:
                blocked.set()

        worker = threading.Thread(target=run_helper, name="nearroute-status-survival-test")
        with patch.object(self.helper, "write_status", side_effect=observe_write):
            try:
                worker.start()
                self.assertTrue(blocked.wait(3), "the occupied snapshot was not attempted")
                self.assertTrue(worker.is_alive(), repr(errors))
                self.assertTrue(self.helper.status.get("storage_error"))
                self.assertEqual(self.path.read_bytes(), old)
                reader.close()
                released.set()
                self.assertTrue(recovered.wait(3), "status publication did not recover")
            finally:
                reader.close()
                released.set()
                (self.fixture.state / "stop.request").touch()
                worker.join(3)
                self.assertFalse(worker.is_alive(), "helper did not stop")
        self.assertEqual(errors, [])
        self.assertTrue(recovery_status)
        self.assertFalse(recovery_status[0].get("storage_error"))
        status = optimizer.read_json(self.path, {})
        self.assertEqual(status["phase"], "stopped")
        self.assertIsNone(status["pid"])
        self.assertEqual(list(self.fixture.state.glob("*.tmp")), [])
        self.assertEqual(self.fixture.api.select_calls, [])
        self.fixture.verify.assert_not_called()

    def test_status_write_failure_is_retained_in_memory_then_cleared_on_success(self):
        old = self.path.read_bytes()
        with patch.object(optimizer.os, "replace", side_effect=OSError("mock storage failure")):
            self.assertFalse(self.helper.write_status(phase="monitoring"))
        self.assertEqual(self.path.read_bytes(), old)
        self.assertIn("mock storage failure", self.helper.status["storage_error"])
        self.assertEqual(list(self.fixture.state.glob("*.tmp")), [])

        self.assertTrue(self.helper.write_status(phase="monitoring"))
        self.assertFalse(self.helper.status.get("storage_error"))
        status = optimizer.read_json(self.path, {})
        self.assertEqual(status["phase"], "monitoring")
        self.assertFalse(status.get("storage_error"))

    def test_transaction_failure_is_bounded_and_preserves_the_previous_record(self):
        path = self.fixture.state / "pending-selection.json"
        optimizer.atomic_json(path, {"transaction_id": "previous"})
        old = path.read_bytes()
        with patch.object(optimizer.os, "replace", side_effect=PermissionError("mock occupied file")) as replace:
            with self.assertRaises(PermissionError):
                optimizer.atomic_json(path, {"transaction_id": "new"})
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(path.read_bytes(), old)
        self.assertEqual(list(self.fixture.state.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
