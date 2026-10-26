"""Real inline navigation uses temporary data and never contacts a controller."""

import json
from unittest.mock import Mock, patch
import unittest

import dashboard
from tests import test_dashboard_layout as layout_tests
from tests.test_dashboard_updates import RELEASE


class DashboardTabsTests(unittest.TestCase):
    publish = layout_tests.DashboardLayoutTests.publish
    arrange = layout_tests.DashboardLayoutTests.arrange
    widgets = layout_tests.DashboardLayoutTests.widgets
    assert_inside_window = layout_tests.DashboardLayoutTests.assert_inside_window

    def setUp(self):
        layout_tests.DashboardLayoutTests.setUp(self)
        settings, client = dashboard.SettingsDialog, dashboard.ClientDialog
        self.stack.enter_context(patch.object(dashboard, "SettingsDialog", side_effect=
            lambda owner, *, parent: settings(owner, parent=parent, load_nodes=False)))
        self.stack.enter_context(patch.object(dashboard, "ClientDialog", side_effect=
            lambda owner, *, parent: client(owner, parent=parent, visible=False)))
        self.stack.enter_context(patch.object(dashboard.tk, "Toplevel", side_effect=AssertionError("no child windows")))
        self.stack.enter_context(patch.object(dashboard.core, "Controller", side_effect=AssertionError("no controller calls")))
        self.stack.enter_context(patch.object(dashboard.core.sqlite3, "connect", side_effect=AssertionError("Codex logs require opt-in")))

    def test_pages_are_lazy_single_window_tabs_with_no_modal_grab(self):
        self.assertEqual([self.app.tabs.tab(tab, "text") for tab in self.app.tabs.tabs()],
                         ["线路概览", "客户端连接", "测速设置", "版本与更新", "诊断信息"])
        for attribute in ("client_dialog", "settings_dialog", "update_dialog", "diagnostic_dialog"):
            self.assertIsNone(getattr(self.app, attribute))
        for key, attribute in (("client", "client_dialog"), ("settings", "settings_dialog"),
                               ("updates", "update_dialog"), ("diagnostics", "diagnostic_dialog")):
            with self.subTest(page=key):
                self.app.tabs.select(self.app.pages[key])
                self.root.update()
                panel = getattr(self.app, attribute)
                self.assertIs(panel.window.master, self.app.pages[key])
                self.assertIs(panel.window.winfo_toplevel(), self.root)
                self.assertEqual(self.app.active_tab, key)
                self.assertIsNone(self.root.grab_current())
        self.assertFalse(any(widget.winfo_class() == "Toplevel" for widget in self.widgets()))
        self.app.update_dialog.open_diagnostics()
        self.assertEqual(self.app.active_tab, "diagnostics")

    def test_navigation_preserves_drafts_and_save_keeps_the_current_page(self):
        self.app.open_settings()
        settings = self.app.settings_dialog
        settings.url.set("https://fixture.invalid/new-health")
        settings.close()
        self.assertIs(self.app.settings_dialog, settings)
        self.assertTrue(settings.has_unsaved_changes())
        self.app.open_client()
        client = self.app.client_dialog
        client.controller.set("http://127.0.0.1:9090")
        self.app.open_updates()
        updates = self.app.update_dialog
        updates.auto_check.set(not self.app.update_preferences["auto_check"])
        self.app.open_settings()
        self.assertEqual(settings.url.get(), "https://fixture.invalid/new-health")
        self.assertEqual(client.controller.get(), "http://127.0.0.1:9090")
        self.app.settings_saved = Mock()
        settings.save()
        self.assertFalse(settings.closed)
        self.assertEqual(self.app.active_tab, "settings")
        self.assertFalse(settings.has_unsaved_changes())
        saved = json.loads(dashboard.CONFIG.read_text(encoding="utf-8"))
        self.assertEqual(saved["probe_url"], "https://fixture.invalid/new-health")
        self.assertIn("已保存", settings.save_message.get())
        self.app.open_diagnostics()
        diagnostics = self.app.diagnostic_dialog
        summary = diagnostics.text.get("1.0", "end")
        diagnostics.close()
        self.app.open_diagnostics()
        self.assertIs(self.app.diagnostic_dialog, diagnostics)
        self.assertEqual(diagnostics.text.get("1.0", "end"), summary)

    def test_installation_protects_hidden_drafts_and_clean_hidden_pages_allow_idle_install(self):
        self.app.open_settings()
        self.app.settings_dialog.url.set("https://fixture.invalid/draft")
        self.app.show_tab("overview")
        self.app.update_release = RELEASE
        self.app.update_can_install = True
        self.app.update_prepared = self.location / "prepared"
        with patch.object(dashboard.messagebox, "askyesno") as confirm:
            self.app.request_update_install()
        confirm.assert_not_called()
        self.assertIn("未保存", self.app.update_text.get())
        self.assertFalse(self.app.automatic_install_ready(self.status))
        self.app.update_requested_install = True
        self.app.begin_update_install()
        self.assertFalse(self.app.exiting)
        self.assertFalse(self.app.update_requested_install)
        self.app.settings_dialog.discard_changes()
        self.app.open_updates()
        updates = self.app.update_dialog
        self.assertFalse(self.app.automatic_install_ready(self.status))
        self.app.hidden_to_tray = True
        self.assertTrue(self.app.automatic_install_ready(self.status))
        self.app.hidden_to_tray = False
        updates.close()
        self.assertIs(self.app.update_dialog, updates)
        self.assertTrue(self.app.automatic_install_ready(self.status))
        updates.auto_check.set(not self.app.update_preferences["auto_check"])
        self.app.hidden_to_tray = True
        self.assertFalse(self.app.automatic_install_ready(self.status))
        updates.discard_changes()
        self.assertIsNone(self.app.update_dialog)
        self.assertTrue(self.app.automatic_install_ready(self.status))

    def test_every_page_keeps_fields_and_actions_inside_the_minimum_main_window(self):
        self.arrange(920, 650)
        for key, attribute in (("client", "client_dialog"), ("settings", "settings_dialog"),
                               ("updates", "update_dialog"), ("diagnostics", "diagnostic_dialog")):
            with self.subTest(page=key):
                self.app.show_tab(key)
                self.root.update()
                panel = getattr(self.app, attribute)
                controls = [widget for widget in self.widgets(panel.window)
                            if widget.winfo_ismapped() and widget.winfo_class() in
                            ("TButton", "TEntry", "TCombobox", "Listbox", "Text")]
                self.assertGreaterEqual(len(controls), 3)
                for widget in controls:
                    self.assert_inside_window(widget)
                for notebook in [widget for widget in self.widgets(panel.window)
                                 if widget.winfo_class() == "TNotebook"]:
                    for tab in notebook.tabs():
                        notebook.select(tab)
                        self.root.update()
                        for widget in self.widgets(notebook):
                            if widget.winfo_ismapped() and widget.winfo_class() in ("TButton", "TEntry", "TCombobox"):
                                self.assert_inside_window(widget)


if __name__ == "__main__":
    unittest.main()
