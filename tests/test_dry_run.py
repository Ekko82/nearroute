"""Dry runs never recover unfinished selections by changing the active route."""

import io
import unittest
from unittest.mock import patch

import optimizer
from tests import test_runtime
import updater


class DryRunTests(unittest.TestCase):
    def setUp(self):
        self.fixture = test_runtime.RuntimeTests()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.setUp()
        self.pending = self.fixture.state / "pending-selection.json"
        self.fixture.stack.enter_context(patch.object(updater, "is_installing", return_value=False))

    def unfinished_selection(self):
        self.fixture.api.proxies["Main"]["now"] = "JP fast"
        self.fixture.journal(transaction_id="interrupted-selection")
        return self.pending.read_bytes()

    def cli(self, command, *, dry_run=True):
        arguments = ["optimizer.py", command] + (["--dry-run"] if dry_run else [])
        with patch.object(optimizer.sys, "argv", arguments), \
             patch.object(optimizer.sys, "stdout", None):
            optimizer.main()

    def assert_preserved(self, before):
        self.assertEqual(self.fixture.api.select_calls, [])
        self.assertEqual(self.fixture.api.proxies["Main"]["now"], "JP fast")
        self.assertEqual(self.pending.read_bytes(), before)

    def test_cli_scan_rejects_unfinished_recovery_without_changing_the_route(self):
        before = self.unfinished_selection()
        with self.assertRaisesRegex(optimizer.ControllerError, "只测速.*restore"):
            self.cli("scan")
        self.assert_preserved(before)
        self.fixture.instance_lock.close.assert_called_once()

    def test_dry_run_health_check_preserves_unfinished_recovery(self):
        before = self.unfinished_selection()
        self.fixture.helper.dry_run = True
        with self.assertRaisesRegex(optimizer.ControllerError, "只测速.*restore"):
            self.fixture.helper.tick_health()
        self.assert_preserved(before)
        self.fixture.verify.assert_not_called()

    def test_cli_run_reports_pending_recovery_without_changing_the_route(self):
        before = self.unfinished_selection()
        stop = self.fixture.state / "stop.request"
        with patch.object(optimizer.time, "sleep", side_effect=lambda _seconds: stop.touch()):
            self.cli("run")
        self.assert_preserved(before)
        status = optimizer.read_json(self.fixture.state / "status.json", {})
        self.assertRegex(status["last_error"], "只测速.*restore")
        self.assertEqual(status["phase"], "stopped")
        self.fixture.instance_lock.close.assert_called_once()

    def test_committed_journal_can_be_retired_during_a_dry_scan(self):
        self.unfinished_selection()
        optimizer.save_owned_record(self.fixture.state / "selection.json", {
            **optimizer.controller_identity(self.fixture.cfg), "group": "Main", "name": "JP fast",
            "transaction_id": "interrupted-selection",
        })
        self.cli("scan")
        self.assertEqual(self.fixture.api.select_calls, [])
        self.assertEqual(self.fixture.api.proxies["Main"]["now"], "JP fast")
        self.assertFalse(self.pending.exists())
        self.assertTrue(optimizer.read_json(self.fixture.state / "status.json", {})["nodes"])

    def test_normal_cli_scan_and_run_still_recover_interrupted_selections(self):
        self.fixture.cfg["enabled"] = False
        for command in ("scan", "run"):
            with self.subTest(command=command):
                self.unfinished_selection()
                self.fixture.api.select_calls.clear()
                (self.fixture.state / "scan.request").touch()
                stop = self.fixture.state / "stop.request"
                with patch.object(optimizer.time, "sleep", side_effect=lambda _seconds: stop.touch()):
                    self.cli(command, dry_run=False)
                self.assertEqual(self.fixture.api.select_calls, [("Main", "JP old")])
                self.assertEqual(self.fixture.api.proxies["Main"]["now"], "JP old")
                self.assertFalse(self.pending.exists())

    def test_restore_dry_run_is_rejected_before_restoration(self):
        self.fixture.helper.protect_original("Main", "JP old")
        optimizer.save_owned_record(self.fixture.state / "selection.json", {
            **optimizer.controller_identity(self.fixture.cfg), "group": "Main", "name": "JP fast",
        })
        self.fixture.api.proxies["Main"]["now"] = "JP fast"
        before = (self.fixture.state / "original-selection.json").read_bytes()
        with patch.object(optimizer, "stop_daemon", return_value=True) as stop, \
             patch.object(optimizer.sys, "stderr", io.StringIO()) as error:
            with self.assertRaises(SystemExit) as rejected:
                self.cli("restore")
        self.assertEqual(rejected.exception.code, 2)
        self.assertIn("--dry-run", error.getvalue())
        stop.assert_not_called()
        self.assertEqual(self.fixture.api.select_calls, [])
        self.assertEqual(self.fixture.api.proxies["Main"]["now"], "JP fast")
        self.assertEqual((self.fixture.state / "original-selection.json").read_bytes(), before)

    def test_other_unsupported_commands_reject_dry_run(self):
        for command in ("stop", "status"):
            with self.subTest(command=command), \
                 patch.object(optimizer, "stop_daemon") as stop, \
                 patch.object(optimizer.sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as rejected:
                    self.cli(command)
                self.assertEqual(rejected.exception.code, 2)
                stop.assert_not_called()


if __name__ == "__main__":
    unittest.main()
