"""Offline tests for NearRoute settings; never contact or select a proxy."""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import time
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import dashboard


class SettingsPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Path(self.directory.name) / "config.json"
        self.location = patch.object(dashboard, "CONFIG", self.config)
        self.location.start()
        self.addCleanup(self.location.stop)
        self.addCleanup(self.directory.cleanup)

    def write(self, value):
        self.config.write_text(json.dumps(value), encoding="utf-8")

    def test_save_validates_and_preserves_unrelated_latest_fields(self):
        self.write({"enabled": False, "custom_note": {"keep": True}, "health_interval_seconds": 20})
        saved = dashboard.save_settings({
            "probe_url": "https://example.org/generate_204", "probe_expected_statuses": [204],
            "scan_mode": "regions", "scan_regions": ["us", "uk", "UNKNOWN"],
            "scan_nodes": ["日本 exact name"], "prefer_japan": False,
        })
        actual = json.loads(self.config.read_text(encoding="utf-8"))
        self.assertEqual(saved, actual)
        self.assertFalse(actual["enabled"])
        self.assertEqual(actual["custom_note"], {"keep": True})
        self.assertEqual(actual["health_interval_seconds"], 20)
        self.assertEqual(actual["scan_regions"], ["US", "GB", "UNKNOWN"])
        self.assertEqual(actual["probe_expected_statuses"], [204])
        self.assertEqual(list(Path(self.directory.name).glob(".config-*.tmp")), [])

    def test_invalid_settings_leave_original_file_intact(self):
        self.write({"enabled": False, "custom_note": "unchanged"})
        before = self.config.read_bytes()
        with self.assertRaises(ValueError):
            dashboard.save_settings({"probe_url": "file:///etc/passwd"})
        self.assertEqual(self.config.read_bytes(), before)

    def test_empty_explicit_node_list_is_rejected(self):
        self.write({"enabled": False})
        with self.assertRaises(ValueError):
            dashboard.save_settings({"scan_mode": "nodes", "scan_nodes": []})

    def test_status_parser_does_not_guess_for_a_changed_url(self):
        self.write({"probe_expected_statuses": [405]})
        result = dashboard.save_settings({"probe_url": "https://example.org/generate_204"})
        self.assertEqual(result["probe_expected_statuses"], [405])
        self.assertEqual(dashboard.parse_statuses("200, 204，200"), [200, 204])
        with self.assertRaises(ValueError):
            dashboard.parse_statuses("200 or 204")

    def test_regions_support_arbitrary_codes_and_unknown(self):
        self.assertEqual(dashboard.parse_regions("us, DE,unknown,US"), ["US", "DE", "UNKNOWN"])
        with self.assertRaises(ValueError):
            dashboard.parse_regions("Japan")

    def test_summary_reflects_all_modes_target_and_preference(self):
        first = dashboard.policy_summary({"scan_mode": "all", "prefer_japan": False, "probe_url": "https://example.org/check", "probe_expected_statuses": [200, 204]})
        self.assertIn("全部节点", first)
        self.assertNotIn("日本优先", first)
        self.assertIn("https://example.org/check", first)
        self.assertIn("200, 204", first)
        self.assertIn("指定节点（2 个）", dashboard.policy_summary({"scan_mode": "nodes", "scan_nodes": ["A", "B"]}))
        self.assertIn("US、UNKNOWN", dashboard.policy_summary({"scan_regions": ["US", "UNKNOWN"]}))
        adaptive = dashboard.policy_summary({"scan_mode": "adaptive", "scan_regions": ["US"]})
        self.assertIn("自动邻近地区", adaptive)
        self.assertNotIn("US", adaptive)

    def test_runtime_discovery_uses_path_when_sys_executable_empty(self):
        with patch.object(dashboard.sys, "executable", ""), patch.object(dashboard.shutil, "which", return_value="python-from-path"):
            self.assertEqual(dashboard.python_executable(), "python-from-path")


class SettingsWindowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        try:
            cls.root = tk.Tk()
            cls.root.withdraw()
        except tk.TclError as error:
            raise unittest.SkipTest(f"Tk display unavailable: {error}")

    @classmethod
    def tearDownClass(cls):
        cls.root.destroy()

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.config = Path(self.directory.name) / "config.json"
        self.config.write_text(json.dumps({
            "enabled": False, "scan_mode": "nodes", "scan_nodes": ["JP-online", "SG-offline"],
        }), encoding="utf-8")
        self.location = patch.object(dashboard, "CONFIG", self.config)
        self.location.start()
        self.owner = SimpleNamespace(root=self.root, settings_saved=Mock(), settings_dialog=None, show_tab=Mock())
        self.parent = dashboard.ttk.Frame(self.root)
        self.parent.pack(fill="both", expand=True)
        self.dialog = dashboard.SettingsDialog(self.owner, parent=self.parent, load_nodes=False, visible=False)
        self.owner.settings_dialog = self.dialog
        self.root.update_idletasks()

    def tearDown(self):
        if not self.dialog.closed:
            self.dialog.dispose()
        self.parent.destroy()
        self.location.stop()
        self.directory.cleanup()

    def test_offline_open_preserves_names_but_explicit_clear_can_remove_them(self):
        self.assertEqual(set(self.dialog.fields()["scan_nodes"]), {"JP-online", "SG-offline"})
        self.assertEqual(str(self.dialog.node_list.cget("state")), "normal")
        self.dialog.clear_nodes()
        self.assertEqual(self.dialog.fields()["scan_nodes"], [])
        self.assertEqual(set(self.dialog.node_names), {"JP-online", "SG-offline"})

    def test_select_all_and_clear_apply_to_available_and_unavailable_rows(self):
        self.dialog.apply_loaded_nodes(["JP-online", "HK-online"])
        self.dialog.select_all_nodes()
        self.assertEqual(set(self.dialog.fields()["scan_nodes"]), {"JP-online", "HK-online", "SG-offline"})
        self.dialog.clear_nodes()
        self.assertEqual(self.dialog.fields()["scan_nodes"], [])
        self.dialog.select_all_nodes()
        self.assertEqual(set(self.dialog.fields()["scan_nodes"]), {"JP-online", "HK-online", "SG-offline"})

    def test_user_can_deselect_unavailable_name(self):
        self.dialog.apply_loaded_nodes(["JP-online"])
        self.dialog.node_list.selection_clear(0, tk.END)
        self.dialog.selection_changed()
        self.assertEqual(self.dialog.fields()["scan_nodes"], [])

    def test_refresh_failure_keeps_selection(self):
        self.dialog.apply_loaded_nodes(["JP-online", "HK-online"])
        self.dialog.select_all_nodes()
        self.dialog.results.put((False, "offline"))
        self.dialog.window.after_cancel(self.dialog.after_id)
        self.dialog.poll_results()
        self.assertEqual(set(self.dialog.fields()["scan_nodes"]), {"JP-online", "HK-online", "SG-offline"})
        self.assertFalse(self.dialog.nodes_ready)

    def test_refresh_uses_snapshot_and_never_selects_proxy(self):
        with patch.object(dashboard.core, "Controller") as controller:
            instance = controller.return_value
            instance.snapshot.return_value = ({}, {
                "MAIN": {"all": ["JP-online", "DIRECT"], "now": "JP-online"},
                "JP-online": {"type": "Trojan"}, "DIRECT": {"type": "Direct"},
            }, "MAIN")
            self.dialog.refresh_nodes()
            deadline = time.monotonic() + 2
            while self.dialog.results.empty() and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertFalse(self.dialog.results.empty())
            self.dialog.window.after_cancel(self.dialog.after_id)
            self.dialog.poll_results()
            self.assertEqual(self.dialog.available_nodes, ["JP-online"])
            instance.snapshot.assert_called_once()
            instance.select.assert_not_called()

    def test_save_preserves_pause_and_calls_scan_callback(self):
        self.dialog.url.set("https://example.org/check")
        self.dialog.expected.set("200,204")
        self.dialog.mode.set("all")
        self.dialog.prefer_japan.set(False)
        self.dialog.save()
        self.owner.settings_saved.assert_called_once()
        saved = self.owner.settings_saved.call_args.args[0]
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["probe_expected_statuses"], [200, 204])
        self.assertEqual(set(saved["scan_nodes"]), {"JP-online", "SG-offline"})
        self.assertFalse(self.dialog.closed)
        self.assertIs(self.owner.settings_dialog, self.dialog)
        self.assertIn("已保存", self.dialog.save_message.get())
        self.assertFalse(self.dialog.has_unsaved_changes())
        self.owner.show_tab.assert_not_called()

    def test_inline_panel_back_retains_draft_and_explicit_discard_releases_panel(self):
        self.assertIsInstance(self.dialog.window, dashboard.ttk.Frame)
        self.assertIs(self.dialog.window.master, self.parent)
        self.assertIs(self.dialog.window.winfo_toplevel(), self.root)
        self.dialog.expected.set("unfinished,")
        self.dialog.close()
        self.owner.show_tab.assert_called_once_with("overview")
        self.assertFalse(self.dialog.closed)
        self.assertIs(self.owner.settings_dialog, self.dialog)
        self.assertEqual(self.dialog.expected.get(), "unfinished,")
        self.assertTrue(self.dialog.has_unsaved_changes())
        self.dialog.discard_changes()
        self.assertTrue(self.dialog.closed)
        self.assertIsNone(self.owner.settings_dialog)
        self.assertIsNone(self.dialog.after_id)
        self.owner.show_tab.reset_mock()
        self.dialog.dispose()
        self.owner.show_tab.assert_not_called()

    def test_raw_draft_detection_accepts_invalid_fields_and_tracks_node_selection(self):
        self.assertFalse(self.dialog.has_unsaved_changes())
        self.dialog.expected.set("unfinished,")
        self.assertTrue(self.dialog.has_unsaved_changes())
        self.dialog.expected.set("405")
        self.assertFalse(self.dialog.has_unsaved_changes())
        self.dialog.clear_nodes()
        self.assertTrue(self.dialog.has_unsaved_changes())

    def test_invalid_save_displays_error_without_writing(self):
        before = self.config.read_bytes()
        self.dialog.expected.set("200,999")
        with patch.object(dashboard.messagebox, "showerror") as error:
            self.dialog.save()
        error.assert_called_once()
        self.assertEqual(self.config.read_bytes(), before)
        self.owner.settings_saved.assert_not_called()
        self.assertFalse(self.dialog.closed)
        self.assertTrue(self.dialog.has_unsaved_changes())

    def test_busy_owner_preserves_draft_without_saving_or_requesting_scan(self):
        self.dialog.url.set("https://example.org/draft")
        before = self.config.read_bytes()
        with patch.object(dashboard, "save_settings") as save:
            for name in ("exiting", "restore_busy", "manual_busy", "client_busy"):
                with self.subTest(operation=name):
                    setattr(self.owner, name, True)
                    self.dialog.save()
                    self.assertIn("稍后保存", self.dialog.save_message.get())
                    self.assertTrue(self.dialog.has_unsaved_changes())
                    self.assertFalse(self.dialog.closed)
                    setattr(self.owner, name, False)
            save.assert_not_called()
        self.assertEqual(self.config.read_bytes(), before)
        self.owner.settings_saved.assert_not_called()

    def test_scope_controls_follow_mode(self):
        self.dialog.apply_loaded_nodes(["JP-online"])
        self.dialog.mode.set("all")
        self.dialog.update_mode()
        self.assertIn("disabled", self.dialog.region_entry.state())
        self.assertEqual(str(self.dialog.node_list.cget("state")), "disabled")
        self.dialog.mode.set("regions")
        self.dialog.update_mode()
        self.assertNotIn("disabled", self.dialog.region_entry.state())
        self.dialog.regions.set("US, UNKNOWN")
        self.assertEqual(self.dialog.fields()["scan_regions"], ["US", "UNKNOWN"])

    def test_adaptive_scope_preserves_manual_ranges_and_pause(self):
        self.dialog.regions.set("US, TW")
        self.dialog.mode.set("adaptive")
        self.dialog.update_mode()
        self.assertIn("disabled", self.dialog.region_entry.state())
        self.assertEqual(str(self.dialog.node_list.cget("state")), "disabled")
        self.assertEqual(self.dialog.fields()["scan_regions"], ["US", "TW"])
        self.assertEqual(set(self.dialog.fields()["scan_nodes"]), {"JP-online", "SG-offline"})
        self.dialog.save()
        saved = self.owner.settings_saved.call_args.args[0]
        self.assertEqual(saved["scan_mode"], "adaptive")
        self.assertEqual(saved["scan_regions"], ["US", "TW"])
        self.assertFalse(saved["enabled"])
        self.assertTrue(saved["prefer_japan"])


if __name__ == "__main__":
    unittest.main()
