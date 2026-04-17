@echo off
setlocal
cd /d %~dp0..
set PYTHONPATH=%cd%\src
python tests\smoke_gui.py
exit /b %errorlevel%
