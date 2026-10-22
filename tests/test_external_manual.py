"""External manual grace survives restarts without acquiring node ownership."""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime


class ExternalManualTests(unittest.TestCase):
    @contextmanager
    def runtime(self, *, global_mode=False):
        fixture = test_runtime.RuntimeTests()
        fixture.clock = [1000.0]
        try:
            with patch.object(optimizer.time, "time", side_effect=lambda: fixture.clock[0]):
                fixture.setUp()
                fixture.cfg["concurrency"] = 1
                if global_mode:
                    fixture.api.general["mode"] = "global"
                    fixture.api.proxies["GLOBAL"] = {
                        "type": "Selector", "all": ["JP old", "JP fast", "JP manual"], "now": "JP old",
                    }
                general, proxies, group = fixture.api.snapshot(fixture.cfg)
                fixture.group = group
                self.assertTrue(fixture.helper.change_candidate(general, group, proxies[group]["now"], "JP fast"))
                fixture.helper.recover_pending()
                fixture.api.select_calls.clear()
                yield fixture
        finally:
            fixture.doCleanups()

    def external_choice(self, fixture, *, nested=False):
        if nested:
            fixture.api.proxies["Nested"] = {
                "type": "Selector", "all": ["JP fast", "JP manual"], "now": "JP fast",
            }
            fixture.api.proxies[fixture.group]["all"].append("Nested")
            fixture.api.proxies[fixture.group]["now"] = "Nested"
            fixture.clock[0] = 2900.0
            fixture.helper.tick_health()
        fixture.clock[0] = 3000.0
        fixture.api.proxies["Nested" if nested else fixture.group]["now"] = "JP manual"
        fixture.helper.tick_health()
        self.assertEqual(fixture.helper.manual_until, 3600)

    def test_detected_external_selection_survives_restart_and_does_not_become_owned(self):
        for nested in (False, True):
            with self.subTest(nested=nested), self.runtime() as f:
                owned_before = (f.state / "selection.json").read_bytes()
                self.external_choice(f, nested=nested)
                f.clock[0] = 3010.0
                restarted = optimizer.Optimizer()
                self.assertEqual(restarted.manual_until, 0)
                self.assertIsNone(restarted.status.get("manual_until"))

                restarted.scan(forced=True)

                self.assertEqual(restarted.manual_until, 3600)
                self.assertEqual(restarted.status["selected"], "JP manual")
                self.assertEqual(f.api.select_calls, [])
                self.assertEqual((f.state / "selection.json").read_bytes(), owned_before)
                with patch.object(optimizer, "stop_daemon", return_value=True):
                    self.assertEqual(optimizer.restore_original(), [])
                self.assertEqual(f.api.select_calls, [])

    def test_expired_or_changed_actual_path_does_not_restore_grace(self):
        for changed in ("expired", "top", "nested_leaf", "mode"):
            with self.subTest(changed=changed), self.runtime() as f:
                self.external_choice(f, nested=changed == "nested_leaf")
                f.clock[0] = 3600.0 if changed == "expired" else 3010.0
                if changed == "top":
                    f.api.proxies["Main"]["now"] = "JP old"
                elif changed == "nested_leaf":
                    f.api.proxies["Nested"]["now"] = "JP fast"
                elif changed == "mode":
                    f.api.general["mode"] = "global"
                    f.api.proxies["GLOBAL"] = {
                        "type": "Selector", "all": ["JP old", "JP fast", "JP manual"], "now": "JP manual",
                    }
                restarted = optimizer.Optimizer()
                restarted.tick_health()
                self.assertEqual(restarted.manual_until, 0)
                self.assertIsNone(restarted.status.get("manual_until"))
                self.assertEqual(f.api.select_calls, [])

    def test_external_protections_remain_separate_across_controller_auth_and_configured_group(self):
        for change in (
            {"controller": "http://127.0.0.1:9791"},
            {"controller_secret_env": "NEARROUTE_TEST_B"},
            {"group": "Other"},
        ):
            with self.subTest(change=change), self.runtime(global_mode=True) as f:
                source = dict(f.cfg)
                self.external_choice(f)
                f.cfg.update(change)
                f.clock[0] = 3010.0
                other = optimizer.Optimizer()
                other.tick_health()
                self.assertEqual(other.manual_until, 0)
                f.api.proxies["GLOBAL"]["now"] = "JP old"
                other.tick_health()
                self.assertEqual(other.manual_until, 3610)

                f.cfg.update(source)
                f.api.proxies["GLOBAL"]["now"] = "JP manual"
                other.reload_config()
                self.assertEqual(other.manual_until, 0)
                other.tick_health()
                self.assertEqual(other.manual_until, 3600)
                records = optimizer.read_json(f.state / "external-manual.json", {})["records"]
                self.assertEqual(len(records), 2)
                self.assertEqual(f.api.select_calls, [])

    def test_uncommitted_candidate_is_rolled_back_before_matching_saved_manual_path(self):
        with self.runtime() as f:
            self.external_choice(f)
            f.journal(previous="JP manual", candidate="JP fast", transaction_id="uncommitted")
            f.api.proxies["Main"]["now"] = "JP fast"
            f.clock[0] = 3010.0
            restarted = optimizer.Optimizer()
            restarted.scan(forced=True)
            self.assertEqual(f.api.select_calls, [("Main", "JP manual")])
            self.assertEqual(restarted.manual_until, 3600)
            self.assertFalse((f.state / "pending-selection.json").exists())

    def test_cancelled_mode_rebinds_grace_to_rolled_back_leaf_without_extending_deadline(self):
        for action in ("manual", "candidate"):
            for refresh_fails in (False, True):
                with self.subTest(action=action, refresh_fails=refresh_fails), self.runtime() as f:
                    f.api.proxies["GLOBAL"] = {
                        "type": "Selector", "all": ["Main", "JP manual"], "now": "Main",
                    }
                    owned_before = (f.state / "selection.json").read_bytes()
                    f.clock[0] = 3000.0
                    select, request = f.api.select, f.api.request
                    rolled_back = False

                    def change_mode(group, name):
                        nonlocal rolled_back
                        select(group, name)
                        if name == "JP manual":
                            f.api.general["mode"] = "global"
                        elif name == "JP fast":
                            rolled_back = True
                            f.clock[0] = 3005.0

                    def metadata(path, *args, **kwargs):
                        if refresh_fails and rolled_back and path == "/configs":
                            raise optimizer.ControllerError("fixture final metadata unavailable")
                        return request(path, *args, **kwargs)

                    with patch.object(f.api, "select", side_effect=change_mode), \
                         patch.object(f.api, "request", side_effect=metadata):
                        if action == "manual":
                            optimizer.request_manual_selection("JP manual")
                            self.assertFalse(f.helper.process_manual_selection())
                        else:
                            with self.assertRaises(optimizer.ModeChanged):
                                f.helper.change_candidate(f.api.general, "Main", "JP fast", "JP manual")

                    self.assertEqual(f.api.select_calls, [("Main", "JP manual"), ("Main", "JP fast")])
                    self.assertEqual((f.state / "selection.json").read_bytes(), owned_before)
                    saved = optimizer.read_json(f.state / "external-manual.json", {})["records"][0]
                    self.assertEqual(saved["manual_until"], 3600)
                    if refresh_fails:
                        self.assertEqual(saved["path"], ["global", "GLOBAL", "Main", "JP manual"])
                        self.assertFalse(f.helper.status["controller_available"])
                        self.assertIsNone(f.helper.status["selected"])
                    else:
                        actual_path = optimizer.active_path(*f.api.snapshot(f.cfg))
                        self.assertEqual(saved["path"], list(actual_path))
                        self.assertEqual(f.helper.observed_selection, actual_path)
                        f.clock[0] = 3010.0
                        restarted = optimizer.Optimizer()
                        restarted.tick_health()
                        self.assertEqual(restarted.manual_until, 3600)
                        self.assertEqual(restarted.status["selected"], "JP fast")

    def test_mode_refresh_without_saved_grace_keeps_change_for_next_health_observation(self):
        with self.runtime() as f:
            previous = f.helper.observed_selection
            f.api.proxies["GLOBAL"] = {
                "type": "Selector", "all": ["Main", "JP manual"], "now": "Main",
            }
            self.assertFalse((f.state / "external-manual.json").exists())
            optimizer.request_manual_selection("JP manual", expected_mode=("rule", "Main"))
            f.clock[0] = 3000.0
            f.api.general["mode"] = "global"

            self.assertFalse(f.helper.process_manual_selection())

            self.assertEqual(f.helper.observed_selection, previous)
            self.assertEqual(f.api.select_calls, [])
            self.assertEqual(f.helper.manual_until, 0)
            f.helper.tick_health()
            self.assertEqual(f.helper.manual_until, 3600)
            actual_path = optimizer.active_path(*f.api.snapshot(f.cfg))
            saved = optimizer.read_json(f.state / "external-manual.json", {})["records"][0]
            self.assertEqual(saved["path"], list(actual_path))
            self.assertEqual(saved["manual_until"], 3600)
            self.assertEqual(f.api.select_calls, [])

    def test_actual_failures_can_bypass_restored_grace_and_new_commit_invalidates_old_grace(self):
        with self.runtime() as f:
            self.external_choice(f)
            f.clock[0] = 3010.0
            restarted = optimizer.Optimizer()
            f.verify.side_effect = lambda *_args, **_kwargs: {
                "ok": f.api.proxies["Main"]["now"] == "JP fast", "reason": "fake path failure",
            }
            restarted.tick_health()
            restarted.tick_health()
            self.assertEqual(restarted.manual_until, 3600)
            self.assertEqual(restarted.failures, 2)
            restarted.scan()
            self.assertEqual(f.api.select_calls, [("Main", "JP fast")])
            self.assertEqual(restarted.failures, 0)

            # Even if the old path later reappears, a new owned transaction has
            # superseded the protection saved before the fault recovery.
            f.api.proxies["Main"]["now"] = "JP manual"
            f.verify.side_effect = None
            after_commit = optimizer.Optimizer()
            after_commit.tick_health()
            self.assertEqual(after_commit.manual_until, 0)

    def test_new_tool_manual_selection_keeps_its_own_grace(self):
        with self.runtime() as f:
            self.external_choice(f)
            f.clock[0] = 3010.0
            optimizer.request_manual_selection("JP manual")
            self.assertTrue(f.helper.process_manual_selection())
            self.assertEqual(f.helper.manual_until, 3610)
            restarted = optimizer.Optimizer()
            restarted.tick_health()
            self.assertEqual(restarted.manual_until, 3610)
            self.assertEqual(restarted.record["source"], "manual")

    def test_failed_protection_write_aborts_scan_without_advancing_observation_then_retries(self):
        with self.runtime() as f:
            previous = f.helper.observed_selection
            f.clock[0] = 3000.0
            f.api.proxies["Main"]["now"] = "JP manual"
            write = optimizer.atomic_json

            def fail_protection(path, value):
                if path == f.state / "external-manual.json":
                    raise OSError("fixture protection write failed")
                return write(path, value)

            with patch.object(optimizer, "atomic_json", side_effect=fail_protection):
                with self.assertRaisesRegex(OSError, "fixture protection write failed"):
                    f.helper.scan()
            self.assertEqual(f.helper.observed_selection, previous)
            self.assertEqual(f.helper.manual_until, 3600)
            self.assertEqual(f.api.select_calls, [])
            f.helper.scan()
            restarted = optimizer.Optimizer()
            restarted.tick_health()
            self.assertEqual(restarted.manual_until, 3600)
            self.assertEqual(f.api.select_calls, [])


if __name__ == "__main__":
    unittest.main()
