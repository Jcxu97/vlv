@echo off
cd /d "%~dp0"
if not exist "%~dp0add_tkinter_to_embed.ps1" (
  echo.
  echo [ERROR] Missing file: add_tkinter_to_embed.ps1
  echo Your zip is older than the tkinter fix. Do one of:
  echo   1^) Get an updated full folder from whoever packed it, or
  echo   2^) Copy add_tkinter_to_embed.ps1 into this folder from the project, then run this bat again.
  echo.
  pause
  exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0add_tkinter_to_embed.ps1" -ProjectRoot "%~dp0"
if errorlevel 1 pause
exit /b %ERRORLEVEL%
