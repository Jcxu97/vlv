@echo off
cd /d "%~dp0"
if exist "%~dp0python_embed\python.exe" (
  "%~dp0python_embed\python.exe" -u "%~dp0gui.py"
  if errorlevel 1 pause
  exit /b 0
)
if exist "%~dp0venv\Scripts\python.exe" (
  "%~dp0venv\Scripts\python.exe" -u "%~dp0gui.py"
  if errorlevel 1 pause
  exit /b 0
)
echo Missing python_embed or venv. Run prepare script or reinstall embed Python.
pause
exit /b 1
