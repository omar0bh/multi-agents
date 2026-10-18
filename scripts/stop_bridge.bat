@echo off
title Stop DSPy Local PC Bridge
echo Stopping any running DSPy Bridge background processes...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"Name LIKE 'python%%%%'\" | Where-Object CommandLine -match 'local_bridge\.py' | ForEach-Object { Stop-Process -Id $_.ProcessId -Force; Write-Host ('Stopped Bridge process: ' + $_.ProcessId) }"
echo Done! Bridge stopped cleanly.
pause
