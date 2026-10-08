<#
.SYNOPSIS
  Start the Keelson + Merxa stack (docker compose) with verbose, timestamped progress.

.DESCRIPTION
  1. Prerequisites: Docker engine / compose versions and memory, GEMINI_API_KEY (host env;
     only "set" or "not set" is printed), the owner of host port 80, and the host environment
     variables that shadow .env values.
  2. Runs scripts/init.ps1 when .env or secrets/ are missing/incomplete (idempotent).
  3. Removes host environment variables that would shadow .env values for this process
     only (docker compose gives the shell environment precedence over .env - e.g. a
     user-level LITELLM_MASTER_KEY left over from another project). GEMINI_API_KEY is
     intentionally taken from the host.
  4. docker compose up -d --build [services] with plain progress output, so build steps
     stream line by line.
  5. Waits until every started service is healthy / running / completed. Every poll (5 s)
     prints a progress line; every state change gets its own line; every ~30 s the last log
     lines of each still-starting service are shown; a failed service gets its last 40 log
     lines printed automatically.
  6. Prints a status table, the total time and the public URLs.

  Every line is prefixed with "[HH:mm:ss +elapsed] [step/6]" and also written to
  logs/up-<yyyyMMdd-HHmmss>.log under the repository root.

  Exit codes: 0 = stack up, 1 = a service failed (or a prerequisite / compose error),
  2 = services still starting after -TimeoutSec.

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
$oneShotServices = @('kc-config', 'bootstrap', 'signal-hub-bootstrap')   # must reach 'exited 0'
$infraServices = @('pg-core', 'pg-litellm', 'pg-merxa', 'valkey', 'keycloak', 'kc-config', 'litellm', 'egress', 'caddy')
$pollSec = 5          # wait-loop poll interval
$tailEverySec = 30    # show the last log lines of still-starting services this often

# -- logging: "[HH:mm:ss +elapsed] [n/6] message" to the console and logs/up-<ts>.log ----------
$inv = [Globalization.CultureInfo]::InvariantCulture
$script:t0 = Get-Date
$script:stepNo = 0
$script:stepTotal = 6
$script:log = $null
$logFile = Join-Path (Join-Path $root 'logs') ('up-{0}.log' -f $script:t0.ToString('yyyyMMdd-HHmmss', $inv))

function Format-Elapsed([double]$Seconds) {
    $s = [int][Math]::Floor($Seconds)
    if ($s -lt 60) { return "${s}s" }
    return ('{0}m{1:00}s' -f [Math]::Floor($s / 60), ($s % 60))
}

function Write-Log([string]$Message = '', [string]$Color = '') {
    $now = Get-Date
    $tag = if ($script:stepNo -gt 0) { " [$($script:stepNo)/$($script:stepTotal)]" } else { '' }
    $line = '[{0} +{1}]{2} {3}' -f $now.ToString('HH:mm:ss', $inv), (Format-Elapsed ($now - $script:t0).TotalSeconds), $tag, $Message
    if ($Color) { Write-Host $line -ForegroundColor $Color } else { Write-Host $line }
    if ($script:log) { $script:log.WriteLine($line) }
}
function Write-Step([string]$msg) { $script:stepNo++; Write-Log "==> $msg" 'Cyan' }
function Write-Warn([string]$msg) { Write-Log "WARN: $msg" 'Yellow' }
function Write-Err([string]$msg) { Write-Log "ERROR: $msg" 'Red' }

# Native output line (string or stderr ErrorRecord) -> plain text without ANSI colour codes.
function ConvertTo-PlainText($item) {
    $text = if ($item -is [System.Management.Automation.ErrorRecord]) { $item.Exception.Message } else { "$item" }
    return ($text -replace '\x1b\[[0-9;?]*[ -/]*[@-~]', '').TrimEnd("`r", "`n")
}

# Run docker with the given arguments, log stdout + stderr line by line; returns the exit code.
function Invoke-Docker([string[]]$ArgList, [string]$Indent = '    ', [string]$Color = '') {
    & docker @ArgList 2>&1 | ForEach-Object { Write-Log ($Indent + (ConvertTo-PlainText $_)) $Color }
    return $LASTEXITCODE
}

