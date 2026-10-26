"""Offline client connection checks; never use a real controller or helper."""

from contextlib import ExitStack
from datetime import datetime
import gc
import json
from pathlib import Path
import queue
import tempfile
import tkinter as tk
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import weakref

import dashboard


class ClientDialogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nearroute-client-ui-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "config.json"
        self.config.write_text(json.dumps({"enabled": False}), encoding="utf-8")
        self.owner = SimpleNamespace(root=Mock(), exiting=False, restore_busy=False, manual_busy=False,
                                     client_busy=False, client_dialog=None, client_saved=Mock(), cancel_service_start=Mock(), started_helpers=[], show_tab=Mock())
        self.parent = Mock(name="client_tab")
        self.interpreter = tk.Tcl()
        string_var = tk.StringVar
        boolean_var = tk.BooleanVar
        self.workers = []
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(dashboard, "CONFIG", self.config))
        self.toplevel = self.patches.enter_context(patch.object(dashboard.tk, "Toplevel"))
        self.patches.enter_context(patch.object(dashboard.tk, "StringVar", side_effect=lambda **kwargs: string_var(master=self.interpreter, **kwargs)))
        self.patches.enter_context(patch.object(dashboard.tk, "BooleanVar", side_effect=lambda **kwargs: boolean_var(master=self.interpreter, **kwargs)))
        self.patches.enter_context(patch.object(dashboard, "ttk"))
        self.patches.enter_context(patch.object(dashboard.threading, "Thread", side_effect=self.thread))
        self.inspection = {"mode": "rule", "groups": ["PROXY", "GLOBAL"], "active_group": "PROXY", "proxy_port": 7890}
        self.inspect = self.patches.enter_context(patch.object(dashboard.core, "inspect_controller", return_value=self.inspection))
        self.discovery = self.patches.enter_context(patch.object(dashboard.core, "discover_clients"))
        self.configure = self.patches.enter_context(patch.object(dashboard.core, "configure_client"))
        self.import_auth = self.patches.enter_context(patch.object(dashboard.windows_app, "import_maomao_auth", create=True))
        self.wait_for_startup = self.patches.enter_context(patch.object(dashboard, "wait_for_helper_startup", return_value=True))
        self.start_daemon = self.patches.enter_context(patch.object(dashboard, "start_daemon", side_effect=AssertionError("real helper forbidden")))
        self.dialog = dashboard.ClientDialog(self.owner, parent=self.parent, visible=False)
        self.owner.client_dialog = self.dialog
        self.addCleanup(self.close_dialog)

    def thread(self, **kwargs):
        self.workers.append(kwargs["target"])
        return Mock()

    def close_dialog(self):
        self.dialog.applying = False
        if not self.dialog.closed:
            self.dialog.dispose()

    def finish(self):
        self.workers.pop(0)()
        self.dialog.poll_results()

    def test_inspect_is_async_and_only_populates_readonly_groups(self):
        self.dialog.controller.set("http://127.0.0.1:9090/")
        self.dialog.secret_env.set("TEST_CLASH_SECRET")
        self.dialog.inspect_connection()
        self.assertTrue(self.dialog.loading)
        self.inspect.assert_not_called()
        before = self.dialog.message.get()
        self.workers.pop(0)()
        self.assertEqual(self.dialog.message.get(), before)  # Worker never touches Tcl.
        self.dialog.poll_results()
        self.inspect.assert_called_once_with({"controller": "http://127.0.0.1:9090", "controller_secret_env": "TEST_CLASH_SECRET", "group": dashboard.core.DEFAULTS["group"],
                                              "auto_detect_client": True, "auto_select_group": True, "controller_api_mode": "standard"})
        self.assertFalse(self.dialog.loading)
        self.assertEqual(self.dialog.selector_groups, ["PROXY", "GLOBAL"])
        self.assertEqual(self.dialog.group.get(), "PROXY")
        self.assertIn("7890", self.dialog.message.get())
        self.configure.assert_not_called()
        self.start_daemon.assert_not_called()
        self.inspection = self.inspection | {"groups": ["GLOBAL", "PROXY"], "active_group": None}
        self.inspect.return_value = self.inspection
        self.dialog.group.set("old unavailable group")
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.group.get(), "")
        self.dialog.save()
        self.configure.assert_not_called()
        self.assertEqual(self.workers, [])
        self.inspect.return_value = self.inspection | {"mode": "global", "active_group": "GLOBAL"}
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.group.get(), "GLOBAL")

    def test_identity_edit_discards_inflight_result_and_requires_new_inspection(self):
        self.dialog.inspect_connection()
        self.dialog.controller.set("http://127.0.0.1:9090")
        self.finish()
        self.assertFalse(self.dialog.loading)
        self.assertIsNone(self.dialog.verified_identity)
        self.assertEqual(self.dialog.selector_groups, [])
        self.assertIn("重新测试", self.dialog.message.get())
        self.dialog.save()
        self.configure.assert_not_called()
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.verified_identity, ("http://127.0.0.1:9090", ""))
        self.dialog.secret_env.set("OTHER_SECRET")
        self.assertIsNone(self.dialog.verified_identity)
        self.assertEqual(self.dialog.selector_groups, [])

    def test_save_requires_verified_group_then_passes_original_context_without_starting_service(self):
        self.dialog.save()
        self.assertEqual(self.workers, [])
        self.dialog.controller.set("http://127.0.0.1:9090")
        self.dialog.inspect_connection()
        self.finish()
        self.dialog.group.set("not a selector")
        self.dialog.save()
        self.configure.assert_not_called()
        self.assertEqual(self.workers, [])
        self.dialog.group.set("PROXY")
        saved = dashboard.core.DEFAULTS | self.dialog.fields() | {"enabled": False}
        self.configure.return_value = saved
        self.owner.started_helpers = [Mock(name="starting_helper")]
        original = self.dialog.original_context
        self.dialog.save()
        self.assertTrue(self.owner.client_busy)
        self.assertTrue(self.dialog.applying)
        self.owner.cancel_service_start.assert_called_once()
        self.configure.assert_not_called()
        self.dialog.close()
        self.assertFalse(self.dialog.closed)  # Applying cannot lose its completion callback.
        self.workers.pop(0)()
        self.owner.client_saved.assert_not_called()
        self.dialog.poll_results()
        self.configure.assert_called_once_with({"controller": "http://127.0.0.1:9090", "controller_secret_env": "", "group": "PROXY",
                                                "auto_detect_client": True, "auto_select_group": True, "controller_api_mode": "standard"}, expected_context=original)
        self.wait_for_startup.assert_called_once_with(self.owner.started_helpers)
        self.owner.client_saved.assert_called_once_with(saved)
        self.assertFalse(self.owner.client_busy)
        self.assertFalse(self.dialog.closed)
        self.assertIs(self.owner.client_dialog, self.dialog)
        self.assertIn("已保存", self.dialog.message.get())
        self.assertEqual(self.dialog.original_context, dashboard.core.state_context(saved))
        self.assertFalse(self.dialog.has_unsaved_changes())
        self.owner.show_tab.assert_not_called()
        self.start_daemon.assert_not_called()

    def test_selected_group_name_is_saved_exactly_including_surrounding_spaces(self):
        for groups in (["PROXY "], ["PROXY", "PROXY "]):
            with self.subTest(groups=groups):
                if self.dialog.closed:
                    self.dialog = dashboard.ClientDialog(self.owner, parent=self.parent, visible=False)
                    self.owner.client_dialog = self.dialog
                self.dialog.save_button = Mock()
                self.configure.reset_mock()
                self.inspect.return_value = self.inspection | {"groups": groups, "active_group": None}
                self.dialog.inspect_connection()
                self.finish()
                self.dialog.group.set("PROXY ")
                self.dialog.save_button.state.assert_called_with(["!disabled"])
                saved = dashboard.core.DEFAULTS | {"group": "PROXY ", "enabled": False}
                self.configure.return_value = saved
                original = self.dialog.original_context

                self.dialog.save()
                self.assertTrue(self.dialog.applying)
                self.finish()

                self.configure.assert_called_once_with(
                    {"controller": dashboard.core.DEFAULTS["controller"], "controller_secret_env": "", "group": "PROXY ",
                     "auto_detect_client": True, "auto_select_group": True, "controller_api_mode": "standard"},
                    expected_context=original,
                )
                self.assertFalse(self.dialog.closed)
                self.assertFalse(self.owner.client_busy)

    def test_failed_inspection_or_save_releases_busy_and_allows_retry(self):
        self.inspect.side_effect = dashboard.core.ControllerError("无法认证")
        self.dialog.inspect_connection()
        self.finish()
        self.assertFalse(self.dialog.loading)
        self.assertIsNone(self.dialog.verified_identity)
        self.assertIn("无法认证", self.dialog.message.get())
        self.inspect.side_effect = None
        self.dialog.inspect_connection()
        self.finish()
        for error in (ValueError("原连接已改变，请重新打开"), RuntimeError("后台尚未停止")):
            with self.subTest(error=type(error).__name__):
                self.configure.side_effect = error
                self.dialog.save()
                self.finish()
                self.assertFalse(self.owner.client_busy)
                self.assertFalse(self.dialog.applying)
                self.assertFalse(self.dialog.closed)
                self.assertIn("未保存", self.dialog.message.get())
                self.owner.client_saved.assert_not_called()
        self.configure.reset_mock()
        self.wait_for_startup.return_value = False
        self.dialog.save()
        self.finish()
        self.configure.assert_not_called()
        self.assertFalse(self.owner.client_busy)
        self.assertFalse(self.dialog.applying)
        self.assertFalse(self.dialog.closed)
        self.owner.client_saved.assert_not_called()
        self.dialog.discard_changes()
        self.assertTrue(self.dialog.closed)

    def test_closed_dialog_ignores_late_inspection_without_touching_window(self):
        self.dialog.inspect_connection()
        self.dialog.dispose()
        self.dialog.window.reset_mock()
        self.finish()
        self.assertTrue(self.dialog.closed)
        self.assertEqual(self.dialog.window.mock_calls, [])
        self.owner.client_saved.assert_not_called()

    def candidate(self, address, **fields):
        return self.inspection | {"controller": address, "verified": True, "needs_auth": False,
                                  "recommended_group": "PROXY", "group_reason": "优先保留有效代理组。",
                                  "nearby_regions": ["JP", "TW"]} | fields

    def test_inline_panel_has_no_toplevel_and_activation_starts_async_discovery(self):
        self.toplevel.assert_not_called()
        self.dialog.window.pack.assert_any_call(fill="both", expand=True)
        self.discovery.assert_not_called()
        self.assertEqual(self.workers, [])
        self.dialog.discard_changes()
        self.owner.show_tab.assert_called_once_with("overview")
        self.dialog = dashboard.ClientDialog(self.owner, parent=self.parent)
        self.owner.client_dialog = self.dialog
        self.assertTrue(self.dialog.loading)
        self.discovery.assert_not_called()
        self.assertEqual(len(self.workers), 1)

    def test_raw_draft_detection_and_dispose_do_not_validate_or_navigate(self):
        self.assertFalse(self.dialog.has_unsaved_changes())
        self.dialog.controller.set("unfinished address")
        self.assertTrue(self.dialog.has_unsaved_changes())
        self.dialog.controller.set(dashboard.core.DEFAULTS["controller"])
        self.assertFalse(self.dialog.has_unsaved_changes())
        self.dialog.auto_detect.set(False)
        self.assertTrue(self.dialog.has_unsaved_changes())
        self.dialog.dispose()
        self.owner.show_tab.assert_not_called()
        self.assertTrue(self.dialog.closed)
        self.assertIsNone(self.dialog.after_id)

    def test_discovery_preserves_current_verified_client_and_existing_group(self):
        address = self.dialog.fields()["controller"]
        self.dialog.group.set("GLOBAL")
        self.discovery.return_value = {"candidates": [self.candidate(address), self.candidate("http://127.0.0.1:9090")],
                                       "selected_controller": address, "message": "保留当前客户端。"}
        self.dialog.discover_connections()
        self.discovery.assert_not_called()
        self.finish()
        self.assertEqual(self.dialog.fields()["controller"], address)
        self.assertEqual(self.dialog.group.get(), "GLOBAL")
        self.assertEqual(self.dialog.verified_identity, (address, ""))
        self.assertIn("JP、TW", self.dialog.message.get())
        self.configure.assert_not_called()
        self.inspect.assert_not_called()

    def test_multiple_clients_require_choice_and_never_copy_secret_to_another_client(self):
        original = self.dialog.fields()["controller"]
        self.dialog.secret_env.set("ORIGINAL_SECRET")
        address = "http://127.0.0.1:9090"
        self.discovery.return_value = {"candidates": [self.candidate(address), self.candidate("http://127.0.0.1:9097")],
                                       "selected_controller": None, "message": "发现多个客户端，请选择。"}
        self.dialog.discover_connections()
        self.finish()
        self.assertEqual(self.dialog.fields()["controller"], original)
        self.assertIsNone(self.dialog.verified_identity)
        self.dialog.save()
        self.assertEqual(self.workers, [])
        self.dialog.controller.set(address)
        self.dialog.select_discovered_client()
        self.assertEqual(self.dialog.secret_env.get(), "")
        self.assertEqual(self.dialog.verified_identity, (address, ""))
        self.assertEqual(self.dialog.group.get(), "PROXY")
        self.configure.assert_not_called()
        self.inspect.assert_not_called()

    def test_auth_required_candidate_cannot_be_saved_until_explicit_inspection(self):
        address = "http://127.0.0.1:9090"
        self.discovery.return_value = {"candidates": [self.candidate(address, verified=False, needs_auth=True)],
                                       "selected_controller": None, "message": "控制地址需要认证。"}
        self.dialog.discover_connections()
        self.finish()
        self.dialog.controller.set(address)
        self.dialog.select_discovered_client()
        self.assertIsNone(self.dialog.verified_identity)
        self.assertIn("需要认证", self.dialog.message.get())
        self.dialog.save()
        self.assertEqual(self.workers, [])
        self.dialog.secret_env.set("OTHER_CLIENT_SECRET")
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.verified_identity, (address, "OTHER_CLIENT_SECRET"))

    def test_selecting_standard_discovered_client_resets_compatible_draft_to_verified_contract(self):
        self.dialog.compatible.set(True)
        original = self.dialog.controller.get()
        address = "http://127.0.0.1:9090"
        candidate = self.candidate(address, controller_api_mode="standard")
        self.discovery.return_value = {"candidates": [candidate], "selected_controller": None,
                                       "message": "请选择客户端。"}
        self.dialog.discover_connections()
        self.finish()
        self.assertEqual(self.dialog.controller.get(), original)
        self.assertTrue(self.dialog.compatible.get())
        self.dialog.controller.set(address)
        self.dialog.select_discovered_client()
        self.assertFalse(self.dialog.compatible.get())
        self.assertEqual(self.dialog.fields()["controller_api_mode"], "standard")
        self.assertEqual(self.dialog.verified_identity, (address, ""))
        self.assertIn("规则模式", self.dialog.message.get())
        self.assertNotIn("代理组兼容模式", self.dialog.message.get())
        self.inspect.assert_not_called()
        self.configure.assert_not_called()

    def test_unsupported_config_read_is_explained_and_cannot_be_saved(self):
        # The generic ttk fixture shares a Button mock; this control needs an
        # independent identity so other buttons cannot overwrite its state call.
        self.dialog.save_button = Mock()
        address = self.dialog.controller.get()
        candidate = self.candidate(address, verified=False, needs_auth=False) | {"unsupported": True}
        self.discovery.return_value = {"candidates": [candidate], "selected_controller": None,
                                       "message": str(dashboard.core.ConfigReadUnsupported())}
        self.dialog.discover_connections()
        self.finish()
        self.dialog.controller.set(address)
        self.dialog.select_discovered_client()
        self.assertIsNone(self.dialog.verified_identity)
        self.assertIn("GET /configs", self.dialog.message.get())
        self.assertIn("HTTP 405", self.dialog.message.get())
        self.assertNotIn("需要认证", self.dialog.message.get())
        self.assertEqual(self.dialog.selector_groups, [])
        self.dialog.save_button.state.assert_called_with(["disabled"])
        self.dialog.save()
        self.configure.assert_not_called()
        self.assertEqual(self.workers, [])

    def test_discovery_discards_result_after_address_edit_and_error_allows_retry(self):
        address = self.dialog.fields()["controller"]
        self.discovery.return_value = {"candidates": [self.candidate(address)], "selected_controller": address, "message": "已发现。"}
        self.dialog.discover_connections()
        self.dialog.controller.set("http://127.0.0.1:9097")
        self.finish()
        self.assertIsNone(self.dialog.verified_identity)
        self.assertEqual(self.dialog.discovered_clients, {})
        self.discovery.side_effect = dashboard.core.ControllerError("发现失败")
        self.dialog.discover_connections()
        self.finish()
        self.assertFalse(self.dialog.loading)
        self.assertIn("发现失败", self.dialog.message.get())

    def test_recommendation_fills_missing_group_and_manual_automatic_preferences_are_saved(self):
        self.inspect.return_value = self.inspection | {"recommended_group": "GLOBAL", "group_reason": "推荐包含邻近节点的组。", "nearby_regions": []}
        self.dialog.group.set("missing")
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.group.get(), "GLOBAL")
        self.assertIn("不会扩大", self.dialog.message.get())
        self.dialog.auto_detect.set(False)
        self.dialog.auto_group.set(False)
        self.configure.return_value = dashboard.core.DEFAULTS | self.dialog.fields()
        self.dialog.save()
        self.finish()
        saved = self.configure.call_args.args[0]
        self.assertFalse(saved["auto_detect_client"])
        self.assertFalse(saved["auto_select_group"])

    def test_disabled_group_detection_keeps_missing_group_for_explicit_choice(self):
        self.dialog.auto_group.set(False)
        self.dialog.group.set("missing")
        self.inspect.return_value = self.inspection | {"active_group": None, "recommended_group": "PROXY"}
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.group.get(), "")
        self.dialog.save()
        self.configure.assert_not_called()
        self.assertEqual(self.workers, [])

    def test_discovery_can_retry_when_no_group_was_selected(self):
        self.dialog.group.set("")
        self.discovery.return_value = {"candidates": [], "selected_controller": None, "message": "尚未发现客户端。"}
        self.dialog.discover_connections()
        self.finish()
        self.assertEqual(self.discovery.call_args.args[0]["group"], self.dialog.original_context[2])
        self.assertEqual(self.dialog.group.get(), "")
        self.assertFalse(self.dialog.loading)

    def test_compatible_mode_requires_opt_in_and_discards_old_standard_inspection(self):
        self.assertFalse(self.dialog.compatible.get())
        self.assertFalse(self.dialog.has_unsaved_changes())
        self.dialog.inspect_connection()
        self.dialog.compatible.set(True)
        self.assertTrue(self.dialog.has_unsaved_changes())
        self.finish()
        self.assertIsNone(self.dialog.verified_identity)
        self.dialog.save()
        self.configure.assert_not_called()

        self.inspect.return_value = self.inspection | {
            "mode": "unknown", "proxy_port": None,
            "controller_api_mode": "selector", "verification_scope": "selector",
        }
        self.dialog.inspect_connection()
        self.finish()
        self.assertEqual(self.dialog.verified_identity, (self.dialog.controller.get(), "", "selector"))
        self.assertEqual(self.inspect.call_args.args[0]["controller_api_mode"], "selector")
        self.assertIn("当前模式未知", self.dialog.message.get())
        self.assertIn("未验证 Codex 实际出站路径", self.dialog.message.get())
        self.assertNotIn("规则模式", self.dialog.message.get())
        self.assertNotIn("端口 None", self.dialog.message.get())
        self.configure.return_value = dashboard.core.DEFAULTS | self.dialog.fields()
        self.dialog.save()
        self.finish()
        self.assertEqual(self.configure.call_args.args[0]["controller_api_mode"], "selector")
        self.assertFalse(self.dialog.has_unsaved_changes())
        self.dialog.compatible.set(False)
        self.assertTrue(self.dialog.has_unsaved_changes())
        self.assertIsNone(self.dialog.verified_identity)

    def test_auth_import_is_explicit_async_and_requires_test_and_save(self):
        self.import_auth.assert_not_called()
        self.import_auth.return_value = "NEARROUTE_MAOMAO_SECRET_9790"
        before = self.dialog.secret_env.get()
        self.dialog.import_auth()
        self.assertTrue(self.dialog.applying)
        self.assertTrue(self.dialog.importing)
        self.assertTrue(self.owner.client_busy)
        self.import_auth.assert_not_called()
        self.dialog.close()
        self.dialog.discard_changes()
        self.assertFalse(self.dialog.closed)
        self.workers.pop(0)()
        self.assertEqual(self.dialog.secret_env.get(), before)
        self.dialog.poll_results()
        self.import_auth.assert_called_once_with(self.dialog.controller.get())
        self.assertEqual(self.dialog.secret_env.get(), "NEARROUTE_MAOMAO_SECRET_9790")
        self.assertFalse(self.dialog.applying)
        self.assertFalse(self.dialog.importing)
        self.assertFalse(self.owner.client_busy)
        self.assertIsNone(self.dialog.verified_identity)
        self.assertIn("请测试连接", self.dialog.message.get())
        self.inspect.assert_not_called()
        self.configure.assert_not_called()
        self.start_daemon.assert_not_called()
        self.dialog.save()
        self.configure.assert_not_called()

    def test_cancelled_auth_import_preserves_fields_and_does_not_retry(self):
        self.dialog.secret_env.set("KEEP_EXISTING_SECRET")
        before = self.dialog.fields()
        self.import_auth.side_effect = RuntimeError("已取消管理员确认")
        self.dialog.import_auth()
        self.finish()
        self.assertEqual(self.dialog.fields(), before)
        self.assertFalse(self.dialog.applying)
        self.assertFalse(self.owner.client_busy)
        self.assertIn("已取消管理员确认", self.dialog.message.get())
        self.dialog.poll_results()
        self.import_auth.assert_called_once()
        self.configure.assert_not_called()

    def test_auth_result_does_not_overwrite_another_connection_draft(self):
        self.import_auth.return_value = "NEARROUTE_MAOMAO_SECRET_9790"
        self.dialog.import_auth()
        self.dialog.controller.set("http://127.0.0.1:9090")
        self.dialog.secret_env.set("OTHER_CLIENT_SECRET")
        self.finish()
        self.assertEqual(self.dialog.secret_env.get(), "OTHER_CLIENT_SECRET")
        self.assertIn("本次认证未应用", self.dialog.message.get())
        self.configure.assert_not_called()


