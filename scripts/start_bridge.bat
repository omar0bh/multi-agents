@echo off
title DSPy Local PC Bridge
echo ========================================================
echo   Starting DSPy Cloud-to-Local PC Bridge...
echo ========================================================
cd /d "%~dp0"
"..\.venv\Scripts\python.exe" -u local_bridge.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo [!] Error occurred. Press any key to close.
    pause
)
