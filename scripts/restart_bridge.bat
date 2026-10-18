@echo off
title Restart DSPy Local PC Bridge
echo ========================================================
echo   Restarting DSPy Local PC Bridge...
echo ========================================================
cd /d "%~dp0"
echo [1/2] Terminating any existing bridge instances...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%%%%'\" | Where-Object CommandLine -match 'local_bridge\.py' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Stopped Bridge process: ' + $_.ProcessId) }"
ping 127.0.0.1 -n 2 >nul
echo.
echo [2/2] Starting new background bridge process...
start "" "%~dp0..\.venv\Scripts\pythonw.exe" "%~dp0local_bridge.py"
echo.
echo [OK] Bridge successfully restarted cleanly in the background!
pause
