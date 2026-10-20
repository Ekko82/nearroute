param([string]$PythonExe = '', [switch]$NoStart)

$ErrorActionPreference = 'Stop'
$applicationRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$stateDirectory = Join-Path $applicationRoot 'state'
$installStatePath = Join-Path $stateDirectory 'install-state.json'
$runSubKeyPath = 'Software\Microsoft\Windows\CurrentVersion\Run'
# Keep the legacy value name to avoid creating a second startup entry.
$runValueName = 'CodexNodeOptimizer'
$launchPath = Join-Path $applicationRoot 'launch.ps1'
$shellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$desktopDirectory = [Environment]::GetFolderPath('Desktop')
if ([string]::IsNullOrWhiteSpace($desktopDirectory)) { throw '无法定位当前用户桌面。' }
$shortcutPath = Join-Path $desktopDirectory '近舟 NearRoute.lnk'

function Write-InstallState {
    param([object]$Value, [string]$RawJson)
    $temporary = Join-Path $stateDirectory ('.install-state-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        $content = if ($PSBoundParameters.ContainsKey('RawJson')) { $RawJson } else { $Value | ConvertTo-Json -Depth 12 }
        [IO.File]::WriteAllText($temporary, $content, [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $installStatePath -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Test-OwnedShortcut {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $process = $null
    try {
        $arguments = @('-B', ('"' + (Join-Path $applicationRoot 'windows_app.py') + '"'),
            '--check-shortcut', ('"' + $Path + '"'), '--target', ('"' + $shellPath + '"'),
            '--script', ('"' + $launchPath + '"'))
        $process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WorkingDirectory $applicationRoot -WindowStyle Hidden -Wait -PassThru
        return ($process.ExitCode -eq 0)
    } catch { return $false }
    finally { if ($null -ne $process) { $process.Dispose() } }
}

foreach ($name in @('VERSION', 'optimizer.py', 'dashboard.py', 'updater.py', 'tray_icon.py', 'windows_app.py', 'launch.ps1', 'config.example.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $applicationRoot $name) -PathType Leaf)) { throw "安装文件不完整：缺少 $name" }
}
$PythonExe = & $launchPath -ResolveOnly -PythonExe $PythonExe
$existing = $null
$existingStateJson = $null
if (Test-Path -LiteralPath $installStatePath -PathType Leaf) {
    $existingStateJson = Get-Content -LiteralPath $installStatePath -Raw -Encoding UTF8
    $existing = $existingStateJson | ConvertFrom-Json
    if ($existing.schema_version -notin @(1, 2) -or $existing.root -ne $applicationRoot) { throw '安装记录与当前目录不匹配，请先从原目录卸载旧安装。' }
}
$null = New-Item -ItemType Directory -Path $stateDirectory -Force
$runArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launchPath + '" -Mode run -PythonExe "' + $PythonExe + '"'
$installedRunValue = '"' + $shellPath + '" ' + $runArguments
$runKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($runSubKeyPath)
$temporaryShortcut = Join-Path $stateDirectory ('.nearroute-' + [guid]::NewGuid().ToString('N') + '.lnk')
$stateChanged = $false
$runChanged = $false
$shortcutChanged = $false
$installationCommitted = $false
$priorShortcutBytes = $null
try {
    $currentRun = $runKey.GetValue($runValueName, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
    $currentRunKind = if ($null -ne $currentRun) { $runKey.GetValueKind($runValueName) } else { $null }
    if ($null -ne $existing -and $existing.installed -eq $true) {
        if ($null -ne $currentRun -and $currentRun -ne $existing.installed_run_value) { throw '同名登录启动项已被修改，已保留该设置。' }
        $previousRun = $existing.previous_run
    } else {
        if ($null -ne $currentRun -and -not ([string]$currentRun).Contains('"' + $launchPath + '"')) { throw '同名登录启动项属于其他目录，已保留。请先卸载原安装。' }
        $previousRun = [ordered]@{ existed = $false; kind = $null; value = $null }
    }
    if ($null -ne $existing -and $existing.installed -eq $true -and $existing.shortcut_path -eq $shortcutPath) {
        $previousShortcut = $existing.previous_shortcut
        if ((Test-Path -LiteralPath $shortcutPath -PathType Leaf) -and $existing.shortcut_sha256) {
            if ((Get-FileHash -LiteralPath $shortcutPath -Algorithm SHA256).Hash -ne $existing.shortcut_sha256) { throw '近舟快捷方式已被修改，已保留该文件。' }
        }
    } else {
        if ((Test-Path -LiteralPath $shortcutPath -PathType Leaf) -and -not (Test-OwnedShortcut $shortcutPath)) { throw '桌面已有同名的其他快捷方式，已保留。请先重命名该文件。' }
        $previousShortcut = [ordered]@{ existed = $false; bytes_base64 = $null }
    }
    if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) { $priorShortcutBytes = [IO.File]::ReadAllBytes($shortcutPath) }
    # Create and stamp only a new temporary link through Unicode Shell APIs.
    # WScript.Shell cannot read/save paths outside the system ANSI code page.
    $identityArguments = @('-B', ('"' + (Join-Path $applicationRoot 'windows_app.py') + '"'),
        '--create-shortcut', ('"' + $temporaryShortcut + '"'), '--target', ('"' + $shellPath + '"'),
        '--script', ('"' + $launchPath + '"'), '--python', ('"' + $PythonExe + '"'))
    $identityProcess = Start-Process -FilePath $PythonExe -ArgumentList $identityArguments -WorkingDirectory $applicationRoot -WindowStyle Hidden -Wait -PassThru
    try {
        if ($identityProcess.ExitCode -ne 0) { throw '未能写入近舟快捷方式的任务栏标识，原快捷方式保持不变。' }
    } finally { $identityProcess.Dispose() }
    $installState = [ordered]@{
        schema_version = 2; installed = $true; root = $applicationRoot
        installed_at = [DateTimeOffset]::Now.ToString('o'); python_exe = $PythonExe
        run_subkey = $runSubKeyPath; run_value_name = $runValueName
        installed_run_value = $installedRunValue; previous_run = $previousRun
        shortcut_path = $shortcutPath; shortcut_sha256 = (Get-FileHash -LiteralPath $temporaryShortcut -Algorithm SHA256).Hash
        previous_shortcut = $previousShortcut
    }
    $stateChanged = $true
    Write-InstallState $installState
    $configPath = Join-Path $applicationRoot 'config.json'
    if (-not (Test-Path -LiteralPath $configPath)) { Copy-Item -LiteralPath (Join-Path $applicationRoot 'config.example.json') -Destination $configPath }
    $runChanged = $true
    $runKey.SetValue($runValueName, $installedRunValue, [Microsoft.Win32.RegistryValueKind]::String)
    $shortcutChanged = $true
    Move-Item -LiteralPath $temporaryShortcut -Destination $shortcutPath -Force
    $installationCommitted = $true
    foreach ($legacyName in @('Codex线路优选.lnk', 'Codex Node Optimizer.lnk')) {
        try {
            $legacyPath = Join-Path $desktopDirectory $legacyName
            if (-not (Test-OwnedShortcut $legacyPath)) { continue }
            if ($null -ne $existing -and $existing.shortcut_path -eq $legacyPath -and $existing.shortcut_sha256) {
                if ((Get-FileHash -LiteralPath $legacyPath -Algorithm SHA256).Hash -ne $existing.shortcut_sha256) { Write-Warning "旧项目快捷方式已修改，已保留：$legacyName"; continue }
                if ($existing.previous_shortcut.existed) {
                    [IO.File]::WriteAllBytes($legacyPath, [Convert]::FromBase64String($existing.previous_shortcut.bytes_base64))
                    continue
                }
            }
            Remove-Item -LiteralPath $legacyPath -Force
        } catch { Write-Warning "近舟已安装，但旧快捷方式暂时无法清理：$legacyName" }
    }
} catch {
    $installError = $_
    if (-not $installationCommitted) {
        $rollbackSucceeded = $true
        try {
            if ($runChanged -and $runKey.GetValue($runValueName, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames) -eq $installedRunValue) {
                if ($null -ne $currentRun) { $runKey.SetValue($runValueName, $currentRun, $currentRunKind) }
                else { $runKey.DeleteValue($runValueName, $false) }
            }
            if ($shortcutChanged) {
                $shortcutExists = Test-Path -LiteralPath $shortcutPath -PathType Leaf
                $isNewShortcut = $shortcutExists -and (Get-FileHash -LiteralPath $shortcutPath -Algorithm SHA256).Hash -eq $installState.shortcut_sha256
                if ($isNewShortcut -or -not $shortcutExists) {
                    if ($null -ne $priorShortcutBytes) { [IO.File]::WriteAllBytes($shortcutPath, $priorShortcutBytes) }
                    elseif ($isNewShortcut) { Remove-Item -LiteralPath $shortcutPath -Force }
                }
            }
        } catch { $rollbackSucceeded = $false; Write-Warning '安装未完成，部分设置未能回滚；已保留安装记录，可再次运行卸载。' }
        if ($stateChanged -and $rollbackSucceeded) {
            if ($null -ne $existingStateJson) { Write-InstallState -RawJson $existingStateJson }
            elseif (Test-Path -LiteralPath $installStatePath -PathType Leaf) { Remove-Item -LiteralPath $installStatePath -Force }
        }
    }
    throw $installError
} finally {
    $runKey.Dispose()
    if (Test-Path -LiteralPath $temporaryShortcut) { Remove-Item -LiteralPath $temporaryShortcut -Force }
}
if (-not $NoStart) { Start-Process -FilePath $shellPath -ArgumentList $runArguments -WorkingDirectory $applicationRoot -WindowStyle Hidden }
Write-Output '已安装近舟 NearRoute：登录时后台启动，桌面快捷方式可打开设置。'
