# 打包成两个单文件 exe：命令行版 + 图形版
#   powershell -ExecutionPolicy Bypass -File .\build.ps1
$ErrorActionPreference = 'Stop'
Set-Location $PSScriptRoot

$python = (Get-Command python -ErrorAction SilentlyContinue)
if (-not $python) { throw "找不到 python，请先安装 Python 3.9+" }

& $python.Source -m pip install --disable-pip-version-check -q pyinstaller
if ($LASTEXITCODE -ne 0) { throw "安装 pyinstaller 失败" }

$common = @(
    '--noconfirm', '--clean', '--onefile',
    '--add-data', 'wxar\patterns.json;wxar',
    '--exclude-module', 'unittest',
    '--exclude-module', 'pydoc'
)

Write-Host '==> 构建命令行版'
& $python.Source -m PyInstaller @common --console --name WeChatAntiRecall entry_cli.py
if ($LASTEXITCODE -ne 0) { throw "构建 CLI 失败" }

Write-Host '==> 构建图形版'
& $python.Source -m PyInstaller @common --windowed --name WeChatAntiRecall-GUI entry_gui.py
if ($LASTEXITCODE -ne 0) { throw "构建 GUI 失败" }

Write-Host ''
Write-Host "完成：dist\WeChatAntiRecall.exe / dist\WeChatAntiRecall-GUI.exe"
