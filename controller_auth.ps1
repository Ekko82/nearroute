# Reads only the selected, current user's own MAOMAO controller credential.
# PrivateCredential is NearRoute's automatic path; legacy explicit import uses HKCU.
param(
    [Parameter(Mandatory = $true)]
    [ValidateRange(1, 65535)]
    [int]$ControllerPort,
    [Parameter(Mandatory = $true)]
    [ValidateSet('127.0.0.1', '::1', 'localhost')]
    [string]$ControllerHost,
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^S-\d+(?:-\d+)+$')]
    [string]$ExpectedSid,
    [Parameter(Mandatory = $true)]
    [long]$RequestExpiresUtcTicks,
    [switch]$PrivateCredential
)
$ErrorActionPreference = 'Stop'
$targetName = 'NEARROUTE_MAOMAO_SECRET_' + $ControllerPort
$failureStage = 10

function Select-MaomaoApplicationPath {
    param($Kernel, $Parent, [string[]]$ApplicationPaths)
    foreach ($application in $ApplicationPaths) {
        if ([string]::Equals($Parent.ExecutablePath, $application, [StringComparison]::OrdinalIgnoreCase)) {
            return $application
        }
    }
    foreach ($application in $ApplicationPaths) {
        $installedKernel = [IO.Path]::Combine([IO.Path]::GetDirectoryName($application), 'resources', 'extra', $Kernel.Name)
        if ([string]::Equals($Kernel.ExecutablePath, $installedKernel, [StringComparison]::OrdinalIgnoreCase)) {
            return $application
        }
    }
    # Protected processes may hide both paths. This is used only for the permission
    # hint; credential acquisition below still requires an exact visible path.
    return $ApplicationPaths[0]
}

