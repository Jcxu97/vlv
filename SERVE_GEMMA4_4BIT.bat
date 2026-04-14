@echo off
REM ASCII-only: UTF-8 Chinese in .bat breaks cmd.exe on some systems (echo/chcp split errors).
setlocal EnableExtensions
cd /d "%~dp0"
REM UTF-8 code page: avoids PyTorch/subprocess reader threads crashing on GBK (CP936) vs UTF-8 mismatch.
chcp 65001 >nul 2>&1

if not exist "venv_gemma4\Scripts\python.exe" (
  echo Creating venv_gemma4...
  py -3.11 -m venv venv_gemma4 2>nul
  if errorlevel 1 py -3 -m venv venv_gemma4
  if not exist "venv_gemma4\Scripts\python.exe" (
    echo ERROR: Could not create venv. Install Python 3.11+ from python.org and retry.
    exit /b 1
  )
  call venv_gemma4\Scripts\python.exe -m pip install -U pip
  echo.
  echo Install CUDA PyTorch + torchvision from pytorch.org first, for example:
  echo   venv_gemma4\Scripts\pip install torch torchvision --index-url https://download.pytorch.org/whl/cu128
  echo Then install other dependencies:
  call venv_gemma4\Scripts\pip.exe install -r "%~dp0requirements-gemma4-4bit.txt"
  echo.
  echo If torch is missing, install it then run this script again.
)

set "MODEL_DIR=%~dp0models\Gemma-4-31B-it-abliterated"
if not exist "%MODEL_DIR%\config.json" (
  echo ERROR: Model not found. Expected: "%MODEL_DIR%\config.json"
  exit /b 1
)

echo.
echo If your model folder has only weights+tokenizer, the server may download processor files from HF
echo   google/gemma-4-31B-it on first run (needs network or Hugging Face cache). Pillow required.
echo.
echo Starting server. When ready, open: http://127.0.0.1:18090/health
echo Or run: set PYTHONPATH=src ^&^& venv_gemma4\Scripts\python.exe -m bilibili_vision.check_local_model
echo.

set "PYTHONPATH=%~dp0src"
venv_gemma4\Scripts\python.exe -u -m bilibili_vision.serve_gemma4_4bit --model "%MODEL_DIR%" --host 127.0.0.1 --port 18090 --listen-model-id gemma-4-31b-4bit --max-model-len 8192 --default-temperature 0 --default-top-p 0.82 --repetition-penalty 1.22 --no-repeat-ngram 6

set "EC=%ERRORLEVEL%"
endlocal & exit /b %EC%
