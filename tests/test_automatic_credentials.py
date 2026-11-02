"""Automatic credentials use fixtures only; no real clients or elevation prompts."""

import base64
from contextlib import ExitStack
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import windows_app as app


class AutomaticCredentialTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(app, "IS_WINDOWS", True))
        self.stack.enter_context(patch.object(app, "_credential_permission_required", set()))
        self.run = self.stack.enter_context(patch.object(
            app.subprocess, "run", return_value=SimpleNamespace(returncode=0)))
        self.read = self.stack.enter_context(patch.object(
            app, "read_controller_credential", return_value="a" * 48))

    def test_silent_refresh_uses_private_storage_without_elevation_or_environment_changes(self):
        before = dict(os.environ)
        with patch.object(app, "_read_user_environment") as registry:
            self.assertTrue(app.refresh_controller_credential("http://127.0.0.1:9790"))
        program = base64.b64decode(self.run.call_args.args[0][-1]).decode("utf-16-le")
        self.assertIn("'-PrivateCredential'", program)
        self.assertNotIn("RunAs", program)
        self.assertNotIn("a" * 48, program)
        self.assertIn("-WindowStyle Hidden", program)
        self.assertEqual(self.run.call_args.kwargs["stdout"], subprocess.DEVNULL)
        self.assertEqual(self.run.call_args.kwargs["stderr"], subprocess.DEVNULL)
        self.assertEqual(dict(os.environ), before)
        registry.assert_not_called()

    def test_explicit_elevation_keeps_private_storage(self):
        self.assertTrue(app.refresh_controller_credential("http://[::1]:9790", elevate=True))
        program = base64.b64decode(self.run.call_args.args[0][-1]).decode("utf-16-le")
        self.assertIn("'-PrivateCredential'", program)
        self.assertIn("-Verb RunAs -WindowStyle Hidden", program)

    def test_automatic_localhost_uses_the_same_literal_ipv4_credential_and_permission_key(self):
        self.run.return_value.returncode = 16
        self.assertFalse(app.refresh_controller_credential("http://localhost:9790"))
        program = base64.b64decode(self.run.call_args.args[0][-1]).decode("utf-16-le")
        self.assertIn("'-ControllerHost', '127.0.0.1'", program)
        self.assertNotIn("localhost", program)
        self.assertTrue(app.controller_credential_needs_permission("http://127.0.0.1:9790"))
        self.assertTrue(app.controller_credential_needs_permission("http://LOCALHOST:9790/"))
        self.assertFalse(app.controller_credential_needs_permission("http://[::1]:9790"))

    def test_failure_does_not_reuse_stale_credential_or_expose_external_errors(self):
        for status in (10, 11, 12, 13, 14, 15, 124, 1223):
            self.run.return_value.returncode = status
            self.assertFalse(app.refresh_controller_credential("http://localhost:9790"))
            self.assertFalse(app.controller_credential_needs_permission("http://localhost:9790"))
        self.read.assert_not_called()
        for error in (OSError("sensitive"), subprocess.TimeoutExpired(["sensitive"], 1)):
            self.run.side_effect = error
            self.assertFalse(app.refresh_controller_credential("http://localhost:9790"))

    def test_permission_hint_requires_identified_protected_candidate_and_is_cleared_by_next_attempt(self):
        endpoint = "http://127.0.0.1:9790"
        self.assertFalse(app.controller_credential_needs_permission(endpoint))
        self.run.return_value.returncode = 16
        self.assertFalse(app.refresh_controller_credential(endpoint))
        self.assertTrue(app.controller_credential_needs_permission(endpoint))
        self.assertFalse(app.controller_credential_needs_permission("http://127.0.0.1:9791"))
        self.assertFalse(app.refresh_controller_credential(endpoint, elevate=True))
        self.assertFalse(app.controller_credential_needs_permission(endpoint))
        self.run.return_value.returncode = 13
        self.assertFalse(app.refresh_controller_credential(endpoint))
        self.assertFalse(app.controller_credential_needs_permission(endpoint))

    def test_unsupported_or_invalid_controller_never_launches_a_helper(self):
        for endpoint in ("http://example.com:9790", "https://127.0.0.1:9790",
                         "http://127.0.0.1:9790/path", "http://127.0.0.1:9790?",
                         "http://user:pass@localhost:9790", None):
            self.assertFalse(app.refresh_controller_credential(endpoint))
        with patch.object(app, "IS_WINDOWS", False):
            self.assertFalse(app.refresh_controller_credential("http://127.0.0.1:9790"))
        self.run.assert_not_called()


