"""Offline controller-ownership regressions using the public selection flows."""

import copy
import time
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


class ControllerScopeTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.RuntimeTests(
            "test_paused_manual_switch_is_confirmed_and_survives_helper_restart"
        )
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.cfg = self.fixture.cfg
        self.state = self.fixture.state
        self.api = self.fixture.api
        self.helper = self.fixture.helper
        self.controllers = {self.identity(self.cfg): self.api}
        self.controller_requests = []
        self.fixture.stack.enter_context(
            patch.object(optimizer, "Controller", side_effect=self.make_controller)
        )
        self.fixture.stack.enter_context(
            patch.object(optimizer, "stop_daemon", return_value=True)
        )

    @staticmethod
    def identity(cfg):
        return cfg["controller"], cfg["controller_secret_env"]

    def make_controller(self, cfg):
        identity = self.identity(cfg)
        self.controller_requests.append(identity)
        # Unknown controllers cannot fall back to a real network connection.
        return self.controllers[identity]

    def choose(self, name, group="Main"):
        self.cfg["group"] = group
        optimizer.request_manual_selection(name)
        self.assertTrue(self.helper.process_manual_selection())

    def use_other_controller(self, current="JP fast", **identity):
        self.cfg.update(identity)
        other = test_runtime.FakeController()
        other.proxies["Main"]["now"] = current
        self.controllers[self.identity(self.cfg)] = other
        self.helper = optimizer.Optimizer()
        return other

    def test_restored_controller_a_cannot_claim_untouched_controller_b(self):
        self.choose("JP fast")
        self.assertEqual(optimizer.restore_original(), ["Main"])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")

        other = self.use_other_controller(controller="http://127.0.0.1:9791")
        self.assertEqual(optimizer.restore_original(), [])
        self.assertEqual(other.select_calls, [])
        self.assertEqual(other.proxies["Main"]["now"], "JP fast")

    def test_controller_b_restores_its_own_baseline_after_controller_a(self):
        self.choose("JP fast")
        self.assertEqual(optimizer.restore_original(), ["Main"])

        other = self.use_other_controller(
            current="JP manual", controller="http://127.0.0.1:9791"
        )
        self.choose("JP fast")
        self.assertEqual(optimizer.restore_original(), ["Main"])
        self.assertEqual(other.select_calls, [("Main", "JP fast"), ("Main", "JP manual")])
        self.assertEqual(other.proxies["Main"]["now"], "JP manual")

    def test_unrestored_controllers_with_identical_groups_keep_separate_records(self):
        source_identity = optimizer.controller_identity(self.cfg)
        self.choose("JP fast")

        other = self.use_other_controller(controller="http://127.0.0.1:9791")
        self.assertEqual(optimizer.restore_original(), [])
        self.assertEqual(other.select_calls, [])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP fast")

        other.proxies["Main"]["now"] = "JP manual"
        self.choose("JP fast")
        self.assertEqual(optimizer.restore_original(), ["Main"])
        self.assertEqual(other.select_calls, [("Main", "JP fast"), ("Main", "JP manual")])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP fast")

        self.cfg.update(source_identity)
        self.helper = optimizer.Optimizer()
        self.assertEqual(optimizer.restore_original(), ["Main"])
        self.assertEqual(self.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
        self.assertEqual(other.proxies["Main"]["now"], "JP manual")

    def test_new_session_after_restore_uses_the_new_manual_baseline(self):
        self.choose("JP fast")
        self.assertEqual(optimizer.restore_original(), ["Main"])
        self.api.proxies["Main"]["now"] = "JP manual"
        self.helper = optimizer.Optimizer()

        self.choose("JP fast")
        self.assertEqual(optimizer.restore_original(), ["Main"])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP manual")
        self.assertEqual(self.api.select_calls[-2:], [("Main", "JP fast"), ("Main", "JP manual")])

    def test_unbound_legacy_records_are_preserved_without_restoring_or_upgrading(self):
        self.choose("JP fast")
        original_path = self.state / "original-selection.json"
        selection_path = self.state / "selection.json"
        pending_path = self.state / "pending-selection.json"
        bound_original = optimizer.read_json(original_path, {})
        bound_selection = optimizer.read_json(selection_path, {})
        legacy_original = {"Main": {"name": "JP old", "saved_at": "legacy"}}
        legacy_selection = {
            "group": "Main", "name": "JP fast", "transaction_id": "legacy",
            "groups": {"Main": {"name": "JP fast", "transaction_id": "legacy"}},
        }
        legacy_pending = {
            "group": "Main", "previous": "JP old", "candidate": "JP fast",
            "transaction_id": "legacy", "at": "legacy",
        }
        cases = {
            "original": (legacy_original, bound_selection, {}),
            "selection": (bound_original, legacy_selection, {}),
            "pending": (bound_original, {}, legacy_pending),
            "all": (legacy_original, legacy_selection, legacy_pending),
        }
        for label, records in cases.items():
            with self.subTest(legacy=label):
                expected = copy.deepcopy(records)
                for path, value in zip((original_path, selection_path, pending_path), expected):
                    optimizer.atomic_json(path, value)
                self.api.proxies["Main"]["now"] = "JP fast"
                self.api.select_calls.clear()
                self.helper = optimizer.Optimizer()

                self.assertEqual(optimizer.restore_original(), [])
                if expected[2]:
                    with self.assertRaisesRegex(optimizer.ControllerError, r"人工|备份.*移走"):
                        self.helper.recover_pending()
                else:
                    self.helper.recover_pending()
                self.assertEqual(self.api.select_calls, [])
                self.assertEqual(self.api.proxies["Main"]["now"], "JP fast")
                for path, value in zip((original_path, selection_path, pending_path), expected):
                    self.assertEqual(optimizer.read_json(path, None), value)

    def test_partial_restore_retires_successes_and_keeps_other_group_records(self):
        for group, original in (
            ("Main", "JP old"),
            ("Other", "JP manual"),
            ("External", "JP old"),
            ("Failed", "JP manual"),
        ):
            self.api.proxies[group] = {
                "type": "Selector", "all": ["JP old", "JP fast", "JP manual"],
                "now": original,
            }
            self.choose("JP fast", group)
        self.api.proxies["External"]["now"] = "JP manual"
        self.api.select_calls.clear()
        select = self.api.select

        def fail_last_restore(group, name):
            if group == "Failed" and name == "JP manual":
                raise optimizer.ControllerError("mock failed group restore")
            return select(group, name)

        with patch.object(self.api, "select", side_effect=fail_last_restore):
            with self.assertRaises(optimizer.ControllerError):
                optimizer.restore_original()
        self.assertEqual(self.api.select_calls, [("Main", "JP old"), ("Other", "JP manual")])
        self.assertEqual(self.api.proxies["External"]["now"], "JP manual")
        self.assertEqual(self.api.proxies["Failed"]["now"], "JP fast")

        # A retry must not reclaim groups that were already restored successfully.
        self.api.proxies["Main"]["now"] = "JP fast"
        self.api.proxies["Other"]["now"] = "JP fast"
        self.api.select_calls.clear()
        self.assertEqual(optimizer.restore_original(), ["Failed"])
        self.assertEqual(self.api.select_calls, [("Failed", "JP manual")])
        self.assertEqual(self.api.proxies["Main"]["now"], "JP fast")
        self.assertEqual(self.api.proxies["Other"]["now"], "JP fast")
        self.assertEqual(self.api.proxies["External"]["now"], "JP manual")

        # The externally changed group was skipped, so its recovery record remains.
        self.api.proxies["External"]["now"] = "JP fast"
        self.assertEqual(optimizer.restore_original(), ["External"])
        self.assertEqual(self.api.proxies["External"]["now"], "JP old")

    def test_pending_recovery_uses_both_original_endpoint_and_secret_environment(self):
        for endpoint, source_env, omit_env in (
            ("http://127.0.0.1:9791", "NEARROUTE_TEST_A", False),
            ("http://127.0.0.1:9790", "NEARROUTE_TEST_A", False),
            ("http://127.0.0.1:9790", "", True),
        ):
            with self.subTest(current_controller=endpoint, legacy_empty_env=omit_env):
                self.cfg.update(controller="http://127.0.0.1:9790", controller_secret_env=source_env)
                source_identity = self.identity(self.cfg)
                source = test_runtime.FakeController()
                self.controllers[source_identity] = source
                self.helper = optimizer.Optimizer()
                source.lose_next_put_response = True
                optimizer.request_manual_selection("JP fast")
                self.assertFalse(self.helper.process_manual_selection())
                self.assertTrue((self.state / "pending-selection.json").exists())
                self.assertEqual(source.proxies["Main"]["now"], "JP fast")
                if omit_env:
                    pending_path = self.state / "pending-selection.json"
                    pending = optimizer.read_json(pending_path, {})
                    pending.pop("controller_secret_env")
                    optimizer.atomic_json(pending_path, pending)

                other = self.use_other_controller(
                    controller=endpoint, controller_secret_env="NEARROUTE_TEST_B"
                )
                source.offline = False
                self.controller_requests.clear()
                self.helper.recover_pending()

                self.assertIn(source_identity, self.controller_requests)
                self.assertEqual(source.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
                self.assertEqual(other.select_calls, [])
                self.assertEqual(other.proxies["Main"]["now"], "JP fast")
                self.assertFalse((self.state / "pending-selection.json").exists())

    def seed_manual_context(self):
        self.choose("JP manual")
        self.assertGreater(self.helper.manual_until, time.time())
        self.helper.add_sample("JP fast", 321)
        self.helper.quarantine["JP fast"] = time.time() + 600
        self.helper.failures = 2
        optimizer.atomic_json(self.state / "history.json", self.helper.history)
        optimizer.atomic_json(self.state / "quarantine.json", self.helper.quarantine)

    def assert_context_cleared(self, helper):
        self.assertEqual(helper.manual_until, 0)
        self.assertEqual(helper.history, {})
        self.assertEqual(helper.quarantine, {})
        self.assertEqual(helper.failures, 0)
        self.assertIsNone(helper.observed_selection)

    def test_controller_auth_and_group_changes_reset_manual_and_measurement_context(self):
        base = dict(self.cfg)
        for change in (
            {"controller": "http://127.0.0.1:9791"},
            {"controller_secret_env": "NEARROUTE_TEST_OTHER"},
            {"group": "Other"},
        ):
            with self.subTest(change=change):
                self.cfg.update(base)
                self.controllers[self.identity(self.cfg)] = self.api
                self.helper = optimizer.Optimizer()
                self.seed_manual_context()
                self.cfg.update(change)
                if "group" in change:
                    self.api.proxies["Other"] = {
                        "type": "Selector", "all": ["JP old", "JP fast", "JP manual"],
                        "now": "JP old",
                    }
                else:
                    self.controllers[self.identity(self.cfg)] = test_runtime.FakeController()

                self.helper.reload_config()
                self.assert_context_cleared(self.helper)
                self.assert_context_cleared(optimizer.Optimizer())
                self.assertEqual(optimizer.read_json(self.state / "history.json", None), {})
                self.assertEqual(optimizer.read_json(self.state / "quarantine.json", None), {})

    def test_controller_change_during_validation_rolls_back_a_then_uses_clean_b_context(self):
        self.seed_manual_context()
        self.api.select_calls.clear()
        other = test_runtime.FakeController()
        self.controllers[("http://127.0.0.1:9791", self.cfg["controller_secret_env"])] = other

        def change_controller(*args, **kwargs):
            self.cfg["controller"] = "http://127.0.0.1:9791"
            return {"ok": True, "http_status": 405}

        self.fixture.verify.side_effect = change_controller
        self.assertFalse(self.helper.change_candidate(self.api.general, "Main", "JP manual", "JP fast"))
        self.assertEqual(self.api.select_calls, [("Main", "JP fast"), ("Main", "JP manual")])
        self.assertEqual(other.select_calls, [])
        self.assertEqual(self.helper.cfg["controller"], self.cfg["controller"])

        # cfg is already B; reset cannot rely only on comparing the next loaded cfg.
        self.helper.reload_config()
        self.assert_context_cleared(self.helper)
        self.assertIs(self.helper.api, other)
        self.choose("JP manual")
        self.assertEqual(other.select_calls, [("Main", "JP manual")])
        self.assertEqual(self.api.select_calls, [("Main", "JP fast"), ("Main", "JP manual")])

    def test_restore_retry_retires_record_after_commit_failure_without_another_put(self):
        self.choose("JP fast")
        self.api.select_calls.clear()
        write = optimizer.atomic_json

        def fail_selection_retirement(path, value):
            if path == self.state / "selection.json":
                raise OSError("mock selection retirement write failure")
            return write(path, value)

        with patch.object(optimizer, "atomic_json", side_effect=fail_selection_retirement):
            with self.assertRaises(OSError):
                optimizer.restore_original()
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertEqual(self.api.select_calls, [("Main", "JP old")])

        optimizer.restore_original()
        self.assertEqual(self.api.select_calls, [("Main", "JP old")])
        self.helper = optimizer.Optimizer()
        self.assertEqual(self.helper.manual_until, 0)
        self.api.proxies["Main"]["now"] = "JP fast"
        self.assertEqual(optimizer.restore_original(), [])
        self.assertEqual(self.api.select_calls, [("Main", "JP old")])

    def test_restore_rebinds_status_after_failed_client_save_or_legacy_snapshot(self):
        for source in ("failed client save", "legacy snapshot"):
            with self.subTest(source=source):
                self.choose("JP fast")
                optimizer.atomic_json(optimizer.CONFIG, self.cfg)
                if source == "failed client save":
                    write = optimizer.atomic_json

                    def fail_config_write(path, value):
                        if path == optimizer.CONFIG:
                            raise PermissionError("fixture config file is occupied")
                        return write(path, value)

                    with patch.object(optimizer, "atomic_json", side_effect=fail_config_write):
                        with self.assertRaises(PermissionError):
                            optimizer.configure_client({"controller": "http://127.0.0.1:9791"})
                    self.assertEqual(optimizer.read_json(optimizer.CONFIG, {}), self.cfg)
                    stale = optimizer.read_json(self.state / "status.json", {})
                    self.assertNotEqual(stale.get("controller_context"), list(optimizer.state_context(self.cfg)))
                else:
                    optimizer.atomic_json(self.state / "status.json", {
                        "selected": "legacy node", "group": "legacy group",
                        "nodes": [{"name": "legacy node"}], "health": {"ok": True},
                    })

                self.assertEqual(optimizer.restore_original(), ["Main"])
                self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
                status = optimizer.read_json(self.state / "status.json", {})
                # The dashboard accepts a snapshot only when this identity matches.
                self.assertEqual(status.get("controller_context"), list(optimizer.state_context(self.cfg)))
                self.assertEqual(status.get("group"), "Main")
                self.assertEqual(status.get("selected"), "JP old")
                self.assertEqual(status.get("nodes", []), [])
                self.assertIsNone(status.get("health"))
                for name in ("selection.json", "original-selection.json"):
                    self.assertEqual(optimizer.owned_records(optimizer.read_json(self.state / name, {})), [])

    def test_restore_status_uses_current_rule_group_after_restoring_multiple_groups(self):
        self.choose("JP fast", "Main")
        self.api.proxies["Other"] = {
            "type": "Selector", "all": ["JP old", "JP fast", "JP manual"], "now": "JP manual",
        }
        self.choose("JP fast", "Other")

        self.assertEqual(optimizer.restore_original(), ["Main", "Other"])
        status = optimizer.read_json(self.state / "status.json", {})
        self.assertEqual((status.get("mode"), status.get("group"), status.get("selected")),
                         ("rule", "Other", "JP manual"))
        self.assertEqual(self.api.proxies["Main"]["now"], "JP old")
        self.assertEqual(self.api.proxies["Other"]["now"], "JP manual")

    def test_restore_status_resolves_nested_global_and_refreshes_without_restoration(self):
        self.api.general["mode"] = "global"
        self.api.proxies["GLOBAL"] = {
            "type": "Selector", "all": ["Main", "JP fast"], "now": "Main",
        }
        self.choose("JP fast")

        self.assertEqual(optimizer.restore_original(), ["GLOBAL"])
        status = optimizer.read_json(self.state / "status.json", {})
        self.assertEqual((status.get("mode"), status.get("group"), status.get("selected")),
                         ("global", "GLOBAL", "JP old"))
        self.assertEqual(self.api.proxies["GLOBAL"]["now"], "Main")
        self.api.select_calls.clear()

        self.api.proxies["Main"]["now"] = "JP manual"
        for mode, group, selected in (("global", "GLOBAL", "JP manual"),
                                      ("direct", None, "DIRECT")):
            with self.subTest(mode=mode):
                self.api.general["mode"] = mode
                self.assertEqual(optimizer.restore_original(), [])
                status = optimizer.read_json(self.state / "status.json", {})
                self.assertEqual((status.get("mode"), status.get("group"), status.get("selected")),
                                 (mode, group, selected))
                self.assertTrue(status["controller_available"])
        self.assertEqual(self.api.select_calls, [])

    def test_restore_status_read_failure_preserves_completed_restoration(self):
        self.choose("JP fast")
        self.api.select_calls.clear()
        request = self.api.request

        def fail_mode_read(path, *args, **kwargs):
            if path == "/configs":
                raise optimizer.ControllerError("mock final metadata read failed")
            return request(path, *args, **kwargs)

        with patch.object(self.api, "request", side_effect=fail_mode_read):
            self.assertEqual(optimizer.restore_original(), ["Main"])
        status = optimizer.read_json(self.state / "status.json", {})
        self.assertEqual(status["phase"], "stopped")
        self.assertFalse(status.get("controller_available", True))
        for field in ("mode", "group", "selected", "health"):
            self.assertIsNone(status.get(field))
        self.assertIn("无法确认", status["message"])
        self.assertEqual(status["last_error"], "mock final metadata read failed")
        for name in ("selection.json", "original-selection.json"):
            self.assertEqual(optimizer.owned_records(optimizer.read_json(self.state / name, {})), [])
        self.assertEqual(self.api.select_calls, [("Main", "JP old")])

        self.api.proxies["Main"]["now"] = "JP manual"
        self.assertEqual(optimizer.restore_original(), [])
        status = optimizer.read_json(self.state / "status.json", {})
        self.assertEqual(status["selected"], "JP manual")
        self.assertTrue(status["controller_available"])
        self.assertIsNone(status["last_error"])
        self.assertEqual(self.api.select_calls, [("Main", "JP old")])


if __name__ == "__main__":
    unittest.main()
