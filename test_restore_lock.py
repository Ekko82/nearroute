"""Windows restore exclusion tests with unique real mutexes and fake controllers."""

import os
import threading
import unittest
import uuid
from unittest.mock import patch

import optimizer
import test_runtime


REAL_INSTANCE_LOCK = optimizer.InstanceLock


@unittest.skipUnless(os.name == "nt", "requires the Windows named mutex")
class RestoreLockTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.RuntimeTests(
            "test_paused_manual_switch_is_confirmed_and_survives_helper_restart"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.state = self.fixture.state
        self.api = self.fixture.api
        self.fixture.stack.enter_context(patch.object(optimizer, "InstanceLock", REAL_INSTANCE_LOCK))
        self.fixture.stack.enter_context(
            patch.object(optimizer, "MUTEX_NAME", "Local\\NearRouteRestoreTest_" + uuid.uuid4().hex)
        )
        self.fixture.stack.enter_context(patch.object(optimizer, "stop_daemon", return_value=True))
        optimizer.atomic_json(optimizer.CONFIG, self.fixture.cfg)
        optimizer.request_manual_selection("JP fast")
        self.assertTrue(self.fixture.helper.process_manual_selection())
        self.api.select_calls.clear()

    def state_bytes(self):
        return {path.relative_to(self.state): path.read_bytes()
                for path in self.state.rglob("*") if path.is_file()}

    def test_restore_holds_real_lock_while_waiting_for_controller_response(self):
        entered = threading.Event()
        release = threading.Event()
        results = []
        errors = []
        request = self.api.request
        paused = False

        def pause_first_proxy_read(path, *args, **kwargs):
            nonlocal paused
            if path == "/proxies" and not paused:
                paused = True
                entered.set()
                if not release.wait(5):
                    raise AssertionError("restore read gate timed out")
            return request(path, *args, **kwargs)

        def restore():
            try:
                results.append(optimizer.restore_original())
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=restore, name="nearroute-restore-lock-test")
        with patch.object(self.api, "request", side_effect=pause_first_proxy_read):
            worker.start()
            try:
                self.assertTrue(entered.wait(5), "restore did not reach the controller read")
                contender = REAL_INSTANCE_LOCK()
                try:
                    self.assertFalse(contender.acquire())
                finally:
                    contender.close()
            finally:
                release.set()
                worker.join(5)
                self.assertFalse(worker.is_alive(), "restore worker did not stop")

        self.assertEqual(errors, [])
        self.assertEqual(results, [["Main"]])
        self.assertEqual(self.api.select_calls, [("Main", "JP old")])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        available = REAL_INSTANCE_LOCK()
        try:
            self.assertTrue(available.acquire())
        finally:
            available.close()

    def test_restart_between_stop_and_reacquire_prevents_all_restore_work(self):
        before_state = self.state_bytes()
        before_config = optimizer.CONFIG.read_bytes()
        holder = REAL_INSTANCE_LOCK()

        def stop_then_restart():
            self.assertTrue(holder.acquire())
            return True

        try:
            with (
                patch.object(optimizer, "stop_daemon", side_effect=stop_then_restart),
                patch.object(optimizer, "load_config", wraps=optimizer.load_config) as load,
                patch.object(optimizer, "Controller", wraps=optimizer.Controller) as controller,
                patch.object(optimizer, "atomic_json", wraps=optimizer.atomic_json) as write,
            ):
                with self.assertRaisesRegex(RuntimeError, "重试"):
                    optimizer.restore_original()
                load.assert_not_called()
                controller.assert_not_called()
                write.assert_not_called()
            self.assertEqual(optimizer.CONFIG.read_bytes(), before_config)
            self.assertEqual(self.state_bytes(), before_state)
            self.assertEqual(self.api.select_calls, [])
            self.assertEqual(self.api.proxies["Main"]["now"], "JP fast")
        finally:
            holder.close()

    def test_restore_error_releases_real_lock_and_preserves_records_for_retry(self):
        record_paths = [self.state / name for name in ("original-selection.json", "selection.json")]
        before_records = {path: path.read_bytes() for path in record_paths}
        lock_checks = []

        def fail_while_locked(path, *args, **kwargs):
            self.assertEqual(path, "/proxies")
            contender = REAL_INSTANCE_LOCK()
            try:
                lock_checks.append(contender.acquire())
            finally:
                contender.close()
            raise optimizer.ControllerError("mock controller failed during restore")

        with patch.object(self.api, "request", side_effect=fail_while_locked):
            with self.assertRaisesRegex(optimizer.ControllerError, "mock controller failed"):
                optimizer.restore_original()

        self.assertEqual(lock_checks, [False])
        self.assertEqual({path: path.read_bytes() for path in record_paths}, before_records)
        self.assertEqual(self.api.select_calls, [])
        available = REAL_INSTANCE_LOCK()
        try:
            self.assertTrue(available.acquire())
        finally:
            available.close()

        self.assertEqual(optimizer.restore_original(), ["Main"])
        self.assertEqual(self.api.select_calls, [("Main", "JP old")])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")


if __name__ == "__main__":
    unittest.main()
