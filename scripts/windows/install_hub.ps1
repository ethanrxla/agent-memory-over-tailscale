<#
.SYNOPSIS
    Run the agent-memory hub natively on Windows, start it at boot, and serve it
    to the tailnet.

.DESCRIPTION
    pop-os is the usual hub, but it is a dual boot: while the PC is in Windows
    no hub is on the tailnet and every device loses memory. This makes the
    Windows side a hub too:

      * a virtualenv in the repo with the hub's dependencies
      * %ProgramData%\agent-memory\hub.env  (settings and keys, admin-only ACL)
      * scheduled task "AgentMemoryHub", run as SYSTEM at boot, before anyone
        logs in, restarted if it exits
      * Tailscale set to run unattended, and Tailscale Serve proxying
        http://<this-pc>.<tailnet>.ts.net:8787 to the hub on 127.0.0.1:8787,
        the same shape as pop-os

    Safe to re-run: settings already in hub.env are kept unless overridden.

.EXAMPLE
    # From an elevated PowerShell in the repo:
    powershell -ExecutionPolicy Bypass -File scripts\windows\install_hub.ps1 -SharedKey <key>
#>
[CmdletBinding()]
param(
    [string]$SharedKey = $env:AGENT_MEMORY_SHARED_KEY,
    [string]$NvidiaApiKey = $env:NVIDIA_API_KEY,
    [int]$Port = 8787,
    [string]$ConfigDir = (Join-Path $env:ProgramData 'agent-memory'),
    # Other hubs devices should try, in preference order (pop-os by default).
    [string]$OtherHubs,
    # Keep the PC awake on AC power so the hub stays reachable.
    [switch]$DisableSleep,
    [switch]$SkipTailscale
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Admin

$TaskName = 'AgentMemoryHub'
$envPath = Join-Path $ConfigDir 'hub.env'
$dataDir = Join-Path $ConfigDir 'data'

# 1. Python environment ------------------------------------------------------
Initialize-Venv

# 2. Tailnet identity --------------------------------------------------------
$tailscale = $null
$selfUrl = $null
if (-not $SkipTailscale) {
    $tailscale = Find-Tailscale
    if (-not $tailscale) { throw 'Tailscale is not installed (or pass -SkipTailscale).' }
    $name = Get-TailnetName $tailscale
    if (-not $name) { throw 'Tailscale is not logged in: run "tailscale up" first.' }
    $selfUrl = "http://${name}:$Port"
}

# 3. Settings ----------------------------------------------------------------
New-Item -ItemType Directory -Force -Path $dataDir | Out-Null
$settings = Read-EnvFile $envPath
if (-not $OtherHubs) {
    $OtherHubs = $DefaultHubs
    if ($settings['AGENT_MEMORY_PUBLIC_URL']) { $OtherHubs = $settings['AGENT_MEMORY_PUBLIC_URL'] }
}
$settings['AGENT_MEMORY_DB_PATH'] = Join-Path $dataDir 'agent_memory.db'
$settings['AGENT_MEMORY_PORT'] = "$Port"
$settings['PYTHONUNBUFFERED'] = '1'
if ($SharedKey) { $settings['AGENT_MEMORY_SHARED_KEY'] = $SharedKey }
if ($NvidiaApiKey) { $settings['NVIDIA_API_KEY'] = $NvidiaApiKey }
# The list served to devices by /skill/install.sh: pop-os first, then this PC.
$hubList = Join-HubList @($OtherHubs, $selfUrl)
$settings['AGENT_MEMORY_PUBLIC_URL'] = $hubList
Write-EnvFile $envPath $settings
# hub.env holds keys: SYSTEM and Administrators only (SIDs, so any locale works).
Invoke-Checked icacls.exe @($ConfigDir, '/inheritance:r', '/grant:r', '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F') | Out-Null

# 4. Boot task ---------------------------------------------------------------
$existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if ($existing) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    # Stopping the task does not always stop its child python.
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like '*uvicorn*main:app*' -and $_.ExecutablePath -eq $VenvPython } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    Start-Sleep -Seconds 2
}

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $owner = (Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue).ProcessName
    throw "Port $Port is already in use by '$owner' (pid $($listener.OwningProcess)). Stop it (e.g. a Docker copy of the hub) or pass -Port."
}

$runner = Join-Path $PSScriptRoot 'run_hub.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -ConfigDir `"$ConfigDir`"" `
    -WorkingDirectory $RepoDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId 'S-1-5-18' -LogonType ServiceAccount -RunLevel Highest
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $taskSettings -Description 'Agent memory hub (agent-memory-over-tailscale)' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Scheduled task '$TaskName' registered (runs at boot as SYSTEM) and started"

# 5. Wait for it -------------------------------------------------------------
$headers = @{}
if ($settings['AGENT_MEMORY_SHARED_KEY']) { $headers['X-Shared-Key'] = $settings['AGENT_MEMORY_SHARED_KEY'] }
$healthy = $false
for ($i = 0; $i -lt 45 -and -not $healthy; $i++) {
    Start-Sleep -Seconds 2
    try {
        $healthy = (Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -Headers $headers -TimeoutSec 3).status -eq 'ok'
    } catch { }
}
if (-not $healthy) {
    Write-Warning "Hub did not answer on 127.0.0.1:$Port. Last log lines:"
    Get-Content (Join-Path $ConfigDir 'hub.log') -Tail 30 -ErrorAction SilentlyContinue
    throw 'Hub failed to start.'
}
Write-Host "Hub healthy on http://127.0.0.1:$Port"

# 6. Tailnet -----------------------------------------------------------------
if ($tailscale) {
    # By default Windows Tailscale disconnects when nobody is logged in, which
    # would hide a boot-time hub until someone signs in.
    & $tailscale set --unattended=true
    if ($LASTEXITCODE -ne 0) {
        Write-Warning 'Could not enable unattended mode; enable "Run unattended" in the Tailscale tray menu.'
    }
    # Serve config persists in tailscaled's state across reboots.
    Invoke-Checked $tailscale @('serve', '--bg', "--http=$Port", "http://127.0.0.1:$Port")
    Write-Host "Serving to the tailnet at $selfUrl"
}

if ($DisableSleep) {
    powercfg.exe /change standby-timeout-ac 0
    powercfg.exe /change hibernate-timeout-ac 0
    Write-Host 'Sleep and hibernate disabled on AC power'
}

Write-Host ''
Write-Host 'Done. On every device, point clients at all hubs (first reachable wins):'
Write-Host "  AGENT_MEMORY_URL=$hubList"
Write-Host "Logs: $(Join-Path $ConfigDir 'hub.log')"
