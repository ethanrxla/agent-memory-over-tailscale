<#
.SYNOPSIS
    Expose an already-running hub (e.g. the Docker Desktop container) to the
    tailnet with Tailscale Serve.

.DESCRIPTION
    Use this when the hub already runs on this PC on 127.0.0.1:<port> -- the
    common case is the `docker compose up -d` container, which binds to
    127.0.0.1 on purpose. This does not start a hub; it only makes the existing
    one reachable from other tailnet devices (the Mac), and sets Tailscale to
    stay connected when nobody is logged in. The Serve config persists in
    tailscaled across reboots, so it is a one-time setup.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File scripts\windows\serve_docker_hub.ps1
#>
[CmdletBinding()]
param([int]$Port = 8787)

$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'common.ps1')

$tailscale = Find-Tailscale
if (-not $tailscale) { throw 'Tailscale is not installed.' }

$name = Get-TailnetName $tailscale
if (-not $name) { throw 'Tailscale is not logged in: run "tailscale up" first.' }

# Confirm something is actually listening on the port before serving it.
try {
    $null = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/health" -TimeoutSec 5
} catch {
    Write-Warning "Nothing healthy answered http://127.0.0.1:$Port/health."
    Write-Warning "Start the hub first (e.g. 'docker compose up -d' in the repo), then re-run."
    throw
}

& $tailscale set --unattended=true
if ($LASTEXITCODE -ne 0) {
    Write-Warning 'Could not enable unattended mode; turn on "Run unattended" in the Tailscale tray menu so the hub stays reachable when you are logged out.'
}

Invoke-Checked $tailscale @('serve', '--bg', "--http=$Port", "http://127.0.0.1:$Port")

$selfUrl = "http://${name}:$Port"
Write-Host ''
Write-Host "Hub on this PC is now served to the tailnet at $selfUrl"
Write-Host 'On every device, point clients at both hubs (first reachable wins):'
Write-Host "  AGENT_MEMORY_URL=http://pop-os.tailf11891.ts.net:8787,$selfUrl"
Write-Host ''
Write-Host 'Check it with: tailscale serve status'
