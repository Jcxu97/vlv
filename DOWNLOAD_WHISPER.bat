@echo off
cd /d "%~dp0"
if not exist "%~dp0python_embed\python.exe" (
  echo Missing python_embed\python.exe
  pause
  exit /b 1
)
echo Pre-download large-v3 and small into whisper-models. Internet required.
set "PYTHONPATH=%~dp0src"
"%~dp0python_embed\python.exe" -u -m bilibili_vision.download_whisper_models large-v3 small
if errorlevel 1 pause
exit /b %ERRORLEVEL%
