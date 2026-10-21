"""Real process-lock regressions using isolated config/state and fake controllers."""

from contextlib import ExitStack
import json
import multiprocessing
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dashboard
import optimizer


def _config_worker(config_path, operation, hold_phase, started, holding, release, completed, result):
    """A cooperating writer; events expose the actual read/write boundary."""
    config = Path(config_path)
    lock = optimizer.config_update_lock
    atomic = optimizer.atomic_json
    gui_atomic = dashboard.atomic_write_config

    def update_lock(path=None):
        started.set()
        return lock(path)

    def wait_if_requested(phase):
        if hold_phase == phase:
            holding.set()
            if not release.wait(10):
                raise TimeoutError("test gate was not released")

    def core_write(path, value):
        if path == config:
            wait_if_requested("config")
        return atomic(path, value)

    def gui_write(value):
        wait_if_requested("config")
        return gui_atomic(value)

    def controller_read(_path):
        wait_if_requested("controller")
        if _path == "/configs":
            return {"mode": "direct", "mixed-port": 7890}
        assert _path == "/proxies", "unexpected controller request"
        return {"proxies": {}}

    snapshot = optimizer.Controller.snapshot
    api = Mock()
    api.request.side_effect = controller_read
    api.snapshot.side_effect = lambda cfg, **kwargs: snapshot(api, cfg, **kwargs)
    api.select.side_effect = AssertionError("test must not select a node")
    try:
        with ExitStack() as stack:
            stack.enter_context(patch.object(optimizer, "CONFIG", config))
            stack.enter_context(patch.object(dashboard, "CONFIG", config))
            stack.enter_context(patch.object(optimizer, "STATE", config.parent / "test-state"))
            stack.enter_context(patch.object(optimizer, "config_update_lock", side_effect=update_lock))
            stack.enter_context(patch.object(optimizer, "atomic_json", side_effect=core_write))
            stack.enter_context(patch.object(dashboard, "atomic_write_config", side_effect=gui_write))
            stack.enter_context(patch.object(optimizer, "Controller", return_value=api))
            stack.enter_context(patch.object(optimizer.urllib.request, "build_opener", side_effect=AssertionError("real network forbidden")))
            stack.enter_context(patch.object(optimizer.http.client, "HTTPConnection", side_effect=AssertionError("real HTTP forbidden")))
            stack.enter_context(patch.object(optimizer.http.client, "HTTPSConnection", side_effect=AssertionError("real TLS forbidden")))
            action = operation["action"]
            if action == "save":
                value = dashboard.save_settings(operation["fields"])
            elif action == "enable":
                value = dashboard.update_enabled(operation["enabled"])
            elif action == "restore":
                value = optimizer._restore_original_locked()
            elif action == "load":
                value = optimizer.load_config()
            else:
                raise AssertionError("unknown worker action")
            result.send(("ok", value))
    except Exception as error:
        result.send(("error", type(error).__name__, str(error)))
    finally:
        completed.set()
        result.close()


class ConfigUpdateTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nearroute-config-test-")
        self.addCleanup(temporary.cleanup)
        self.config = Path(temporary.name) / "config.json"
        self.context = multiprocessing.get_context("spawn")
        self.initial = {
            "enabled": True, "probe_url": "https://example.org/old", "probe_expected_statuses": [204],
            "codex_log_db": "", "custom_note": "original", "health_interval_seconds": 15,
        }
        self.write(self.initial)

    def write(self, value):
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def read(self):
        return json.loads(self.config.read_text(encoding="utf-8"))

    def start_worker(self, operation, hold_phase=None):
        receive, send = self.context.Pipe(duplex=False)
        worker = SimpleNamespace(
            started=self.context.Event(), holding=self.context.Event(), release=self.context.Event(),
            completed=self.context.Event(), result=receive,
        )
        worker.process = self.context.Process(target=_config_worker, args=(
            str(self.config), operation, hold_phase, worker.started, worker.holding,
            worker.release, worker.completed, send,
        ))
        worker.process.start()
        send.close()
        self.addCleanup(self.cleanup_worker, worker)
        return worker

    @staticmethod
    def cleanup_worker(worker):
        worker.release.set()
        worker.process.join(3)
        if worker.process.is_alive():
            worker.process.terminate()
            worker.process.join(3)
        worker.result.close()
        worker.process.close()

    def finish(self, worker):
        self.assertTrue(worker.completed.wait(8), "config operation did not finish")
        worker.process.join(3)
        self.assertFalse(worker.process.is_alive(), "worker was not released")
        self.assertEqual(worker.process.exitcode, 0)
        outcome = worker.result.recv()
        self.assertEqual(outcome[0], "ok", outcome)
        return outcome[1]

    def contend(self, first_operation, second_operation):
        first = self.start_worker(first_operation, hold_phase="config")
        self.assertTrue(first.holding.wait(8), "first writer did not reach its write boundary")
        second = self.start_worker(second_operation)
        self.assertTrue(second.started.wait(8), "second writer did not attempt the lock")
        try:
            self.assertFalse(second.completed.wait(0.2), "second writer bypassed the held config lock")
        finally:
            first.release.set()
        self.finish(first)
        self.finish(second)

    def test_settings_and_restore_preserve_pause_and_new_settings_in_both_orders(self):
        save = {"action": "save", "fields": {"probe_url": "https://example.org/new"}}
        restore = {"action": "restore"}
        for first, second in ((save, restore), (restore, save)):
            with self.subTest(first=first["action"]):
                self.write(self.initial)
                self.contend(first, second)
                actual = self.read()
                self.assertFalse(actual["enabled"])
                self.assertEqual(actual["probe_url"], "https://example.org/new")
                self.assertEqual(actual["custom_note"], "original")

    def test_enable_and_other_settings_do_not_overwrite_each_other(self):
        self.write(self.initial | {"enabled": False})
        self.contend(
            {"action": "enable", "enabled": True},
            {"action": "save", "fields": {"health_interval_seconds": 30, "custom_note": "latest"}},
        )
        actual = self.read()
        self.assertTrue(actual["enabled"])
        self.assertEqual(actual["health_interval_seconds"], 30)
        self.assertEqual(actual["custom_note"], "latest")

    def test_first_load_does_not_replace_config_created_while_waiting_for_lock(self):
        self.config.unlink()
        with optimizer.config_update_lock(self.config):
            reader = self.start_worker({"action": "load"})
            self.assertTrue(reader.started.wait(8), "initializer did not see the absent config")
            self.assertFalse(reader.completed.wait(0.2))
            self.write(self.initial | {"enabled": False, "custom_note": "created-by-gui"})
        loaded = self.finish(reader)
        self.assertFalse(loaded["enabled"])
        self.assertEqual(loaded["custom_note"], "created-by-gui")
        self.assertEqual(self.read()["custom_note"], "created-by-gui")

    def test_validation_and_write_errors_release_lock_without_changing_config(self):
        with patch.object(dashboard, "CONFIG", self.config):
            with self.assertRaises(ValueError):
                dashboard.save_settings({"probe_url": "file:///invalid"})
            self.assertEqual(self.read(), self.initial)
            self.finish(self.start_worker({"action": "enable", "enabled": False}))
            self.assertFalse(self.read()["enabled"])
            before = self.config.read_bytes()
            with patch.object(dashboard, "atomic_write_config", side_effect=OSError("fake write failure")):
                with self.assertRaises(OSError):
                    dashboard.save_settings({"custom_note": "not-written"})
            self.assertEqual(self.config.read_bytes(), before)
            self.finish(self.start_worker({"action": "enable", "enabled": True}))
            self.assertTrue(self.read()["enabled"])

    def test_restore_releases_config_lock_before_controller_requests(self):
        restore = self.start_worker({"action": "restore"}, hold_phase="controller")
        self.assertTrue(restore.holding.wait(8), "restore did not reach its controller read")
        update = self.start_worker({"action": "save", "fields": {"custom_note": "saved-during-restore"}})
        try:
            self.finish(update)
            self.assertFalse(self.read()["enabled"])
            self.assertEqual(self.read()["custom_note"], "saved-during-restore")
        finally:
            restore.release.set()
        self.finish(restore)


if __name__ == "__main__":
    unittest.main()
