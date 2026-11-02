"""Isolated metadata migration tests: no user profile, engine, auth, or network."""

from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path, PureWindowsPath
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid

import codex_history as history


@contextmanager
def fixture_database(path):
    db = sqlite3.connect(path)
    try:
        with db:
            yield db
    finally:
        db.close()


class HistoryMigrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="nearroute-history-")
        self.addCleanup(temporary.cleanup)
        self.home = Path(temporary.name)
        self.engine = self.home / "CoDeX.exe"
        self.engine.touch()
        self.config = self.home / "config.toml"
        self.config.write_text('''model = "global-model-must-not-replace-saved"
model_reasoning_effort = "low"
model_provider = "nearroute-http"
model_context_window = 1048576
model_auto_compact_token_limit = 950000
[features]
enable_request_compression = true
plugins = true
apps = true
[model_providers.nearroute-http]
name = "OpenAI"
wire_api = "responses"
requires_openai_auth = true
supports_websockets = false
supports_standalone_web_search = true
''', encoding="utf-8")
        self.database = self.home / "state_5.sqlite"
        with fixture_database(self.database) as db:
            db.execute("CREATE TABLE threads(id TEXT PRIMARY KEY, rollout_path TEXT, model_provider TEXT, "
                       "model TEXT, reasoning_effort TEXT, archived INTEGER)")
        self.ids = [str(uuid.uuid4()), str(uuid.uuid4())]
        self.paths, self.settings, self.prefixes = {}, {}, {}
        for index, thread_id in enumerate(self.ids):
            settings = {"model": ["gpt-6.1-sol", "gpt-6-sol"][index],
                        "reasoning_effort": ["ultra", "high"][index], "service_tier": "priority",
                        "model_provider_id": "openai", "cwd": str(self.home / "workspace"),
                        "approval_policy": "on-request", "approvals_reviewer": "user",
                        "permission_profile": {"type": "managed", "network": "restricted"},
                        "active_permission_profile": {"id": ":read-only"},
                        "runtime_workspace_roots": [str(self.home / "workspace")],
                        "disabled_plugin_ids": ["disabled-plugin"]}
            path = self.home / ("rollout-" + thread_id + ".jsonl")
            records = [{"type": "session_meta", "payload": {"id": thread_id, "model_provider": "openai"}},
                       {"type": "response_item", "payload": {"private_message": "Never expose this fixture prompt"}},
                       {"type": "event_msg", "payload": {"type": "thread_settings_applied", "thread_settings": settings}}]
            path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")
            with fixture_database(self.database) as db:
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?,0)",
                           (thread_id, str(path), "openai", settings["model"], settings["reasoning_effort"]))
            self.paths[thread_id], self.settings[thread_id] = path, settings
            self.prefixes[thread_id] = path.read_bytes()
        self.calls, self.closed = [], 0
        self.fail_after = None
        self.response_override = {}
        self.persisted_override = {}
        self.corrupt_prefix = False
        self.skip_database_write = False
        self.persist_extended_path = False
        outer = self

        class FixtureClient:
            def __init__(self, engine, home, sqlite_home, report):
                outer.assertEqual(engine, outer.engine)
                outer.assertEqual(home, outer.home)
                outer.assertEqual(sqlite_home, outer.home)
                self.proc = SimpleNamespace(pid=987654)
                report["app_server_started"] = True

            def request(self, method, params):
                outer.calls.append((method, params))
                if outer.fail_after == len(outer.calls):
                    raise RuntimeError("Authorization: Bearer fixture-secret; C:/private-user/path")
                thread_id = params["threadId"]
                saved = dict(outer.settings[thread_id])
                saved.update(model=params["model"], model_provider_id=params["modelProvider"],
                             reasoning_effort=params["config"]["model_reasoning_effort"])
                saved["service_tier"] = params.get("serviceTier")
                with outer.paths[thread_id].open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "event_msg", "payload": {
                        "type": "thread_settings_applied", "thread_settings": {**saved, **outer.persisted_override}}}) + "\n")
                if not outer.skip_database_write:
                    with fixture_database(outer.database) as db:
                        db.execute("UPDATE threads SET model_provider=?,model=?,reasoning_effort=? WHERE id=?",
                                   (params["modelProvider"], saved["model"], saved["reasoning_effort"], thread_id))
                        if outer.persist_extended_path:
                            db.execute("UPDATE threads SET rollout_path=? WHERE id=?",
                                       ("\\\\?\\" + str(outer.paths[thread_id]), thread_id))
                if outer.corrupt_prefix:
                    with outer.paths[thread_id].open("r+b") as stream:
                        stream.write(b"!")
                result = {"thread": {"id": thread_id}, "modelProvider": params["modelProvider"],
                          "model": saved["model"], "reasoningEffort": saved["reasoning_effort"],
                          "serviceTier": saved["service_tier"]}
                for key, response_key in history.RESPONSE_KEYS.items():
                    result[response_key] = saved[key]
                result.update(outer.response_override)
                return result

            def close(self):
                outer.closed += 1
                return {"helper_stopped": True, "reader_stopped": True,
                        "child_cleanup_job_closed": True, "exit_code": 0, "forced_owned_cleanup": False}

        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(history, "WINDOWS", True))
        self.stack.enter_context(patch.object(history, "process_inventory", return_value=[]))
        self.stack.enter_context(patch.object(history, "Client", FixtureClient))
        self.stack.enter_context(patch.dict(os.environ))
        os.environ.pop("CODEX_SQLITE_HOME", None)

    def migrate(self, ids=None, provider=history.PROVIDER, **kwargs):
        return history.migrate(self.engine, self.home, self.ids if ids is None else ids, provider, **kwargs)

    def test_move_retains_ids_current_models_efforts_permissions_and_backups(self):
        config_before = self.config.read_bytes()
        report = self.migrate()
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["changed"])
        self.assertEqual(report["model_turns_requested"], 0)
        self.assertEqual(report["original"], dict.fromkeys(self.ids, "openai"))
        self.assertEqual(self.closed, 1)
        backup = Path(report["backup_directory"])
        self.assertEqual((backup / "config.toml.before").read_bytes(), config_before)
        self.assertEqual(self.config.read_bytes(), config_before)
        with history.read_database(backup / self.database.name) as db:
            self.assertEqual([row[0] for row in db.execute("SELECT model_provider FROM threads")], ["openai", "openai"])
        for thread_id, (method, params), safe in zip(self.ids, self.calls, report["targets"]):
            self.assertEqual(method, "thread/resume")
            self.assertEqual(set(params), {"threadId", "modelProvider", "model", "config", "excludeTurns", "serviceTier"})
            self.assertEqual(params["threadId"], thread_id)
            self.assertEqual(params["config"], {"model_reasoning_effort": self.settings[thread_id]["reasoning_effort"]})
            self.assertEqual(params["model"], self.settings[thread_id]["model"])
            self.assertEqual(params["serviceTier"], "priority")
            self.assertTrue(params["excludeTurns"])
            self.assertEqual(safe["state"], "persisted")
            self.assertTrue(safe["original_prefix_intact"])
            self.assertEqual((backup / (thread_id + ".jsonl.before")).read_bytes(), self.prefixes[thread_id])
            self.assertTrue(self.paths[thread_id].read_bytes().startswith(self.prefixes[thread_id]))
        public = dict(report)
        public.pop("backup_directory")
        self.assertNotIn(str(self.home), json.dumps(public))
        self.assertNotIn("fixture prompt", json.dumps(report))

    def test_restore_uses_recorded_provider_and_current_saved_model(self):
        self.assertTrue(self.migrate()["ok"])
        self.calls.clear()
        thread_id = self.ids[0]
        self.settings[thread_id].update(model="gpt-current-exact", reasoning_effort="xhigh", model_provider_id=history.PROVIDER)
        with fixture_database(self.database) as db:
            db.execute("UPDATE threads SET model=?,reasoning_effort=? WHERE id=?", ("gpt-current-exact", "xhigh", thread_id))
        with self.paths[thread_id].open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "thread_settings_applied",
                         "thread_settings": self.settings[thread_id]}}) + "\n")
        report = self.migrate([thread_id], provider=None, original={thread_id: "openai"})
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.calls[0][1]["modelProvider"], "openai")
        self.assertEqual(self.calls[0][1]["model"], "gpt-current-exact")
        self.assertEqual(self.calls[0][1]["config"], {"model_reasoning_effort": "xhigh"})

    def test_running_process_gate_is_case_insensitive_and_never_stops_user_processes(self):
        for name in ("Codex.exe", "CODEX.EXE", "codex.exe"):
            with self.subTest(name=name), patch.object(history, "process_inventory", return_value=[
                {"Name": name, "ProcessId": 21, "ParentProcessId": 0}]), patch.object(history.subprocess, "Popen") as spawn:
                report = self.migrate()
                self.assertFalse(report["ok"])
                self.assertFalse(report["mutation_attempted"])
                self.assertNotIn("backup_directory", report)
                spawn.assert_not_called()
        self.assertFalse(self.calls)

    def test_owned_descendants_are_excluded_but_other_engine_is_not(self):
        rows = [{"Name": "codex.exe", "ProcessId": 10, "ParentProcessId": 1},
                {"Name": "CoDeX.exe", "ProcessId": 11, "ParentProcessId": 10}]
        with patch.object(history, "process_inventory", return_value=rows):
            history.assert_no_user_codex(10)
            with self.assertRaises(history.Refused):
                history.assert_no_user_codex(12)

    def test_missing_model_effort_archived_or_original_refuses_before_backup(self):
        for assignment in ("model=NULL", "reasoning_effort=NULL", "archived=1"):
            with self.subTest(assignment=assignment):
                with fixture_database(self.database) as db:
                    db.execute("UPDATE threads SET " + assignment + " WHERE id=?", (self.ids[0],))
                report = self.migrate([self.ids[0]])
                self.assertFalse(report["ok"])
                self.assertFalse(report["mutation_attempted"])
                self.assertNotIn("backup_directory", report)
                with fixture_database(self.database) as db:
                    saved = self.settings[self.ids[0]]
                    db.execute("UPDATE threads SET model=?,reasoning_effort=?,archived=0 WHERE id=?",
                               (saved["model"], saved["reasoning_effort"], self.ids[0]))
        report = self.migrate(provider=None, original={})
        self.assertFalse(report["ok"])
        self.assertFalse(self.calls)

    def test_lost_rpc_retains_partial_progress_and_redacts_exception(self):
        self.fail_after = 2
        report = self.migrate()
        self.assertFalse(report["ok"])
        self.assertTrue(report["changed"])
        self.assertTrue(report["mutation_attempted"])
        self.assertEqual(report["targets"][0]["state"], "resumed")
        self.assertEqual(report["targets"][1]["state"], "resume_requested")
        self.assertTrue(Path(report["backup_directory"]).is_dir())
        self.assertEqual(self.closed, 1)
        self.assertNotIn("fixture-secret", json.dumps(report))
        self.assertNotIn("private-user", json.dumps(report))

    def test_first_lost_rpc_marks_change_uncertain_and_still_cleans_up(self):
        self.fail_after = 1
        report = self.migrate()
        self.assertFalse(report["ok"])
        self.assertIsNone(report["changed"])
        self.assertTrue(report["cleanup"]["helper_stopped"])

    def test_rejects_response_permissions_change_with_backup_intact(self):
        self.response_override = {"approvalPolicy": "never"}
        report = self.migrate()
        self.assertFalse(report["ok"])
        self.assertIn("permission", report["reason"])
        self.assertEqual(self.closed, 1)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual((Path(report["backup_directory"]) / (self.ids[0] + ".jsonl.before")).read_bytes(), self.prefixes[self.ids[0]])

    def test_persisted_database_and_original_prefix_are_both_required(self):
        for failure in ("skip_database_write", "corrupt_prefix"):
            with self.subTest(failure=failure):
                setattr(self, failure, True)
                report = self.migrate([self.ids[0]])
                self.assertFalse(report["ok"])
                self.assertEqual(report["targets"][0]["state"], "resumed")
                setattr(self, failure, False)

    def test_persisted_permission_profile_is_verified_even_when_response_matches(self):
        self.persisted_override = {"permission_profile": {"type": "unrestricted"}}
        report = self.migrate([self.ids[0]])
        self.assertFalse(report["ok"])
        self.assertIn("permission", report["reason"])
        self.assertEqual(report["targets"][0]["state"], "resumed")

    def test_saved_default_tier_matches_native_null_response_and_persistence(self):
        thread_id = self.ids[0]
        self.settings[thread_id]["service_tier"] = "default"
        with self.paths[thread_id].open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "thread_settings_applied",
                         "thread_settings": self.settings[thread_id]}}) + "\n")
        self.response_override = {"serviceTier": None}
        self.persisted_override = {"service_tier": None}
        report = self.migrate([thread_id])
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.calls[0][1]["serviceTier"], "default")
        self.assertTrue(report["targets"][0]["original_prefix_intact"])

    def test_nondefault_tiers_are_never_treated_as_native_null(self):
        thread_id = self.ids[0]
        for tier in ("priority", "fast", "flex", "Default"):
            with self.subTest(tier=tier):
                self.settings[thread_id]["service_tier"] = tier
                with self.paths[thread_id].open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps({"type": "event_msg", "payload": {"type": "thread_settings_applied",
                                 "thread_settings": self.settings[thread_id]}}) + "\n")
                self.response_override = {"serviceTier": None}
                report = self.migrate([thread_id])
                self.assertFalse(report["ok"])
                self.assertIn("service tier", report["reason"])

    def test_saved_null_tier_explicitly_retains_standard_speed(self):
        thread_id = self.ids[0]
        self.settings[thread_id]["service_tier"] = None
        with self.paths[thread_id].open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "thread_settings_applied",
                         "thread_settings": self.settings[thread_id]}}) + "\n")
        self.response_override = {"serviceTier": None}
        report = self.migrate([thread_id])
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.calls[0][1]["serviceTier"], "default")

    def test_online_backup_contains_committed_wal_metadata(self):
        with fixture_database(self.database) as writer:
            writer.execute("PRAGMA journal_mode=WAL")
            writer.execute("CREATE TABLE wal_fixture(value TEXT)")
            writer.execute("INSERT INTO wal_fixture VALUES('committed')")
            writer.commit()
            report = self.migrate([self.ids[0]])
            self.assertTrue(report["ok"], report)
            with history.read_database(Path(report["backup_directory"]) / self.database.name) as db:
                self.assertEqual(db.execute("SELECT value FROM wal_fixture").fetchone()[0], "committed")

    @unittest.skipUnless(os.name == "nt", "Windows extended paths require Windows filesystem I/O")
    def test_existing_extended_rollout_path_inside_same_home_migrates(self):
        thread_id = self.ids[0]
        extended = "\\\\?\\" + str(self.paths[thread_id])
        with fixture_database(self.database) as db:
            db.execute("UPDATE threads SET rollout_path=? WHERE id=?", (extended, thread_id))
        report = self.migrate([thread_id])
        self.assertTrue(report["ok"], report)
        self.assertEqual(report["targets"][0]["state"], "persisted")
        self.assertTrue(self.paths[thread_id].read_bytes().startswith(self.prefixes[thread_id]))

    @unittest.skipUnless(os.name == "nt", "Windows extended paths require Windows filesystem I/O")
    def test_engine_persisting_extended_prefix_retains_same_original_file(self):
        self.persist_extended_path = True
        report = self.migrate([self.ids[0]])
        self.assertTrue(report["ok"], report)
        self.assertTrue(report["targets"][0]["original_prefix_intact"])

    def test_extended_drive_and_unc_normalization_preserves_containment_boundary(self):
        cases = [(r"\\?\D:\Codex\sessions\rollout.jsonl", r"D:\Codex\sessions\rollout.jsonl", r"D:\Codex"),
                 (r"\\?\UNC\server\share\Codex\sessions\rollout.jsonl",
                  r"\\server\share\Codex\sessions\rollout.jsonl", r"\\server\share\Codex")]
        for extended, normal, home in cases:
            with self.subTest(extended=extended):
                normalized = history.comparison_path(PureWindowsPath(extended))
                self.assertEqual(normalized, PureWindowsPath(normal))
                self.assertEqual(normalized.relative_to(PureWindowsPath(home)), PureWindowsPath("sessions/rollout.jsonl"))
                with self.assertRaises(ValueError):
                    normalized.relative_to(PureWindowsPath(home + "-other"))

    def test_device_and_globalroot_namespaces_are_refused(self):
        for value in (r"\\?\GLOBALROOT\Device\HarddiskVolume1\rollout.jsonl",
                      r"\\.\D:\Codex\rollout.jsonl", r"\??\D:\Codex\rollout.jsonl"):
            with self.subTest(value=value), self.assertRaises(history.Refused):
                history.comparison_path(PureWindowsPath(value))


if __name__ == "__main__":
    unittest.main()
