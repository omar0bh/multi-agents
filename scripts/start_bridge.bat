@echo off
title DSPy Local PC Bridge
echo ========================================================
echo   Starting DSPy Cloud-to-Local PC Bridge...
echo ========================================================
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%%%%'\" | Where-Object CommandLine -match 'local_bridge\.py' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }" >nul 2>&1
"..\.venv\Scripts\python.exe" -u local_bridge.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Error occurred. Press any key to close.
    pause
)
