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
    param([object]$Value)
    $temporary = Join-Path $stateDirectory ('.install-state-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
        Move-Item -LiteralPath $temporary -Destination $installStatePath -Force
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Test-OwnedShortcut {
    param([string]$Path, [object]$Shell)
    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $link = $null
    try {
        $link = $Shell.CreateShortcut($Path)
        return ($link.TargetPath -ieq $shellPath -and $link.Arguments.Contains('"' + $launchPath + '"'))
    } catch { return $false }
    finally { if ($null -ne $link) { $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) } }
}

foreach ($name in @('optimizer.py', 'dashboard.py', 'launch.ps1', 'config.example.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $applicationRoot $name) -PathType Leaf)) { throw "安装文件不完整：缺少 $name" }
}
$PythonExe = & $launchPath -ResolveOnly -PythonExe $PythonExe
$existing = $null
if (Test-Path -LiteralPath $installStatePath -PathType Leaf) {
    $existing = Get-Content -LiteralPath $installStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($existing.schema_version -notin @(1, 2) -or $existing.root -ne $applicationRoot) { throw '安装记录与当前目录不匹配，请先从原目录卸载旧安装。' }
}
$null = New-Item -ItemType Directory -Path $stateDirectory -Force
$runArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launchPath + '" -Mode run -PythonExe "' + $PythonExe + '"'
$dashboardArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + $launchPath + '" -Mode dashboard -PythonExe "' + $PythonExe + '"'
$installedRunValue = '"' + $shellPath + '" ' + $runArguments
$runKey = [Microsoft.Win32.Registry]::CurrentUser.CreateSubKey($runSubKeyPath)
$temporaryShortcut = Join-Path $stateDirectory ('.nearroute-' + [guid]::NewGuid().ToString('N') + '.lnk')
$wscript = $null
$shortcut = $null
try {
    $wscript = New-Object -ComObject WScript.Shell
    $currentRun = $runKey.GetValue($runValueName, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
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
        if ((Test-Path -LiteralPath $shortcutPath -PathType Leaf) -and -not (Test-OwnedShortcut $shortcutPath $wscript)) { throw '桌面已有同名的其他快捷方式，已保留。请先重命名该文件。' }
        $previousShortcut = [ordered]@{ existed = $false; bytes_base64 = $null }
    }
    $shortcut = $wscript.CreateShortcut($temporaryShortcut)
    $shortcut.TargetPath = $shellPath
    $shortcut.Arguments = $dashboardArguments
    $shortcut.WorkingDirectory = $applicationRoot
    $shortcut.WindowStyle = 7
    $shortcut.Description = '近舟 NearRoute：线路探测、范围设置和自动优选'
    $shortcut.IconLocation = (Join-Path $env:SystemRoot 'System32\shell32.dll') + ',17'
    $shortcut.Save()
    $installState = [ordered]@{
        schema_version = 2; installed = $true; root = $applicationRoot
        installed_at = [DateTimeOffset]::Now.ToString('o'); python_exe = $PythonExe
        run_subkey = $runSubKeyPath; run_value_name = $runValueName
        installed_run_value = $installedRunValue; previous_run = $previousRun
        shortcut_path = $shortcutPath; shortcut_sha256 = (Get-FileHash -LiteralPath $temporaryShortcut -Algorithm SHA256).Hash
        previous_shortcut = $previousShortcut
    }
    Write-InstallState $installState
    $configPath = Join-Path $applicationRoot 'config.json'
    if (-not (Test-Path -LiteralPath $configPath)) { Copy-Item -LiteralPath (Join-Path $applicationRoot 'config.example.json') -Destination $configPath }
    $runKey.SetValue($runValueName, $installedRunValue, [Microsoft.Win32.RegistryValueKind]::String)
    Move-Item -LiteralPath $temporaryShortcut -Destination $shortcutPath -Force
    foreach ($legacyName in @('Codex线路优选.lnk', 'Codex Node Optimizer.lnk')) {
        $legacyPath = Join-Path $desktopDirectory $legacyName
        if (-not (Test-OwnedShortcut $legacyPath $wscript)) { continue }
        if ($null -ne $existing -and $existing.shortcut_path -eq $legacyPath -and $existing.shortcut_sha256) {
            if ((Get-FileHash -LiteralPath $legacyPath -Algorithm SHA256).Hash -ne $existing.shortcut_sha256) { Write-Warning "旧项目快捷方式已修改，已保留：$legacyName"; continue }
            if ($existing.previous_shortcut.existed) {
                [IO.File]::WriteAllBytes($legacyPath, [Convert]::FromBase64String($existing.previous_shortcut.bytes_base64))
                continue
            }
        }
        Remove-Item -LiteralPath $legacyPath -Force
    }
} finally {
    $runKey.Dispose()
    if ($null -ne $shortcut) { $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut) }
    if ($null -ne $wscript) { $null = [Runtime.InteropServices.Marshal]::FinalReleaseComObject($wscript) }
    if (Test-Path -LiteralPath $temporaryShortcut) { Remove-Item -LiteralPath $temporaryShortcut -Force }
}
if (-not $NoStart) { Start-Process -FilePath $shellPath -ArgumentList $runArguments -WorkingDirectory $applicationRoot -WindowStyle Hidden }
Write-Output '已安装近舟 NearRoute：登录时后台启动，桌面快捷方式可打开设置。'
