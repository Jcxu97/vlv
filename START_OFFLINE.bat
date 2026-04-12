@echo off
REM 公司内网 / 无外网：禁止 Hugging Face 与 static-ffmpeg 联网，本地转写仅用 whisper-models/ + ffmpeg/
cd /d "%~dp0"
set "BILIBILI_OFFLINE=1"
set "HF_HUB_OFFLINE=1"
set "TRANSFORMERS_OFFLINE=1"
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