class ClientDialogLifecycleTests(unittest.TestCase):
    def test_discovery_controls_and_connection_message_fit_inline_panel(self):
        try:
            root = tk.Tk()
            root.withdraw()
            root.attributes("-alpha", 0)
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        dialog = None
        try:
            dashboard.apply_theme(root)
            with tempfile.TemporaryDirectory(prefix="nearroute-client-layout-") as directory:
                config = Path(directory) / "config.json"
                config.write_text(json.dumps({"enabled": False}), encoding="utf-8")
                owner = SimpleNamespace(root=root, client_dialog=None, show_tab=Mock())
                parent = dashboard.ttk.Frame(root)
                parent.pack(fill="both", expand=True)
                with patch.object(dashboard, "CONFIG", config):
                    dialog = dashboard.ClientDialog(owner, parent=parent, visible=False)
                owner.client_dialog = dialog
                dialog.message.set("连接成功，HTTP 代理端口 7890，读取 8 个代理组。规则模式使用所选代理组。优先保留有效代理组。邻近地区：JP、TW、SG、HK、PH。")
                root.geometry("920x580+0+0")
                root.deiconify()
                root.update()
                self.assertIsInstance(dialog.window, dashboard.ttk.Frame)
                self.assertIs(dialog.window.winfo_toplevel(), root)
                for widget in (dialog.address_entry, dialog.discover_button, dialog.secret_entry,
                               dialog.auto_detect_button, dialog.groups, dialog.inspect_button,
                               dialog.auto_group_button, dialog.save_button, dialog.cancel_button, dialog.discard_button):
                    self.assertTrue(widget.winfo_ismapped(), str(widget))
                    x = widget.winfo_rootx() - dialog.window.winfo_rootx()
                    y = widget.winfo_rooty() - dialog.window.winfo_rooty()
                    self.assertGreaterEqual(x, 0, str(widget))
                    self.assertGreaterEqual(y, 0, str(widget))
                    self.assertLessEqual(x + widget.winfo_width(), dialog.window.winfo_width(), str(widget))
                    self.assertLessEqual(y + widget.winfo_height(), dialog.window.winfo_height(), str(widget))
        finally:
            if dialog is not None:
                dialog.dispose()
            root.destroy()

    def test_back_retains_draft_and_discard_releases_traces_after_apply_finishes(self):
        try:
            root = tk.Tk()
            root.withdraw()
        except tk.TclError as error:
            self.skipTest(f"Tk display unavailable: {error}")
        try:
            with tempfile.TemporaryDirectory(prefix="nearroute-client-lifecycle-") as directory:
                config = Path(directory) / "config.json"
                config.write_text(json.dumps({"enabled": False}), encoding="utf-8")
                owner = SimpleNamespace(root=root, client_dialog=None, show_tab=Mock())
                parent = dashboard.ttk.Frame(root)
                parent.pack(fill="both", expand=True)
                with patch.object(dashboard, "CONFIG", config):
                    # Real widgets verify trace and scheduled callback cleanup.
                    dialog = dashboard.ClientDialog(owner, parent=parent, visible=False)
                owner.client_dialog = dialog
                variables = (dialog.controller, dialog.secret_env, dialog.group)
                reference = weakref.ref(dialog)
                self.assertTrue(all(len(variable.trace_info()) == 1 for variable in variables))
                dialog.applying = True
                dialog.close()
                dialog.discard_changes()
                self.assertFalse(dialog.closed)
                self.assertTrue(dialog.window.winfo_exists())
                self.assertTrue(all(len(variable.trace_info()) == 1 for variable in variables))
                dialog.applying = False
                dialog.controller.set("unfinished address")
                dialog.close()
                self.assertFalse(dialog.closed)
                self.assertIs(owner.client_dialog, dialog)
                self.assertEqual(dialog.controller.get(), "unfinished address")
                self.assertTrue(dialog.has_unsaved_changes())
                owner.show_tab.assert_called_once_with("overview")
                dialog.discard_changes()
                self.assertTrue(dialog.closed)
                self.assertTrue(all(not variable.trace_info() for variable in variables))
                self.assertIsNone(owner.client_dialog)
                del dialog
                gc.collect()
                self.assertIsNone(reference())
        finally:
            root.destroy()