@unittest.skipUnless(os.name == "nt" and shutil.which("powershell.exe"), "Windows DPAPI fixtures")
class WindowsCredentialPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.script = self.root / "controller_auth.ps1"
        shutil.copyfile(app.ROOT / "controller_auth.ps1", self.script)
        self.stack.enter_context(patch.object(app, "ROOT", self.root))
        self.secret = "b" * 48

    def powershell(self, program, expected_status=0):
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
             base64.b64encode(program.encode("utf-16-le")).decode("ascii")],
            stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        self.assertEqual(result.returncode, expected_status, result.stderr.decode("utf-8", "replace"))
        self.assertNotIn(self.secret.encode(), result.stdout)
        self.assertNotIn(self.secret.encode(), result.stderr)
        return result.stdout.decode("utf-8-sig").strip()

    def literal(self, value):
        return "'" + str(value).replace("'", "''") + "'"

    def definitions(self, names):
        return """
$ErrorActionPreference = 'Stop'
$errors = $null; $tokens = $null
$ast = [Management.Automation.Language.Parser]::ParseFile(SCRIPT, [ref]$tokens, [ref]$errors)
if ($errors.Count) { throw 'parse_failure' }
foreach ($name in @(NAMES)) {
    $definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq $name }, $true)
    Invoke-Expression $definition.Extent.Text
}
""".replace("SCRIPT", self.literal(self.script)).replace("NAMES", ",".join(map(self.literal, names)))

    def test_actual_dpapi_storage_decrypts_only_for_matching_endpoint_and_overwrites_atomically(self):
        program = self.definitions(["Save-ControllerCredential"]) + """
$state = STATE
Save-ControllerCredential $state '127.0.0.1' 9790 ('b' * 48)
Save-ControllerCredential $state '127.0.0.1' 9790 ('b' * 48)
""".replace("STATE", self.literal(self.root / "state"))
        self.assertEqual(self.powershell(program), "")
        path = self.root / "state/controller-credentials/127.0.0.1-9790.dpapi"
        self.assertNotIn(self.secret.encode(), path.read_bytes())
        self.assertEqual(app.read_controller_credential("http://127.0.0.1:9790/"), self.secret)
        self.assertEqual(app.read_controller_credential("http://localhost:9790"), self.secret)
        shutil.copyfile(path, path.with_name("__1-9790.dpapi"))
        self.assertEqual(app.read_controller_credential("http://[::1]:9790"), "")
        self.assertEqual(app.read_controller_credential("http://127.0.0.1:9791"), "")
        path.write_bytes(b"invalid-dpapi")
        with patch.object(app.subprocess, "run") as run:
            self.assertEqual(app.read_controller_credential("http://127.0.0.1:9790"), "")
            self.assertEqual(app.read_controller_credential("http://example.com:9790"), "")
        run.assert_not_called()
        self.assertEqual(list(path.parent.glob("*.tmp")), [])

    def test_wildcard_matching_and_permission_hint_keep_credential_identity_boundary(self):
        program = self.definitions(["Get-ControllerLoopbackAddress", "Test-MaomaoPermissionCandidate",
                                    "Test-MaomaoCoreIdentity", "Read-MaomaoCoreArgument"]) + r"""
$application = 'C:\Fixture\MAOMAOYUNAPP\client.exe'
$directory = 'C:\Fixture\Roaming\MAOMAOYUNAPP'
$kernel = [pscustomobject]@{Name='mihomo-windows-386.exe'; ExecutablePath=$null; ParentProcessId=200; CommandLine=$null}
$parent = [pscustomobject]@{Name='client.exe'; ExecutablePath=$null; ProcessId=200}
$owner = [pscustomobject]@{ReturnValue=0; Sid='S-1-5-21-123'}
$protected = [pscustomobject]@{ReturnValue=2; Sid=$null}
$results = @{}
$results.ipv4 = (Get-ControllerLoopbackAddress '0.0.0.0' '127.0.0.1') -eq '127.0.0.1'
$results.ipv6 = (Get-ControllerLoopbackAddress '::' '::1') -eq '::1'
$results.localhost = (Get-ControllerLoopbackAddress '::' 'localhost') -eq '::1'
$results.crossfamily = $null -eq (Get-ControllerLoopbackAddress '::' '127.0.0.1')
$results.remote = $null -eq (Get-ControllerLoopbackAddress '192.168.1.5' 'localhost')
$results.protected = Test-MaomaoPermissionCandidate $kernel $parent $owner $protected $owner.Sid $directory $application
$results.cannotread = $null -eq (Read-MaomaoCoreArgument $kernel $parent $directory $application)
$parent.Name = 'unrelated.exe'
$results.name = -not (Test-MaomaoPermissionCandidate $kernel $parent $owner $protected $owner.Sid $directory $application)
$parent.Name = 'client.exe'; $parent.ExecutablePath = 'C:\Unrelated\client.exe'
$results.path = -not (Test-MaomaoPermissionCandidate $kernel $parent $owner $protected $owner.Sid $directory $application)
$parent.ExecutablePath = $null
$results.owner = -not (Test-MaomaoPermissionCandidate $kernel $parent $owner $protected 'S-1-5-21-999' $directory $application)
$otherOwner = [pscustomobject]@{ReturnValue=0; Sid='S-1-5-21-999'}
$results.parentowner = -not (Test-MaomaoPermissionCandidate $kernel $parent $owner $otherOwner $owner.Sid $directory $application)
$results | ConvertTo-Json -Compress
"""
        results = json.loads(self.powershell(program))
        self.assertEqual(len(results), 11)
        self.assertTrue(all(results.values()), results)

    def test_exact_per_user_installation_is_selected_without_allowing_other_directories(self):
        program = self.definitions(["Select-MaomaoApplicationPath", "Test-MaomaoCoreIdentity",
                                    "Read-MaomaoCoreArgument"]) + r"""
$system = 'C:\Program Files (x86)\MAOMAOYUNAPP\client.exe'
$user = 'C:\Users\Fixture\AppData\Local\Programs\MAOMAOYUNAPP\client.exe'
$directory = 'C:\Users\Fixture\AppData\Roaming\MAOMAOYUNAPP'
$kernel = [pscustomobject]@{Name='mihomo-windows-386.exe'; ExecutablePath='C:\Users\Fixture\AppData\Local\Programs\MAOMAOYUNAPP\resources\extra\mihomo-windows-386.exe'; ParentProcessId=200; CommandLine=('fixture -mmy -secret ' + ('b' * 48))}
$parent = [pscustomobject]@{ExecutablePath=$user; ProcessId=200}
$paths = @($system, $user)
$results = @{}
$selected = Select-MaomaoApplicationPath $kernel $parent $paths
$results.selected = $selected -eq $user
$results.identity = Test-MaomaoCoreIdentity $kernel $parent $directory $selected
$results.secret = (Read-MaomaoCoreArgument $kernel $parent $directory $selected) -eq ('b' * 48)
$parent.ExecutablePath = $null
$results.hiddenparent = (Select-MaomaoApplicationPath $kernel $parent $paths) -eq $user
$parent.ExecutablePath = 'C:\Users\Fixture\Downloads\MAOMAOYUNAPP\client.exe'
$selected = Select-MaomaoApplicationPath $kernel $parent $paths
$results.unrelatedparent = -not (Test-MaomaoCoreIdentity $kernel $parent $directory $selected)
$parent.ExecutablePath = $user; $kernel.ExecutablePath = 'C:\Users\Fixture\Downloads\mihomo-windows-386.exe'
$selected = Select-MaomaoApplicationPath $kernel $parent $paths
$results.unrelatedkernel = -not (Test-MaomaoCoreIdentity $kernel $parent $directory $selected)
$results | ConvertTo-Json -Compress
"""
        results = json.loads(self.powershell(program))
        self.assertEqual(len(results), 6)
        self.assertTrue(all(results.values()), results)

    def test_helper_authenticates_version_before_saving_and_never_writes_environment_in_private_mode(self):
        requests = []
        response_status = [200]

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(handler):
                requests.append((handler.path, handler.headers.get("Authorization")))
                handler.send_response(response_status[0])
                handler.end_headers()
                handler.wfile.write(b'{"version":"fixture"}')

            def log_message(handler, *args):
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        self.addCleanup(worker.join, 3)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        port = server.server_address[1]
        program = r"""
$ErrorActionPreference = 'Stop'
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
$appName = ([char]0x732b).ToString() + [char]0x732b + [char]0x4e91 + '.exe'
$programFiles = ${env:ProgramFiles(x86)}
if (-not $programFiles) { $programFiles = $env:ProgramFiles }
$application = Join-Path $programFiles ('MAOMAOYUNAPP\' + $appName)
$kernelPath = Join-Path ([IO.Path]::GetDirectoryName($application)) 'resources\extra\mihomo-windows-386.exe'
function Get-CimInstance {
    param($ClassName, $Filter, $Namespace, $OperationTimeoutSec, $ErrorAction)
    if ($ClassName -eq 'MSFT_NetTCPConnection') { return [pscustomobject]@{LocalAddress='0.0.0.0'; OwningProcess=100} }
    if ($Filter -eq 'ProcessId = 100') {
        return [pscustomobject]@{Name='mihomo-windows-386.exe'; ExecutablePath=$kernelPath; ProcessId=100; ParentProcessId=200; CommandLine=('fixture -mmy -secret ' + ('b' * 48))}
    }
    return [pscustomobject]@{Name=$appName; ExecutablePath=$application; ProcessId=200}
}
function Invoke-CimMethod {
    param($InputObject, $MethodName, $OperationTimeoutSec)
    return [pscustomobject]@{ReturnValue=0; Sid=$sid}
}
$before = [Environment]::GetEnvironmentVariable('NEARROUTE_MAOMAO_SECRET_PORT', 'User')
& SCRIPT -ControllerHost '127.0.0.1' -ControllerPort PORT -ExpectedSid $sid -RequestExpiresUtcTicks ([DateTime]::UtcNow.AddSeconds(30).Ticks) -PrivateCredential
if ([Environment]::GetEnvironmentVariable('NEARROUTE_MAOMAO_SECRET_PORT', 'User') -ne $before) { throw 'environment_changed' }
""".replace("SCRIPT", self.literal(self.script)).replace("PORT", str(port))
        # The helper exits its own script scope; its exit status becomes $LASTEXITCODE.
        program += "\nexit $LASTEXITCODE\n"
        self.assertEqual(self.powershell(program), "")
        endpoint = f"http://127.0.0.1:{port}"
        self.assertEqual(requests, [("/version", "Bearer " + self.secret)])
        self.assertEqual(app.read_controller_credential(endpoint), self.secret)
        path = self.root / f"state/controller-credentials/127.0.0.1-{port}.dpapi"
        saved = path.read_bytes()
        response_status[0] = 401
        self.assertEqual(self.powershell(program, expected_status=13), "")
        self.assertEqual(path.read_bytes(), saved)


if __name__ == "__main__":
    unittest.main()
