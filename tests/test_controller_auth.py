"""Explicit controller-auth import boundaries; never invoke UAC or real clients."""

import base64
from contextlib import ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import windows_app as app


class ControllerAuthTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(app, "IS_WINDOWS", True))
        self.stack.enter_context(patch.dict(os.environ))
        self.run = self.stack.enter_context(patch.object(
            app.subprocess, "run", return_value=SimpleNamespace(returncode=0)))
        self.read = self.stack.enter_context(patch.object(
            app, "_read_user_environment", return_value="a" * 48))

    def test_import_returns_only_port_specific_name_and_refreshes_current_process(self):
        name = "NEARROUTE_MAOMAO_SECRET_9790"
        os.environ[name] = "old-value"
        result = app.import_maomao_auth("http://127.0.0.1:9790")
        self.assertEqual(result, name)
        self.assertEqual(os.environ[name], "a" * 48)
        self.read.assert_called_once_with(name)
        command = self.run.call_args.args[0]
        self.assertNotIn("a" * 48, " ".join(command))
        program = base64.b64decode(command[-1]).decode("utf-16-le")
        self.assertIn("-Verb RunAs -WindowStyle Hidden", program)
        self.assertIn("'-ControllerPort', '9790'", program)
        self.assertIn("'-ControllerHost', '127.0.0.1'", program)
        self.assertEqual(self.run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(self.run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertLessEqual(self.run.call_args.kwargs["timeout"], 180)

    def test_ipv6_and_localhost_are_passed_as_literal_host_choices(self):
        for address, host, port in [("http://[::1]:9090/", "::1", 9090),
                                    ("http://localhost:9791", "localhost", 9791)]:
            with self.subTest(address=address):
                self.assertEqual(app.import_maomao_auth(address), f"NEARROUTE_MAOMAO_SECRET_{port}")
                program = base64.b64decode(self.run.call_args.args[0][-1]).decode("utf-16-le")
                self.assertIn(f"'-ControllerHost', '{host}'", program)
                self.assertIn(f"'-ControllerPort', '{port}'", program)

    def test_helper_write_deadline_precedes_parent_wait_timeout_even_after_uac_delay(self):
        app.import_maomao_auth("http://127.0.0.1:9790")
        program = base64.b64decode(self.run.call_args.args[0][-1]).decode("utf-16-le")
        expires_after = int(re.search(r"UtcNow\.AddSeconds\((\d+)\)", program)[1])
        parent_wait = int(re.search(r"WaitForExit\((\d+)\)", program)[1]) / 1000
        self.assertLessEqual(expires_after, parent_wait)
        self.assertLess(program.index("$expires ="), program.index("Start-Process"))

    def test_invalid_or_nonlocal_address_never_launches_admin_helper(self):
        for address in ["http://example.com:9790", "https://127.0.0.1:9790", "http://127.0.0.1:0",
                        "http://127.0.0.1:65536", "http://user:password@127.0.0.1:9790",
                        "http://127.0.0.1:9790/path", "http://127.0.0.1:9790?", "http://127.0.0.1:9790#",
                        "http://127.0.0.1:9790\\other", "http://127.0.0.1:9790\n", None]:
            with self.subTest(address=address), self.assertRaises(ValueError):
                app.import_maomao_auth(address)
        self.run.assert_not_called()
        self.read.assert_not_called()

    def test_non_windows_and_missing_component_never_launch_uac(self):
        with patch.object(app, "IS_WINDOWS", False), self.assertRaisesRegex(RuntimeError, "Windows"):
            app.import_maomao_auth("http://127.0.0.1:9790")
        with patch.object(Path, "is_file", return_value=False), self.assertRaisesRegex(RuntimeError, "缺少"):
            app.import_maomao_auth("http://127.0.0.1:9790")
        self.run.assert_not_called()

    def test_cancel_or_failure_does_not_read_or_reuse_old_registry_credential(self):
        name = "NEARROUTE_MAOMAO_SECRET_9790"
        os.environ[name] = "unchanged"
        for status, message in [(1223, "已取消"), (10, "没有唯一"), (11, "身份"),
                                (12, "不属于"), (13, "验证失败"), (14, "保存"),
                                (15, "启动"), (124, "超时"), (99, "未完成")]:
            self.run.return_value.returncode = status
            with self.subTest(status=status), self.assertRaisesRegex(RuntimeError, message):
                app.import_maomao_auth("http://127.0.0.1:9790")
        self.read.assert_not_called()
        self.assertEqual(os.environ[name], "unchanged")

    def test_external_errors_are_sanitized(self):
        for error in [OSError("sensitive-payload"), subprocess.TimeoutExpired(["sensitive-payload"], 1)]:
            self.run.side_effect = error
            with self.subTest(error=type(error).__name__), self.assertRaises(RuntimeError) as raised:
                app.import_maomao_auth("http://127.0.0.1:9790")
            self.assertNotIn("sensitive-payload", str(raised.exception))
        self.read.assert_not_called()

    def test_invalid_or_unreadable_registry_value_is_not_loaded(self):
        name = "NEARROUTE_MAOMAO_SECRET_9790"
        os.environ[name] = "unchanged"
        for value in ["", "a" * 47, "g" * 48, "a" * 48 + "\n", None]:
            self.read.return_value = value
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, "当前窗口"):
                app.import_maomao_auth("http://127.0.0.1:9790")
            self.assertEqual(os.environ[name], "unchanged")
        self.read.side_effect = OSError("sensitive-payload")
        with self.assertRaises(RuntimeError) as raised:
            app.import_maomao_auth("http://127.0.0.1:9790")
        self.assertNotIn("sensitive-payload", str(raised.exception))


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "PowerShell metadata checks")
class ControllerAuthPowerShellTests(unittest.TestCase):
    def powershell(self, program):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(program.encode("utf-16-le")).decode("ascii")],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", "replace"))
        return result.stdout.decode("utf-8-sig").strip()

    def script_literal(self):
        return "'" + str(app.ROOT / "controller_auth.ps1").replace("'", "''") + "'"

    def test_script_parses_in_powershell_5_and_pure_metadata_guard_checks_real_function(self):
        # Extract only this pure function AST. The main script, Get-NetTCPConnection,
        # process inspection, HTTP client and environment writes never execute.
        program = r"""
$ErrorActionPreference = 'Stop'
$errors = $null; $tokens = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse_failure' }
$definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Read-MaomaoCoreArgument' }, $true)
Invoke-Expression $definition.Extent.Text
$application = 'C:\Fixture\MAOMAOYUNAPP\client.exe'
$directory = 'C:\Fixture\Roaming\MAOMAOYUNAPP'
$installed = 'C:\Fixture\MAOMAOYUNAPP\resources\extra\mihomo-windows-386.exe'
$metadata = @{Name='mihomo-windows-386.exe'; ExecutablePath=$installed; ParentProcessId=200; CommandLine=('fixture -mmy -secret ' + ('a' * 48))}
$parent = [pscustomobject]@{ExecutablePath=$application; ProcessId=200}
$results = @{}
$results.installed = (Read-MaomaoCoreArgument ([pscustomobject]$metadata) $parent $directory $application) -eq ('a' * 48)
$copy = $metadata.Clone(); $copy.ExecutablePath = Join-Path $directory $copy.Name
$results.roaming = (Read-MaomaoCoreArgument ([pscustomobject]$copy) $parent $directory $application) -eq ('a' * 48)
$copy = $metadata.Clone(); $copy.ExecutablePath = 'C:\Unrelated\mihomo-windows-386.exe'
$results.unrelated = $null -eq (Read-MaomaoCoreArgument ([pscustomobject]$copy) $parent $directory $application)
$copy = $metadata.Clone(); $copy.ParentProcessId = 201
$results.parent = $null -eq (Read-MaomaoCoreArgument ([pscustomobject]$copy) $parent $directory $application)
$copy = $metadata.Clone(); $copy.CommandLine += (' -secret ' + ('b' * 48))
$results.duplicate = $null -eq (Read-MaomaoCoreArgument ([pscustomobject]$copy) $parent $directory $application)
$copy = $metadata.Clone(); $copy.CommandLine = 'fixture -secret ' + ('a' * 48)
$results.flag = $null -eq (Read-MaomaoCoreArgument ([pscustomobject]$copy) $parent $directory $application)
$copy = $metadata.Clone(); $copy.Name = 'other.exe'
$results.name = $null -eq (Read-MaomaoCoreArgument ([pscustomobject]$copy) $parent $directory $application)
$otherParent = [pscustomobject]@{ExecutablePath='C:\Unrelated\client.exe'; ProcessId=200}
$results.gui = $null -eq (Read-MaomaoCoreArgument ([pscustomobject]$metadata) $otherParent $directory $application)
$results | ConvertTo-Json -Compress
""".replace("SCRIPT", self.script_literal())
        results = json.loads(self.powershell(program))
        self.assertEqual(set(results), {"installed", "roaming", "unrelated", "parent", "duplicate", "flag", "name", "gui"})
        self.assertTrue(all(results.values()))

    def test_listener_query_has_an_operation_timeout_without_executing_it(self):
        program = r"""
$errors = $null; $tokens = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse_failure' }
$queries = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.CommandAst] -and
    $node.GetCommandName() -eq 'Get-CimInstance' -and
    $node.Extent.Text.Contains('MSFT_NetTCPConnection')
}, $true))
if ($queries.Count -ne 1) { throw 'missing_bounded_listener_query' }
$elements = $queries[0].CommandElements
$parameters = @{}
for ($index = 1; $index -lt $elements.Count - 1; $index++) {
    if ($elements[$index] -is [Management.Automation.Language.CommandParameterAst]) {
        $parameters[$elements[$index].ParameterName] = $elements[$index + 1].Extent.Text
    }
}
$unbounded = @($ast.FindAll({ param($node)
    $node -is [Management.Automation.Language.CommandAst] -and
    $node.GetCommandName() -eq 'Get-NetTCPConnection'
}, $true)).Count
@{parameters=$parameters; unbounded=$unbounded} | ConvertTo-Json -Compress
""".replace("SCRIPT", self.script_literal())
        result = json.loads(self.powershell(program))
        self.assertEqual(result["unbounded"], 0)
        parameters = result["parameters"]
        self.assertEqual(parameters["Namespace"], "'root/StandardCimv2'")
        self.assertEqual(parameters["ClassName"], "MSFT_NetTCPConnection")
        self.assertLessEqual(int(parameters["OperationTimeoutSec"]), 5)
        self.assertIn("State = 2 AND LocalPort = ", parameters["Filter"])
        self.assertIn("$ControllerPort", parameters["Filter"])


if __name__ == "__main__":
    unittest.main()
