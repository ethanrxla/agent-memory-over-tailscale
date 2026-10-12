# Launched at boot by the "AgentMemoryHub" scheduled task (see install_hub.ps1).
# Loads hub.env, then runs the hub on localhost and keeps it running. Tailscale
# Serve, not this process, is what makes it reachable from other devices.
param([string]$ConfigDir = (Join-Path $env:ProgramData 'agent-memory'))

. (Join-Path $PSScriptRoot 'common.ps1')

Import-EnvFile (Join-Path $ConfigDir 'hub.env')
$port = $env:AGENT_MEMORY_PORT
if (-not $port) { $port = '8787' }

Set-Location $RepoDir
Invoke-Supervised -Exe $VenvPython `
    -Arguments @('-m', 'uvicorn', 'main:app', '--host', '127.0.0.1', '--port', $port) `
    -Log (Join-Path $ConfigDir 'hub.log') -Name "hub on 127.0.0.1:$port"
