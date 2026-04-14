@echo off
setlocal EnableExtensions
cd /d "%~dp0"
chcp 65001 >nul 2>&1

if not exist "venv_gemma4\Scripts\python.exe" (
  echo ERROR: venv_gemma4 not found. Run SERVE_GEMMA4_4BIT.bat once first, or use:
  echo   py -3 pull_gemma4_processor_local.py
  exit /b 1
)

set "PYTHONPATH=%~dp0src"
"%~dp0venv_gemma4\Scripts\python.exe" -m bilibili_vision.pull_gemma4_processor_local %*
exit /b %ERRORLEVEL%
