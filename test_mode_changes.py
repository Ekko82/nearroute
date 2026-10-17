"""Offline mode-transition regressions with the real controller snapshot rules."""

from contextlib import contextmanager
import unittest
from unittest.mock import patch

import optimizer
import test_runtime


# Capture before RuntimeTests patches optimizer.Controller with its fake factory.
REAL_SNAPSHOT = optimizer.Controller.snapshot
TRANSITIONS = (("rule", "global"), ("global", "rule"), ("rule", "direct"), ("global", "direct"))


class ModeChangeTests(unittest.TestCase):
    @contextmanager
    def runtime(self, mode, enabled=True):
        fixture = test_runtime.RuntimeTests(
            "test_paused_manual_switch_is_confirmed_and_survives_helper_restart"
        )
        try:
            fixture.setUp()
            fixture.cfg.update(enabled=enabled, concurrency=1)
            fixture.api.general["mode"] = mode
            fixture.api.proxies["GLOBAL"] = {
                "type": "Selector", "all": ["JP global", "JP fast", "JP manual"],
                "now": "JP global",
            }
            fixture.api.proxies["JP global"] = {"type": "Trojan"}
            fixture.api.delays.update({"JP old": 600, "JP global": 700, "JP fast": 100, "JP manual": 200})
            fixture.api.snapshot = REAL_SNAPSHOT.__get__(fixture.api, type(fixture.api))
            yield fixture
        finally:
            fixture.doCleanups()

    def assert_not_committed(self, fixture):
        committed = optimizer.read_json(fixture.state / "selection.json", {})
        self.assertNotEqual(committed.get("name"), "JP fast")
        self.assertNotIn("JP fast", fixture.helper.quarantine)

    def assert_refreshed_status(self, fixture, mode):
        self.assertEqual(fixture.helper.next_scan, 0)
        status = optimizer.read_json(fixture.state / "status.json", {})
        self.assertTrue(status["controller_available"])
        self.assertEqual(status["mode"], mode)
        if mode == "direct":
            self.assertIsNone(status["group"])
            self.assertEqual(status["selected"], "DIRECT")
        else:
            group = "GLOBAL" if mode == "global" else fixture.cfg["group"]
            self.assertEqual(status["group"], group)
            self.assertEqual(status["selected"], optimizer.resolve_leaf(fixture.api.proxies, fixture.api.proxies[group]["now"]))

    def test_auto_mode_change_before_put_rejects_the_stale_group(self):
        for source, target in TRANSITIONS:
            with self.subTest(source=source, target=target), self.runtime(source) as fixture:
                general, proxies, group = fixture.api.snapshot(fixture.cfg)
                previous = proxies[group]["now"]
                fixture.api.general["mode"] = target

                with self.assertRaises(optimizer.ControllerError):
                    fixture.helper.change_candidate(general, group, previous, "JP fast")

                self.assertEqual(fixture.api.select_calls, [])
                self.assertEqual(fixture.api.proxies[group]["now"], previous)
                self.assert_not_committed(fixture)

    def test_auto_mode_change_during_validation_rolls_back_without_commit_or_quarantine(self):
        for source, target in TRANSITIONS:
            for verify_ok in (True, False):
                with self.subTest(source=source, target=target, verify_ok=verify_ok), self.runtime(source) as fixture:
                    general, proxies, group = fixture.api.snapshot(fixture.cfg)
                    previous = proxies[group]["now"]

                    def change_mode(*args, **kwargs):
                        fixture.api.general["mode"] = target
                        return {"ok": verify_ok, "http_status": 405 if verify_ok else 403}

                    fixture.verify.side_effect = change_mode
                    with self.assertRaises(optimizer.ControllerError):
                        fixture.helper.change_candidate(general, group, previous, "JP fast")

                    self.assertEqual(fixture.api.select_calls, [(group, "JP fast"), (group, previous)])
                    self.assertEqual(fixture.api.proxies[group]["now"], previous)
                    self.assert_not_committed(fixture)

    def test_auto_mode_change_keeps_journal_when_original_group_rollback_is_unconfirmed(self):
        for source, target in TRANSITIONS:
            with self.subTest(source=source, target=target), self.runtime(source) as fixture:
                general, proxies, group = fixture.api.snapshot(fixture.cfg)
                previous = proxies[group]["now"]
                select = fixture.api.select

                def reject_rollback(selected_group, name):
                    if selected_group == group and name == previous:
                        fixture.api.select_calls.append((selected_group, name))
                        return
                    return select(selected_group, name)

                def change_mode(*args, **kwargs):
                    fixture.api.general["mode"] = target
                    return {"ok": True, "http_status": 405}

                fixture.verify.side_effect = change_mode
                with patch.object(fixture.api, "select", side_effect=reject_rollback):
                    with self.assertRaises(optimizer.ControllerError):
                        fixture.helper.change_candidate(general, group, previous, "JP fast")

                self.assertEqual(fixture.api.proxies[group]["now"], "JP fast")
                self.assertTrue((fixture.state / "pending-selection.json").exists())
                self.assertTrue(all(selected_group == group for selected_group, _ in fixture.api.select_calls))
                self.assert_not_committed(fixture)
                fixture.helper.recover_pending()
                self.assertEqual(fixture.api.proxies[group]["now"], previous)
                self.assertFalse((fixture.state / "pending-selection.json").exists())

    def test_manual_mode_change_after_put_reports_failure_and_rolls_back_even_when_paused(self):
        for source, target in TRANSITIONS:
            for enabled in (True, False):
                with self.subTest(source=source, target=target, enabled=enabled), self.runtime(source, enabled) as fixture:
                    _, proxies, group = fixture.api.snapshot(fixture.cfg)
                    previous = proxies[group]["now"]
                    select = fixture.api.select

                    def change_mode_after_put(selected_group, name):
                        select(selected_group, name)
                        if selected_group == group and name == "JP fast":
                            fixture.api.general["mode"] = target

                    optimizer.request_manual_selection("JP fast")
                    with patch.object(fixture.api, "select", side_effect=change_mode_after_put):
                        self.assertFalse(fixture.helper.process_manual_selection())

                    self.assertFalse(fixture.helper.status["manual_selection_result"]["ok"])
                    self.assertEqual(fixture.api.select_calls, [(group, "JP fast"), (group, previous)])
                    self.assertEqual(fixture.api.proxies[group]["now"], previous)
                    self.assert_not_committed(fixture)

    def test_paused_manual_mode_change_keeps_unconfirmed_rollback_for_recovery(self):
        for source, target in TRANSITIONS:
            with self.subTest(source=source, target=target), self.runtime(source, enabled=False) as fixture:
                _, proxies, group = fixture.api.snapshot(fixture.cfg)
                previous = proxies[group]["now"]
                select = fixture.api.select

                def change_mode_and_reject_rollback(selected_group, name):
                    if selected_group == group and name == previous:
                        fixture.api.select_calls.append((selected_group, name))
                        return
                    select(selected_group, name)
                    if selected_group == group and name == "JP fast":
                        fixture.api.general["mode"] = target

                optimizer.request_manual_selection("JP fast")
                with patch.object(fixture.api, "select", side_effect=change_mode_and_reject_rollback):
                    self.assertFalse(fixture.helper.process_manual_selection())

                self.assertFalse(fixture.helper.status["manual_selection_result"]["ok"])
                self.assertEqual(fixture.api.proxies[group]["now"], "JP fast")
                self.assertTrue((fixture.state / "pending-selection.json").exists())
                self.assert_not_committed(fixture)
                fixture.helper.recover_pending()
                self.assertEqual(fixture.api.proxies[group]["now"], previous)
                self.assertFalse((fixture.state / "pending-selection.json").exists())

    def test_scan_mode_change_during_measurement_does_not_select_or_report_old_group(self):
        for source, target in TRANSITIONS:
            with self.subTest(source=source, target=target), self.runtime(source) as fixture:
                delay = fixture.api.delay

                def change_mode_during_measurement(name):
                    fixture.api.general["mode"] = target
                    return delay(name)

                with patch.object(fixture.api, "delay", side_effect=change_mode_during_measurement):
                    fixture.helper.scan(forced=True)

                self.assertEqual(fixture.api.select_calls, [])
                self.assert_not_committed(fixture)
                self.assert_refreshed_status(fixture, target)

    def test_scan_mode_change_during_first_candidate_stops_remaining_old_group_candidates(self):
        for source, target in TRANSITIONS:
            with self.subTest(source=source, target=target), self.runtime(source) as fixture:
                _, proxies, group = fixture.api.snapshot(fixture.cfg)
                previous = proxies[group]["now"]
                changed = False

                def change_mode_during_candidate(*args, **kwargs):
                    nonlocal changed
                    if fixture.api.proxies[group]["now"] == "JP fast":
                        changed = True
                        fixture.api.general["mode"] = target
                        return {"ok": False, "http_status": 403}
                    return {"ok": True, "http_status": 405}

                fixture.verify.side_effect = change_mode_during_candidate
                fixture.helper.scan(forced=True)

                self.assertTrue(changed, "the scan must exercise the first candidate verification")
                self.assertEqual(fixture.api.select_calls, [(group, "JP fast"), (group, previous)])
                self.assertEqual(fixture.api.proxies[group]["now"], previous)
                self.assert_not_committed(fixture)
                self.assert_refreshed_status(fixture, target)

    def test_nested_global_path_is_refreshed_after_rollback_or_marked_unconfirmed(self):
        for action in ("manual", "candidate", "scan"):
            for refresh_fails in (False, True):
                with self.subTest(action=action, refresh_fails=refresh_fails), self.runtime(
                    "rule", enabled=action != "manual"
                ) as fixture:
                    fixture.api.proxies["GLOBAL"].update(all=["Main", "JP global"], now="Main")
                    general, _, group = fixture.api.snapshot(fixture.cfg)
                    select = fixture.api.select
                    request = fixture.api.request
                    rolled_back = False

                    def select_and_change_mode(selected_group, name):
                        nonlocal rolled_back
                        select(selected_group, name)
                        if selected_group == "Main" and name == "JP fast":
                            fixture.api.general["mode"] = "global"
                        elif selected_group == "Main" and name == "JP old":
                            rolled_back = True

                    def read_metadata(path, *args, **kwargs):
                        if refresh_fails and rolled_back and path == "/configs":
                            raise optimizer.ControllerError("mock metadata unavailable after rollback")
                        return request(path, *args, **kwargs)

                    with patch.object(fixture.api, "select", side_effect=select_and_change_mode), \
                         patch.object(fixture.api, "request", side_effect=read_metadata):
                        if action == "manual":
                            optimizer.request_manual_selection("JP fast")
                            self.assertFalse(fixture.helper.process_manual_selection())
                        elif action == "candidate":
                            with self.assertRaises(optimizer.ModeChanged):
                                fixture.helper.change_candidate(general, group, "JP old", "JP fast")
                        else:
                            fixture.helper.scan(forced=True)

                    self.assertEqual(fixture.api.select_calls, [("Main", "JP fast"), ("Main", "JP old")])
                    self.assertEqual(fixture.api.proxies["Main"]["now"], "JP old")
                    status = fixture.helper.status
                    if refresh_fails:
                        self.assertFalse(status["controller_available"])
                        self.assertIsNone(status["selected"])
                        self.assertTrue(status["last_error"])
                    else:
                        self.assertTrue(status["controller_available"])
                        self.assertEqual((status["mode"], status["group"], status["selected"]),
                                         ("global", "GLOBAL", "JP old"))


if __name__ == "__main__":
    unittest.main()