class ClientContextTests(unittest.TestCase):
    def setUp(self):
        self.context = dashboard.core.state_context(dashboard.core.DEFAULTS)
        self.app = dashboard.Dashboard.__new__(dashboard.Dashboard)
        self.app.root = Mock()
        self.app.closed = self.app.exiting = self.app.restore_busy = self.app.manual_busy = self.app.client_busy = False
        self.app.update_busy = None
        self.app.settings_dialog = self.app.client_dialog = None
        self.app.results = queue.SimpleQueue()
        self.app.manual_results = queue.SimpleQueue()
        self.app.node_context = self.context
        self.app.node_mode = ("rule", self.context[2])
        self.app.manual_attempt = self.app.manual_request_id = None
        self.app.manual_request_name = ""
        self.app.manual_request_started = self.app.started_at = self.app.notice_until = 0
        self.app.node_signature = "old nodes"
        for field in ("current", "health", "policy", "timings", "message", "badge", "badge_label", "runtime_text", "runtime_label",
                      "node_title", "node_meta", "connection_value", "node_count_value", "error_value",
                      "scan_time_value", "switch_time_value", "next_scan_value", "update_time_value",
                      "enable_button", "pause_button", "scan_button", "settings_button", "restore_button", "exit_button", "client_button"):
            setattr(self.app, field, Mock())
        self.app.notify = Mock()
        self.app.render_nodes = Mock()
        self.app.poll_updates = Mock()
        self.app.poll_manual_selection = Mock()
        self.app.ensure_service = Mock(return_value=True)
        self.app.show_window = Mock()
        self.app.started_helpers = []

    def test_old_or_missing_status_context_clears_nodes_and_cannot_queue_old_selection(self):
        cfg = dict(zip(("controller", "controller_secret_env", "group"), self.context))
        old_status = {"controller_context": ["http://127.0.0.1:9090", "", "PROXY"],
                      "selected": "old node", "nodes": [{"name": "old node"}],
                      "phase": "monitoring", "controller_available": True,
                      "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat()}
        with patch.object(dashboard, "read_json", side_effect=lambda path: old_status if path == dashboard.STATUS else cfg), \
             patch.object(dashboard.core, "request_manual_selection") as request:
            for status in (old_status, {key: value for key, value in old_status.items() if key != "controller_context"}):
                old_status = status
                self.app.refresh()
                self.assertIsNone(self.app.node_context)
                self.assertIsNone(self.app.node_mode)
                self.app.render_nodes.assert_called_with("—", [])
                self.app.poll_manual_selection.assert_called_with({})
                self.app.poll_updates.assert_called_with({})
                self.app.switch_node("old node")
            request.assert_not_called()
            self.app.ensure_service.assert_not_called()
            old_status = old_status | {"controller_context": list(self.context), "mode": "global", "group": "GLOBAL", "selected": "new node", "nodes": [{"name": "new node"}],
                                      "nodes_probe_identity": dashboard.core.probe_identity(dashboard.core.DEFAULTS)}
            self.app.refresh()
            self.assertEqual(self.app.node_context, self.context)
            self.assertEqual(self.app.node_mode, ("global", "GLOBAL"))
            self.app.render_nodes.assert_called_with("new node", [{"name": "new node"}])

    def test_manual_worker_keeps_context_and_mode_of_its_visible_node_list(self):
        work = []
        with patch.object(dashboard.threading, "Thread", side_effect=lambda **kwargs: work.append(kwargs["target"]) or Mock()), \
             patch.object(dashboard.core, "request_manual_selection", return_value="request") as request:
            self.app.switch_node("visible node")
            self.app.node_context = ("http://127.0.0.1:9090", "", "OTHER")
            self.app.node_mode = ("global", "GLOBAL")
            work.pop()()
        request.assert_called_once_with("visible node", expected_context=self.context, expected_mode=("rule", self.context[2]))

    def test_starting_keeps_cached_rows_untrusted_until_first_client_snapshot(self):
        cfg = dict(zip(("controller", "controller_secret_env", "group"), self.context)) | {"enabled": False}
        nodes = [{"name": "old node", "eligible": True}]
        status = {"controller_context": list(self.context), "selected": "old node", "nodes": nodes,
                  "nodes_probe_identity": dashboard.core.probe_identity(dashboard.core.DEFAULTS),
                  "phase": "starting", "pid": 12345, "controller_available": True,
                  "mode": "rule", "group": self.context[2],
                  "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat()}
        work = []
        with patch.object(dashboard, "read_json", side_effect=lambda path: status if path == dashboard.STATUS else cfg), \
             patch.object(dashboard.threading, "Thread", side_effect=lambda **kwargs: work.append(kwargs["target"]) or Mock()), \
             patch.object(dashboard.core, "request_manual_selection", return_value="request") as request:
            self.app.refresh()
            self.assertIn("上次记录节点：old node", self.app.current.set.call_args.args[0])
            self.assertIn("控制接口：状态待更新", self.app.health.set.call_args.args[0])
            self.app.render_nodes.assert_called_with("old node", nodes)
            self.assertIsNone(self.app.node_mode)
            self.app.switch_node("old node")
            self.assertEqual(work, [])
            request.assert_not_called()
            self.app.ensure_service.assert_not_called()

            for phase in ("paused", "waiting"):
                with self.subTest(phase=phase):
                    status.update(phase=phase, controller_available=False)
                    self.app.refresh()
                    self.assertIn("上次记录节点：old node", self.app.current.set.call_args.args[0])
                    self.app.render_nodes.assert_called_with("old node", nodes)
                    self.assertIsNone(self.app.node_mode)
                    self.app.switch_node("old node")
                    self.assertEqual(work, [])
                    request.assert_not_called()
                    self.app.ensure_service.assert_not_called()

            status.update(phase="paused", controller_available=True, mode="global", group="GLOBAL", selected="fresh node")
            self.app.refresh()
            self.assertIn("当前节点：fresh node", self.app.current.set.call_args.args[0])
            self.assertIn("控制接口：已连接", self.app.health.set.call_args.args[0])
            self.assertEqual(self.app.node_mode, ("global", "GLOBAL"))
            self.app.switch_node("fresh node")
            work.pop()()
            request.assert_called_once_with("fresh node", expected_context=self.context, expected_mode=("global", "GLOBAL"))

    def test_status_without_actual_selector_mode_cannot_submit_visible_node(self):
        cfg = dict(zip(("controller", "controller_secret_env", "group"), self.context))
        status = {"controller_context": list(self.context), "selected": "visible node", "nodes": [{"name": "visible node"}],
                  "nodes_probe_identity": dashboard.core.probe_identity(dashboard.core.DEFAULTS),
                  "phase": "monitoring", "controller_available": True,
                  "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat()}
        with patch.object(dashboard, "read_json", side_effect=lambda path: status if path == dashboard.STATUS else cfg), \
             patch.object(dashboard.core, "request_manual_selection") as request:
            for fields in ({"mode": None, "group": None}, {"mode": "rule"}, {"mode": "direct", "group": None}):
                status = {key: value for key, value in status.items() if key not in ("mode", "group")} | fields
                self.app.refresh()
                self.assertIsNone(self.app.node_mode)
                self.app.switch_node("visible node")
            request.assert_not_called()
            self.app.ensure_service.assert_not_called()

    def test_selector_scope_labels_unknown_route_and_allows_group_selection_only_with_marker(self):
        cfg = dashboard.core.DEFAULTS | {"controller_api_mode": "selector"}
        context = dashboard.core.state_context(cfg)
        status = {"controller_context": list(context), "selected": "visible node", "nodes": [{"name": "visible node"}],
                  "nodes_probe_identity": dashboard.core.probe_identity(cfg), "mode": "unknown", "group": cfg["group"],
                  "verification_scope": "selector", "phase": "monitoring", "controller_available": True,
                  "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat()}
        with patch.object(dashboard, "read_json", side_effect=lambda path: status if path == dashboard.STATUS else cfg):
            self.app.refresh()
            self.assertEqual(self.app.node_mode, ("unknown", cfg["group"]))
            self.assertIn("组内选择", self.app.current.set.call_args.args[0])
            self.assertIn("Codex 实际路径未验证", self.app.node_meta.set.call_args.args[0])
            self.assertIn("不核对 HTTP 状态码", self.app.policy.set.call_args.args[0])
            status.pop("verification_scope")
            self.app.refresh()
            self.assertIsNone(self.app.node_mode)

    def test_client_save_clears_old_nodes_and_leaves_background_stopped(self):
        cfg = dashboard.core.DEFAULTS | {"controller": "http://127.0.0.1:9090", "group": "PROXY", "enabled": False}
        with patch.object(dashboard, "start_daemon", side_effect=AssertionError("must stay stopped")):
            self.app.client_saved(cfg)
        self.assertIsNone(self.app.node_context)
        self.assertIsNone(self.app.node_mode)
        self.app.render_nodes.assert_called_once_with("—", [])
        self.app.node_title.set.assert_called_with("等待客户端状态")
        self.app.connection_value.set.assert_called_with("等待检测")
        self.app.node_count_value.set.assert_called_with("—")
        self.app.ensure_service.assert_not_called()
        self.assertIn("立即测速", self.app.notify.call_args.args[0])

    def test_saving_only_automatic_preferences_preserves_live_nodes_and_selection(self):
        self.app.manual_request_id = "pending-current-client-request"
        mode = self.app.node_mode
        for enabled in (False, True):
            with self.subTest(enabled=enabled):
                cfg = dashboard.core.DEFAULTS | {"enabled": enabled, "auto_detect_client": False, "auto_select_group": False}
                self.app.client_saved(cfg)
                self.assertEqual(self.app.node_context, self.context)
                self.assertEqual(self.app.node_mode, mode)
                self.assertEqual(self.app.node_signature, "old nodes")
                self.assertEqual(self.app.manual_request_id, "pending-current-client-request")
                self.app.render_nodes.assert_not_called()
                self.app.ensure_service.assert_not_called()
                self.app.policy.set.assert_called_with(dashboard.policy_summary(cfg))
                self.assertIn("当前节点和优选开关保持", self.app.notify.call_args.args[0])

    def test_client_save_busy_blocks_node_actions_and_auto_install(self):
        self.app.client_busy = True
        with patch.object(dashboard, "update_enabled") as enabled, \
             patch.object(dashboard, "start_daemon") as daemon, \
             patch.object(dashboard.core, "request_manual_selection") as request:
            self.assertFalse(dashboard.Dashboard.ensure_service(self.app))
            self.app.enable()
            self.app.pause()
            self.app.scan()
            self.app.restore()
            self.app.request_exit()
            self.app.switch_node("old node")
            self.assertFalse(self.app.automatic_install_ready({"phase": "monitoring", "updated_at": datetime.now(dashboard.CHINA_TIME).isoformat()}))
        enabled.assert_not_called()
        daemon.assert_not_called()
        request.assert_not_called()
        self.app.ensure_service.assert_not_called()


if __name__ == "__main__":
    unittest.main()
