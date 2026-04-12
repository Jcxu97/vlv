#Requires -Version 5.1
<#
  Separate venv for Qwen3.5 serving — do NOT mix into python_embed (faster-whisper stack).
  Default model: Qwen/Qwen3.5-27B-GPTQ-Int4 (official 4-bit; fits single RTX 5090 class GPU).
  Full BF16 Qwen/Qwen3.5-27B needs multi-GPU or CPU offload; use transformers serve --force-model if you have that setup.

  Usage (PowerShell, repo root):
    Set-ExecutionPolicy -Scope CurrentUser RemoteSigned -Force
    .\install_qwen35_venv.ps1
#>
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
Set-Location $root

$venv = Join-Path $root "venv_qwen35"
$embedPy = Join-Path $root "python_embed\python.exe"

# Embeddable Python often ships without venv — prefer Windows "py" launcher or system python.
function Test-PythonHasVenv([string]$exe) {
  try {
    & $exe -c "import venv" 2>$null | Out-Null
    return ($LASTEXITCODE -eq 0)
  } catch { return $false }
}

if (-not (Test-Path $venv)) {
  $created = $false
  if (Get-Command py -ErrorAction SilentlyContinue) {
    & py -3.11 -m venv $venv
    if ($LASTEXITCODE -eq 0) { $created = $true }
    if (-not $created) {
      & py -3.12 -m venv $venv
      if ($LASTEXITCODE -eq 0) { $created = $true }
    }
    if (-not $created) {
      & py -3 -m venv $venv
      if ($LASTEXITCODE -eq 0) { $created = $true }
    }
  }
  if (-not $created -and (Test-Path $embedPy) -and (Test-PythonHasVenv $embedPy)) {
    & $embedPy -m venv $venv
    if ($LASTEXITCODE -eq 0) { $created = $true }
  }
  if (-not $created -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -m venv $venv
    if ($LASTEXITCODE -eq 0) { $created = $true }
  }
  if (-not $created) {
    throw "Cannot create venv. Install Python 3.11+ from python.org and ensure 'py' or 'python' is on PATH (embed Python here has no 'venv' module)."
  }
}

$pip = Join-Path $venv "Scripts\pip.exe"
$python = Join-Path $venv "Scripts\python.exe"
if (-not (Test-Path $pip)) { throw "venv creation failed: $venv" }

Write-Host "Upgrading pip..."
& $python -m pip install -U pip wheel

# PyTorch: pick CUDA 12.4 wheels (works on many 50-series setups). For issues, see https://pytorch.org/
Write-Host "Installing PyTorch (CUDA 12.4 wheels)..."
& $python -m pip install --upgrade torch torchvision --index-url "https://download.pytorch.org/whl/cu124"

Write-Host "Installing transformers[serving] (Qwen3.5 needs recent transformers)..."
& $python -m pip install "transformers[serving] @ git+https://github.com/huggingface/transformers.git@main"
if ($LASTEXITCODE -ne 0) {
  Write-Host "Git install failed (network?). Falling back to PyPI transformers[serving]>=5.5 ..."
  & $python -m pip install "transformers[serving]>=5.5" uvicorn fastapi
}

Write-Host "Installing client + helpers..."
& $python -m pip install -U openai pillow accelerate protobuf requests

Write-Host ""
Write-Host "Done. Next:"
Write-Host "  1) SERVE_QWEN35.bat   (downloads weights on first run; ~20GB+ for GPTQ-Int4)"
Write-Host "  2) TEST_QWEN35_VISION.bat   (after server is up)"
Write-Host ""
Write-Host "Optional pre-download:"
Write-Host "  .\venv_qwen35\Scripts\huggingface-cli.exe download Qwen/Qwen3.5-27B-GPTQ-Int4"
