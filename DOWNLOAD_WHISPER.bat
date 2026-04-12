@echo off
cd /d "%~dp0"
if not exist "%~dp0python_embed\python.exe" (
  echo Missing python_embed\python.exe
  pause
  exit /b 1
)
echo Pre-download large-v3 and small into whisper-models. Internet required.
"%~dp0python_embed\python.exe" -u "%~dp0download_whisper_models.py" large-v3 small
if errorlevel 1 pause
exit /b %ERRORLEVEL%
