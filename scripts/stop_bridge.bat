@echo off
title Stop DSPy Local PC Bridge
echo Stopping any running DSPy Bridge background processes...
taskkill /F /FI "WINDOWTITLE eq DSPy Local PC Bridge*" >nul 2>&1
wmic process where "commandline like '%%local_bridge.py%%'" call terminate >nul 2>&1
echo Done! Bridge stopped.
pause
