# Launched at logon by the "AgentMemoryWatcher" scheduled task (see
# install_watcher.ps1). Loads watcher.env and keeps the watcher running.
param([string]$ConfigDir = (Join-Path $env:LOCALAPPDATA 'agent-memory'))

. (Join-Path $PSScriptRoot 'common.ps1')

Import-EnvFile (Join-Path $ConfigDir 'watcher.env')

Set-Location $RepoDir
Invoke-Supervised -Exe $VenvPython `
    -Arguments @('-m', 'watcher.daemon', '--device', $env:COMPUTERNAME.ToLower()) `
    -Log (Join-Path $ConfigDir 'watcher.log') -Name 'watcher'
