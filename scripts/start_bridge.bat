@echo off
chcp 65001 > nul
set PYTHONIOENCODING=utf-8
title DSPy Local PC Bridge
echo ========================================================
echo   Starting DSPy Cloud-to-Local PC Bridge...
echo ========================================================
cd /d "%~dp0"
call ..\.venv\Scripts\activate.bat
python local_bridge.py
pause
