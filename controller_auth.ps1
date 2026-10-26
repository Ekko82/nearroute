# Imports only the selected, current user's own MAOMAO controller credential.
# Run explicitly from NearRoute's Import button, never during discovery.
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
    [long]$RequestExpiresUtcTicks
)
$ErrorActionPreference = 'Stop'
$targetName = 'NEARROUTE_MAOMAO_SECRET_' + $ControllerPort
$failureStage = 10

function Read-MaomaoCoreArgument {
    param($Kernel, $Parent, [string]$ClientDirectory, [string]$ApplicationPath)
    if ($Kernel.Name -notin @('mihomo-windows-386.exe', 'mihomo-windows-amd64.exe')) { return $null }
    $roamingKernel = [IO.Path]::Combine($ClientDirectory, $Kernel.Name)
    $installedKernel = [IO.Path]::Combine([IO.Path]::GetDirectoryName($ApplicationPath), 'resources', 'extra', $Kernel.Name)
    if (-not ([string]::Equals($Kernel.ExecutablePath, $roamingKernel, [StringComparison]::OrdinalIgnoreCase) -or
              [string]::Equals($Kernel.ExecutablePath, $installedKernel, [StringComparison]::OrdinalIgnoreCase))) { return $null }
    if (-not [string]::Equals($Parent.ExecutablePath, $ApplicationPath, [StringComparison]::OrdinalIgnoreCase)) { return $null }
    if ($Kernel.ParentProcessId -ne $Parent.ProcessId) { return $null }
    if ([string]::IsNullOrWhiteSpace($Kernel.CommandLine)) { return $null }
    if ($Kernel.CommandLine -notmatch '(?:^|\s)-mmy(?:\s|$)') { return $null }
    $found = [regex]::Matches($Kernel.CommandLine, '(?:^|\s)-secret\s+"?([0-9a-fA-F]{48})"?(?=\s|$)')
    if ($found.Count -ne 1) { return $null }
    return $found[0].Groups[1].Value
}

try {
    if ([DateTime]::UtcNow.Ticks -gt $RequestExpiresUtcTicks) { exit 124 }
    $addresses = @($ControllerHost)
    if ($ControllerHost -eq 'localhost') { $addresses = @('127.0.0.1', '::1') }
    $listeners = @(Get-CimInstance -Namespace 'root/StandardCimv2' -ClassName MSFT_NetTCPConnection `
        -Filter ('State = 2 AND LocalPort = ' + $ControllerPort) -OperationTimeoutSec 5 -ErrorAction Stop |
        Where-Object { $_.LocalAddress -in $addresses })
    $kernelIds = @($listeners | Select-Object -ExpandProperty OwningProcess -Unique)
    if ($kernelIds.Count -ne 1) { throw 'ambiguous_controller' }
    $failureStage = 11
    $currentSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    if ($currentSid -ne $ExpectedSid) { throw 'different_user_elevation' }
    $kernel = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$kernelIds[0]) -OperationTimeoutSec 5
    $parent = Get-CimInstance Win32_Process -Filter ("ProcessId = " + [int]$kernel.ParentProcessId) -OperationTimeoutSec 5
    foreach ($process in @($kernel, $parent)) {
        $owner = Invoke-CimMethod -InputObject $process -MethodName GetOwnerSid -OperationTimeoutSec 5
        if ($owner.ReturnValue -ne 0 -or $owner.Sid -ne $ExpectedSid) { throw 'different_owner' }
    }
    $failureStage = 12
    $clientDirectory = Join-Path $env:APPDATA 'MAOMAOYUNAPP'
    $appName = ([char]0x732b).ToString() + [char]0x732b + [char]0x4e91 + '.exe'
    $programFiles = ${env:ProgramFiles(x86)}
    if (-not $programFiles) { $programFiles = $env:ProgramFiles }
    $applicationPath = Join-Path $programFiles ('MAOMAOYUNAPP\' + $appName)
    $secret = Read-MaomaoCoreArgument $kernel $parent $clientDirectory $applicationPath
    if (-not $secret) { throw 'credential_not_readable' }

    $failureStage = 13
    # Probe the literal loopback listener; do not resolve localhost through DNS.
    $requestHost = ($listeners | Sort-Object LocalAddress | Select-Object -First 1).LocalAddress
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

    # Uses NearRoute's existing environment-variable authentication mechanism.
    # No proxy group, client configuration or running process is modified.
    $failureStage = 14
    if ([DateTime]::UtcNow.Ticks -gt $RequestExpiresUtcTicks) { exit 124 }
    [Environment]::SetEnvironmentVariable($targetName, $secret, 'User')
    $secret = $null
    exit 0
} catch {
    # Never print process arguments or raw network/process exceptions.
    exit $failureStage
}
