# Shared helpers for the Windows installers. Dot-source, do not run.
# Kept to Windows PowerShell 5.1 syntax: that is what a fresh Windows ships.

$RepoDir = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
$VenvPython = Join-Path $RepoDir '.venv\Scripts\python.exe'
$DefaultHubs = 'http://pop-os.tailf11891.ts.net:8787'

function Assert-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = New-Object Security.Principal.WindowsPrincipal($id)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run this from an elevated PowerShell (Run as administrator).'
    }
}

function Invoke-Checked {
    # Run a native command and stop on a non-zero exit code.
    param([string]$Exe, [string[]]$Arguments)
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe $($Arguments -join ' ') failed with exit code $LASTEXITCODE" }
}

function Find-BasePython {
    $probe = @('-c', 'import sys; print(sys.executable); print(sys.version_info >= (3, 10))')
    $candidates = @(
        @{ Exe = 'py'; Prefix = @('-3') },
        @{ Exe = 'python'; Prefix = @() }
    )
    foreach ($candidate in $candidates) {
        $cmd = Get-Command $candidate.Exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $cmd) { continue }
        $probeArgs = @($candidate.Prefix) + $probe
        $out = @(& $cmd.Source @probeArgs 2>$null)
        if ($LASTEXITCODE -ne 0 -or -not $out) { continue }
        $exe = $out[0].Trim()
        # The Microsoft Store "python" is an app-execution alias that SYSTEM
        # (which runs the hub at boot) cannot launch.
        if ($exe -like '*\WindowsApps\*') { continue }
        if ($out[1].Trim() -ne 'True') { continue }
        return $exe
    }
    throw 'Python 3.10+ from python.org is required (the Microsoft Store build cannot run as a boot task). Install it with "winget install Python.Python.3.12", then re-run.'
}

function Initialize-Venv {
    if (-not (Test-Path $VenvPython)) {
        $base = Find-BasePython
        Write-Host "Creating virtualenv with $base"
        Invoke-Checked $base @('-m', 'venv', (Join-Path $RepoDir '.venv'))
    }
    Write-Host 'Installing Python dependencies'
    Invoke-Checked $VenvPython @('-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', '-r', (Join-Path $RepoDir 'requirements.txt'))
}

function Find-Tailscale {
    $cmd = Get-Command tailscale -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    $default = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path $default) { return $default }
    return $null
}

function Get-TailnetName {
    # This machine's MagicDNS name, e.g. desktop-abc.tailf11891.ts.net
    param([string]$Tailscale)
    if (-not $Tailscale) { return $null }
    try {
        $status = (& $Tailscale status --json) -join "`n" | ConvertFrom-Json
        return $status.Self.DNSName.TrimEnd('.')
    } catch {
        return $null
    }
}

function Read-EnvFile {
    param([string]$Path)
    $values = [ordered]@{}
    if (Test-Path $Path) {
        foreach ($line in Get-Content $Path) {
            if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)=(.*)$') { $values[$Matches[1]] = $Matches[2] }
        }
    }
    return $values
}

function Write-EnvFile {
    param([string]$Path, $Values)
    $lines = foreach ($key in $Values.Keys) { "$key=$($Values[$key])" }
    Set-Content -Path $Path -Value $lines -Encoding ASCII
}

function Import-EnvFile {
    param([string]$Path)
    $values = Read-EnvFile $Path
    foreach ($key in $values.Keys) {
        [Environment]::SetEnvironmentVariable($key, $values[$key], 'Process')
    }
}

function Join-HubList {
    # Comma-separated, de-duplicated, order preserved.
    param([string[]]$Urls)
    $seen = New-Object System.Collections.Generic.List[string]
    foreach ($group in $Urls) {
        foreach ($url in ("$group" -split '[,\s]+')) {
            $url = $url.Trim().TrimEnd('/')
            if ($url -and -not $seen.Contains($url)) { $seen.Add($url) }
        }
    }
    return ($seen -join ',')
}

function Invoke-Supervised {
    # Run a long-lived native command forever, appending its output to a log
    # and restarting it if it exits. Task Scheduler's own restart-on-failure
    # does not fire on a non-zero exit, so the loop lives here.
    param([string]$Exe, [string[]]$Arguments, [string]$Log, [string]$Name)
    $ErrorActionPreference = 'Continue'
    while ($true) {
        if ((Test-Path $Log) -and (Get-Item $Log).Length -gt 10MB) { Move-Item -Force $Log "$Log.1" }
        Add-Content -Path $Log -Value "[$(Get-Date -Format o)] starting $Name"
        & $Exe @Arguments 2>&1 | ForEach-Object { "$_" } | Add-Content -Path $Log
        Add-Content -Path $Log -Value "[$(Get-Date -Format o)] $Name exited with code $LASTEXITCODE; restarting in 15s"
        Start-Sleep -Seconds 15
    }
}
