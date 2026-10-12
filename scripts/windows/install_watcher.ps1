<#
.SYNOPSIS
    Run the transcript watcher on Windows at logon.

.DESCRIPTION
    The Windows counterpart of scripts/install_watcher.sh. Registers the
    "AgentMemoryWatcher" scheduled task for the current user (it reads that
    user's ~/.claude and ~/.codex transcripts), restarted if it exits.
    Settings live in %LOCALAPPDATA%\agent-memory\watcher.env.

.EXAMPLE
    # From an elevated PowerShell in the repo (needed to register a logon task):
    powershell -ExecutionPolicy Bypass -File scripts\windows\install_watcher.ps1 `
        -BackendUrl "http://pop-os.tailf11891.ts.net:8787,http://<this-pc>.tailf11891.ts.net:8787" -SharedKey <key>
#>
[CmdletBinding()]
param(
    # Comma-separated hubs in preference order; the first reachable one wins.
    [string]$BackendUrl = $env:AGENT_MEMORY_URL,
    [string]$SharedKey = $env:AGENT_MEMORY_SHARED_KEY,
    [string]$AgentId = "$($env:COMPUTERNAME.ToLower())-watcher",
    # Project path prefixes never to ingest.
    [string[]]$Deny = @(),
    [switch]$Backfill
)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')
Assert-Admin

$TaskName = 'AgentMemoryWatcher'
$configDir = Join-Path $env:LOCALAPPDATA 'agent-memory'
$envPath = Join-Path $configDir 'watcher.env'

Initialize-Venv
New-Item -ItemType Directory -Force -Path $configDir | Out-Null

$settings = Read-EnvFile $envPath
if ($BackendUrl) { $settings['AGENT_MEMORY_URL'] = Join-HubList @($BackendUrl) }
if (-not $settings['AGENT_MEMORY_URL']) {
    $settings['AGENT_MEMORY_URL'] = Join-HubList @('http://127.0.0.1:8787', $DefaultHubs)
}
if ($SharedKey) { $settings['AGENT_MEMORY_SHARED_KEY'] = $SharedKey }
$settings['AGENT_MEMORY_AGENT_ID'] = $AgentId
if ($Deny.Count) { $settings['AGENT_MEMORY_DENY'] = ($Deny -join ';') }
$settings['PYTHONUNBUFFERED'] = '1'
Write-EnvFile $envPath $settings

if ($Backfill) {
    Write-Host "Backfilling existing transcripts into $($settings['AGENT_MEMORY_URL']) ..."
    Import-EnvFile $envPath
    Push-Location $RepoDir
    try { Invoke-Checked $VenvPython @('-m', 'watcher.daemon', '--backfill') } finally { Pop-Location }
}

$user = [Security.Principal.WindowsIdentity]::GetCurrent().Name
Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
$runner = Join-Path $PSScriptRoot 'run_watcher.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument "-NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$runner`" -ConfigDir `"$configDir`"" `
    -WorkingDirectory $RepoDir
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
$taskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1)
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $taskSettings -Description 'Agent memory transcript watcher' -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName

Write-Host "Watcher task '$TaskName' registered (runs at logon for $user) and started."
Write-Host "Hubs: $($settings['AGENT_MEMORY_URL'])"
Write-Host "Logs: $(Join-Path $configDir 'watcher.log')"