# Last log lines of a service (docker compose logs), optionally trimmed to $MaxLen characters.
function Show-ServiceLogs([string]$Service, [int]$Tail, [int]$MaxLen = 0, [string]$Indent = '      ') {
    $lines = @(& docker @composeArgs logs --tail $Tail --no-log-prefix --no-color $Service 2>&1 |
            ForEach-Object { ConvertTo-PlainText $_ } | Where-Object { $_.Trim() })
    if ($lines.Count -eq 0) { Write-Log "${Indent}(no log output yet)" 'DarkGray'; return }
    foreach ($l in $lines) {
        if ($MaxLen -gt 0 -and $l.Length -gt $MaxLen) { $l = $l.Substring(0, $MaxLen - 3) + '...' }
        Write-Log ($Indent + $l) 'DarkGray'
    }
}

# Host environment variables that would shadow a .env key (GEMINI_API_KEY is meant to).
function Get-ShadowedEnvNames {
    $names = @()
    if (-not (Test-Path -LiteralPath $envFile)) { return $names }
    foreach ($line in Get-Content -LiteralPath $envFile) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=') {
            $name = $Matches[1]
            if ($name -eq 'GEMINI_API_KEY') { continue }
            if (Test-Path "Env:\$name") { $names += $name }
        }
    }
    return $names
}

function Get-ComposeState {
    $items = @()
    foreach ($l in (& docker @composeArgs ps -a --format json 2>$null)) {
        if (-not $l) { continue }
        try { $parsed = $l | ConvertFrom-Json -ErrorAction Stop } catch { Write-Warn "unparsable 'docker compose ps' line: $l"; continue }
        $items += @($parsed)
    }
    return $items
}

function Test-SvcFailed($s) { ($s.State -eq 'exited' -and $s.ExitCode -ne 0) -or $s.Health -eq 'unhealthy' }
function Test-SvcPending($s) {
    ($s.State -eq 'running' -and $s.Health -and $s.Health -ne 'healthy') -or
    ($s.State -eq 'running' -and $oneShotServices -contains $s.Service) -or
    ($s.State -in @('created', 'restarting'))
}
# Fine-grained status used for the state-change lines.
function Get-SvcStatus($s) {
    if ($s.State -eq 'exited') { if ($s.ExitCode -eq 0) { return 'completed' } else { return "exited ($($s.ExitCode))" } }
    if ($s.State -eq 'running') {
        if ($s.Health) { return $s.Health }
        if ($oneShotServices -contains $s.Service) { return 'running (job)' }
        return 'running'
    }
    return $s.State
}
# Bucket used for the progress-line counters.
function Get-SvcCategory($s) {
    if (Test-SvcFailed $s) { return 'failed' }
    if (Test-SvcPending $s) { return 'starting' }
    if ($s.State -eq 'exited') { return 'completed' }
    if ($s.State -eq 'running' -and $s.Health -eq 'healthy') { return 'healthy' }
    if ($s.State -eq 'running') { return 'running' }
    return 'other'
}