function Test-MaomaoCoreIdentity {
    param($Kernel, $Parent, [string]$ClientDirectory, [string]$ApplicationPath)
    if ($Kernel.Name -notin @('mihomo-windows-386.exe', 'mihomo-windows-amd64.exe')) { return $false }
    $roamingKernel = [IO.Path]::Combine($ClientDirectory, $Kernel.Name)
    $installedKernel = [IO.Path]::Combine([IO.Path]::GetDirectoryName($ApplicationPath), 'resources', 'extra', $Kernel.Name)
    if (-not ([string]::Equals($Kernel.ExecutablePath, $roamingKernel, [StringComparison]::OrdinalIgnoreCase) -or
              [string]::Equals($Kernel.ExecutablePath, $installedKernel, [StringComparison]::OrdinalIgnoreCase))) { return $false }
    if (-not [string]::Equals($Parent.ExecutablePath, $ApplicationPath, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    if ($Kernel.ParentProcessId -ne $Parent.ProcessId) { return $false }
    return $true
}

function Read-MaomaoCoreArgument {
    param($Kernel, $Parent, [string]$ClientDirectory, [string]$ApplicationPath)
    if (-not (Test-MaomaoCoreIdentity $Kernel $Parent $ClientDirectory $ApplicationPath)) { return $null }
    if ([string]::IsNullOrWhiteSpace($Kernel.CommandLine)) { return $null }
    if ($Kernel.CommandLine -notmatch '(?:^|\s)-mmy(?:\s|$)') { return $null }
    $found = [regex]::Matches($Kernel.CommandLine, '(?:^|\s)-secret\s+"?([0-9a-fA-F]{48})"?(?=\s|$)')
    if ($found.Count -ne 1) { return $null }
    return $found[0].Groups[1].Value
}

function Test-MaomaoPermissionCandidate {
    param($Kernel, $Parent, $KernelOwner, $ParentOwner, [string]$ExpectedSid,
          [string]$ClientDirectory, [string]$ApplicationPath)
    # This grants permission to ask for elevation, never to read or save a secret.
    if ($Kernel.Name -notin @('mihomo-windows-386.exe', 'mihomo-windows-amd64.exe') -or
            $Parent.Name -ne [IO.Path]::GetFileName($ApplicationPath) -or
            $Kernel.ParentProcessId -ne $Parent.ProcessId) { return $false }
    if ($KernelOwner.ReturnValue -ne 0 -or $KernelOwner.Sid -ne $ExpectedSid) { return $false }
    if ($ParentOwner.ReturnValue -eq 0 -and $ParentOwner.Sid -ne $ExpectedSid) { return $false }
    # Contradictory readable paths reject a candidate even when other metadata is hidden.
    $roamingKernel = [IO.Path]::Combine($ClientDirectory, $Kernel.Name)
    $installedKernel = [IO.Path]::Combine([IO.Path]::GetDirectoryName($ApplicationPath), 'resources', 'extra', $Kernel.Name)
    if ($Kernel.ExecutablePath -and
            -not ([string]::Equals($Kernel.ExecutablePath, $roamingKernel, [StringComparison]::OrdinalIgnoreCase) -or
                  [string]::Equals($Kernel.ExecutablePath, $installedKernel, [StringComparison]::OrdinalIgnoreCase))) { return $false }
    if ($Parent.ExecutablePath -and
            -not [string]::Equals($Parent.ExecutablePath, $ApplicationPath, [StringComparison]::OrdinalIgnoreCase)) { return $false }
    return ($ParentOwner.ReturnValue -ne 0 -or [string]::IsNullOrWhiteSpace($Kernel.ExecutablePath) -or
            [string]::IsNullOrWhiteSpace($Parent.ExecutablePath) -or [string]::IsNullOrWhiteSpace($Kernel.CommandLine))
}

function Get-ControllerLoopbackAddress {
    param([string]$ListenerAddress, [string]$ControllerHost)
    if ($ControllerHost -in @('127.0.0.1', 'localhost') -and $ListenerAddress -in @('127.0.0.1', '0.0.0.0')) {
        return '127.0.0.1'
    }
    if ($ControllerHost -in @('::1', 'localhost') -and $ListenerAddress -in @('::1', '::')) {
        return '::1'
    }
    return $null
}

function Save-ControllerCredential {
    param([string]$StateDirectory, [string]$ControllerHost, [int]$ControllerPort, [string]$Secret)
    $directory = Join-Path $StateDirectory 'controller-credentials'
    $filename = $ControllerHost.Replace(':', '_') + '-' + $ControllerPort + '.dpapi'
    $destination = Join-Path $directory $filename
    foreach ($path in @($StateDirectory, $directory, $destination)) {
        if ((Test-Path -LiteralPath $path) -and
                ((Get-Item -LiteralPath $path -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            throw 'redirected_credential_path'
        }
    }
    $null = [IO.Directory]::CreateDirectory($directory)
    Add-Type -AssemblyName System.Security
    $purpose = [Text.Encoding]::ASCII.GetBytes('NearRoute/controller/v1/' + $ControllerHost + ':' + $ControllerPort)
    $encrypted = [Security.Cryptography.ProtectedData]::Protect(
        [Text.Encoding]::ASCII.GetBytes($Secret), $purpose, [Security.Cryptography.DataProtectionScope]::CurrentUser)
    $temporary = Join-Path $directory ([Guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllBytes($temporary, $encrypted)
        if ([IO.File]::Exists($destination)) {
            [IO.File]::Replace($temporary, $destination, [NullString]::Value)
        } else {
            [IO.File]::Move($temporary, $destination)
        }
    } finally {
        if ([IO.File]::Exists($temporary)) { [IO.File]::Delete($temporary) }
    }
}

try {
    if ([DateTime]::UtcNow.Ticks -gt $RequestExpiresUtcTicks) { exit 124 }
    $listeners = @(Get-CimInstance -Namespace 'root/StandardCimv2' -ClassName MSFT_NetTCPConnection `
        -Filter ('State = 2 AND LocalPort = ' + $ControllerPort) -OperationTimeoutSec 5 -ErrorAction Stop |
        Where-Object { Get-ControllerLoopbackAddress $_.LocalAddress $ControllerHost })
    $kernelIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($kernelIds.Count -ne 1) { throw 'ambiguous_controller' }
    $failureStage = 11
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($currentSid -ne $ExpectedSid) { throw 'different_user_elevation' }
    $kernel = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$kernelIds[0]) -OperationTimeoutSec 5
    $parent = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$kernel.ParentProcessId) -OperationTimeoutSec 5
    $kernelOwner = Invoke-CimMethod -InputObject $kernel -MethodName GetOwnerSid -OperationTimeoutSec 5
    $parentOwner = Invoke-CimMethod -InputObject $parent -MethodName GetOwnerSid -OperationTimeoutSec 5
    $clientDirectory = Join-Path $env:APPDATA 'MAOMAOYUNAPP'
    $appName = ([char]0x732b).ToString() + [char]0x732b + [char]0x4e91 + '.exe'
    $localPrograms = if ($env:LOCALAPPDATA) { Join-Path $env:LOCALAPPDATA 'Programs' } else { $null }
    $applicationPaths = @(@(${env:ProgramFiles(x86)}, $env:ProgramFiles, $localPrograms) |
        Where-Object { $_ } | Select-Object -Unique | ForEach-Object { Join-Path $_ ('MAOMAOYUNAPP\' + $appName) })
    $applicationPath = Select-MaomaoApplicationPath $kernel $parent $applicationPaths
    if ($PrivateCredential -and (Test-MaomaoPermissionCandidate $kernel $parent $kernelOwner $parentOwner `
            $ExpectedSid $clientDirectory $applicationPath)) { exit 16 }
    foreach ($owner in @($kernelOwner, $parentOwner)) {
        if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $ExpectedSid) { throw 'different_owner' }
    }
    $failureStage = 12
    if (-not (Test-MaomaoCoreIdentity $kernel $parent $clientDirectory $applicationPath)) { throw 'unsupported_client' }
    $secret = Read-MaomaoCoreArgument $kernel $parent $clientDirectory $applicationPath
    if (-not $secret) { throw 'credential_not_readable' }

    $failureStage = 13
    # Probe the literal loopback listener; do not resolve localhost through DNS.
    $listenerAddress = ($listeners | Sort-Object LocalAddress | Select-Object -First 1).LocalAddress
    $requestHost = Get-ControllerLoopbackAddress $listenerAddress $ControllerHost
    if ($requestHost -eq '::1') { $requestHost = '[::1]' }
    $versionUri = 'http://' + $requestHost + ':' + $ControllerPort + '/version'
    Add-Type -AssemblyName System.Net.Http
    $handler = [Net.Http.HttpClientHandler]::new()
    $handler.UseProxy = $false
    $handler.AllowAutoRedirect = $false
    $client = [Net.Http.HttpClient]::new($handler)
    $client.Timeout = [TimeSpan]::FromSeconds(5)
    $request = [Net.Http.HttpRequestMessage]::new([Net.Http.HttpMethod]::Get, $versionUri)
    $request.Headers.Authorization = [Net.Http.Headers.AuthenticationHeaderValue]::new('Bearer', $secret)
    $response = $null
    try {
        $response = $client.SendAsync($request).GetAwaiter().GetResult()
        if ([int]$response.StatusCode -ne 200) { throw 'authentication_failed' }
        $version = $response.Content.ReadAsStringAsync().GetAwaiter().GetResult() | ConvertFrom-Json
        if (-not ($version.version -is [string]) -or -not $version.version) { throw 'unexpected_controller' }
    } finally {
        if ($null -ne $response) { $response.Dispose() }
        $request.Dispose()
        $client.Dispose()
    }

    $failureStage = 14
    if ([DateTime]::UtcNow.Ticks -gt $RequestExpiresUtcTicks) { exit 124 }
    Save-ControllerCredential (Join-Path $PSScriptRoot 'state') $ControllerHost $ControllerPort $secret
    if (-not $PrivateCredential) {
        [Environment]::SetEnvironmentVariable($targetName, $secret, 'User')
    }
    $secret = $null
    exit 0
} catch {
    # Never print process arguments or raw network/process exceptions.
    exit $failureStage
}
