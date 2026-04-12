@echo off
REM Retry Playwright Chromium into pw-browsers if network install failed.
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%~dp0pw-browsers"
if not exist "%PLAYWRIGHT_BROWSERS_PATH%" mkdir "%PLAYWRIGHT_BROWSERS_PATH%"
if not exist "%~dp0python_embed\python.exe" (
  echo Missing python_embed\python.exe. Run prepare script first.
  pause
  exit /b 1
)
"%~dp0python_embed\python.exe" -m playwright install chromium
echo.
pause
