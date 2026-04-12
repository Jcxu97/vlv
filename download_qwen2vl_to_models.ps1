#Requires -Version 5.1
# 将 Qwen2-VL-2B-Instruct 下载到 models/（与 whisper-models 类似，大文件不入 Git）
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$venv = Join-Path $root "venv_qwen35\Scripts\hf.exe"
$dest = Join-Path $root "models\Qwen2-VL-2B-Instruct"
if (-not (Test-Path $venv)) {
  throw "Missing $venv — run install_qwen35_venv.ps1 first."
}
New-Item -ItemType Directory -Force -Path $dest | Out-Null
& $venv download "Qwen/Qwen2-VL-2B-Instruct" --local-dir $dest
Write-Host "Done: $dest"
