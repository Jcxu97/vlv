@echo off
REM Test local_vlm_openai_client against serve_gemma4_4bit (default port 18090). ASCII-only.
setlocal
cd /d "%~dp0"

if not exist "%~dp0venv_gemma4\Scripts\python.exe" (
  echo ERROR: venv_gemma4 not found. Run SERVE_GEMMA4_4BIT.bat first to create venv.
  pause
  exit /b 1
)

set "OPENAI_BASE_URL=http://127.0.0.1:18090/v1"
if "%~1"=="" (
  echo Usage: TEST_GEMMA4_VISION.bat ^<image.jpg^|^<video.mp4^>
  echo Requires SERVE_GEMMA4_4BIT.bat running ^(listening 18090^).
  exit /b 1
)

set "INPUT=%~1"
echo Input: %INPUT%
echo Base: %OPENAI_BASE_URL%
echo.

set "PYTHONPATH=%~dp0src"
echo %INPUT% | findstr /i "\.mp4 \.mkv \.webm \.mov \.avi" >nul
if %errorlevel%==0 (
  "%~dp0venv_gemma4\Scripts\python.exe" -m bilibili_vision.local_vlm_openai_client --base-url "%OPENAI_BASE_URL%" --model gemma-4-31b-4bit --video "%INPUT%" --at 1 --prompt "Briefly describe on-screen text and UI."
) else (
  "%~dp0venv_gemma4\Scripts\python.exe" -m bilibili_vision.local_vlm_openai_client --base-url "%OPENAI_BASE_URL%" --model gemma-4-31b-4bit --image "%INPUT%" --prompt "Briefly describe on-screen text and UI."
)
echo.
pause
