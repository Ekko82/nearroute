param(
    [ValidateSet('run', 'dashboard')]
    [string]$Mode = 'dashboard',
    [string]$PythonExe = '',
    [switch]$ResolveOnly
)

$ErrorActionPreference = 'Stop'
$applicationRoot = [IO.Path]::GetFullPath($PSScriptRoot)

function Test-NearRoutePython {
    param([string]$Executable)
    if (-not (Test-Path -LiteralPath $Executable -PathType Leaf)) { return $false }
    $validationExecutable = $Executable
    if ([IO.Path]::GetFileName($Executable) -ieq 'pythonw.exe') {
        $consoleSibling = Join-Path (Split-Path -Parent $Executable) 'python.exe'
        if (Test-Path -LiteralPath $consoleSibling -PathType Leaf) { $validationExecutable = $consoleSibling }
    }
    $process = $null
    try {
        $arguments = @('-B', '-I', '-c', '"import sys, tkinter; sys.exit(0 if sys.version_info >= (3, 10) else 20)"')
        $process = Start-Process -FilePath $validationExecutable -ArgumentList $arguments -WindowStyle Hidden -Wait -PassThru
        return ($process.ExitCode -eq 0)
    } catch { return $false }
    finally { if ($null -ne $process) { $process.Dispose() } }
}

function Resolve-NearRoutePython {
    param([string]$Requested)
    $candidates = [Collections.Generic.List[string]]::new()
    if ($Requested) {
        if (Test-Path -LiteralPath $Requested -PathType Leaf) {
            $candidates.Add([IO.Path]::GetFullPath((Resolve-Path -LiteralPath $Requested).Path))
        } else {
            $command = Get-Command -Name $Requested -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
            if ($null -ne $command) { $candidates.Add($command.Source) }
        }
        if ($candidates.Count -eq 0) { throw "找不到指定 Python：$Requested" }
    } else {
        $installPath = Join-Path $applicationRoot 'state\install-state.json'
        if (Test-Path -LiteralPath $installPath -PathType Leaf) {
            try {
                $saved = Get-Content -LiteralPath $installPath -Raw -Encoding UTF8 | ConvertFrom-Json
                if ($saved.root -eq $applicationRoot -and $saved.python_exe) { $candidates.Add([string]$saved.python_exe) }
            } catch { }
        }
        foreach ($name in @('pythonw.exe', 'python.exe', 'pythonw', 'python')) {
            foreach ($command in @(Get-Command -Name $name -CommandType Application -ErrorAction SilentlyContinue)) {
                if ($command.Source -notmatch '\\Microsoft\\WindowsApps\\') { $candidates.Add($command.Source) }
            }
        }
        $launcher = Get-Command -Name 'py' -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($null -ne $launcher) {
            try {
                $output = & $launcher.Source -3 -B -c 'import sys; print(sys.executable)' 2>$null
                if ($LASTEXITCODE -eq 0 -and $output) { $candidates.Add(([string]($output | Select-Object -Last 1)).Trim()) }
            } catch { }
        }
    }
    $seen = [Collections.Generic.HashSet[string]]::new([StringComparer]::OrdinalIgnoreCase)
    foreach ($candidate in $candidates) {
        if (-not $seen.Add($candidate)) { continue }
        if (Test-NearRoutePython $candidate) {
            if (-not $Requested) {
                $windowless = Join-Path (Split-Path -Parent $candidate) 'pythonw.exe'
                if (Test-NearRoutePython $windowless) { return [IO.Path]::GetFullPath($windowless) }
            }
            return [IO.Path]::GetFullPath($candidate)
        }
    }
    throw '需要带 tkinter 的 Python 3.10 或更新版本。请安装 Python，或用 -PythonExe 指定解释器。'
}

$resolvedPython = Resolve-NearRoutePython $PythonExe
if ($ResolveOnly) { Write-Output $resolvedPython; return }
$scriptName = if ($Mode -eq 'dashboard') { 'dashboard.py' } else { 'optimizer.py' }
$scriptPath = Join-Path $applicationRoot $scriptName
if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) { throw "找不到程序：$scriptPath" }
$arguments = @('-B', ('"' + $scriptPath + '"'))
if ($Mode -eq 'run') { $arguments += 'run' }
Start-Process -FilePath $resolvedPython -ArgumentList $arguments -WorkingDirectory $applicationRoot -WindowStyle Hidden
