param([string]$Python = 'python', [string]$DataHome, [switch]$SkipBrowsers)
$ErrorActionPreference = 'Stop'
$skillRoot = Split-Path -Parent $PSScriptRoot
$pythonExe = Join-Path $skillRoot '.venv\Scripts\python.exe'
$env:PYTHONUTF8 = '1'
if ($DataHome) {
    $env:LITERATURE_WORKFLOW_HOME = [IO.Path]::GetFullPath($DataHome)
    [IO.File]::WriteAllText((Join-Path $skillRoot 'runtime-home.txt'), $env:LITERATURE_WORKFLOW_HOME, (New-Object Text.UTF8Encoding($false)))
}
& $Python -c "import sys,struct; assert sys.platform == 'win32' and sys.version_info[:2] == (3,11) and struct.calcsize('P')==8, 'Windows x64 Python 3.11 is required'"
if ($LASTEXITCODE -ne 0) { throw '请安装 Windows x64 Python 3.11。' }
if (-not (Test-Path -LiteralPath $pythonExe)) {
    & $Python -m venv (Join-Path $skillRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '独立环境创建失败。' }
}
$wheelArgs = @(Get-ChildItem -LiteralPath (Join-Path $skillRoot 'wheels') -Filter '*.whl' | ForEach-Object {$_.FullName})
if ($wheelArgs.Count -ne 3) { throw '缺少三个集成组件，请重新下载完整 Release 包。' }
& $pythonExe -m pip install --disable-pip-version-check --index-url 'https://pypi.org/simple' -c (Join-Path $skillRoot 'constraints-windows.txt') @wheelArgs
if ($LASTEXITCODE -ne 0) { throw '依赖安装未完成，请检查 PyPI 网络后重试；不会静默替换固定版本。' }
& $pythonExe -m pip install --disable-pip-version-check --no-index --no-deps --force-reinstall @wheelArgs
if ($LASTEXITCODE -ne 0) { throw '集成组件安装失败。' }
if (-not $SkipBrowsers) {
    & $pythonExe -m literature_workflow browsers --install
    if ($LASTEXITCODE -ne 0) { throw '固定浏览器官方下载未完成，请根据输出重试 browsers --install；不使用镜像内核或升级替代。' }
}
& $pythonExe -m literature_workflow guide
if ($LASTEXITCODE -ne 0) { throw '配置引导未能启动。' }
Write-Host '安装完成。请按 SKILL.md 配置自己的学校与学术 API。'