$pushed = $false
$prevConsoleEncoding = $null
try {
    New-Item -ItemType Directory -Force -Path (Split-Path $logFile) | Out-Null
    $script:log = New-Object System.IO.StreamWriter($logFile, $true, (New-Object System.Text.UTF8Encoding($false)))
    $script:log.AutoFlush = $true
    # Decode docker / container output as UTF-8 (restored in 'finally').
    try { $prevConsoleEncoding = [Console]::OutputEncoding; [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { $prevConsoleEncoding = $null }

    Write-Log "Keelson stack start - log file: $logFile" 'Cyan'
    Write-Log ("Options: Services=[{0}] Infra={1} Obs={2} NoBuild={3} TimeoutSec={4}" -f ($Services -join ','), [bool]$Infra, [bool]$Obs, [bool]$NoBuild, $TimeoutSec)

    # -- 1. prerequisites ------------------------------------------------------------
    Write-Step 'Checking prerequisites (Docker, GEMINI_API_KEY, port 80, host environment)'
    $dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
    if (-not $dockerCmd) { throw 'docker CLI not found - install/start Docker Desktop.' }
    Write-Log "docker CLI      : $($dockerCmd.Source)"
    $info = @(& docker info --format '{{.ServerVersion}}|{{.MemTotal}}|{{.NCPU}}|{{.OperatingSystem}}' 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'Docker engine is not reachable - start Docker Desktop.' }
    $parts = "$($info[0])".Split('|')
    $memGiB = if ($parts.Count -gt 1 -and $parts[1] -match '^\d+$') { ([double]$parts[1] / 1GB).ToString('0.0', $inv) + ' GiB' } else { '?' }
    $clientVersion = @(& docker version --format '{{.Client.Version}}' 2>$null)
    Write-Log ("Docker engine   : server {0}, client {1} ({2})" -f $parts[0], "$($clientVersion[0])", $(if ($parts.Count -gt 3) { $parts[3] } else { '?' }))
    Write-Log ("Docker resources: {0} CPUs, {1} memory" -f $(if ($parts.Count -gt 2) { $parts[2] } else { '?' }), $memGiB)
    $composeVersion = @(& docker compose version --short 2>$null)
    if ($LASTEXITCODE -ne 0) { throw 'docker compose (v2 plugin) not available - update Docker Desktop.' }
    Write-Log "Docker compose  : $($composeVersion[0])"

    $geminiSource = 'process environment'
    if (-not $env:GEMINI_API_KEY) {
        $userKey = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'User')
        if ($userKey) { $env:GEMINI_API_KEY = $userKey; $geminiSource = 'user environment, loaded for this run' }
        else {
            Write-Log 'GEMINI_API_KEY  : no'
            throw 'GEMINI_API_KEY is not set. Set it in your user environment (never in .env), e.g. setx GEMINI_API_KEY <key>, then open a new terminal.'
        }
    }
    Write-Log "GEMINI_API_KEY  : yes ($geminiSource; value not shown)"

    $listener = Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($listener) {
        $proc = Get-Process -Id $listener.OwningProcess -ErrorAction SilentlyContinue
        $pname = if ($proc) { $proc.ProcessName } else { "pid $($listener.OwningProcess)" }
        if ($pname -notmatch '^(com\.docker\.backend|wslrelay|vpnkit|docker)') {
            Write-Log "Port 80         : in use by '$pname' (pid $($listener.OwningProcess))"
            Write-Warn "Port 80 is already in use by '$pname'. Caddy needs it (see docs/infra.md, 'Port 80 busy')."
        } else {
            Write-Log "Port 80         : held by '$pname' (pid $($listener.OwningProcess)) - Docker, OK"
        }
    } else {
        Write-Log 'Port 80         : free (Caddy will bind it)'
    }

    if (Test-Path -LiteralPath $envFile) {
        $shadowed = @(Get-ShadowedEnvNames)
        if ($shadowed.Count -gt 0) { Write-Log (".env shadowed by: {0} (host env; ignored for this run in step 3)" -f ($shadowed -join ', ')) 'Yellow' }
        else { Write-Log '.env shadowing  : none (no host variable overrides a .env key)' }
    } else {
        Write-Log '.env shadowing  : .env not found yet (created in step 2)'
    }

    # -- 2. init (idempotent) ------------------------------------------------------------
    Write-Step 'Checking .env and secrets/ (scripts\init.ps1 -Check)'
    $initScript = Join-Path $PSScriptRoot 'init.ps1'
    # init.ps1 logs "[HH:mm:ss +elapsed] [n/5] ..." itself: keep its step tag, drop its clock.
    $initFilter = { Write-Log ('  init: ' + ((ConvertTo-PlainText $_) -replace '^\[\d\d:\d\d:\d\d \+[^\]]*\]\s?', '')) 'DarkGray' }
    & $initScript -Check *>&1 | ForEach-Object $initFilter
    if ($LASTEXITCODE -ne 0) {
        Write-Log "init check reported missing/invalid values (exit $LASTEXITCODE)" 'Yellow'
        Write-Log 'Generating missing .env values / secrets (existing values are kept)'
        & $initScript *>&1 | ForEach-Object $initFilter
        if ($LASTEXITCODE -ne 0) { throw 'init failed' }
    }
    Write-Log '.env and secrets/ are complete.' 'Green'

    # -- 3. host env vars shadowing .env -------------------------------------------------
    Write-Step 'Host environment variables vs .env'
    $shadowed = @(Get-ShadowedEnvNames)
    foreach ($name in $shadowed) {
        Write-Warn "Host environment variable $name shadows .env - ignoring it for this run."
        Remove-Item "Env:\$name"
    }
    if ($shadowed.Count -eq 0) { Write-Log 'No host environment variable shadows a .env key.' }
    Write-Log 'GEMINI_API_KEY is taken from the host environment (value not shown).'

    # -- 4. compose up ---------------------------------------------------------------------
    Write-Step 'Starting containers (docker compose up)'
    # With 'powershell -File', "-Services caddy,keycloak" arrives as ONE string: split it.
    $Services = @($Services | ForEach-Object { $_ -split '[,\s]+' } | Where-Object { $_ })
    if ($Infra) { $Services = $infraServices + $Services }
    $profileArgs = @()
    if ($Obs) { $profileArgs = @('--profile', 'obs') }
    $composeArgs = @('compose') + $profileArgs
    # Plain progress: build steps and container events stream line by line (no TTY redraw).
    # Older compose releases lack the global --progress flag; the env vars cover those.
    $progressArgs = @()
    $composeHelp = (& docker compose --help 2>$null) -join "`n"
    if ($composeHelp -match '--progress') { $progressArgs = @('--progress', 'plain') }
    else { $env:BUILDKIT_PROGRESS = 'plain'; $env:COMPOSE_PROGRESS = 'plain' }
    $upArgs = @('compose') + $progressArgs + $profileArgs + @('up', '-d')
    if (-not $NoBuild) { $upArgs += '--build' }
    $upArgs += $Services
    Write-Log ("Services : {0}" -f $(if ($Services.Count) { $Services -join ', ' } else { 'all (default profile' + $(if ($Obs) { ' + obs' } else { '' }) + ')' }))
    Write-Log ("Build    : {0}" -f $(if ($NoBuild) { 'no (-NoBuild)' } else { 'yes (cached layers are reused)' }))
    Write-Log ("Command  : docker " + ($upArgs -join ' '))

    Push-Location $root
    $pushed = $true
    $upStart = Get-Date
    $upCode = Invoke-Docker $upArgs
    $upTook = Format-Elapsed ((Get-Date) - $upStart).TotalSeconds
    if ($upCode -ne 0) {
        Write-Err "docker compose up failed after $upTook (exit $upCode)"
        throw "docker compose up failed (exit $upCode)"
    }
    Write-Log "docker compose up finished in $upTook" 'Green'

    # -- 5. wait for health ----------------------------------------------------------------
    Write-Step "Waiting up to $TimeoutSec s for services (poll every $pollSec s, log tail every ~$tailEverySec s)"
    $waitStart = Get-Date
    $deadline = $waitStart.AddSeconds($TimeoutSec)
    $lastTail = $waitStart
    $prev = @{}
    $first = $true
    do {
        $pollStart = Get-Date
        $state = @(Get-ComposeState)
        $pending = @($state | Where-Object { Test-SvcPending $_ })
        $failed = @($state | Where-Object { Test-SvcFailed $_ })
        $waited = ((Get-Date) - $waitStart).TotalSeconds

        if ($first) {
            Write-Log ("Initial state of {0} container(s):" -f $state.Count)
            foreach ($grp in ($state | Group-Object { Get-SvcStatus $_ } | Sort-Object Name)) {
                Write-Log ('  {0,-14} ({1,2}) {2}' -f $grp.Name, $grp.Count, (($grp.Group | ForEach-Object { $_.Service } | Sort-Object) -join ', '))
            }
        }
        foreach ($s in ($state | Sort-Object Service)) {
            $cur = Get-SvcStatus $s
            if (-not $first -and $prev[$s.Name] -ne $cur) {
                $from = if ($prev.ContainsKey($s.Name)) { $prev[$s.Name] } else { '(new)' }
                $color = switch (Get-SvcCategory $s) { 'failed' { 'Red' } 'starting' { 'Yellow' } default { 'Green' } }
                Write-Log ('{0}: {1} -> {2} ({3})' -f $s.Service, $from, $cur, (Format-Elapsed $waited)) $color
            }
            $prev[$s.Name] = $cur
        }
        $first = $false

        $counts = @{ healthy = 0; running = 0; completed = 0; starting = 0; failed = 0; other = 0 }
        foreach ($s in $state) { $counts[(Get-SvcCategory $s)]++ }
        $pendingNames = @($pending | Where-Object { -not (Test-SvcFailed $_) } | ForEach-Object { $_.Service } | Sort-Object -Unique)
        $progress = '{0}s/{1}s  healthy {2} | running {3} | completed {4} | starting {5} | failed {6}{7}  of {8}' -f `
            [int]$waited, $TimeoutSec, $counts.healthy, $counts.running, $counts.completed, $counts.starting, $counts.failed,
            $(if ($counts.other) { " | other $($counts.other)" } else { '' }), $state.Count
        if ($pendingNames.Count -gt 0) { $progress += '  pending: ' + ($pendingNames -join ', ') }
        Write-Log $progress $(if ($failed.Count -gt 0) { 'Red' } elseif ($pendingNames.Count -gt 0) { '' } else { 'Green' })

        if ($pending.Count -eq 0 -or $failed.Count -gt 0) { break }

        if (((Get-Date) - $lastTail).TotalSeconds -ge $tailEverySec) {
            $lastTail = Get-Date
            foreach ($p in ($pending | Sort-Object Service)) {
                if ($p.State -eq 'created') { Write-Log ('  {0}: created - waiting for its dependencies' -f $p.Service) 'DarkGray'; continue }
                Write-Log ('  {0} ({1}) - last log lines:' -f $p.Service, (Get-SvcStatus $p)) 'DarkGray'
                Show-ServiceLogs $p.Service 3 160
            }
        }
        $sleepMs = [int](($pollSec - ((Get-Date) - $pollStart).TotalSeconds) * 1000)
        if ($sleepMs -lt 1000) { $sleepMs = 1000 }
        Start-Sleep -Milliseconds $sleepMs
    } while ((Get-Date) -lt $deadline)
    $waitTook = Format-Elapsed ((Get-Date) - $waitStart).TotalSeconds

    if ($failed.Count -gt 0) {
        foreach ($f in ($failed | Sort-Object Service)) {
            Write-Log ('---- {0} ({1}): last 40 log lines ----' -f $f.Service, (Get-SvcStatus $f)) 'Red'
            Show-ServiceLogs $f.Service 40 0 '  '
        }
    } elseif ($pending.Count -gt 0) {
        Write-Warn "Timeout after $waitTook - showing the last 20 log lines of each service still starting."
        foreach ($p in ($pending | Where-Object { $_.State -ne 'created' } | Sort-Object Service)) {
            Write-Log ('---- {0} ({1}): last 20 log lines ----' -f $p.Service, (Get-SvcStatus $p)) 'Yellow'
            Show-ServiceLogs $p.Service 20 0 '  '
        }
    } else {
        Write-Log "All services are healthy / running / completed after $waitTook of waiting." 'Green'
    }

    # -- 6. summary -----------------------------------------------------------------------
    Write-Step 'Summary'
    $rows = foreach ($s in ($state | Sort-Object Service)) {
        $status = if ($s.State -eq 'exited') {
            if ($s.ExitCode -eq 0) { 'completed' } else { "FAILED (exit $($s.ExitCode))" }
        } elseif ($s.Health) { $s.Health } else { $s.State }
        [pscustomobject]@{ Service = $s.Service; Status = $status; Ports = $s.Ports }
    }
    ($rows | Format-Table -AutoSize | Out-String -Width 220) -split "`r?`n" | Where-Object { $_.Trim() } |
        ForEach-Object { Write-Log ('  ' + $_.TrimEnd()) }
    Write-Log ("Total time: {0} (compose up {1}, waiting {2})" -f (Format-Elapsed ((Get-Date) - $script:t0).TotalSeconds), $upTook, $waitTook)

    Write-Log 'Public URLs (HTTP, via Caddy on port 80):'
    Write-Log '  http://keelson.localhost   Keelson (engine UI + /api)'
    Write-Log '  http://merxa.localhost     Merxa Retail Suite (mock ERP)'
    Write-Log '  http://auth.localhost      Keycloak (realms keelson, merxa)'
    Write-Log '  http://gateway.localhost   LiteLLM console (platform-admin)'
    if ($Obs) { Write-Log '  http://traces.localhost    Phoenix traces (platform-admin)' }
    Write-Log 'Browsers resolve *.localhost automatically; for Node/Python on the host run scripts\hosts.ps1.'

    if ($failed.Count -gt 0) {
        Write-Log ("Failed: " + (($failed | ForEach-Object { $_.Service }) -join ', ') + "  -> docker compose logs <service>") 'Red'
        exit 1
    }
    if ($pending.Count -gt 0) {
        Write-Warn ("Still starting after $TimeoutSec s: " + (($pending | ForEach-Object { $_.Service }) -join ', '))
        exit 2
    }
    Write-Log 'Next: powershell -ExecutionPolicy Bypass -File scripts\verify-infra.ps1 -SkipLlm   (drop -SkipLlm to include one tiny Gemini call)'
    Write-Log 'Stack is up.' 'Green'
} catch {
    Write-Err "$($_.Exception.Message)"
    throw
} finally {
    if ($pushed) { Pop-Location }
    if ($prevConsoleEncoding) { try { [Console]::OutputEncoding = $prevConsoleEncoding } catch { } }
    if ($script:log) {
        Write-Log ("Finished after {0} - log file: {1}" -f (Format-Elapsed ((Get-Date) - $script:t0).TotalSeconds), $logFile) 'Cyan'
        $script:log.Dispose()
        $script:log = $null
    }
}
