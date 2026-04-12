@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0venv_qwen35\Scripts\python.exe" (
  echo [ERROR] Missing venv_qwen35. Run:  powershell -ExecutionPolicy Bypass -File ".\install_qwen35_venv.ps1"
  pause
  exit /b 1
)

REM Optional: keep HF cache inside project (easy to delete / move)
if not defined HF_HOME set "HF_HOME=%~dp0.hf_cache"
if not defined HF_HUB_CACHE set "HF_HUB_CACHE=%HF_HOME%\hub"

REM Default: official 4-bit checkpoint (single-GPU friendly). Override before start:
REM   set QWEN35_MODEL=Qwen/Qwen3.5-27B
if not defined QWEN35_MODEL set "QWEN35_MODEL=Qwen/Qwen3.5-27B-GPTQ-Int4"

echo Model: %QWEN35_MODEL%
echo API:   http://127.0.0.1:8000/v1  (OpenAI compatible)
echo Stop:  Ctrl+C
echo.

REM transformers 5.x: model id is a positional arg, not --force-model
"%~dp0venv_qwen35\Scripts\transformers.exe" serve "%QWEN35_MODEL%" ^
  --port 8000 ^
  --host 0.0.0.0 ^
  --continuous-batching

if errorlevel 1 (
  echo.
  echo If "transformers.exe" failed, try:
  echo   "%~dp0venv_qwen35\Scripts\transformers.exe" serve --help
  pause
)
exit /b %ERRORLEVEL%
