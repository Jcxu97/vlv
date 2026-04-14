@echo off
REM Check serve_gemma4_4bit /health (default listen port 18090; ASCII-only).
setlocal EnableExtensions
echo GET http://127.0.0.1:18090/health
echo.
powershell -NoProfile -Command "try { $r = Invoke-WebRequest -UseBasicParsing -Uri 'http://127.0.0.1:18090/health' -TimeoutSec 5; Write-Host $r.Content } catch { Write-Host 'NOT RUNNING or wrong app on 18090:' $_.Exception.Message }"
echo.
pause
endlocal
