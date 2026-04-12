@echo off
setlocal
cd /d "%~dp0"

if not exist "%~dp0venv_qwen35\Scripts\python.exe" (
  echo Run install_qwen35_venv.ps1 first.
  pause
  exit /b 1
)

set "OPENAI_BASE_URL=http://127.0.0.1:8000/v1"
if not defined QWEN35_MODEL set "QWEN35_MODEL=Qwen/Qwen3.5-27B-GPTQ-Int4"

echo Requires SERVE_QWEN35.bat running in another window.
echo.
if "%~1"=="" (
  echo Usage: TEST_QWEN35_VISION.bat ^<image.jpg^|^<video.mp4^>
  echo Example: TEST_QWEN35_VISION.bat "docs\screenshots\gui-extract.png"
  exit /b 1
)

set "INPUT=%~1"
echo Input: %INPUT%
echo.

echo %INPUT% | findstr /i "\.mp4 \.mkv \.webm \.mov \.avi" >nul
if %errorlevel%==0 (
  "%~dp0venv_qwen35\Scripts\python.exe" "%~dp0qwen35_vision_client.py" --video "%INPUT%" --at 1 --prompt "简要描述这一帧画面中的文字和界面。"
) else (
  "%~dp0venv_qwen35\Scripts\python.exe" "%~dp0qwen35_vision_client.py" --image "%INPUT%" --prompt "简要描述画面中的文字和界面元素。"
)
echo.
pause
