"""A running control window restores only an enabled, absent helper."""

from contextlib import ExitStack
from datetime import datetime, timedelta
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid

import dashboard
from tests import test_dashboard_manual as manual_tests


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture = manual_tests.StatusFreshnessTests()
        fixture.setUp()
        self.app = fixture.app
        del self.app.ensure_service  # Exercise the real start/tracking path.
        self.app.service_recovery_enabled = True
        self.app.next_service_recovery_at = 0
        self.app.started_helpers = []
        self.app.update_busy = None
        self.app.updates_enabled = False
        self.config = dashboard.core.DEFAULTS | {"enabled": True}
        self.status = fixture.status | {
            "updated_at": (datetime.now(dashboard.CHINA_TIME) - timedelta(minutes=10)).isoformat(),
        }
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.location = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.location / "state"
        self.state.mkdir()
        for key, path in (("ROOT", self.location), ("STATE", self.state),
                          ("CONFIG", self.location / "config.json"),
                          ("STATUS", self.state / "status.json")):
            self.stack.enter_context(patch.object(dashboard, key, path))
        dashboard.CONFIG.write_text(json.dumps(self.config), encoding="utf-8")
        dashboard.STATUS.write_text(json.dumps(self.status), encoding="utf-8")
        self.clock = self.stack.enter_context(patch.object(dashboard.time, "monotonic", return_value=1000.0))
        self.child = Mock()
        self.child.poll.return_value = None
        self.start = self.stack.enter_context(patch.object(dashboard, "start_daemon", return_value=self.child))
        self.lock = Mock()
        self.lock.acquire.return_value = True
        self.real_instance_lock = dashboard.core.InstanceLock
        self.lock_factory = self.stack.enter_context(patch.object(dashboard.core, "InstanceLock", return_value=self.lock))
        self.sleep = self.stack.enter_context(patch.object(dashboard.time, "sleep", side_effect=AssertionError("UI must not wait")))

    def recover(self):
        self.app.recover_service(self.status, self.config)

    def test_refresh_recovers_missing_backend_and_keeps_cached_status_marked_stale(self):
        self.app.refresh()
        self.start.assert_called_once()
        self.assertEqual(self.app.started_helpers, [self.child])
        self.assertIn("恢复自动优选", self.app.notify.call_args.args[0])
        self.assertIn("未更新", self.app.badge.set.call_args.args[0])
        self.assertNotIn("控制接口：已连接", self.app.health.set.call_args.args[0])
        self.lock.close.assert_called_once()

        self.clock.return_value += dashboard.SERVICE_RECOVERY_SECONDS
        self.app.refresh()
        self.start.assert_called_once()  # A launched, still-starting child is tracked.
        self.assertEqual(json.loads(dashboard.CONFIG.read_text(encoding="utf-8")), self.config)
        self.assertFalse((self.state / "scan.request").exists())

    def test_healthy_snapshot_and_startup_grace_do_not_start_another_helper(self):
        self.status["updated_at"] = datetime.now(dashboard.CHINA_TIME).isoformat()
        self.recover()
        self.status = {}
        self.app.next_service_recovery_at = self.clock.return_value + 1
        self.recover()
        self.start.assert_not_called()
        self.lock_factory.assert_not_called()
        self.clock.return_value += 1
        self.recover()
        self.start.assert_called_once()

    def test_refresh_recovers_with_the_default_enabled_setting(self):
        dashboard.CONFIG.write_text("{}", encoding="utf-8")
        self.app.refresh()
        self.start.assert_called_once()
        self.assertEqual(json.loads(dashboard.CONFIG.read_text(encoding="utf-8")), {})

    def test_bad_or_unreadable_config_does_not_recover_from_old_enabled_status(self):
        dashboard.STATUS.write_text(json.dumps(self.status | {"enabled": True}), encoding="utf-8")
        for invalid in ("{incomplete", "[]", None):
            with self.subTest(config=invalid):
                if invalid is None:
                    dashboard.CONFIG.unlink()
                else:
                    dashboard.CONFIG.write_text(invalid, encoding="utf-8")
                self.app.refresh()
        self.start.assert_not_called()
        self.lock_factory.assert_not_called()

    def test_active_helper_with_delayed_status_is_left_running(self):
        self.lock.acquire.return_value = False
        self.recover()
        self.start.assert_not_called()
        self.lock.close.assert_called_once()
        self.lock.acquire.return_value = True
        self.clock.return_value += dashboard.SERVICE_RECOVERY_SECONDS
        self.recover()
        self.start.assert_called_once()

    def test_paused_read_only_and_conflicting_ui_operations_never_recover(self):
        for enabled in (False, None):
            with self.subTest(enabled=enabled):
                self.app.recover_service(self.status, self.config | {"enabled": enabled})
        for attribute in ("closed", "exiting", "restore_busy", "manual_busy", "client_busy", "update_busy"):
            with self.subTest(attribute=attribute):
                setattr(self.app, attribute, True)
                self.recover()
                setattr(self.app, attribute, False)
        self.app.service_recovery_enabled = False
        self.recover()
        self.start.assert_not_called()
        self.lock_factory.assert_not_called()

    def test_explicit_stop_and_installer_lock_are_respected(self):
        stop = self.state / "stop.request"
        stop.touch()
        self.recover()
        stop.unlink()
        self.clock.return_value += dashboard.SERVICE_RECOVERY_SECONDS
        install = dashboard.updater.InstallLock(self.location)
        self.assertTrue(install.acquire())
        try:
            self.recover()
        finally:
            install.close()
        self.start.assert_not_called()
        self.lock_factory.assert_not_called()

    def test_failed_launch_is_visible_and_waits_before_retrying(self):
        self.start.side_effect = OSError("fixture launch failure")
        self.recover()
        self.assertIn("服务启动失败", self.app.notify.call_args.args[0])
        self.recover()
        self.clock.return_value += dashboard.SERVICE_RECOVERY_SECONDS - 1
        self.recover()
        self.start.assert_called_once()
        self.clock.return_value += 1
        self.recover()
        self.assertEqual(self.start.call_count, 2)
        self.assertEqual(self.app.started_helpers, [])

    def test_lock_probe_failure_does_not_break_refresh_or_spawn(self):
        self.lock.acquire.side_effect = OSError("fixture mutex failure")
        self.app.refresh()
        self.start.assert_not_called()
        self.lock.close.assert_called_once()
        self.assertIn("后台恢复未完成", self.app.notify.call_args.args[0])
        self.app.root.after.assert_called_with(2000, self.app.refresh)

    @unittest.skipUnless(os.name == "nt", "requires the real Windows helper mutex")
    def test_real_mutex_distinguishes_slow_helper_from_a_released_instance(self):
        with patch.object(dashboard.core, "InstanceLock", self.real_instance_lock), \
             patch.object(dashboard.core, "MUTEX_NAME", "Local\\NearRouteRecoveryTest_" + uuid.uuid4().hex):
            owner = dashboard.core.InstanceLock()
            self.assertTrue(owner.acquire())
            try:
                self.recover()
                self.start.assert_not_called()
            finally:
                owner.close()
            self.clock.return_value += dashboard.SERVICE_RECOVERY_SECONDS
            self.recover()
            self.start.assert_called_once()


if __name__ == "__main__":
    unittest.main()
