"""Offline public transport workflow fixtures; never access the user's Codex home."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import codex_transport as transport


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.home = self.root / "codex-home"
        self.home.mkdir()
        self.config = self.home / "config.toml"
        self.original = (b'\xef\xbb\xbf# retain user comment\r\nmodel = "gpt-6-astra"\r\n'
                         b'model_reasoning_effort = "ultra"\r\nmodel_context_window = 900000\r\n'
                         b'model_auto_compact_token_limit = 800000\r\n'
                         b'[features]\r\nenable_request_compression = true\r\n'
                         b'[mcp_servers.example]\r\ncommand = "example"\r\n')
        self.config.write_bytes(self.original)
        self.engine = self.root / "codex.exe"
        self.engine.write_bytes(b"fixture engine is never executed")
        self.running = []
        self.process_check = lambda: list(self.running)
        self.capabilities = Mock(return_value={"version": "codex-cli 0.166.0-alpha.5.1", "metadata_resume_supported": True})
        self.startup = Mock(return_value={"startup_validated": True})
        self.migration_calls = []
        for name, value in (("discover_desktop", transport._DESKTOP_APP_ID), ("open_desktop", None)):
            patcher = patch.object(transport, name, return_value=value)
            setattr(self, name, patcher.start())
            self.addCleanup(patcher.stop)
        self.manager = self.new_manager()

    def new_manager(self):
        return transport.TransportManager(root=self.root, home=self.home, engine=self.engine,
                                          process_check=self.process_check, migrate=self.migrate,
                                          capability_check=self.capabilities, startup_check=self.startup)

    def create_threads(self, rows):
        with self.database() as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY,model_provider TEXT,model TEXT,reasoning_effort TEXT,archived INTEGER)")
            db.executemany("INSERT INTO threads VALUES(?,?,?,?,?)", rows)

    @contextmanager
    def database(self):
        db = sqlite3.connect(self.home / "state_5.sqlite")
        try:
            with db:
                yield db
        finally:
            db.close()

    def migrate(self, engine, home, thread_ids, provider_id, *, original=None):
        self.migration_calls.append((list(thread_ids), provider_id, original))
        with self.database() as db:
            for thread_id in thread_ids:
                db.execute("UPDATE threads SET model_provider=? WHERE id=?", (provider_id or original[thread_id], thread_id))
        return {"ok": True, "changed": True, "target_count": len(thread_ids), "backup_directory": "fixture backups"}

    def apply_http(self):
        self.manager.request_mode("http")
        result = self.manager.process_pending()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "applied")
        return result

    def parsed(self):
        return transport.tomllib.loads(self.config.read_bytes().decode("utf-8-sig"))

    def test_request_persists_across_restart_and_waits_for_normal_exit(self):
        self.running.append({"ProcessId": 1234, "Name": "Codex.exe"})
        result = self.manager.request_mode("http")
        self.assertEqual(result["pending"], "http")
        self.assertEqual(self.config.read_bytes(), self.original)
        restarted = self.new_manager()
        self.assertEqual(restarted.process_pending()["status"], "pending")
        self.capabilities.assert_not_called()
        self.startup.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.running.clear()
        result = restarted.process_pending()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["pending"], None)
        self.assertEqual(self.parsed()["model_provider"], transport.PROVIDER_ID)

    def test_user_click_reopen_intent_waits_for_exit_then_opens_once(self):
        self.running.append({"ProcessId": 1234, "Name": "ChatGPT.exe"})
        result = self.manager.request_mode("http", reopen=True)
        self.assertTrue(result["reopen_requested"])
        self.assertIn("尚未生效", result["explanation"])
        self.assertEqual(self.manager._load("pending")["reopen_app_id"], transport._DESKTOP_APP_ID)
        restarted = self.new_manager()
        self.assertEqual(restarted.process_pending()["status"], "pending")
        self.open_desktop.assert_not_called()
        self.assertEqual(self.config.read_bytes(), self.original)
        self.running.clear()
        result = restarted.process_pending()
        self.assertTrue(result["ok"])
        self.assertTrue(result["reopened"])
        self.assertIsNone(result["pending"])
        self.open_desktop.assert_called_once_with(transport._DESKTOP_APP_ID)
        self.assertIn("已请求自动打开", self.new_manager().inspect()["explanation"])
        restarted.process_pending()
        self.open_desktop.assert_called_once()
        self.assertEqual(self.capabilities.call_count, 1)

    def test_no_user_click_or_old_pending_never_opens_desktop(self):
        self.manager.inspect()
        self.manager.process_pending()
        self.assertFalse((self.manager.state / "pending.json").exists())
        self.assertEqual(self.config.read_bytes(), self.original)
        self.manager.request_mode("http")
        pending = self.manager._load("pending")
        pending.pop("reopen")  # Intent saved by an earlier release.
        self.manager._save("pending", pending)
        self.assertTrue(self.new_manager().process_pending()["ok"])
        self.discover_desktop.assert_not_called()
        self.open_desktop.assert_not_called()

    def test_launch_failure_keeps_completed_config_and_survives_restart(self):
        self.open_desktop.side_effect = OSError("private launcher failure detail")
        self.manager.request_mode("http", reopen=True)
        result = self.manager.process_pending()
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "applied")
        self.assertEqual(self.parsed()["model_provider"], transport.PROVIDER_ID)
        self.assertIsNone(result["pending"])
        self.assertFalse(result["reopened"])
        self.assertIn("手动打开", result["reopen_error"])
        self.assertNotIn("private", result["reopen_error"])
        restarted = self.new_manager()
        self.assertEqual(restarted.inspect()["reopen_error"], result["reopen_error"])
        self.assertIn("手动打开", restarted.process_pending()["explanation"])
        self.assertEqual(self.capabilities.call_count, 1)
        self.open_desktop.assert_called_once()

    def test_cancelled_reopen_and_failed_switch_never_launch(self):
        self.manager.request_mode("http", reopen=True)
        self.manager.cancel_pending()
        self.manager.process_pending()
        self.open_desktop.assert_not_called()
        self.manager.request_mode("http", reopen=True)
        self.capabilities.side_effect = transport.Refused("当前引擎接口不可用。")
        self.assertEqual(self.manager.process_pending()["status"], "error")
        self.manager.process_pending()
        self.open_desktop.assert_not_called()

    def test_unavailable_launcher_preserves_an_existing_pending_request(self):
        self.manager.request_mode("http")
        before = (self.manager.state / "pending.json").read_bytes()
        self.discover_desktop.side_effect = transport.Refused("未找到桌面应用；可取消自动打开后继续。")
        result = self.manager.request_mode("http", reopen=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["status"], "pending")
        self.assertIn("取消自动打开", result["request_error"])
        self.assertEqual((self.manager.state / "pending.json").read_bytes(), before)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_malformed_persisted_launch_id_refuses_before_config_write(self):
        self.manager.request_mode("http", reopen=True)
        pending = self.manager._load("pending")
        pending["reopen_app_id"] = "OpenAI.Codex_2p2nqsd0c76g0!App & unwanted"
        self.manager._save("pending", pending)
        result = self.manager.process_pending()
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.config.read_bytes(), self.original)
        self.capabilities.assert_not_called()
        self.open_desktop.assert_not_called()

    def test_user_reopened_after_completed_migration_skips_duplicate_launch(self):
        self.create_threads([("target", "openai", "gpt-6-sol", "high", 0)])
        def migrate_then_reopen(*args, **kwargs):
            result = self.migrate(*args, **kwargs)
            self.running.append({"ProcessId": 4321, "Name": "ChatGPT.exe"})
            return result
        self.manager.migrate = migrate_then_reopen
        self.manager.request_mode("http", reopen=True)
        result = self.manager.process_pending()
        self.assertTrue(result["ok"])
        self.assertTrue(result["reopen_skipped"])
        self.assertIsNone(result["pending"])
        self.open_desktop.assert_not_called()

    def test_http_preserves_every_other_setting_and_backups_original(self):
        before = self.parsed()
        result = self.apply_http()
        after = self.parsed()
        after.pop("model_provider")
        provider = after.pop("model_providers")[transport.PROVIDER_ID]
        self.assertEqual(after, before)
        self.assertEqual(provider, transport.PROVIDER)
        self.assertNotIn("base_url", provider)
        self.assertTrue(self.config.read_bytes().startswith(b"\xef\xbb\xbf"))
        self.assertTrue(result["can_restore"])
        backups = list((self.manager.state / "backups").glob("*-config.toml"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)
        self.assertNotIn("\n", self.config.read_bytes().decode("utf-8-sig").replace("\r\n", ""))

    def test_scope_uses_exact_saved_settings_and_skips_archived_and_unknown(self):
        self.create_threads([("target", "openai", "gpt-6-astra", "ultra", 0),
                             ("archive", "openai", "gpt-6-sol", "high", 1),
                             ("missing-model", "openai", None, "high", 0),
                             ("missing-effort", "openai", "gpt-6-sol", None, 0),
                             ("custom", "another", "gpt-6-sol", "high", 0)])
        before = self.manager.inspect()
        self.assertEqual(before["affected_count"], 1)
        self.assertEqual(before["skipped_count"], 2)
        self.apply_http()
        self.assertEqual(self.migration_calls, [(["target"], transport.PROVIDER_ID, None)])
        with self.database() as db:
            self.assertEqual(db.execute("SELECT model,reasoning_effort FROM threads WHERE id='target'").fetchone(), ("gpt-6-astra", "ultra"))
        ownership = self.manager._load("ownership")
        self.assertEqual(ownership["targets"], {"target": "openai"})

    def test_restore_owns_only_selected_fields_and_preserves_new_settings(self):
        self.create_threads([("target", "openai", "gpt-6-sol", "high", 0)])
        self.apply_http()
        current = self.config.read_bytes().replace(b'model = "gpt-6-astra"', b'model = "user-updated-model"')
        self.config.write_bytes(current)
        # Current saved per-chat settings can evolve after migration.
        with self.database() as db:
            db.execute("UPDATE threads SET model='user-model',reasoning_effort='max'")
            db.execute("INSERT INTO threads VALUES('new-chat','nearroute-http','gpt-6-astra','ultra',0)")
        self.manager.request_mode("original")
        result = self.manager.process_pending()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.config.read_bytes(), self.original.replace(b'model = "gpt-6-astra"', b'model = "user-updated-model"'))
        self.assertEqual(self.migration_calls[-1], (["target", "new-chat"], None, {"target": "openai", "new-chat": "openai"}))
        with self.database() as db:
            self.assertEqual(db.execute("SELECT model_provider,model,reasoning_effort FROM threads WHERE id='target'").fetchone(), ("openai", "user-model", "max"))
        self.assertFalse(result["can_restore"])

    def test_restore_retains_definition_for_archived_or_missing_saved_settings(self):
        self.create_threads([("target", "openai", "gpt-6-sol", "high", 0)])
        self.apply_http()
        with self.database() as db:
            db.execute("UPDATE threads SET archived=1")
            db.execute("INSERT INTO threads VALUES('empty','nearroute-http',NULL,NULL,0)")
        self.manager.request_mode("original")
        result = self.manager.process_pending()
        self.assertTrue(result["ok"], result)
        self.assertNotIn("model_provider", self.parsed())
        self.assertEqual(self.parsed()["model_providers"][transport.PROVIDER_ID], transport.PROVIDER)
        self.assertEqual(len(self.migration_calls), 1)

    def test_existing_http_is_read_only_until_explicit_request_then_restorable(self):
        original = b'model_provider = "openai-http" # existing\n[model_providers.openai-http]\nname="OpenAI"\nwire_api="responses"\nrequires_openai_auth=true\nsupports_websockets=false\nbase_url="https://chatgpt.com/backend-api/codex"\n'
        self.config.write_bytes(original)
        status = self.manager.inspect()
        self.assertTrue(status["compatible_http"])
        self.assertFalse(status["owned"])
        self.assertEqual(self.config.read_bytes(), original)
        self.manager.process_pending()
        self.assertEqual(self.config.read_bytes(), original)
        self.apply_http()
        self.manager.request_mode("original")
        result = self.manager.process_pending()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.config.read_bytes(), original)

    def test_profile_custom_provider_or_owned_name_conflict_refuse_without_write(self):
        for content in (b'model_provider="other"\n', b'profile="work"\n',
                        b'[profiles.work]\nmodel_provider="openai"\n',
                        b'[model_providers.nearroute-http]\nname="someone else"\n'):
            with self.subTest(content=content):
                self.config.write_bytes(content)
                status = self.manager.request_mode("http")
                self.assertFalse(status["ok"])
                self.assertFalse(status["can_request"])
                self.assertEqual(self.config.read_bytes(), content)
                self.assertFalse((self.manager.state / "pending.json").exists())

    def test_engine_capability_failure_never_writes_user_config_and_no_auto_retry(self):
        self.capabilities.side_effect = transport.Refused("版本缺少会话迁移接口。")
        self.manager.request_mode("http")
        result = self.manager.process_pending()
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.config.read_bytes(), self.original)
        self.manager.process_pending()
        self.assertEqual(self.capabilities.call_count, 1)
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_startup_failure_restores_config_and_retains_pending_error(self):
        self.startup.side_effect = transport.Refused("引擎拒绝目标配置。")
        self.manager.request_mode("http")
        result = self.manager.process_pending()
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.config.read_bytes(), self.original)
        self.assertEqual(self.migration_calls, [])
        self.assertEqual(self.new_manager().inspect()["pending"], "http")

    def test_external_config_edit_during_validation_is_preserved(self):
        def edit(*args):
            self.config.write_bytes(self.config.read_bytes() + b'\r\n# external edit\r\n')
        self.startup.side_effect = edit
        self.manager.request_mode("http")
        result = self.manager.process_pending()
        self.assertEqual(result["status"], "error")
        self.assertIn(b"# external edit", self.config.read_bytes())
        self.assertEqual(self.parsed()["model_provider"], transport.PROVIDER_ID)
        self.assertEqual(self.migration_calls, [])

    def test_user_relaunch_during_validation_does_not_trigger_config_rollback(self):
        self.startup.side_effect = lambda *args: self.running.append({"ProcessId": 4321})
        self.manager.request_mode("http")
        result = self.manager.process_pending()
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.parsed()["model_provider"], transport.PROVIDER_ID)
        self.assertEqual(self.migration_calls, [])

    def test_partial_metadata_failure_retains_history_provider_and_stops_until_retry(self):
        self.create_threads([("target", "openai", "gpt-6-sol", "high", 0)])
        failed = Mock(return_value={"ok": False, "changed": None, "mutation_attempted": True, "reason": "迁移尚未完成。"})
        self.manager.migrate = failed
        self.manager.request_mode("http")
        result = self.manager.process_pending()
        self.assertEqual(result["status"], "error")
        self.assertEqual(self.parsed()["model_provider"], transport.PROVIDER_ID)
        self.assertTrue(result["can_restore"])
        self.assertEqual(self.manager._load("ownership")["targets"], {"target": "openai"})
        self.manager.process_pending()
        failed.assert_called_once()

    def failed_resume_fixture(self):
        self.create_threads([("target", "openai", "gpt-6-sol", "high", 0)])
        rollout = self.home / "target.jsonl"
        prefix = b'{"type":"original-history"}\n'
        rollout.write_bytes(prefix)
        backup = self.home / "fixture-migration-backup"
        backup.mkdir()
        (backup / "target.jsonl.before").write_bytes(prefix)
        (backup / "manifest.json").write_text(json.dumps({"targets": [{
            "thread_id": "target", "original_path": str(rollout), "bytes": len(prefix),
            "prefix_sha256": hashlib.sha256(prefix).hexdigest()}]}), encoding="utf-8")

        def lost_response(engine, home, thread_ids, provider_id, *, original=None):
            self.migrate(engine, home, thread_ids, provider_id, original=original)
            with rollout.open("ab") as stream:
                stream.write(b'{"type":"appended-settings"}\n')
            return {"ok": False, "changed": None, "mutation_attempted": True,
                    "backup_directory": str(backup), "reason": "A metadata RPC response was lost."}

        self.manager.migrate = lost_response
        self.manager.request_mode("http")
        result = self.manager.process_pending()
        self.assertEqual(result["status"], "error")
        self.assertNotIn("RPC", result["explanation"])
        self.assertIn("重试", result["explanation"])
        self.assertIn("A metadata RPC response was lost.", (self.manager.state / "journal.jsonl").read_text(encoding="utf-8"))
        self.assertEqual(self.manager._load("ownership")["checkpoint"]["backups"], [str(backup)])
        return rollout

    def test_lost_response_retry_revalidates_same_id_already_on_http(self):
        self.failed_resume_fixture()
        restarted = self.new_manager()
        restarted.request_mode("http")
        result = restarted.process_pending()
        self.assertTrue(result["ok"], result)
        self.assertEqual(self.migration_calls, [(["target"], transport.PROVIDER_ID, None)] * 2)
        self.assertNotIn("checkpoint", restarted._load("ownership"))
        self.assertEqual(restarted._load("ownership")["targets"], {"target": "openai"})

    def test_retry_refuses_changed_original_prefix_before_new_baseline_or_write(self):
        rollout = self.failed_resume_fixture()
        rollout.write_bytes(b"!" + rollout.read_bytes()[1:])
        before_config = self.config.read_bytes()
        before_backups = list((self.manager.state / "backups").iterdir())
        restarted = self.new_manager()
        restarted.request_mode("http")
        result = restarted.process_pending()
        self.assertEqual(result["status"], "error")
        self.assertIn("校验未通过", result["explanation"])
        self.assertEqual(len(self.migration_calls), 1)
        self.assertEqual(self.config.read_bytes(), before_config)
        self.assertEqual(list((self.manager.state / "backups").iterdir()), before_backups)

    def test_retry_refuses_unexpected_model_or_effort_change(self):
        self.failed_resume_fixture()
        with self.database() as db:
            db.execute("UPDATE threads SET model='unexpected-model'")
        restarted = self.new_manager()
        restarted.request_mode("http")
        result = restarted.process_pending()
        self.assertEqual(result["status"], "error")
        self.assertIn("对话设置已发生变化", result["explanation"])
        self.assertEqual(len(self.migration_calls), 1)

    def test_two_installations_share_home_guard_during_metadata_migration(self):
        self.create_threads([("target", "openai", "gpt-6-sol", "high", 0)])
        second_capabilities = Mock(return_value={"metadata_resume_supported": True})
        second_startup = Mock(return_value={"startup_validated": True})
        second_migrate = Mock()
        second = transport.TransportManager(
            root=self.root / "another-install", home=self.home, engine=self.engine,
            process_check=self.process_check, migrate=second_migrate,
            capability_check=second_capabilities, startup_check=second_startup)
        self.manager.request_mode("http")
        second.request_mode("http")
        busy_results = []

        def overlap(engine, home, thread_ids, provider_id, *, original=None):
            before_second = self.config.read_bytes()
            busy = second.process_pending()
            busy_results.append(busy)
            self.assertEqual(busy["status"], "pending")
            self.assertTrue(busy["ok"])
            self.assertEqual(busy["pending"], "http")
            self.assertEqual(self.config.read_bytes(), before_second)
            self.assertNotIn("error", second._load("pending"))
            self.assertFalse((second.state / "ownership.json").exists())
            second_capabilities.assert_not_called()
            second_startup.assert_not_called()
            second_migrate.assert_not_called()
            return self.migrate(engine, home, thread_ids, provider_id, original=original)

        self.manager.migrate = overlap
        result = self.manager.process_pending()
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(busy_results), 1)
        self.assertEqual(len(self.migration_calls), 1)

    def test_home_guard_releases_after_failure_so_other_install_can_apply(self):
        second = transport.TransportManager(
            root=self.root / "another-install", home=self.home, engine=self.engine,
            process_check=self.process_check, migrate=self.migrate,
            capability_check=Mock(return_value={"metadata_resume_supported": True}),
            startup_check=self.startup)
        self.manager.request_mode("http")
        second.request_mode("http")
        self.capabilities.side_effect = transport.Refused("当前引擎接口不可用。")
        self.assertEqual(self.manager.process_pending()["status"], "error")
        result = second.process_pending()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["status"], "applied")

    def test_cancel_pending_only_removes_intent(self):
        self.running.append({"ProcessId": 1234})
        self.manager.request_mode("http")
        result = self.manager.cancel_pending()
        self.assertIsNone(result["pending"])
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_changed_codex_home_does_not_apply_old_intent(self):
        self.manager.request_mode("http")
        other = self.root / "different-home"
        other.mkdir()
        manager = transport.TransportManager(root=self.root, home=other, engine=self.engine, process_check=lambda: [])
        result = manager.process_pending()
        self.assertFalse(result["ok"])
        self.assertFalse((other / "config.toml").exists())
        self.assertEqual(self.config.read_bytes(), self.original)

    def test_modified_owned_provider_is_not_overwritten_on_restore(self):
        self.apply_http()
        external = self.config.read_bytes().replace(b'supports_websockets = false', b'supports_websockets = true')
        self.config.write_bytes(external)
        result = self.manager.request_mode("original")
        self.assertFalse(result["ok"])
        self.assertEqual(self.config.read_bytes(), external)


class CapabilityTests(unittest.TestCase):
    def test_real_schema_fields_gate_without_exposing_raw_output(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            calls = []
            def run(engine, home, args):
                calls.append(args)
                if args == ["--version"]:
                    return b"codex-cli 0.170.0\n"
                output = Path(args[-1]) / "v2"
                output.mkdir()
                (output / "ThreadResumeParams.json").write_text(json.dumps({"properties": dict.fromkeys(("threadId", "modelProvider", "model", "config", "excludeTurns"), {})}))
                (output / "ThreadResumeResponse.json").write_text(json.dumps({"properties": dict.fromkeys(("modelProvider", "model", "reasoningEffort", "thread"), {})}))
                return b""
            with patch.object(transport, "_engine_run", side_effect=run):
                report = transport.validate_capabilities(root / "codex.exe", root, root / "state")
            self.assertTrue(report["metadata_resume_supported"])
            self.assertEqual(list((root / "state").iterdir()), [])
            self.assertEqual(len(calls), 2)

    def test_missing_resume_capability_and_old_version_are_rejected(self):
        with tempfile.TemporaryDirectory() as location:
            root = Path(location)
            with patch.object(transport, "_engine_run", return_value=b"codex-cli 0.165.0"):
                with self.assertRaises(transport.Refused):
                    transport.validate_capabilities(root / "codex.exe", root, root / "state")


class DesktopApplicationTests(unittest.TestCase):
    registration = {"family": transport._DESKTOP_FAMILY, "application_id": "App",
                    "install_location": r"C:\Program Files\WindowsApps\OpenAI.Codex_26.1027.3384.0_x64__2p2nqsd0c76g0",
                    "executable": "app/ChatGPT.exe"}

    def test_inventory_keeps_official_wrapper_without_engine_and_ignores_other_chatgpt(self):
        wrapper = transport.ntpath.join(self.registration["install_location"], "app", "ChatGPT.exe")
        document = {"desktop": self.registration, "processes": [
            {"ProcessId": 10, "Name": "ChatGPT.exe", "ExecutablePath": wrapper},
            {"ProcessId": 20, "Name": "ChatGPT.exe", "ExecutablePath": r"C:\Other ChatGPT\ChatGPT.exe"}]}
        with patch.object(transport.os, "name", "nt"), patch.object(transport, "_powershell_json", return_value=document):
            self.assertEqual([row["ProcessId"] for row in transport.process_inventory()], [10])
            document["processes"].append({"ProcessId": 30, "Name": "CODEX.EXE", "ExecutablePath": None})
            self.assertEqual([row["ProcessId"] for row in transport.process_inventory()], [10, 30])
            document["desktop"] = dict(self.registration, family="Other.ChatGPT_publisher")
            self.assertEqual([row["ProcessId"] for row in transport.process_inventory()], [30])

    def test_missing_desktop_process_path_is_not_mistaken_for_an_exited_app(self):
        document = {"desktop": self.registration, "processes": [
            {"ProcessId": 10, "Name": "ChatGPT.exe", "ExecutablePath": None}]}
        with patch.object(transport.os, "name", "nt"), patch.object(transport, "_powershell_json", return_value=document):
            with self.assertRaisesRegex(transport.Refused, "无法确认身份"):
                transport.process_inventory()

    def test_registered_app_opens_only_through_shell_app_id(self):
        with patch.object(transport, "_require_user_launch"), \
                patch.object(transport, "_powershell_json", return_value=self.registration), \
                patch.object(transport.subprocess, "Popen") as launch:
            self.assertEqual(transport.discover_desktop(), transport._DESKTOP_APP_ID)
            transport.open_desktop(transport._DESKTOP_APP_ID)
            command = launch.call_args.args[0]
            self.assertEqual(transport.ntpath.basename(command[0]), "explorer.exe")
            self.assertEqual(command[1:], ["shell:AppsFolder\\" + transport._DESKTOP_APP_ID])
            launch.assert_called_once()

    def test_invalid_app_ids_and_unregistered_or_elevated_launch_are_refused(self):
        with patch.object(transport.subprocess, "Popen") as launch, \
                patch.object(transport, "_require_user_launch"), \
                patch.object(transport, "_powershell_json", return_value=None):
            for app_id in (None, "com.openai.codex", transport._DESKTOP_APP_ID + " extra"):
                with self.subTest(app_id=app_id), self.assertRaises(transport.Refused):
                    transport.open_desktop(app_id)
            with self.assertRaises(transport.Refused):
                transport.discover_desktop()
            launch.assert_not_called()
        with patch.object(transport, "_require_user_launch", side_effect=transport.Refused("普通用户")), \
                patch.object(transport, "_powershell_json") as query:
            with self.assertRaises(transport.Refused):
                transport.discover_desktop()
            query.assert_not_called()


if __name__ == "__main__":
    unittest.main()
