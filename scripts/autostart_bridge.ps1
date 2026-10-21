# autostart_bridge.ps1
# Waits for internet connectivity then starts the local bridge
# Designed to be called by Windows Task Scheduler on startup / network events

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$projectDir = Split-Path -Parent $scriptDir
$pythonw    = Join-Path $projectDir ".venv\Scripts\pythonw.exe"
$bridge     = Join-Path $projectDir "scripts\local_bridge.py"
$logFile    = Join-Path $projectDir "bridge_autostart.log"

function Log($msg) {
    $ts = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
    $line = "[$ts] $msg"
    Add-Content -Path $logFile -Value $line
    Write-Host $line
}

Log "=== autostart_bridge.ps1 triggered ==="

# ── 1. Wait for internet (max 3 minutes) ─────────────────────────────────────
$maxWait   = 180   # seconds
$waited    = 0
$connected = $false

while ($waited -lt $maxWait) {
    try {
        $ping = Test-Connection -ComputerName "8.8.8.8" -Count 1 -Quiet -ErrorAction Stop
        if ($ping) { $connected = $true; break }
    } catch {}
    Log "No internet yet, waiting 5s... ($waited/$maxWait)"
    Start-Sleep -Seconds 5
    $waited += 5
}

if (-not $connected) {
    Log "ERROR: No internet after $maxWait seconds. Bridge NOT started."
    exit 1
}

Log "Internet detected after ${waited}s. Proceeding..."

# ── 2. Kill any existing bridge process ──────────────────────────────────────
$existing = Get-CimInstance Win32_Process -Filter "Name LIKE 'python%'" |
            Where-Object { $_.CommandLine -match "local_bridge\.py" }

if ($existing) {
    foreach ($p in $existing) {
        Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
        Log "Killed old bridge PID $($p.ProcessId)"
    }
    Start-Sleep -Seconds 2
}

# ── 3. Start bridge silently in background ───────────────────────────────────
if (-not (Test-Path $pythonw)) {
    Log "ERROR: pythonw.exe not found at $pythonw"
    exit 1
}

if (-not (Test-Path $bridge)) {
    Log "ERROR: local_bridge.py not found at $bridge"
    exit 1
}

Start-Process -FilePath $pythonw `
              -ArgumentList "`"$bridge`"" `
              -WorkingDirectory $projectDir `
              -WindowStyle Hidden

Log "Bridge started successfully!"
exit 0
