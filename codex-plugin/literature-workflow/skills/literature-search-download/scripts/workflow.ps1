$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $skillRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonExe)) { throw '请先运行 scripts/install.ps1 安装独立环境。' }
$env:PYTHONUTF8 = '1'
$homePointer = Join-Path $skillRoot 'runtime-home.txt'
if (-not $env:LITERATURE_WORKFLOW_HOME -and (Test-Path -LiteralPath $homePointer)) {
    $env:LITERATURE_WORKFLOW_HOME = [IO.File]::ReadAllText($homePointer).Trim()
}
& $pythonExe -u -m literature_workflow @args
exit $LASTEXITCODE
