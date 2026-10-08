<#
.SYNOPSIS
  Start the Keelson + Merxa stack (docker compose) with a readable status summary.

.DESCRIPTION
  1. Checks Docker, GEMINI_API_KEY (host env) and whether host port 80 is free.
  2. Runs scripts/init.ps1 when .env or secrets/ are missing/incomplete (idempotent).
  3. Removes host environment variables that would shadow .env values for this process
     only (docker compose gives the shell environment precedence over .env - e.g. a
     user-level LITELLM_MASTER_KEY left over from another project). GEMINI_API_KEY is
     intentionally taken from the host.
  4. docker compose up -d --build [services], then waits until every started service is
     healthy / running / completed and prints a status table plus the public URLs.

.PARAMETER Services
  Only start these services (and their dependencies), e.g. -Services caddy,keycloak.

.PARAMETER Infra
  Shortcut for the infrastructure services only:
  pg-core pg-litellm pg-merxa valkey keycloak kc-config litellm egress caddy.

.PARAMETER Obs
  Also enable the "obs" profile (Phoenix at http://traces.localhost).

.PARAMETER NoBuild
  Skip --build.

.PARAMETER TimeoutSec
  How long to wait for services to become healthy (default 420).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1
  powershell -ExecutionPolicy Bypass -File scripts\up.ps1 -Infra
#>
[CmdletBinding()]
param(
    [string[]]$Services = @(),
    [switch]$Infra,
    [switch]$Obs,
    [switch]$NoBuild,
    [int]$TimeoutSec = 420
)

# Native tools (docker) write progress/logs to stderr; with 'Stop' Windows PowerShell 5.1
# would turn that into terminating errors. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$envFile = Join-Path $root '.env'
$oneShotServices = @('kc-config', 'bootstrap')   # must reach 'exited 0'
$infraServices = @('pg-core', 'pg-litellm', 'pg-merxa', 'valkey', 'keycloak', 'kc-config', 'litellm', 'egress', 'caddy')

function Write-Step([string]$msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Warn([string]$msg) { Write-Host "WARN: $msg" -ForegroundColor Yellow }

# -- 1. prerequisites ------------------------------------------------------------
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw 'docker CLI not found - install/start Docker Desktop.' }
& docker info --format '{{.ServerVersion}}' *> $null
if ($LASTEXITCODE -ne 0) { throw 'Docker engine is not reachable - start Docker Desktop.' }

if (-not $env:GEMINI_API_KEY) {
    $userKey = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'User')
    if ($userKey) { $env:GEMINI_API_KEY = $userKey }
    else { throw 'GEMINI_API_KEY is not set. Set it in your user environment (never in .env), e.g. setx GEMINI_API_KEY <key>, then open a new terminal.' }
}

$listener = Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
if ($listener) {
    $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
    $pname = if ($proc) { $proc.ProcessName } else { "pid $($listener.OwningProcess)" }
    if ($pname -notmatch '^(com\.docker\.backend|wslrelay|vpnkit|docker)') {
        Write-Warn "Port 80 is already in use by '$pname'. Caddy needs it (see docs/infra.md, 'Port 80 busy')."
    }
}

# -- 2. init (idempotent) ------------------------------------------------------------
Write-Step 'Checking .env and secrets/'
& (Join-Path $PSScriptRoot 'init.ps1') -Check *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Step 'Generating missing .env values / secrets (existing values are kept)'
    & (Join-Path $PSScriptRoot 'init.ps1')
    if ($LASTEXITCODE -ne 0) { throw 'init failed' }
}

# -- 3. host env vars shadowing .env -------------------------------------------------
foreach ($line in Get-Content -LiteralPath $envFile) {
    if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
        $name = $Matches[1]
        if ($name -eq 'GEMINI_API_KEY') { continue }
        if (Test-Path "Env:\$name") {
            Write-Warn "Host environment variable $name shadows .env - ignoring it for this run."
            Remove-Item "Env:\$name"
        }
    }
}

# -- 4. compose up ---------------------------------------------------------------------
# With 'powershell -File', "-Services caddy,keycloak" arrives as ONE string: split it.
$Services = @($Services | ForEach-Object { $_ -split '[,\s]+' } | Where-Object { $_ })
if ($Infra) { $Services = $infraServices + $Services }
$composeArgs = @('compose')
if ($Obs) { $composeArgs += @('--profile', 'obs') }
$upArgs = $composeArgs + @('up', '-d')
if (-not $NoBuild) { $upArgs += '--build' }
$upArgs += $Services

Push-Location $root
try {
    Write-Step ("docker " + ($upArgs -join ' '))
    & docker @upArgs
    if ($LASTEXITCODE -ne 0) { throw "docker compose up failed (exit $LASTEXITCODE)" }

    function Get-ComposeState {
        $raw = & docker @composeArgs ps -a --format json
        $items = @()
        foreach ($l in $raw) {
            if (-not $l) { continue }
            $parsed = $l | ConvertFrom-Json
            $items += @($parsed)
        }
        return $items
    }

    Write-Step "Waiting up to $TimeoutSec s for services"
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    do {
        $state = Get-ComposeState
        $pending = @($state | Where-Object {
                ($_.State -eq 'running' -and $_.Health -and $_.Health -ne 'healthy') -or
                ($_.State -eq 'running' -and $oneShotServices -contains $_.Service) -or
                ($_.State -in @('created', 'restarting'))
            })
        $failed = @($state | Where-Object {
                ($_.State -eq 'exited' -and $_.ExitCode -ne 0) -or $_.Health -eq 'unhealthy'
            })
        if ($pending.Count -eq 0 -or $failed.Count -gt 0) { break }
        Start-Sleep -Seconds 5
    } while ((Get-Date) -lt $deadline)

    $rows = foreach ($s in ($state | Sort-Object Service)) {
        $status = if ($s.State -eq 'exited') {
            if ($s.ExitCode -eq 0) { 'completed' } else { "FAILED (exit $($s.ExitCode))" }
        } elseif ($s.Health) { $s.Health } else { $s.State }
        [pscustomobject]@{ Service = $s.Service; Status = $status; Ports = $s.Ports }
    }
    $rows | Format-Table -AutoSize | Out-String | Write-Host

    Write-Host 'Public URLs (HTTP, via Caddy on port 80):'
    Write-Host '  http://keelson.localhost   Keelson (engine UI + /api)'
    Write-Host '  http://merxa.localhost     Merxa Retail Suite (mock ERP)'
    Write-Host '  http://auth.localhost      Keycloak (realms keelson, merxa)'
    Write-Host '  http://gateway.localhost   LiteLLM console (platform-admin)'
    if ($Obs) { Write-Host '  http://traces.localhost    Phoenix traces (platform-admin)' }
    Write-Host 'Browsers resolve *.localhost automatically; for Node/Python on the host run scripts\hosts.ps1.'

    if ($failed.Count -gt 0) {
        Write-Host ("Failed: " + (($failed | ForEach-Object { $_.Service }) -join ', ') + "  -> docker compose logs <service>") -ForegroundColor Red
        exit 1
    }
    if ($pending.Count -gt 0) {
        Write-Warn ("Still starting after $TimeoutSec s: " + (($pending | ForEach-Object { $_.Service }) -join ', '))
        exit 2
    }
    Write-Host 'Stack is up.' -ForegroundColor Green
} finally {
    Pop-Location
}
