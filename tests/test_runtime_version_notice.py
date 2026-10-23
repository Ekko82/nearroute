"""Offline reminders distinguish installed files from already loaded runtimes."""

from datetime import datetime, timedelta
import unittest
from unittest.mock import patch

import dashboard


class RuntimeVersionNoticeTests(unittest.TestCase):
    def status(self, **fields):
        return {
            "pid": 12345,
            "phase": "monitoring",
            "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat(),
            "runtime_version": "0.2.34",
            **fields,
        }

    def notice(self, status=None, *, installed="0.2.34", gui="0.2.34"):
        return dashboard.runtime_version_notice(
            self.status() if status is None else status,
            installed_version=installed, gui_version=gui,
        )

    def assert_restart_guidance(self, text):
        for wording in ("退出程序", "重新打开", "托盘"):
            self.assertIn(wording, text)

    def test_equal_versions_are_quiet_and_only_use_supplied_values(self):
        status = self.status()
        original = dict(status)
        with patch.object(dashboard, "read_json", side_effect=AssertionError("no file reads")), \
                patch.object(dashboard.updater, "current_version", side_effect=AssertionError("no version reads")):
            self.assertEqual(self.notice(status), "")
        self.assertEqual(status, original)

    def test_gui_mismatch_or_unknown_version_remains_visible_without_helper(self):
        for gui, expected in (("0.2.26", "窗口仍运行 0.2.26"),
                              (None, "窗口版本未知"),
                              ("GUI_TOKEN_MARKER", "窗口版本未知"),
                              ("v0.2.34", "窗口版本未知")):
            with self.subTest(gui=gui):
                text = self.notice({}, gui=gui)
                self.assertIn(expected, text)
                self.assertNotIn("GUI_TOKEN_MARKER", text)
                self.assert_restart_guidance(text)

    def test_fresh_active_helper_mismatch_is_visible_in_each_live_phase(self):
        for phase in ("monitoring", "paused", "waiting", "scanning", "testing", "measuring", "verifying", "error"):
            with self.subTest(phase=phase):
                text = self.notice(self.status(phase=phase, runtime_version="0.2.26"))
                self.assertIn("后台仍运行 0.2.26", text)
                self.assert_restart_guidance(text)

    def test_legacy_or_invalid_active_helper_version_is_unknown(self):
        for reported in (None, "HELPER_TOKEN_MARKER", "v0.2.34", "0.2.34-beta"):
            with self.subTest(reported=reported):
                status = self.status(runtime_version=reported)
                if reported is None:
                    status.pop("runtime_version")
                text = self.notice(status)
                self.assertIn("后台版本未知", text)
                self.assertNotIn("后台仍运行", text)
                self.assertNotIn("HELPER_TOKEN_MARKER", text)
                self.assert_restart_guidance(text)

    def test_inactive_or_unreliable_status_never_claims_helper_runtime(self):
        old = (datetime.now(dashboard.CHINA_TIME) - timedelta(minutes=10)).isoformat()
        no_pid = self.status()
        no_pid.pop("pid")
        inactive = [({}, "missing"),
                    (self.status(phase="starting"), "starting"),
                    (self.status(phase="stopping"), "stopping"),
                    (self.status(phase="stopped"), "stopped"),
                    (self.status(phase="unexpected"), "unknown phase"),
                    (self.status(updated_at=old), "stale"),
                    (self.status(updated_at="invalid"), "invalid time"),
                    (no_pid, "missing pid"),
                    (self.status(pid=0), "zero pid")]
        for status, reason in inactive:
            for reported in (None, "0.2.26"):
                with self.subTest(reason=reason, reported=reported):
                    status = status | {"runtime_version": reported}
                    self.assertEqual(self.notice(status), "")
                    text = self.notice(status, gui="0.2.26")
                    self.assertIn("窗口仍运行 0.2.26", text)
                    self.assertNotIn("后台", text)

    def test_invalid_installed_version_has_no_raw_value_or_mismatch_claim(self):
        for installed in (None, "INSTALLED_TOKEN_MARKER", "v0.2.34", "0.2.34-beta"):
            with self.subTest(installed=installed):
                text = self.notice(installed=installed)
                self.assertIn("安装版本无法读取", text)
                self.assertNotIn("INSTALLED_TOKEN_MARKER", text)
                self.assertNotIn("窗口仍运行", text)
                self.assertNotIn("后台仍运行", text)

    def test_both_old_runtimes_are_named_with_persistent_restart_guidance(self):
        text = self.notice(self.status(runtime_version="0.2.27"), gui="0.2.26")
        self.assertIn("窗口仍运行 0.2.26", text)
        self.assertIn("后台仍运行 0.2.27", text)
        self.assert_restart_guidance(text)


if __name__ == "__main__":
    unittest.main()
