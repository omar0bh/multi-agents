@echo off
title Install DSPy Bridge Autostart
echo ========================================================
echo   Setting up DSPy Bridge to start automatically on boot
echo ========================================================

set "STARTUP_FOLDER=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT_PATH=%STARTUP_FOLDER%\DSPy_Local_Bridge.lnk"
set "TARGET_VBS=C:\dspy-multi-agents\scripts\launch_bridge_silent.vbs"

powershell -Command "$WshShell = New-Object -ComObject WScript.Shell; $Shortcut = $WshShell.CreateShortcut('%SHORTCUT_PATH%'); $Shortcut.TargetPath = '%TARGET_VBS%'; $Shortcut.WorkingDirectory = 'C:\dspy-multi-agents\scripts'; $Shortcut.Save()"

if %ERRORLEVEL% EQU 0 (
    echo.
    echo [OK] DSPy Bridge installed to Windows Startup successfully!
    echo [*] From now on, whenever you turn on your PC or connect to Wi-Fi,
    echo     the bridge will start SILENTLY in the background automatically!
    echo.
) else (
    echo.
    echo [!] Failed to create startup shortcut.
)
pause
