# Install the agent-memory transcript watcher as a per-user Scheduled Task (Windows).
# Runs scripts/watcher_windows.pyw with the repo venv's pythonw.exe at logon,
# restarts on failure, and starts it immediately.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_windows.ps1
#   powershell -ExecutionPolicy Bypass -File scripts\install_watcher_windows.ps1 -Uninstall
#
# Hub URL / key come from user env vars AGENT_MEMORY_URL / AGENT_MEMORY_SHARED_KEY
# (set them with [Environment]::SetEnvironmentVariable(..., 'User')); never put the key here.
param(
    [string]$TaskName = "AgentMemoryWatcher",
    [string]$Python = "",
    [switch]$Uninstall
)
$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot

if ($Uninstall) {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe'" |
        Where-Object { $_.CommandLine -like "*watcher_windows.pyw*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
    Write-Host "Removed $TaskName"
    return
}

if (-not $Python) {
    foreach ($venv in @(".venv", ".venv-recovery", "venv")) {
        $candidate = Join-Path $Root "$venv\Scripts\pythonw.exe"
        if (Test-Path $candidate) { $Python = $candidate; break }
    }
}
if (-not $Python) { throw "No venv pythonw.exe found under $Root; pass -Python" }

$Script = Join-Path $Root "scripts\watcher_windows.pyw"
$action = New-ScheduledTaskAction -Execute $Python -Argument "`"$Script`"" -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "agent-memory transcript watcher ($Root)" -Force | Out-Null
Start-ScheduledTask -TaskName $TaskName
Write-Host "Installed and started $TaskName -> $Python $Script"
Write-Host "Log: $env:USERPROFILE\.cache\agent-memory\watcher.log"
