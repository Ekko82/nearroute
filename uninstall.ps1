param([switch]$NoRestore, [string]$PythonExe = '')

$ErrorActionPreference = 'Stop'
$applicationRoot = [IO.Path]::GetFullPath($PSScriptRoot)
$stateDirectory = Join-Path $applicationRoot 'state'
$installStatePath = Join-Path $stateDirectory 'install-state.json'
$optimizerPath = Join-Path $applicationRoot 'optimizer.py'
$installState = $null
if (Test-Path -LiteralPath $installStatePath -PathType Leaf) {
    $installState = Get-Content -LiteralPath $installStatePath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($installState.schema_version -notin @(1, 2) -or $installState.root -ne $applicationRoot) { throw '安装记录与当前目录不匹配，已保留启动设置。' }
}
try { $PythonExe = & (Join-Path $applicationRoot 'launch.ps1') -ResolveOnly -PythonExe $PythonExe }
catch { $PythonExe = ''; Write-Warning '没有可用 Python，将保留停止请求并继续撤销本项目启动设置。' }

function Invoke-Optimizer {
    param([string]$Command)
    if (-not $PythonExe -or -not (Test-Path -LiteralPath $optimizerPath -PathType Leaf)) { return $false }
    try {
        $arguments = @('-B', ('"' + $optimizerPath + '"'), $Command)
        $process = Start-Process -FilePath $PythonExe -ArgumentList $arguments -WorkingDirectory $applicationRoot -WindowStyle Hidden -Wait -PassThru
        return ($process.ExitCode -eq 0)
    } catch { return $false }
}

if (-not (Invoke-Optimizer 'stop')) {
    $null = New-Item -ItemType Directory -Path $stateDirectory -Force
    [IO.File]::WriteAllText((Join-Path $stateDirectory 'stop.request'), '', [Text.UTF8Encoding]::new($false))
    Write-Warning '已保留停止请求；后台会在可执行检查时退出。'
}
if (-not $NoRestore -and -not (Invoke-Optimizer 'restore')) { Write-Warning '原节点恢复未完成。控制器恢复在线后，可再次运行 optimizer.py restore。' }
if ($null -eq $installState -or $installState.installed -ne $true) { Write-Output '已请求停止后台。没有有效安装记录，未改动启动项或快捷方式。'; return }
$expectedSubKey = 'Software\Microsoft\Windows\CurrentVersion\Run'
if ($installState.run_subkey -ne $expectedSubKey -or $installState.run_value_name -ne 'CodexNodeOptimizer') { throw '安装记录的启动项不匹配，已保留。' }
$runKey = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($expectedSubKey, $true)
if ($null -ne $runKey) {
    try {
        $current = $runKey.GetValue('CodexNodeOptimizer', $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($current -eq $installState.installed_run_value) {
            if ($installState.previous_run.existed) {
                $kind = [Enum]::Parse([Microsoft.Win32.RegistryValueKind], [string]$installState.previous_run.kind)
                $value = $installState.previous_run.value
                switch ($kind.ToString()) {
                    'Binary' { $value = [byte[]]$value }
                    'MultiString' { $value = [string[]]$value }
                    'DWord' { $value = [int]$value }
                    'QWord' { $value = [long]$value }
                    default { $value = [string]$value }
                }
                $runKey.SetValue('CodexNodeOptimizer', $value, $kind)
            } else { $runKey.DeleteValue('CodexNodeOptimizer', $false) }
        } elseif ($null -ne $current) { Write-Warning '同名启动项已被修改，已保留。' }
    } finally { $runKey.Dispose() }
}
$shortcutPath = [IO.Path]::GetFullPath([string]$installState.shortcut_path)
if ([IO.Path]::GetFileName($shortcutPath) -notin @('近舟 NearRoute.lnk', 'Codex线路优选.lnk', 'Codex Node Optimizer.lnk')) { throw '安装记录的快捷方式名称不匹配，已保留。' }
if (Test-Path -LiteralPath $shortcutPath -PathType Leaf) {
    if ((Get-FileHash -LiteralPath $shortcutPath -Algorithm SHA256).Hash -eq $installState.shortcut_sha256) {
        if ($installState.previous_shortcut.existed) { [IO.File]::WriteAllBytes($shortcutPath, [Convert]::FromBase64String($installState.previous_shortcut.bytes_base64)) }
        else { Remove-Item -LiteralPath $shortcutPath -Force }
    } else { Write-Warning '项目快捷方式已被修改，已保留。' }
}
$installState.installed = $false
$installState | Add-Member -NotePropertyName 'uninstalled_at' -NotePropertyValue ([DateTimeOffset]::Now.ToString('o')) -Force
$temporary = Join-Path $stateDirectory ('.install-state-' + [guid]::NewGuid().ToString('N') + '.tmp')
try {
    [IO.File]::WriteAllText($temporary, ($installState | ConvertTo-Json -Depth 12), [Text.UTF8Encoding]::new($false))
    Move-Item -LiteralPath $temporary -Destination $installStatePath -Force
} finally { if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force } }
Write-Output '已撤销近舟 NearRoute 的登录启动项和快捷方式。源码、本机配置、记录和安装前备份已保留。'
