# install_autostart.ps1
# Registers the bridge autostart in Windows Task Scheduler
# Run this script ONCE as Administrator

$scriptDir  = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$psScript   = Join-Path $scriptDir "autostart_bridge.ps1"
$taskName   = "DSpyBridgeAutostart"

Write-Host "`n=== DSPy Bridge Auto-Start Installer ===" -ForegroundColor Cyan

# Check if running as admin
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
        [Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Write-Host "ERROR: Please run this script as Administrator!" -ForegroundColor Red
    Write-Host "Right-click PowerShell → Run as Administrator, then run:`n  $MyInvocation.MyCommand.Path" -ForegroundColor Yellow
    exit 1
}

# Remove old task if exists
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
    Write-Host "[OK] Removed old task." -ForegroundColor Green
}

# Action: run autostart_bridge.ps1 silently
$action = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$psScript`"" `
    -WorkingDirectory $projectDir

# Triggers:
#   1. At system startup (with 30s delay for network stack)
#   2. On THIS user's logon
$triggerStartup = New-ScheduledTaskTrigger -AtStartup
$triggerStartup.Delay = "PT30S"   # wait 30 seconds after startup

$triggerLogon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

# Settings
$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit (New-TimeSpan -Hours 0 -Minutes 5) `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

# Principal – run as current user (no password needed)
$principal = New-ScheduledTaskPrincipal `
    -UserId $env:USERNAME `
    -LogonType Interactive `
    -RunLevel Highest

# Register the task
Register-ScheduledTask `
    -TaskName   $taskName `
    -Action     $action `
    -Trigger    @($triggerStartup, $triggerLogon) `
    -Settings   $settings `
    -Principal  $principal `
    -Description "Auto-starts the DSPy local bridge when the PC boots or user logs in. Waits for internet before launching." `
    -Force | Out-Null

Write-Host "`n[OK] Task '$taskName' registered successfully!" -ForegroundColor Green
Write-Host ""
Write-Host "Triggers:" -ForegroundColor Yellow
Write-Host "  - At system startup (30s delay)"
Write-Host "  - At logon for user: $env:USERNAME"
Write-Host ""
Write-Host "The bridge will start automatically next time you boot or log in." -ForegroundColor Cyan

# Optional: run it now to test
$run = Read-Host "`nRun the task NOW to test? (y/n)"
if ($run -eq "y") {
    Start-ScheduledTask -TaskName $taskName
    Write-Host "[OK] Task started. Check bridge_autostart.log in project folder." -ForegroundColor Green
}
