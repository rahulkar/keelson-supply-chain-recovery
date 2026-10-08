<#
.SYNOPSIS
  Run the Keelson + Merxa stack on Windows WITHOUT Docker (entry point: startup.bat).

.DESCRIPTION
  The native equivalent of scripts/up.ps1. Everything it needs lives in the git-ignored
  .native/ folder next to the repository:

    .native/downloads   pinned archives (scripts/native/versions.psd1), verified by SHA-256 when pinned
    .native/tools       Postgres 18 + PostGIS + pgvector, Redis (Valkey stand-in), Keycloak,
                        keycloak-config-cli, Caddy, uv, pnpm
    .native/venvs       one Python venv per service (installed from the services' uv.lock pins)
    .native/build       isolated copies of the two Next.js apps (the repo's node_modules are untouched)
    .native/data        Postgres cluster, Redis AOF, Merxa snapshots
    .native/logs        startup-<timestamp>.log plus one <service>.log per process
    .native/run         PIDs and launcher files (no secrets: env vars are inherited, never written)

  'start' runs, with timestamped step-by-step progress:
     1 prerequisites (Java 21+, Node 20+, GEMINI_API_KEY, *.localhost names, free ports)
     2 .env + secrets/ (scripts/init_secrets.py, natively)
     3 download + unpack the pinned tools (first run only, ~650 MB)
     4 Python environments (first run / when a uv.lock changed)
     5 web apps: pnpm install + next build (first run / when their sources changed)
     6 Postgres: initdb (first run), start, roles + databases + extensions
     7 Redis + Caddy (http://*.localhost on port 80)
     8 Keycloak, LiteLLM, Merxa API, optimizer (started together, then waited for)
     9 one-shot jobs: keycloak-config-cli, signal-hub bootstrap, engine bootstrap (seed + migrations)
    10 engine API + worker, Signal Hub API + poller, Keelson web, Merxa web
    11 status table + URLs
  Every step is idempotent: services already running are left alone.

  Differences from compose: one Postgres cluster instead of three (pg-core/pg-litellm/pg-merxa
  become databases), Redis for Windows instead of Valkey, no Squid egress proxy (connector and feed
  traffic goes out directly), and services reach each other on 127.0.0.1. The public hostnames
  (keelson/merxa/auth/gateway.localhost) must resolve on the host: scripts/hosts.ps1 -Apply
  (administrator) adds them; 'start' offers to run it.

.PARAMETER Command
  start (default) | stop | restart | status | logs <service> | fetch (download + install only)

.PARAMETER Service
  For 'logs': the service whose log to follow (e.g. engine-worker). Omit to list the log files.

.PARAMETER Yes
  Answer yes to prompts (stop Docker's stack when it holds port 80, add the hosts entries).

.PARAMETER Rebuild
  Reinstall the Python environments and rebuild the web apps even when nothing changed.

.PARAMETER SkipWeb
  Do not build/start the two Next.js apps (API-only stack).

.PARAMETER TimeoutSec
  How long to wait for each group of services to become ready (default 300).

.PARAMETER NativeDir
  Put the native folder somewhere else than <repo>\.native (needs ~6-7 GB), e.g. on a drive with
  more space. Also read from $env:KEELSON_NATIVE_DIR; remembered in .native.path for later runs.

.EXAMPLE
  startup.bat
  startup.bat status
  startup.bat logs engine-worker
  startup.bat stop
  powershell -ExecutionPolicy Bypass -File scripts\native\keelson-native.ps1 start -Yes -Rebuild
#>
[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [ValidateSet('start', 'stop', 'restart', 'status', 'logs', 'fetch')]
    [string]$Command = 'start',
    [Parameter(Position = 1)]
    [string]$Service = '',
    [switch]$Yes,
    [switch]$Rebuild,
    [switch]$SkipWeb,
    [int]$TimeoutSec = 300,
    [string]$NativeDir = ''
)

# Native tools write progress to stderr; with 'Stop' Windows PowerShell 5.1 would turn that into
# terminating errors. Exit codes are checked explicitly instead (same as scripts/up.ps1).
$ErrorActionPreference = 'Continue'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch { }

$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
# Where tools/venvs/data live: -NativeDir, else $env:KEELSON_NATIVE_DIR, else the folder remembered in
# .native.path (written when -NativeDir / KEELSON_NATIVE_DIR was used), else <repo>\.native.
$pointer = Join-Path $Root '.native.path'
if (-not $NativeDir) { $NativeDir = $env:KEELSON_NATIVE_DIR }
if ($NativeDir) {
    $Native = [IO.Path]::GetFullPath($NativeDir)
    if ($Native -ne (Join-Path $Root '.native')) { Set-Content -LiteralPath $pointer -Value $Native -Encoding ASCII }
} elseif (Test-Path $pointer) {
    $Native = (Get-Content -LiteralPath $pointer -Raw).Trim()
} else {
    $Native = Join-Path $Root '.native'
}
$P = @{
    Downloads = Join-Path $Native 'downloads'
    Tools     = Join-Path $Native 'tools'
    Venvs     = Join-Path $Native 'venvs'
    Build     = Join-Path $Native 'build'
    Data      = Join-Path $Native 'data'
    Logs      = Join-Path $Native 'logs'
    Run       = Join-Path $Native 'run'
    Secrets   = Join-Path $Native 'secrets'
    Cache     = Join-Path $Native 'cache'
    State     = Join-Path $Native 'state'
}
$Versions = Import-PowerShellDataFile (Join-Path $PSScriptRoot 'versions.psd1')

# ----------------------------- logging -----------------------------
$script:T0 = Get-Date
$script:StepNo = 0
$script:StepTotal = 11
$script:StepT0 = Get-Date
$script:LogFile = $null

function Get-Elapsed([datetime]$Since = $script:T0) {
    $s = [int]((Get-Date) - $Since).TotalSeconds
    return ('{0}m{1:00}s' -f [math]::Floor($s / 60), ($s % 60))
}

function Write-Log([string]$Message, [string]$Color = 'Gray') {
    $line = '[{0:HH:mm:ss} +{1}] {2}' -f (Get-Date), (Get-Elapsed), $Message
    Write-Host $line -ForegroundColor $Color
    if ($script:LogFile) { [IO.File]::AppendAllText($script:LogFile, $line + "`r`n") }
}
function Write-Step([string]$Title) {
    if ($script:StepNo -gt 0) { Write-Log ('      step finished in {0}' -f (Get-Elapsed $script:StepT0)) 'DarkGray' }
    $script:StepNo++
    $script:StepT0 = Get-Date
    Write-Log ''
    Write-Log ('==> [{0}/{1}] {2}' -f $script:StepNo, $script:StepTotal, $Title) 'Cyan'
}
function Write-Info([string]$Message) { Write-Log "    $Message" 'Gray' }
function Write-Detail([string]$Message) { Write-Log "      $Message" 'DarkGray' }
function Write-Ok([string]$Message) { Write-Log "    OK   $Message" 'Green' }
function Write-Warn2([string]$Message) { Write-Log "    WARN $Message" 'Yellow' }
function Write-Fail([string]$Message) { Write-Log "    FAIL $Message" 'Red' }

function Stop-WithError([string]$Message, [int]$Code = 1) {
    Write-Fail $Message
    if ($script:LogFile) { Write-Log "Full log: $script:LogFile" 'Yellow' }
    exit $Code
}

function Format-OutputLine([string]$Line) {
    # Make structured JSON log lines (structlog / Caddy / LiteLLM) readable on the console; the
    # raw line is kept in the log file.
    $t = $Line.Trim()
    if ($t.StartsWith('{') -and $t.EndsWith('}')) {
        try {
            $o = $t | ConvertFrom-Json
            $msg = $o.event; if (-not $msg) { $msg = $o.msg }; if (-not $msg) { $msg = $o.message }
            $lvl = $o.level; if (-not $lvl) { $lvl = $o.levelname }
            if ($msg) {
                $extra = @()
                foreach ($prop in $o.PSObject.Properties) {
                    if ($prop.Name -in @('event', 'msg', 'message', 'level', 'levelname', 'timestamp', 'ts', 'logger', 'time')) { continue }
                    if ($null -eq $prop.Value -or $prop.Value -is [System.Management.Automation.PSCustomObject] -or $prop.Value -is [array]) { continue }
                    $extra += ('{0}={1}' -f $prop.Name, $prop.Value)
                    if ($extra.Count -ge 4) { break }
                }
                $t = ('{0} {1} {2}' -f $lvl, $msg, ($extra -join ' ')).Trim()
            }
        } catch { }
    }
    if ($t.Length -gt 200) { $t = $t.Substring(0, 197) + '...' }
    return $t
}

function Invoke-Native {
    # Runs a program in the foreground. Output is streamed to the console (prefixed, readable) and
    # appended raw to the startup log. Returns the exit code.
    param([string]$Label, [string]$Exe, [string[]]$Arguments = @(), [string]$WorkDir = '', [hashtable]$Env = @{})
    $saved = Set-ChildEnv $Env
    if ($WorkDir) { Push-Location -LiteralPath $WorkDir }
    try {
        $script:SacHit = $false
        & $Exe @Arguments 2>&1 | ForEach-Object {
            $raw = "$_"
            if ($raw -match 'Application Control policy|WinError 4551|os error 4551') { $script:SacHit = $true }
            if ($script:LogFile) { [IO.File]::AppendAllText($script:LogFile, "      | [$Label] $raw`r`n") }
            if ($raw.Trim()) { Write-Host ("      | [{0}] {1}" -f $Label, (Format-OutputLine $raw)) -ForegroundColor DarkGray }
        }
        $code = $LASTEXITCODE
    } finally {
        if ($WorkDir) { Pop-Location }
        Restore-ChildEnv $saved
    }
    if ($null -eq $code) { $code = 0 }
    return $code
}

function Read-YesNo([string]$Question) {
    if ($Yes) { Write-Info "$Question -> yes (-Yes)"; return $true }
    if ([Console]::IsInputRedirected) { Write-Warn2 "$Question -> no (non-interactive; pass -Yes to accept)"; return $false }
    $a = Read-Host "    $Question [Y/n]"
    return (-not $a) -or ($a -match '^(y|yes)$')
}

# ----------------------------- .env + child environments -----------------------------
$script:DotEnv = @{}
function Import-DotEnv {
    $script:DotEnv = @{}
    $file = Join-Path $Root '.env'
    if (-not (Test-Path $file)) { return }
    foreach ($line in [IO.File]::ReadAllLines($file)) {
        if ($line -match '^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$') {
            $v = $Matches[2].Trim()
            if ($v.Length -ge 2 -and (($v.StartsWith('"') -and $v.EndsWith('"')) -or ($v.StartsWith("'") -and $v.EndsWith("'")))) { $v = $v.Substring(1, $v.Length - 2) }
            $script:DotEnv[$Matches[1]] = $v
        }
    }
}
# compose ${NAME:-default}: unset or empty -> default
function V([string]$Name, [string]$Default = '') {
    $v = $script:DotEnv[$Name]
    if ([string]::IsNullOrEmpty($v)) { return $Default }
    return $v
}

Add-Type -Namespace Keelson -Name Win32Env -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("kernel32.dll", CharSet = System.Runtime.InteropServices.CharSet.Unicode, SetLastError = true)]
public static extern bool SetEnvironmentVariable(string name, string value);
"@

function Set-ChildEnv([hashtable]$Vars) {
    # Child processes inherit this process' environment. Like compose, a child only gets what its
    # service definition lists: every .env key (and GEMINI_API_KEY, the host's LITELLM_* etc.) is
    # removed first, then the service's own values are set. Restored afterwards.
    $saved = @{}
    $names = @($script:DotEnv.Keys) + @('GEMINI_API_KEY') + @($Vars.Keys) | Select-Object -Unique
    foreach ($n in $names) { $saved[$n] = [Environment]::GetEnvironmentVariable($n, 'Process') }
    if ($Vars.Count -gt 0) {
        foreach ($n in $names) { [Environment]::SetEnvironmentVariable($n, $null, 'Process') }
        foreach ($k in $Vars.Keys) {
            $val = [string]$Vars[$k]
            if ($val -eq '<host>') { $val = $saved[$k] }
            # [Environment]::SetEnvironmentVariable deletes a variable set to '' - compose's explicit
            # empty values (e.g. CONNECTOR_PROXY_URL) must reach the child as empty, not as unset.
            [void][Keelson.Win32Env]::SetEnvironmentVariable($k, $val)
        }
    }
    return $saved
}
function Restore-ChildEnv([hashtable]$Saved) {
    foreach ($k in $Saved.Keys) { [Environment]::SetEnvironmentVariable($k, $Saved[$k], 'Process') }
}

# ----------------------------- downloads -----------------------------
function Get-Download([string]$Name, [hashtable]$Spec) {
    $file = Join-Path $P.Downloads ([IO.Path]::GetFileName(([Uri]$Spec.Url).AbsolutePath))
    if (-not (Test-Path $file)) {
        Write-Info "downloading $Name $($Spec.Version)"
        Write-Detail $Spec.Url
        Add-Type -AssemblyName System.Net.Http
        $handler = New-Object System.Net.Http.HttpClientHandler
        $handler.AllowAutoRedirect = $true
        $client = New-Object System.Net.Http.HttpClient($handler)
        $client.Timeout = [TimeSpan]::FromMinutes(60)
        $client.DefaultRequestHeaders.UserAgent.ParseAdd('keelson-startup/1.0')
        $part = "$file.part"
        try {
            $resp = $client.GetAsync($Spec.Url, [System.Net.Http.HttpCompletionOption]::ResponseHeadersRead).GetAwaiter().GetResult()
            if (-not $resp.IsSuccessStatusCode) { throw "HTTP $([int]$resp.StatusCode) for $($Spec.Url)" }
            $total = $resp.Content.Headers.ContentLength
            $in = $resp.Content.ReadAsStreamAsync().GetAwaiter().GetResult()
            $out = [IO.File]::Create($part)
            try {
                $buf = New-Object byte[] (1MB)
                $done = [long]0; $tick = Get-Date; $tickBytes = [long]0; $t0 = Get-Date
                while (($n = $in.Read($buf, 0, $buf.Length)) -gt 0) {
                    $out.Write($buf, 0, $n)
                    $done += $n
                    if (((Get-Date) - $tick).TotalSeconds -ge 3) {
                        $rate = ($done - $tickBytes) / ((Get-Date) - $tick).TotalSeconds / 1MB
                        if ($total) {
                            $eta = if ($rate -gt 0) { [int](($total - $done) / 1MB / $rate) } else { 0 }
                            Write-Detail ('{0}: {1:N1} / {2:N1} MB ({3:N0}%)  {4:N1} MB/s  ~{5}s left' -f $Name, ($done / 1MB), ($total / 1MB), (100 * $done / $total), $rate, $eta)
                        } else {
                            Write-Detail ('{0}: {1:N1} MB  {2:N1} MB/s' -f $Name, ($done / 1MB), $rate)
                        }
                        $tick = Get-Date; $tickBytes = $done
                    }
                }
            } finally { $out.Dispose(); $in.Dispose() }
            Move-Item -LiteralPath $part -Destination $file -Force
            Write-Detail ('{0}: {1:N1} MB in {2}' -f $Name, ($done / 1MB), (Get-Elapsed $t0))
        } catch {
            Remove-Item -LiteralPath $part -Force -ErrorAction SilentlyContinue
            Stop-WithError "download of $Name failed: $($_.Exception.Message)"
        } finally { $client.Dispose() }
    } else {
        Write-Detail "$Name $($Spec.Version): using cached $file"
    }
    $hash = (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($Spec.Sha256) {
        if ($hash -ne $Spec.Sha256.ToLowerInvariant()) {
            Remove-Item -LiteralPath $file -Force
            Stop-WithError "SHA-256 mismatch for $Name (got $hash, expected $($Spec.Sha256)); the file was deleted"
        }
        Write-Detail "$Name sha256 verified"
    } else {
        Write-Warn2 "$Name is not pinned in versions.psd1 (sha256 $hash)"
    }
    return $file
}

function Expand-ZipFile([string]$Zip, [string]$Dest, [string[]]$Exclude = @()) {
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destFull = [IO.Path]::GetFullPath($Dest).TrimEnd('\') + '\'
    $z = [IO.Compression.ZipFile]::OpenRead($Zip)
    try {
        $entries = $z.Entries; $count = $entries.Count; $i = 0; $tick = Get-Date
        Write-Detail ('unpacking {0} ({1} entries) -> {2}' -f [IO.Path]::GetFileName($Zip), $count, $Dest)
        foreach ($e in $entries) {
            $i++
            $rel = $e.FullName.Replace('/', '\')
            $skip = $false
            foreach ($x in $Exclude) { if ($rel -like $x) { $skip = $true; break } }
            if ($skip) { continue }
            $target = [IO.Path]::GetFullPath((Join-Path $Dest $rel))
            if (-not $target.StartsWith($destFull, [StringComparison]::OrdinalIgnoreCase)) { throw "unsafe path in archive: $rel" }
            if ($rel.EndsWith('\')) { [void][IO.Directory]::CreateDirectory($target); continue }
            [void][IO.Directory]::CreateDirectory([IO.Path]::GetDirectoryName($target))
            [IO.Compression.ZipFileExtensions]::ExtractToFile($e, $target, $true)
            if (((Get-Date) - $tick).TotalSeconds -ge 3) {
                Write-Detail ('  {0:N0}% ({1}/{2})' -f (100 * $i / $count), $i, $count)
                $tick = Get-Date
            }
        }
    } finally { $z.Dispose() }
}

function Test-Installed([string]$Name, [string]$Version) {
    $m = Join-Path $P.Tools "$Name.installed"
    return (Test-Path $m) -and ((Get-Content -LiteralPath $m -Raw).Trim() -eq $Version)
}
function Set-Installed([string]$Name, [string]$Version) {
    Set-Content -LiteralPath (Join-Path $P.Tools "$Name.installed") -Value $Version -Encoding ASCII
}

# ----------------------------- tool paths -----------------------------
$T = @{
    Uv       = Join-Path $P.Tools 'uv\uv.exe'
    PgBin    = Join-Path $P.Tools 'pgsql\bin'
    Redis    = Join-Path $P.Tools 'redis\redis-server.exe'
    RedisCli = Join-Path $P.Tools 'redis\redis-cli.exe'
    Keycloak = Join-Path $P.Tools ("keycloak\keycloak-{0}\bin\kc.bat" -f $Versions.keycloak.Version)
    KcConfig = Join-Path $P.Tools 'kcconfig\keycloak-config-cli.jar'
    Caddy    = Join-Path $P.Tools 'caddy\caddy.exe'
    Pnpm     = Join-Path $P.Tools 'pnpm\node_modules\pnpm\bin\pnpm.cjs'
}
function PgExe([string]$Name) { return (Join-Path $T.PgBin "$Name.exe") }

# uv: keep its cache and managed Pythons inside .native
$env:UV_CACHE_DIR = Join-Path $P.Cache 'uv'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Native 'python'
# Use the installed (signed) Python 3.14 only: Windows Application Control / Smart App Control can
# block the unsigned standalone Pythons uv would otherwise download.
$env:UV_PYTHON_DOWNLOADS = 'never'
$env:UV_PYTHON_PREFERENCE = 'only-system'
$env:UV_LINK_MODE = 'hardlink'   # cache and venvs share a volume: no second copy of every wheel
$env:UV_NO_PROGRESS = '1'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# ----------------------------- services -----------------------------
# Order = start order. Health is probed on 127.0.0.1; Port is also used for the conflict check.
$Svc = [ordered]@{
    'postgres'          = @{ Port = 5432; Health = 'pg' }
    'redis'             = @{ Port = 6379; Health = 'tcp' }
    'caddy'             = @{ Port = 80; Health = 'http://127.0.0.1:2019/config/' }
    'keycloak'          = @{ Port = 8080; Health = 'http://127.0.0.1:9000/health/ready'; Expect = '"UP"' }
    'litellm'           = @{ Port = 4000; Health = 'http://127.0.0.1:4000/health/readiness' }
    'merxa-api'         = @{ Port = 8100; Health = 'http://127.0.0.1:8100/healthz' }
    'optimizer'         = @{ Port = 8200; Health = 'http://127.0.0.1:8200/healthz' }
    'signal-hub-api'    = @{ Port = 8300; Health = 'http://127.0.0.1:8300/healthz' }
    'signal-hub-poller' = @{ Port = 0; Health = 'process' }
    'engine-api'        = @{ Port = 8000; Health = 'http://127.0.0.1:8000/api/healthz' }
    'engine-worker'     = @{ Port = 0; Health = 'process' }
    'keelson-web'       = @{ Port = 3000; Health = 'http://127.0.0.1:3000/healthz'; Web = $true }
    'merxa-web'         = @{ Port = 3001; Health = 'http://127.0.0.1:3001/healthz'; Web = $true }
}

function Get-SvcPid([string]$Name) {
    $f = Join-Path $P.Run "$Name.pid"
    if (Test-Path $f) { return [int](Get-Content -LiteralPath $f -Raw).Trim() }
    if ($Name -eq 'postgres') {
        # a postmaster started some other way (e.g. pg_ctl by hand)
        $pidFile = Join-Path $P.Data 'pg\postmaster.pid'
        if (Test-Path $pidFile) { try { return [int](Get-Content -LiteralPath $pidFile -TotalCount 1) } catch { return 0 } }
    }
    return 0
}
function Test-PidAlive([int]$ProcessId) {
    return ($ProcessId -gt 0) -and [bool](Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

function Test-TcpPort([int]$Port) {
    $c = New-Object Net.Sockets.TcpClient
    try {
        $ar = $c.BeginConnect('127.0.0.1', $Port, $null, $null)
        if (-not $ar.AsyncWaitHandle.WaitOne(500)) { return $false }
        $c.EndConnect($ar); return $true
    } catch { return $false } finally { $c.Close() }
}
function Test-Http([string]$Url, [string]$Expect = '') {
    try {
        $r = Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 4 -MaximumRedirection 0
        if ($r.StatusCode -ge 400) { return $false }
        if ($Expect) { return ($r.Content -match [regex]::Escape($Expect)) }
        return $true
    } catch { return $false }
}
function Test-SvcHealthy([string]$Name) {
    $s = $Svc[$Name]
    switch ($s.Health) {
        'pg' { if (-not (Test-Path (PgExe 'pg_isready'))) { return $false }; & (PgExe 'pg_isready') -h 127.0.0.1 -p 5432 -q *> $null; return ($LASTEXITCODE -eq 0) }
        'tcp' { return (Test-TcpPort $s.Port) }
        'process' { return (Test-PidAlive (Get-SvcPid $Name)) }
        default { return (Test-Http $s.Health $s.Expect) }
    }
}

function Get-LastLogLine([string]$Name) {
    $f = Join-Path $P.Logs "$Name.log"
    if (-not (Test-Path $f)) { return '(no output yet)' }
    $lines = @(Get-Content -LiteralPath $f -Tail 5 -ErrorAction SilentlyContinue | Where-Object { $_.Trim() })
    if ($lines.Count -eq 0) { return '(no output yet)' }
    return (Format-OutputLine $lines[-1])
}
function Show-LogTail([string]$Name, [int]$Lines = 30) {
    $f = Join-Path $P.Logs "$Name.log"
    Write-Info "last $Lines lines of $f :"
    if (Test-Path $f) { Get-Content -LiteralPath $f -Tail $Lines | ForEach-Object { Write-Log "      | $_" 'DarkYellow' } }
}

function Format-CmdArg([string]$a) {
    $a = $a.Replace('%', '%%')
    if ($a -match '[\s&|<>^()"]' -or $a -eq '') { return '"' + $a.Replace('"', '\"') + '"' }
    return $a
}

function Start-NativeService {
    # Starts a long-running process hidden, stdout+stderr appended to .native/logs/<name>.log. A
    # small launcher .cmd holds only the command line; the environment is inherited (no secrets
    # on disk).
    param([string]$Name, [string]$Exe, [string[]]$Arguments = @(), [hashtable]$Env = @{}, [string]$WorkDir = $Root)
    $why = Get-BlockedReason $Name
    if ($why) { Write-Warn2 ('{0}: not started - {1}' -f $Name, $why); return }
    $existing = Get-SvcPid $Name
    if ((Test-PidAlive $existing) -and (Test-SvcHealthy $Name)) {
        Write-Info ('{0}: already running (pid {1})' -f $Name, $existing)
        return
    }
    $log = Join-Path $P.Logs "$Name.log"
    if (Test-Path $log) { Move-Item -LiteralPath $log -Destination (Join-Path $P.Logs "$Name.prev.log") -Force -ErrorAction SilentlyContinue }
    $cmdFile = Join-Path $P.Run "$Name.cmd"
    $argLine = ($Arguments | ForEach-Object { Format-CmdArg $_ }) -join ' '
    $content = "@echo off`r`nrem generated by scripts/native/keelson-native.ps1 - command line only, env is inherited`r`ncd /d `"$WorkDir`"`r`n`"$Exe`" $argLine >> `"$log`" 2>&1`r`n"
    [IO.File]::WriteAllText($cmdFile, $content, (New-Object Text.UTF8Encoding($false)))
    $saved = Set-ChildEnv $Env
    try {
        $proc = Start-Process -FilePath $env:ComSpec -ArgumentList "/d /c `"`"$cmdFile`"`"" -WindowStyle Hidden -PassThru
    } finally { Restore-ChildEnv $saved }
    Set-Content -LiteralPath (Join-Path $P.Run "$Name.pid") -Value $proc.Id -Encoding ASCII
    Write-Info ('{0}: started (pid {1}), log {2}' -f $Name, $proc.Id, $log)
}

function Wait-Services([string[]]$Names, [int]$Timeout = $TimeoutSec) {
    # Polls until all are healthy; prints a progress line every 5 s with each pending service's
    # latest log line, and the full tail of any service that dies or times out.
    $t0 = Get-Date; $pending = [System.Collections.ArrayList]@($Names); $lastNote = Get-Date '2000-01-01'
    while ($pending.Count -gt 0) {
        foreach ($n in @($pending)) {
            if (Test-SvcHealthy $n) {
                Write-Ok ('{0} is ready after {1}' -f $n, (Get-Elapsed $t0))
                [void]$pending.Remove($n)
            } elseif (-not (Test-PidAlive (Get-SvcPid $n))) {
                Write-Fail "$n exited during start-up"
                Show-LogTail $n 40
                return $false
            }
        }
        if ($pending.Count -eq 0) { break }
        $el = [int]((Get-Date) - $t0).TotalSeconds
        if ($el -ge $Timeout) {
            Write-Fail ('timed out after {0}s waiting for: {1}' -f $Timeout, ($pending -join ', '))
            foreach ($n in $pending) { Show-LogTail $n 25 }
            return $false
        }
        if (((Get-Date) - $lastNote).TotalSeconds -ge 5) {
            Write-Detail ('waiting {0}s/{1}s for {2}' -f $el, $Timeout, ($pending -join ', '))
            foreach ($n in $pending) { Write-Detail ('  {0,-18} {1}' -f $n, (Get-LastLogLine $n)) }
            $lastNote = Get-Date
        }
        Start-Sleep -Milliseconds 1000
    }
    return $true
}

# ----------------------------- per-service environments (mirror compose.yaml) -----------------------------
function Get-EngineEnv {
    $hub = 'http://127.0.0.1:8300'
    if ($script:DotEnv.ContainsKey('SIGNAL_HUB_URL')) { $hub = ($script:DotEnv['SIGNAL_HUB_URL'] -replace 'signal-hub-api', '127.0.0.1') }
    return @{
        KEELSON_DATABASE_URL      = 'postgresql+asyncpg://keelson:{0}@127.0.0.1:5432/keelson' -f (V KEELSON_DB_PASSWORD)
        ADK_DATABASE_URL          = 'postgresql+asyncpg://adk:{0}@127.0.0.1:5432/adk' -f (V ADK_DB_PASSWORD)
        VALKEY_URL                = 'redis://:{0}@127.0.0.1:6379/0' -f (V VALKEY_PASSWORD)
        LITELLM_BASE_URL          = 'http://127.0.0.1:4000'
        LITELLM_MASTER_KEY        = V LITELLM_MASTER_KEY
        LLM_REPLAY_MODE           = V LLM_REPLAY_MODE 'record_or_replay'
        KEYCLOAK_INTERNAL_URL     = 'http://127.0.0.1:8080'
        KC_ADMIN_PASSWORD         = V KC_ADMIN_PASSWORD
        OIDC_ISSUER               = 'http://auth.localhost/realms/keelson'
        MERXA_ISSUER              = 'http://auth.localhost/realms/merxa'
        MERXA_MCP_URL             = 'http://merxa.localhost/mcp'
        MERXA_API_URL             = 'http://127.0.0.1:8100'
        PUBLIC_URL                = 'http://keelson.localhost'
        CONNECTOR_PROXY_URL       = ''   # no Squid in native mode: connector traffic goes out directly
        OPTIMIZER_URL             = 'http://127.0.0.1:8200'
        OPTIMIZER_SHARED_SECRET   = V OPTIMIZER_SHARED_SECRET
        SIGNAL_HUB_URL            = $hub
        SIGNAL_HUB_SHARED_SECRET  = V SIGNAL_HUB_SHARED_SECRET
        KEELSON_BFF_CLIENT_SECRET = V KEELSON_BFF_CLIENT_SECRET
        KEELSON_DEMO_CLIENT_SECRET = V KEELSON_DEMO_CLIENT_SECRET
        DEMO_USER_PASSWORD        = V DEMO_USER_PASSWORD
        SESSION_SECRET            = V SESSION_SECRET
        DEMO_ACCESS_SECRET        = V DEMO_ACCESS_SECRET
        DEMO_MODE                 = V DEMO_MODE '1'
        PUBLIC_BASE_DOMAIN        = V PUBLIC_BASE_DOMAIN 'localhost'
        MERXA_WEBHOOK_SECRET      = V MERXA_WEBHOOK_SECRET
        CONNECTOR_DEV_HOSTS       = 'merxa.localhost'
        PACKS_DIR                 = Join-Path $Root 'data\packs'
        BRAND_ENGINE_NAME         = V BRAND_ENGINE_NAME 'Keelson'
        BRAND_VENDOR_NAME         = V BRAND_VENDOR_NAME 'Merxa'
        MODELS_LOCK               = Join-Path $Root 'models.lock'
        TENANTS_FILE              = Join-Path $Root 'infra\tenants.yaml'
        SECRETS_DIR               = $P.Secrets
        PYTHONUNBUFFERED          = '1'
        PYTHONUTF8                = '1'
    }
}
function Get-SignalHubEnv {
    return @{
        PORT                     = '8300'
        SIGNALS_DATABASE_URL     = 'postgresql://signals:{0}@127.0.0.1:5432/signals' -f (V SIGNALS_DB_PASSWORD)
        SIGNAL_HUB_SHARED_SECRET = V SIGNAL_HUB_SHARED_SECRET
        LITELLM_BASE_URL         = 'http://127.0.0.1:4000'
        LITELLM_SIGNALS_KEY      = V LITELLM_SIGNALS_KEY
        SIGNALS_DATA_KEY         = V SIGNALS_DATA_KEY
        DEMO_MODE                = V DEMO_MODE '1'
        DEMO_TENANTS             = V SIGNALS_DEMO_TENANTS
        DEMO_FEEDS_BASE_URL      = 'http://127.0.0.1:8300/demo-feeds'
        GDACS_URL                = V GDACS_URL 'https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?eventlist=TC;EQ;FL&alertlevel=orange;red'
        USGS_URL                 = V USGS_URL 'https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson'
        RSS_FEEDS                = V RSS_FEEDS 'rss.theloadstar=The Loadstar|https://theloadstar.com/feed/'
        LOG_LEVEL                = V SIGNALS_LOG_LEVEL 'INFO'
        SCENARIOS_DIR            = Join-Path $Root 'data\scenarios'
        GEO_DIR                  = Join-Path $Root 'data\geo'
        PYTHONUNBUFFERED         = '1'
        PYTHONUTF8               = '1'
    }
}
function VenvExe([string]$Venv, [string]$Exe) { return (Join-Path $P.Venvs "$Venv\Scripts\$Exe") }

# ----------------------------- steps -----------------------------
function Initialize-Dirs {
    foreach ($d in $P.Values) { [void][IO.Directory]::CreateDirectory($d) }
}

function Step-Prerequisites {
    Write-Step 'Checking prerequisites'
    Write-Info ('repository: {0}' -f $Root)
    Write-Info ('Windows {0}, PowerShell {1}, {2} logical CPUs' -f [Environment]::OSVersion.Version, $PSVersionTable.PSVersion, [Environment]::ProcessorCount)
    $os = Get-CimInstance Win32_OperatingSystem -ErrorAction SilentlyContinue
    if ($os) { Write-Info ('memory: {0:N1} GB free of {1:N1} GB' -f ($os.FreePhysicalMemory / 1MB), ($os.TotalVisibleMemorySize / 1MB)) }
    Write-Info ('native folder: {0}' -f $Native)
    $drive = Get-PSDrive -Name $Native.Substring(0, 1) -ErrorAction SilentlyContinue
    if ($drive) {
        Write-Info ('disk {0}: {1:N1} GB free' -f $drive.Name, ($drive.Free / 1GB))
        $firstRun = -not (Test-Installed 'keycloak' $Versions.keycloak.Version)
        if ($firstRun -and $drive.Free -lt 7GB) {
            Stop-WithError ("the first run needs about 6-7 GB on {0}: (tools, venvs, node_modules, builds); only {1:N1} GB are free.`r`n    Put the native folder on another drive, e.g.:  startup.bat -NativeDir C:\keelson-native   (remembered for later runs)" -f $drive.Name, ($drive.Free / 1GB))
        }
        if ($drive.Free -lt 1GB) { Write-Warn2 'less than 1 GB free: Postgres/Redis may fail to write' }
    }

    $sac = (Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' -ErrorAction SilentlyContinue).VerifiedAndReputablePolicyState
    if ($sac -eq 1) {
        Write-Warn2 'Windows Smart App Control is ON: it may refuse to load downloaded native libraries without reputation (e.g. OR-Tools for the optimizer).'
        Write-Info  'Such components are detected, reported as BLOCKED and skipped; everything else still starts. This script never changes that setting.'
    }

    $java = Get-Command java -ErrorAction SilentlyContinue
    if (-not $java) { Stop-WithError 'Java 21+ is required for Keycloak (e.g. winget install Microsoft.OpenJDK.25)' }
    $jv = (& java -version 2>&1 | Select-Object -First 1 | ForEach-Object { "$_" })
    if ($jv -match '"(\d+)') { $jmajor = [int]$Matches[1] } else { $jmajor = 0 }
    if ($jmajor -lt 21) { Stop-WithError "Java 21+ is required for Keycloak; found: $jv" }
    Write-Ok "java: $jv ($($java.Source))"

    if (-not $SkipWeb) {
        $node = Get-Command node -ErrorAction SilentlyContinue
        if (-not $node) { Stop-WithError 'Node.js 20+ is required for the web apps (https://nodejs.org) - or pass -SkipWeb' }
        $nv = (& node --version).Trim()
        if ([int]($nv.TrimStart('v').Split('.')[0]) -lt 20) { Stop-WithError "Node.js 20+ is required; found $nv" }
        Write-Ok "node: $nv ($($node.Source))"
    }

    if (-not $env:GEMINI_API_KEY) {
        $userKey = [Environment]::GetEnvironmentVariable('GEMINI_API_KEY', 'User')
        if ($userKey) { $env:GEMINI_API_KEY = $userKey; Write-Info 'GEMINI_API_KEY taken from the user environment' }
    }
    if ($env:GEMINI_API_KEY) { Write-Ok 'GEMINI_API_KEY is set (value not shown)' }
    else { Write-Warn2 'GEMINI_API_KEY is not set: everything starts, but model calls will fail (setx GEMINI_API_KEY <key>, then open a new terminal)' }

    if ($Command -eq 'fetch') { Write-Info 'fetch: hostname and port checks skipped (nothing is started)'; return }

    # *.localhost must resolve for the services themselves (Python/Node/Java use the Windows resolver).
    $names = @('keelson.localhost', 'merxa.localhost', 'auth.localhost', 'gateway.localhost')
    $bad = @($names | Where-Object { try { [void][Net.Dns]::GetHostAddresses($_); $false } catch { $true } })
    if ($bad.Count -gt 0) {
        Write-Warn2 ('not resolvable on this host: {0}' -f ($bad -join ', '))
        Write-Info 'the services call each other through these names; scripts\hosts.ps1 -Apply maps them to 127.0.0.1 (administrator).'
        if (Read-YesNo 'Add the hosts entries now (a UAC prompt will ask for administrator rights)?') {
            $ps = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
            try {
                Start-Process -FilePath $ps -Verb RunAs -Wait -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$Root\scripts\hosts.ps1`" -Apply"
            } catch { Write-Warn2 "elevation was cancelled or failed: $($_.Exception.Message)" }
            & ipconfig /flushdns | Out-Null
            $bad = @($names | Where-Object { try { [void][Net.Dns]::GetHostAddresses($_); $false } catch { $true } })
        }
        if ($bad.Count -gt 0) { Stop-WithError 'hostnames still unresolved. In an elevated PowerShell run: powershell -ExecutionPolicy Bypass -File scripts\hosts.ps1 -Apply' }
    }
    Write-Ok ('hostnames resolve: {0}' -f ($names -join ', '))

    # Ports: free, or held by our own running service.
    $conflicts = @()
    foreach ($name in $Svc.Keys) {
        $s = $Svc[$name]
        if ($s.Port -eq 0 -or ($SkipWeb -and $s.Web)) { continue }
        if (Test-PidAlive (Get-SvcPid $name)) { continue }
        $l = Get-NetTCPConnection -LocalPort $s.Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($l) {
            $proc = Get-Process -Id $l.OwningProcess -ErrorAction SilentlyContinue
            $conflicts += [pscustomobject]@{ Service = $name; Port = $s.Port; Owner = $(if ($proc) { "$($proc.ProcessName) (pid $($proc.Id))" } else { "pid $($l.OwningProcess)" }) }
        }
    }
    $extra = @(9000, 2019) | Where-Object { -not (Test-PidAlive (Get-SvcPid 'keycloak')) -and (Get-NetTCPConnection -LocalPort $_ -State Listen -ErrorAction SilentlyContinue) }
    foreach ($port in $extra) { $conflicts += [pscustomobject]@{ Service = 'keycloak/caddy admin'; Port = $port; Owner = 'other process' } }
    $dockerHeld = @($conflicts | Where-Object { $_.Port -eq 80 -and $_.Owner -match 'docker|wslrelay|vpnkit' })
    if ($dockerHeld.Count -gt 0) {
        Write-Warn2 'port 80 is held by Docker (the compose stack of this repo is probably running)'
        if (Read-YesNo 'Stop the Docker stack (docker compose stop - data volumes are kept)?') {
            Write-Info 'running: docker compose stop'
            $code = Invoke-Native 'docker' 'docker' @('compose', 'stop') $Root
            if ($code -ne 0) { Stop-WithError 'docker compose stop failed' }
            Start-Sleep -Seconds 2
            $conflicts = @($conflicts | Where-Object { $_.Port -ne 80 -or (Get-NetTCPConnection -LocalPort 80 -State Listen -ErrorAction SilentlyContinue) })
        }
    }
    if ($conflicts.Count -gt 0) {
        $conflicts | ForEach-Object { Write-Fail ('port {0} ({1}) is in use by {2}' -f $_.Port, $_.Service, $_.Owner) }
        Stop-WithError 'free the ports above (docker compose stop, or stop the other program) and run startup.bat again'
    }
    Write-Ok 'required ports are free (or already ours)'
}

function Step-Secrets {
    Write-Step 'Preparing .env and secrets/ (scripts/init_secrets.py, natively)'
    Install-Uv
    $args0 = @('run', '--no-project', '--quiet', '--python', '3.14', '--with', 'cryptography==50.0.2', 'python', (Join-Path $Root 'scripts\init_secrets.py'), '--root', $Root)
    $code = Invoke-Native 'init' $T.Uv ($args0 + @('--check'))
    if ($code -ne 0) {
        Write-Info 'generating missing values (existing ones are kept)'
        $code = Invoke-Native 'init' $T.Uv $args0
        if ($code -ne 0) { Stop-WithError 'init_secrets.py failed' }
    }
    Import-DotEnv
    Write-Ok ('.env has {0} keys (values not shown)' -f $script:DotEnv.Count)
    # compose "secrets:" -> files named like /run/secrets/<name>
    $map = @{ keelson_client_assertion_key = 'keelson_client_assertion_key.pem'; keelson_receipt_key = 'keelson_receipt_key.pem'; keelson_data_key = 'keelson_data_key' }
    foreach ($k in $map.Keys) { Copy-Item -LiteralPath (Join-Path $Root "secrets\$($map[$k])") -Destination (Join-Path $P.Secrets $k) -Force }
    Write-Ok "engine secrets staged in $($P.Secrets)"
}

function Install-Uv {
    if ((Test-Path $T.Uv) -and (Test-Installed 'uv' $Versions.uv.Version)) { return }
    $zip = Get-Download 'uv' $Versions.uv
    Expand-ZipFile $zip (Join-Path $P.Tools 'uv')
    $exe = Get-ChildItem -LiteralPath (Join-Path $P.Tools 'uv') -Recurse -Filter uv.exe | Select-Object -First 1
    if ($exe.FullName -ne $T.Uv) { Copy-Item $exe.FullName $T.Uv -Force; Copy-Item (Join-Path $exe.DirectoryName 'uvx.exe') (Join-Path $P.Tools 'uv') -Force -ErrorAction SilentlyContinue }
    Set-Installed 'uv' $Versions.uv.Version
    Write-Ok ((& $T.Uv --version) -join ' ')
}

function Step-Tools {
    Write-Step 'Downloading and unpacking tools (first run only)'
    Install-Uv
    Write-Ok ('uv {0}' -f $Versions.uv.Version)

    if (-not (Test-Installed 'postgres' "$($Versions.postgres.Version)+$($Versions.postgis.Version)+$($Versions.pgvector.Version)+r2")) {
        $pgZip = Get-Download 'postgres' $Versions.postgres
        $gisZip = Get-Download 'postgis' $Versions.postgis
        $vecZip = Get-Download 'pgvector' $Versions.pgvector
        Remove-Item -LiteralPath (Join-Path $P.Tools 'pgsql') -Recurse -Force -ErrorAction SilentlyContinue
        Expand-ZipFile $pgZip $P.Tools @('pgsql\pgAdmin 4\*', 'pgsql\symbols\*', 'pgsql\doc\*', 'pgsql\StackBuilder\*')
        foreach ($pair in @(@('postgis', $gisZip), @('pgvector', $vecZip))) {
            $tmp = Join-Path $P.Cache "unpack-$($pair[0])"
            Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
            Expand-ZipFile $pair[1] $tmp
            # Archives hold bin/ lib/ share/ (possibly below one top folder): overlay onto pgsql/.
            $top = Get-ChildItem -LiteralPath $tmp -Directory | Where-Object { Test-Path (Join-Path $_.FullName 'lib') } | Select-Object -First 1
            $src = if (Test-Path (Join-Path $tmp 'lib')) { $tmp } elseif ($top) { $top.FullName } else { $null }
            if (-not $src) { Stop-WithError "unexpected layout of the $($pair[0]) archive (no lib/ folder) in $tmp" }
            foreach ($sub in @('bin', 'lib', 'share')) {
                $from = Join-Path $src $sub
                if (Test-Path $from) {
                    # Add files only, never replace: the PostGIS bundle ships its own OpenSSL/zlib/...
                    # DLLs, and overwriting EDB's breaks pgcrypto ("procedure could not be found").
                    Write-Detail "overlay $($pair[0]) $sub/ -> pgsql\$sub (new files only)"
                    & robocopy $from (Join-Path $P.Tools "pgsql\$sub") /E /XC /XN /XO /NFL /NDL /NJH /NJS /NP | Out-Null
                }
            }
            Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        }
        foreach ($need in @('lib\postgis-3.dll', 'lib\vector.dll', 'share\extension\vector.control', 'share\extension\postgis.control')) {
            if (-not (Test-Path (Join-Path $P.Tools "pgsql\$need"))) { Stop-WithError "pgsql\$need is missing after unpacking PostGIS/pgvector" }
        }
        Set-Installed 'postgres' "$($Versions.postgres.Version)+$($Versions.postgis.Version)+$($Versions.pgvector.Version)+r2"
    }
    Write-Ok ('postgres {0} + postgis {1} + pgvector {2}' -f $Versions.postgres.Version, $Versions.postgis.Version, $Versions.pgvector.Version)

    if (-not (Test-Installed 'redis' $Versions.redis.Version)) {
        $zip = Get-Download 'redis' $Versions.redis
        $dir = Join-Path $P.Tools 'redis'
        Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction SilentlyContinue
        $tmp = Join-Path $P.Cache 'unpack-redis'
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Expand-ZipFile $zip $tmp
        $exe = Get-ChildItem -LiteralPath $tmp -Recurse -Filter redis-server.exe | Select-Object -First 1
        if (-not $exe) { Stop-WithError 'redis-server.exe not found in the Redis archive' }
        Move-Item -LiteralPath $exe.DirectoryName -Destination $dir
        Remove-Item -LiteralPath $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Set-Installed 'redis' $Versions.redis.Version
    }
    Write-Ok ('redis {0} (Valkey stand-in)' -f $Versions.redis.Version)

    if (-not (Test-Installed 'keycloak' $Versions.keycloak.Version)) {
        $zip = Get-Download 'keycloak' $Versions.keycloak
        Remove-Item -LiteralPath (Join-Path $P.Tools 'keycloak') -Recurse -Force -ErrorAction SilentlyContinue
        Expand-ZipFile $zip (Join-Path $P.Tools 'keycloak')
        Set-Installed 'keycloak' $Versions.keycloak.Version
    }
    Write-Ok ('keycloak {0}' -f $Versions.keycloak.Version)

    if (-not (Test-Installed 'kcconfig' $Versions.kcconfig.Version)) {
        $jar = Get-Download 'keycloak-config-cli' $Versions.kcconfig
        [void][IO.Directory]::CreateDirectory((Split-Path $T.KcConfig))
        Copy-Item -LiteralPath $jar -Destination $T.KcConfig -Force
        Set-Installed 'kcconfig' $Versions.kcconfig.Version
    }
    Write-Ok ('keycloak-config-cli {0}' -f $Versions.kcconfig.Version)

    if (-not (Test-Installed 'caddy' $Versions.caddy.Version)) {
        $zip = Get-Download 'caddy' $Versions.caddy
        Expand-ZipFile $zip (Join-Path $P.Tools 'caddy')
        Set-Installed 'caddy' $Versions.caddy.Version
    }
    Write-Ok ('caddy {0}' -f $Versions.caddy.Version)

    if (-not $SkipWeb -and -not (Test-Installed 'pnpm' $Versions.pnpm.Version)) {
        Write-Info "installing pnpm $($Versions.pnpm.Version) into .native\tools\pnpm (npm)"
        $npm = (Get-Command npm.cmd -ErrorAction SilentlyContinue).Source
        $code = Invoke-Native 'npm' $npm @('install', '--prefix', (Join-Path $P.Tools 'pnpm'), "pnpm@$($Versions.pnpm.Version)", '--no-audit', '--no-fund', '--loglevel', 'warn')
        if ($code -ne 0 -or -not (Test-Path $T.Pnpm)) { Stop-WithError 'pnpm installation failed' }
        Set-Installed 'pnpm' $Versions.pnpm.Version
    }
    if (-not $SkipWeb) { Write-Ok ('pnpm {0}' -f $Versions.pnpm.Version) }

    # Everything is unpacked: the archives (~650 MB) are not needed any more (pinned, re-downloadable).
    $archives = @(Get-ChildItem -LiteralPath $P.Downloads -File -ErrorAction SilentlyContinue)
    if ($archives.Count -gt 0) {
        Write-Detail ('removing {0} downloaded archives ({1:N0} MB)' -f $archives.Count, (($archives | Measure-Object Length -Sum).Sum / 1MB))
        foreach ($a in $archives) { [IO.File]::Delete($a.FullName) }
    }
}

function Get-Stamp([string[]]$Files) {
    $sb = New-Object Text.StringBuilder
    foreach ($f in $Files) { if (Test-Path $f) { [void]$sb.Append((Get-FileHash -LiteralPath $f -Algorithm SHA256).Hash) } else { [void]$sb.Append("missing:$f") } }
    $bytes = [Text.Encoding]::UTF8.GetBytes($sb.ToString())
    return ([BitConverter]::ToString((New-Object Security.Cryptography.SHA256Managed).ComputeHash($bytes))).Replace('-', '')
}
function Get-GitStamp([string[]]$Paths) {
    # Content stamp of source folders: index entries + working-tree changes (incl. untracked files).
    $git = Get-Command git -ErrorAction SilentlyContinue
    if (-not $git) { return [guid]::NewGuid().ToString() }
    $text = (& git -C $Root ls-files -s -- @Paths) + (& git -C $Root status --porcelain --untracked-files=all -- @Paths) + (& git -C $Root diff -- @Paths) | Out-String
    $bytes = [Text.Encoding]::UTF8.GetBytes($text)
    return ([BitConverter]::ToString((New-Object Security.Cryptography.SHA256Managed).ComputeHash($bytes))).Replace('-', '')
}
function Test-Stamp([string]$Name, [string]$Stamp) {
    $f = Join-Path $P.State "$Name.stamp"
    return (-not $Rebuild) -and (Test-Path $f) -and ((Get-Content -LiteralPath $f -Raw).Trim() -eq $Stamp)
}
function Set-Stamp([string]$Name, [string]$Stamp) { Set-Content -LiteralPath (Join-Path $P.State "$Name.stamp") -Value $Stamp -Encoding ASCII }

$PyRecipe = 'py-recipe-2'   # bump when the install procedure changes: forces a reinstall

function Install-PyEnv([string]$Name, [string]$ProjectRel, [bool]$UsesContracts, [string[]]$Imports) {
    $proj = Join-Path $Root $ProjectRel
    $venv = Join-Path $P.Venvs $Name
    $stampFiles = @((Join-Path $proj 'uv.lock'), (Join-Path $proj 'pyproject.toml'))
    if ($UsesContracts) { $stampFiles += (Join-Path $Root 'packages\contracts-py\pyproject.toml') }
    $stamp = (Get-Stamp $stampFiles) + "-$PyRecipe"
    $py = Join-Path $venv 'Scripts\python.exe'
    if ((Test-Path $py) -and (Test-Stamp "venv-$Name" $stamp)) {
        Write-Ok "$Name venv is up to date"
        Test-PyImports $Name $py $Imports $true
        return
    }
    $t0 = Get-Date
    Write-Info "$Name : creating venv (Python 3.14) in $venv"
    $code = Invoke-Native 'uv' $T.Uv @('venv', $venv, '--python', '3.14', '--clear', '--quiet')
    if ($code -ne 0) { Stop-WithError "uv venv failed for $Name" }
    # The locks of optimizer/signal-hub are resolved for Linux only (tool.uv environments), so the
    # pinned versions are exported and installed for Windows instead of 'uv sync --frozen'.
    $req = Join-Path $P.Cache "requirements-$Name.txt"
    $exp = @('export', '--project', $proj, '--frozen', '--no-dev', '--no-hashes', '--no-emit-project', '--no-header', '--quiet', '-o', $req)
    if ($UsesContracts) { $exp += @('--no-emit-package', 'keelson-contracts') }
    $code = Invoke-Native 'uv' $T.Uv $exp
    if ($code -ne 0) { Stop-WithError "uv export failed for $Name" }
    # A Linux-only lock exports every pin as "pkg==x ; sys_platform == 'linux'", which Windows would
    # skip entirely: drop that marker (other markers stay). Windows-only transitive dependencies
    # missing from such a lock (colorama, tzdata, ...) are resolved by 'uv pip install' itself.
    $lines = foreach ($l in [IO.File]::ReadAllLines($req)) {
        $l = $l -replace "\s*;\s*sys_platform == 'linux'\s*$", ''
        $l = $l -replace "sys_platform == 'linux' and ", '' -replace " and sys_platform == 'linux'", ''
        $l
    }
    [IO.File]::WriteAllLines($req, [string[]]$lines)
    Write-Info ("$Name : installing {0} pinned packages" -f @($lines | Where-Object { $_ -match '^[A-Za-z0-9]' }).Count)
    $code = Invoke-Native 'uv' $T.Uv @('pip', 'install', '--python', $py, '-r', $req)
    if ($code -ne 0) { Stop-WithError "installing the $Name dependencies failed (see the output above)" }
    $local = @()
    if ($UsesContracts) { $local += @('-e', (Join-Path $Root 'packages\contracts-py')) }
    $local += @('-e', $proj)
    $code = Invoke-Native 'uv' $T.Uv (@('pip', 'install', '--python', $py, '--no-deps') + $local)
    if ($code -ne 0) { Stop-WithError "installing $Name itself failed" }
    Set-Stamp "venv-$Name" $stamp
    Write-Ok ('{0} venv installed in {1}' -f $Name, (Get-Elapsed $t0))
    Test-PyImports $Name $py $Imports $false
}

# Components Windows refused to load (Smart App Control / App Control for Business): name -> reason.
# They are skipped by 'start' and shown as 'blocked' by 'status'; everything else still runs.
$script:Blocked = @{}

function Test-PyImports([string]$Name, [string]$Py, [string[]]$Imports, [bool]$Cached) {
    # Smoke test, on every run: the service's modules and their native dependencies load on Windows.
    $code = Invoke-Native "$Name-import" $Py @('-c', "import importlib; [importlib.import_module(m) for m in '$($Imports -join ' ')'.split()]; print('imports ok: $($Imports -join ', ')')")
    if ($code -eq 0) { return }
    if ($script:SacHit) {
        $script:Blocked[$Name] = 'Windows Smart App Control blocked one of its native libraries'
        Write-Warn2 "$Name : Windows Smart App Control blocked a native library (see above) - $Name will be skipped"
        return
    }
    if ($Cached) { Stop-WithError "$Name venv is broken: importing $($Imports -join ', ') failed (see above). Retry with -Rebuild" }
    Stop-WithError "$Name venv is incomplete: importing $($Imports -join ', ') failed (see above)"
}

function Install-LiteLLM {
    $venv = Join-Path $P.Venvs 'litellm'
    $ver = $Versions.litellm.Version
    $py = Join-Path $venv 'Scripts\python.exe'
    $imports = @('litellm.proxy.proxy_server', 'prisma')
    if ((Test-Path (Join-Path $venv 'Scripts\litellm.exe')) -and (Test-Stamp 'venv-litellm' "$ver-$PyRecipe")) {
        Write-Ok 'litellm venv is up to date'
        Test-PyImports 'litellm' $py $imports $true
        return
    }
    $t0 = Get-Date
    Write-Info "litellm : creating venv (Python $($Versions.litellm.Python))"
    $code = Invoke-Native 'uv' $T.Uv @('venv', $venv, '--python', $Versions.litellm.Python, '--clear', '--quiet')
    if ($code -ne 0) { Stop-WithError 'uv venv failed for litellm' }
    $code = Invoke-Native 'uv' $T.Uv @('pip', 'install', '--python', $py, "litellm[proxy]==$ver", 'prisma')
    if ($code -ne 0) { Stop-WithError 'installing litellm failed' }
    # LiteLLM keeps keys/models/spend in Postgres through Prisma: generate the client once.
    $schema = Get-ChildItem -LiteralPath (Join-Path $venv 'Lib\site-packages\litellm\proxy') -Filter schema.prisma -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $schema) { Stop-WithError 'litellm/proxy/schema.prisma not found' }
    Write-Info 'litellm : prisma generate (downloads the Prisma engines once)'
    $code = Invoke-Native 'prisma' (Join-Path $venv 'Scripts\prisma.exe') @('generate', '--schema', $schema.FullName) $venv @{
        PRISMA_HOME_DIR = (Join-Path $P.Cache 'prisma'); PATH = ((Join-Path $venv 'Scripts') + ';' + $env:PATH)
    }
    if ($code -ne 0) {
        if ($script:SacHit) { $script:Blocked['litellm'] = 'Windows Smart App Control blocked the Prisma engine'; Write-Warn2 'litellm : Windows Smart App Control blocked the Prisma engine - litellm will be skipped'; return }
        Stop-WithError 'prisma generate failed for litellm'
    }
    Set-Stamp 'venv-litellm' "$ver-$PyRecipe"
    Write-Ok ('litellm venv installed in {0}' -f (Get-Elapsed $t0))
    Test-PyImports 'litellm' $py $imports $false
}

function Step-Python {
    Write-Step 'Python environments (installed from the uv.lock pins; skipped when unchanged)'
    Install-PyEnv 'engine' 'services\engine' $true @('keelson_engine.config', 'fastapi', 'asyncpg', 'redis', 'google.adk', 'uvicorn')
    Install-PyEnv 'merxa-api' 'services\merxa-api' $false @('merxa_api.app', 'asyncpg', 'uvicorn')
    Install-PyEnv 'optimizer' 'services\optimizer' $true @('keelson_opt.main', 'ortools')
    Install-PyEnv 'signal-hub' 'services\signal-hub' $true @('signal_hub.app', 'signal_hub.poller', 'signal_hub.bootstrap')
    Install-LiteLLM
    Save-Blocked
}

function Sync-Tree([string]$From, [string]$To, [string[]]$ExcludeDirs = @('node_modules', '.next', '.turbo', '.pnpm-store', 'coverage')) {
    $xd = if ($ExcludeDirs.Count -gt 0) { @('/XD') + $ExcludeDirs } else { @() }
    & robocopy $From $To /MIR @xd /NFL /NDL /NJH /NJS /NP | Out-Null
    if ($LASTEXITCODE -ge 8) { Stop-WithError "copying $From failed (robocopy $LASTEXITCODE)" }
}

function Invoke-Pnpm([string]$Label, [string]$Dir, [string[]]$Arguments, [hashtable]$Extra = @{}) {
    $env0 = @{ CI = 'true'; NEXT_TELEMETRY_DISABLED = '1' }
    foreach ($k in $Extra.Keys) { $env0[$k] = $Extra[$k] }
    return (Invoke-Native $Label 'node' (@($T.Pnpm) + $Arguments) $Dir $env0)
}

function Set-WebBlocked([string]$Name) {
    $script:Blocked[$Name] = 'Windows Smart App Control blocked a native Node module during the build'
    Write-Warn2 "$Name : Windows Smart App Control blocked a native module (see above) - $Name will be skipped"
}

function Build-KeelsonWeb {
    $dst = Join-Path $P.Build 'keelson'
    $paths = @('package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml', 'apps/keelson-web', 'packages/tokens-keelson', 'packages/ui-keelson', 'packages/viz', 'packages/contracts-ts')
    $stamp = Get-GitStamp $paths
    $server = Join-Path $dst 'apps\keelson-web\.next\standalone\apps\keelson-web\server.js'
    if ((Test-Path $server) -and (Test-Stamp 'web-keelson' $stamp)) { Write-Ok 'keelson-web build is up to date'; return }
    $t0 = Get-Date
    Write-Info "keelson-web : syncing sources -> $dst (repo node_modules are not touched)"
    [void][IO.Directory]::CreateDirectory($dst)
    foreach ($f in @('package.json', 'pnpm-lock.yaml', 'pnpm-workspace.yaml')) { Copy-Item -LiteralPath (Join-Path $Root $f) -Destination $dst -Force }
    foreach ($d in $paths | Where-Object { $_ -like '*/*' }) { Sync-Tree (Join-Path $Root $d) (Join-Path $dst $d) }
    Write-Info 'keelson-web : pnpm install --frozen-lockfile'
    $store = Join-Path $P.Cache 'pnpm-store'
    if ((Invoke-Pnpm 'pnpm' $dst @('install', '--frozen-lockfile', '--store-dir', $store, '--reporter', 'append-only')) -ne 0) { if ($script:SacHit) { Set-WebBlocked 'keelson-web'; return }; Stop-WithError 'pnpm install failed for keelson-web' }
    Write-Info 'keelson-web : next build (production, standalone) - takes a few minutes'
    if ((Invoke-Pnpm 'next' $dst @('--filter', '@keelson/web', 'build') @{ NODE_ENV = 'production'; NODE_OPTIONS = '--max-old-space-size=4096' }) -ne 0) { if ($script:SacHit) { Set-WebBlocked 'keelson-web'; return }; Stop-WithError 'next build failed for keelson-web' }
    if (-not (Test-Path $server)) { Stop-WithError "standalone server not found: $server" }
    # Standalone layout (as in apps/keelson-web/Dockerfile): static + public next to server.js.
    $app = Join-Path $dst 'apps\keelson-web'
    Sync-Tree (Join-Path $app '.next\static') (Join-Path $app '.next\standalone\apps\keelson-web\.next\static') @()
    Sync-Tree (Join-Path $app 'public') (Join-Path $app '.next\standalone\apps\keelson-web\public') @()
    Set-Stamp 'web-keelson' $stamp
    Write-Ok ('keelson-web built in {0}' -f (Get-Elapsed $t0))
}

function Build-MerxaWeb {
    $dst = Join-Path $P.Build 'merxa-web'
    $stamp = Get-GitStamp @('apps/merxa-web')
    $server = Join-Path $dst '.next\standalone\server.js'
    if ((Test-Path $server) -and (Test-Stamp 'web-merxa' $stamp)) { Write-Ok 'merxa-web build is up to date'; return }
    $t0 = Get-Date
    Write-Info "merxa-web : syncing sources -> $dst"
    Sync-Tree (Join-Path $Root 'apps\merxa-web') $dst
    Write-Info 'merxa-web : pnpm install --frozen-lockfile'
    $store = Join-Path $P.Cache 'pnpm-store'
    if ((Invoke-Pnpm 'pnpm' $dst @('install', '--frozen-lockfile', '--store-dir', $store, '--reporter', 'append-only')) -ne 0) { if ($script:SacHit) { Set-WebBlocked 'merxa-web'; return }; Stop-WithError 'pnpm install failed for merxa-web' }
    Write-Info 'merxa-web : next build (production, standalone)'
    if ((Invoke-Pnpm 'next' $dst @('build') @{ NODE_ENV = 'production'; NODE_OPTIONS = '--max-old-space-size=4096' }) -ne 0) { if ($script:SacHit) { Set-WebBlocked 'merxa-web'; return }; Stop-WithError 'next build failed for merxa-web' }
    if (-not (Test-Path $server)) { Stop-WithError "standalone server not found: $server" }
    Sync-Tree (Join-Path $dst '.next\static') (Join-Path $dst '.next\standalone\.next\static') @()
    Sync-Tree (Join-Path $dst 'public') (Join-Path $dst '.next\standalone\public') @()
    Set-Stamp 'web-merxa' $stamp
    Write-Ok ('merxa-web built in {0}' -f (Get-Elapsed $t0))
}

function Step-Web {
    Write-Step 'Web apps: pnpm install + next build (skipped when the sources are unchanged)'
    if ($SkipWeb) { Write-Info 'skipped (-SkipWeb)'; return }
    Build-KeelsonWeb
    Build-MerxaWeb
    Save-Blocked
}

function Step-Postgres {
    Write-Step 'Postgres 18 (one native cluster for core, litellm and merxa)'
    $data = Join-Path $P.Data 'pg'
    $super = V PG_CORE_SUPERPASSWORD
    if (-not $super) { Stop-WithError 'PG_CORE_SUPERPASSWORD is missing in .env' }
    if (-not (Test-Path (Join-Path $data 'PG_VERSION'))) {
        Write-Info "initdb (first run): $data"
        $pw = Join-Path $P.Run 'pg-pw.tmp'
        [IO.File]::WriteAllText($pw, $super)
        try {
            $code = Invoke-Native 'initdb' (PgExe 'initdb') @('-D', $data, '-U', 'postgres', '--pwfile', $pw, '-A', 'scram-sha-256', '-E', 'UTF8', '--no-locale')
        } finally { Remove-Item -LiteralPath $pw -Force -ErrorAction SilentlyContinue }
        if ($code -ne 0) { Stop-WithError 'initdb failed' }
    }
    if (Test-SvcHealthy 'postgres') {
        Write-Info ('postgres: already running (pid {0})' -f (Get-SvcPid 'postgres'))
    } else {
        # postgres.exe directly, like every other service (pg_ctl start keeps the caller's output
        # pipe open through the postmaster, so a captured 'pg_ctl start' never returns).
        Start-NativeService 'postgres' (PgExe 'postgres') @('-D', $data, '-p', '5432', '-c', 'listen_addresses=127.0.0.1', '-c', 'max_connections=250', '-c', 'shared_buffers=128MB', '-c', 'work_mem=8MB')
        if (-not (Wait-Services @('postgres') 90)) { Stop-WithError 'Postgres did not start' }
    }
    # Roles, databases and extensions - idempotent, re-applied on every start so passwords follow .env.
    Write-Info 'roles + databases: keelson, adk, keycloak, litellm, merxa; extensions postgis, vector, btree_gist, pgcrypto'
    $env1 = @{
        PGPASSWORD = $super; PGHOST = '127.0.0.1'; PGPORT = '5432'; PGUSER = 'postgres'
        KEELSON_DB_PASSWORD = V KEELSON_DB_PASSWORD; ADK_DB_PASSWORD = V ADK_DB_PASSWORD; KEYCLOAK_DB_PASSWORD = V KEYCLOAK_DB_PASSWORD
        LITELLM_DB_PASSWORD = V LITELLM_DB_PASSWORD; MERXA_DB_PASSWORD = V MERXA_DB_PASSWORD
    }
    $code = Invoke-Native 'psql' (PgExe 'psql') @('-X', '-q', '-d', 'postgres', '-f', (Join-Path $PSScriptRoot 'pg-init.sql')) $PSScriptRoot $env1
    if ($code -ne 0) { Stop-WithError 'database initialisation failed (pg-init.sql)' }
    Write-Ok 'databases ready'
}

function Step-Edge {
    Write-Step 'Redis (Valkey stand-in) + Caddy (http://*.localhost on port 80)'
    $rdata = Join-Path $P.Data 'redis'
    [void][IO.Directory]::CreateDirectory($rdata)
    Start-NativeService 'redis' $T.Redis @('--port', '6379', '--bind', '127.0.0.1', '--requirepass', (V VALKEY_PASSWORD), '--appendonly', 'yes', '--dir', $rdata.Replace('\', '/'), '--maxmemory', '128mb', '--maxmemory-policy', 'noeviction') @{} $rdata

    # Caddy: the repo Caddyfile with native upstreams (env overrides it already supports) and
    # loopback-only listeners (no firewall prompt, nothing exposed on the LAN).
    $cf = [IO.File]::ReadAllText((Join-Path $Root 'infra\caddy\Caddyfile'))
    $cf = $cf.Replace("auto_https off`n", "auto_https off`n`tdefault_bind 127.0.0.1 [::1]`n").Replace("auto_https off`r`n", "auto_https off`r`n`tdefault_bind 127.0.0.1 [::1]`r`n")
    $nativeCf = Join-Path $P.Run 'Caddyfile'
    [IO.File]::WriteAllText($nativeCf, $cf, (New-Object Text.UTF8Encoding($false)))
    Start-NativeService 'caddy' $T.Caddy @('run', '--config', $nativeCf, '--adapter', 'caddyfile') @{
        ENGINE_API_UPSTREAM = '127.0.0.1:8000'; KEELSON_WEB_UPSTREAM = '127.0.0.1:3000'
        MERXA_API_UPSTREAM = '127.0.0.1:8100'; MERXA_WEB_UPSTREAM = '127.0.0.1:3001'
        KEYCLOAK_UPSTREAM = '127.0.0.1:8080'; LITELLM_UPSTREAM = '127.0.0.1:4000'; PHOENIX_UPSTREAM = '127.0.0.1:6006'
        DEV_ALLOW_KC_ADMIN = V DEV_ALLOW_KC_ADMIN '0'
    }
    if (-not (Wait-Services @('redis', 'caddy') 60)) { Stop-WithError 'Redis/Caddy did not start' }
}

function Step-Core {
    Write-Step 'Keycloak, LiteLLM, Merxa API, optimizer (started together)'

    $kc = @{
        KC_DB = 'postgres'; KC_DB_URL = 'jdbc:postgresql://127.0.0.1:5432/keycloak'; KC_DB_USERNAME = 'keycloak'; KC_DB_PASSWORD = V KEYCLOAK_DB_PASSWORD
        KC_HOSTNAME = 'http://auth.localhost'; KC_HOSTNAME_BACKCHANNEL_DYNAMIC = 'true'; KC_HTTP_ENABLED = 'true'
        KC_PROXY_HEADERS = 'xforwarded'; KC_HEALTH_ENABLED = 'true'; KC_CACHE = 'local'
        KC_BOOTSTRAP_ADMIN_USERNAME = 'admin'; KC_BOOTSTRAP_ADMIN_PASSWORD = V KC_ADMIN_PASSWORD
        JAVA_OPTS_KC_HEAP = '-XX:MaxRAMPercentage=60 -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=20 -Xmx900m'
    }
    Start-NativeService 'keycloak' $T.Keycloak @('start-dev', '--http-host=127.0.0.1', '--http-port=8080') $kc (Split-Path $T.Keycloak)

    $ll = @{
        DATABASE_URL = 'postgresql://litellm:{0}@127.0.0.1:5432/litellm' -f (V LITELLM_DB_PASSWORD)
        LITELLM_MASTER_KEY = V LITELLM_MASTER_KEY; LITELLM_SALT_KEY = V LITELLM_SALT_KEY; STORE_MODEL_IN_DB = 'True'
        GEMINI_API_KEY = '<host>'; UI_USERNAME = 'admin'; UI_PASSWORD = V LITELLM_UI_PASSWORD
        PROXY_BASE_URL = 'http://gateway.localhost'; LITELLM_LOG = 'INFO'; PRISMA_HOME_DIR = (Join-Path $P.Cache 'prisma')
        PATH = ((Join-Path $P.Venvs 'litellm\Scripts') + ';' + $env:PATH)   # litellm runs 'prisma migrate deploy'
        PYTHONUTF8 = '1'; PYTHONUNBUFFERED = '1'
    }
    Start-NativeService 'litellm' (VenvExe 'litellm' 'litellm.exe') @('--config', (Join-Path $Root 'infra\litellm\config.yaml'), '--host', '127.0.0.1', '--port', '4000', '--num_workers', '1') $ll

    $snap = Join-Path $P.Data 'merxa-snapshots'
    [void][IO.Directory]::CreateDirectory($snap)
    $mx = @{
        PORT = '8100'; MERXA_DATABASE_URL = 'postgresql+asyncpg://merxa:{0}@127.0.0.1:5432/merxa' -f (V MERXA_DB_PASSWORD)
        OIDC_ISSUER = 'http://auth.localhost/realms/merxa'; MCP_RESOURCE = 'http://merxa.localhost/mcp'; PUBLIC_URL = 'http://merxa.localhost'
        TRUSTED_RECEIPT_ISSUERS = 'http://keelson.localhost'; KEELSON_JWKS_URL = 'http://keelson.localhost/api/.well-known/jwks.json'
        MERXA_WEB_CLIENT_SECRET = V MERXA_WEB_CLIENT_SECRET; MERXA_DEMO_CLIENT_SECRET = V MERXA_DEMO_CLIENT_SECRET
        SESSION_SECRET = V SESSION_SECRET; DEMO_MODE = V DEMO_MODE '1'; SEED_PROFILE = V SEED_PROFILE 'full'
        DEMO_USER_PASSWORD = V DEMO_USER_PASSWORD; MERXA_WEBHOOK_SECRET = V MERXA_WEBHOOK_SECRET
        KEELSON_WEBHOOK_URL = 'http://keelson.localhost/api/webhooks/demo-retail/merxa'; MERXA_SNAPSHOT_SECRET = V MERXA_SNAPSHOT_SECRET
        SNAPSHOT_DIR = $snap; PUBLIC_BASE_DOMAIN = V PUBLIC_BASE_DOMAIN 'localhost'; MERXA_SECRETS_DIR = (Join-Path $P.Run 'no-secrets')
        PYTHONUTF8 = '1'; PYTHONUNBUFFERED = '1'
    }
    Start-NativeService 'merxa-api' (VenvExe 'merxa-api' 'python.exe') @('-m', 'merxa_api') $mx

    $op = @{ PORT = '8200'; OPTIMIZER_SHARED_SECRET = V OPTIMIZER_SHARED_SECRET; CPSAT_NUM_WORKERS = '1'; PYTHONUTF8 = '1'; PYTHONUNBUFFERED = '1' }
    Start-NativeService 'optimizer' (VenvExe 'optimizer' 'keelson-optimizer.exe') @() $op

    Write-Info 'Keycloak needs ~1 minute on the first start (it builds its server image), LiteLLM runs its DB migrations'
    if (-not (Wait-Services (Select-Runnable @('keycloak', 'litellm', 'merxa-api', 'optimizer')) ([math]::Max($TimeoutSec, 420)))) { Stop-WithError 'core services did not become ready' }
}

# service -> the venv / build it runs from (for the Smart App Control 'blocked' bookkeeping)
$Component = @{
    'litellm' = 'litellm'; 'merxa-api' = 'merxa-api'; 'optimizer' = 'optimizer'
    'signal-hub-api' = 'signal-hub'; 'signal-hub-poller' = 'signal-hub'
    'engine-api' = 'engine'; 'engine-worker' = 'engine'; 'keelson-web' = 'keelson-web'; 'merxa-web' = 'merxa-web'
}
function Get-BlockedReason([string]$Service) {
    $c = $Component[$Service]
    if ($c -and $script:Blocked.ContainsKey($c)) { return $script:Blocked[$c] }
    return $null
}
function Select-Runnable([string[]]$Names) {
    # Names minus the services whose component Windows blocked (they are not started at all).
    return @($Names | Where-Object { -not (Get-BlockedReason $_) })
}
function Save-Blocked {
    $f = Join-Path $P.State 'blocked.txt'
    $lines = foreach ($k in $script:Blocked.Keys) { "$k`t$($script:Blocked[$k])" }
    [IO.File]::WriteAllLines($f, [string[]]@($lines))
}
function Import-Blocked {
    $script:Blocked = @{}
    $f = Join-Path $P.State 'blocked.txt'
    if (Test-Path $f) { foreach ($l in [IO.File]::ReadAllLines($f)) { $p2 = $l.Split("`t", 2); if ($p2.Count -eq 2) { $script:Blocked[$p2[0]] = $p2[1] } } }
}

function Step-Jobs {
    Write-Step 'One-shot jobs: keycloak-config-cli, signal-hub bootstrap, engine bootstrap'

    Write-Info 'keycloak-config-cli: realms keelson + merxa, clients, demo users'
    $cfgDir = (Join-Path $Root 'infra\keycloak\config').Replace('\', '/')
    $kcc = @{
        KEYCLOAK_URL = 'http://127.0.0.1:8080'; KEYCLOAK_USER = 'admin'; KEYCLOAK_PASSWORD = V KC_ADMIN_PASSWORD
        KEYCLOAK_AVAILABILITYCHECK_ENABLED = 'true'; KEYCLOAK_AVAILABILITYCHECK_TIMEOUT = '180s'
        IMPORT_FILES_LOCATIONS = "file:$cfgDir/*.yaml"; IMPORT_VARSUBSTITUTION_ENABLED = 'true'
        IMPORT_REMOTESTATE_ENABLED = 'true'; IMPORT_MANAGED_CLIENTSCOPE = 'no-delete'; IMPORT_BEHAVIORS_REMOVEDEFAULTROLEFROMUSER = 'false'
        DEMO_ENABLED = V DEMO_ENABLED 'true'
        KEELSON_BFF_CLIENT_SECRET = V KEELSON_BFF_CLIENT_SECRET; KEELSON_DEMO_CLIENT_SECRET = V KEELSON_DEMO_CLIENT_SECRET
        MERXA_WEB_CLIENT_SECRET = V MERXA_WEB_CLIENT_SECRET; MERXA_DEMO_CLIENT_SECRET = V MERXA_DEMO_CLIENT_SECRET
        DEMO_USER_PASSWORD = V DEMO_USER_PASSWORD
    }
    $t0 = Get-Date
    $code = Invoke-Native 'kc-config' 'java' @('-XX:MaxRAMPercentage=60', '-jar', $T.KcConfig) $Root $kcc
    if ($code -ne 0) { Stop-WithError "keycloak-config-cli failed (exit $code)" }
    Write-Ok ('kc-config completed in {0}' -f (Get-Elapsed $t0))

    Write-Info 'signal-hub bootstrap: role + database signals, PostGIS, migrations, LiteLLM extraction key'
    $sh = Get-SignalHubEnv
    $sh['SIGNALS_ADMIN_DSN'] = 'postgresql://postgres:{0}@127.0.0.1:5432/postgres' -f (V PG_CORE_SUPERPASSWORD)
    $sh['SIGNALS_DB_PASSWORD'] = V SIGNALS_DB_PASSWORD
    $sh['LITELLM_MASTER_KEY'] = V LITELLM_MASTER_KEY
    $sh['SIGNALS_LLM_MAX_BUDGET'] = V SIGNALS_LLM_MAX_BUDGET '5'
    $sh['SIGNALS_LITELLM_WAIT_S'] = V SIGNALS_LITELLM_WAIT_S '180'
    $t0 = Get-Date
    $code = Invoke-Native 'signal-hub-bootstrap' (VenvExe 'signal-hub' 'python.exe') @('-m', 'signal_hub.bootstrap') $Root $sh
    if ($code -ne 0) { Stop-WithError "signal-hub bootstrap failed (exit $code)" }
    Write-Ok ('signal-hub bootstrap completed in {0}' -f (Get-Elapsed $t0))

    Write-Info 'engine bootstrap: migrations, LiteLLM model aliases (models.lock), tenants, connectors, snapshot sync, KB seed'
    $be = @{}
    foreach ($k in $script:DotEnv.Keys) { $be[$k] = $script:DotEnv[$k] }   # compose: env_file .env ...
    $ee = Get-EngineEnv
    foreach ($k in $ee.Keys) { $be[$k] = $ee[$k] }                          # ... overridden by engine-env
    $t0 = Get-Date
    $code = Invoke-Native 'bootstrap' (VenvExe 'engine' 'python.exe') @('-m', 'keelson_engine.bootstrap') $Root $be
    if ($code -ne 0) { Stop-WithError "engine bootstrap failed (exit $code)" }
    Write-Ok ('engine bootstrap completed in {0}' -f (Get-Elapsed $t0))
}

function Step-Apps {
    Write-Step 'Engine API + worker, Signal Hub API + poller, web apps'

    $sh = Get-SignalHubEnv
    Start-NativeService 'signal-hub-api' (VenvExe 'signal-hub' 'signal-hub-api.exe') @() $sh
    $po = Get-SignalHubEnv
    $po['LITELLM_SIGNALS_KEY'] = ''; $po['SIGNAL_HUB_SHARED_SECRET'] = ''; $po['SIGNALS_DATA_KEY'] = ''   # least privilege, as in compose
    Start-NativeService 'signal-hub-poller' (VenvExe 'signal-hub' 'python.exe') @('-m', 'signal_hub.poller') $po

    $api = Get-EngineEnv
    $api['EVALS_RESULTS_DIR'] = Join-Path $Root 'evals\results'
    Start-NativeService 'engine-api' (VenvExe 'engine' 'python.exe') @('-m', 'uvicorn', 'keelson_engine.api.main:app', '--host', '127.0.0.1', '--port', '8000', '--proxy-headers', '--forwarded-allow-ips', '*', '--timeout-graceful-shutdown', '5') $api
    Start-NativeService 'engine-worker' (VenvExe 'engine' 'python.exe') @('-m', 'keelson_engine.worker') ((Get-EngineEnv))

    $names = @('signal-hub-api', 'signal-hub-poller', 'engine-api', 'engine-worker')
    if (-not $SkipWeb) {
        $kwDir = Join-Path $P.Build 'keelson\apps\keelson-web\.next\standalone\apps\keelson-web'
        Start-NativeService 'keelson-web' 'node' @('server.js') (@{ PORT = '3000'; HOSTNAME = '127.0.0.1'; NODE_ENV = 'production'; NEXT_TELEMETRY_DISABLED = '1'; ENGINE_API_INTERNAL_URL = 'http://127.0.0.1:8000' }) $kwDir
        $mwDir = Join-Path $P.Build 'merxa-web\.next\standalone'
        Start-NativeService 'merxa-web' 'node' @('server.js') (@{ PORT = '3001'; HOSTNAME = '127.0.0.1'; NODE_ENV = 'production'; NEXT_TELEMETRY_DISABLED = '1'; MERXA_API_INTERNAL_URL = 'http://127.0.0.1:8100' }) $mwDir
        $names += @('keelson-web', 'merxa-web')
    }
    if (-not (Wait-Services (Select-Runnable $names))) { Stop-WithError 'application services did not become ready' }
}

function Show-Status([switch]$Final) {
    $rows = foreach ($name in $Svc.Keys) {
        $s = $Svc[$name]
        if ($SkipWeb -and $s.Web) { continue }
        $procId = Get-SvcPid $name
        $alive = Test-PidAlive $procId
        $healthy = if ($alive -or $name -eq 'postgres') { Test-SvcHealthy $name } else { $false }
        [pscustomobject]@{
            Service = $name
            Status  = $(if ($healthy) { 'healthy' } elseif ($alive) { 'starting/unhealthy' } elseif (Get-BlockedReason $name) { 'BLOCKED by Smart App Control' } else { 'stopped' })
            PID     = $(if ($alive) { $procId } else { '' })
            Port    = $(if ($s.Port) { $s.Port } else { '' })
            Log     = Join-Path $P.Logs "$name.log"
        }
    }
    $text = ($rows | Format-Table -AutoSize | Out-String).TrimEnd()
    Write-Host $text
    if ($script:LogFile) { [IO.File]::AppendAllText($script:LogFile, $text + "`r`n") }
    if (-not $Final -and @($rows | Where-Object { $_.Status -ne 'stopped' -and $_.Status -notlike 'BLOCKED*' }).Count -eq 0) {
        Write-Host "The native stack is not running (native folder: $Native). Start it with: startup.bat" -ForegroundColor Yellow
    }
    return @($rows | Where-Object { $_.Status -ne 'healthy' }).Count
}

function Stop-All {
    Write-Log 'Stopping the native stack (data in .native\data is kept)' 'Cyan'
    $order = @($Svc.Keys); [array]::Reverse($order)
    foreach ($name in $order) {
        if ($name -eq 'postgres') { continue }
        $procId = Get-SvcPid $name
        if (Test-PidAlive $procId) {
            & taskkill /T /F /PID $procId 2>&1 | Out-Null
            Write-Info ('{0}: stopped (pid {1})' -f $name, $procId)
        }
        Remove-Item -LiteralPath (Join-Path $P.Run "$name.pid") -Force -ErrorAction SilentlyContinue
    }
    $data = Join-Path $P.Data 'pg'
    if ((Test-Path (PgExe 'pg_ctl')) -and (Test-Path (Join-Path $data 'postmaster.pid'))) {
        $code = Invoke-Native 'pg_ctl' (PgExe 'pg_ctl') @('stop', '-D', $data, '-m', 'fast', '-w')
        if ($code -eq 0) { Write-Info 'postgres: stopped' } else { Write-Warn2 'pg_ctl stop reported an error' }
    }
    Remove-Item -LiteralPath (Join-Path $P.Run 'postgres.pid') -Force -ErrorAction SilentlyContinue
    Write-Ok 'native stack stopped'
}

# ----------------------------- main -----------------------------
Initialize-Dirs
Import-DotEnv
switch ($Command) {
    'status' {
        Import-Blocked
        $bad = Show-Status
        exit $bad
    }
    'logs' {
        if (-not $Service) {
            Get-ChildItem -LiteralPath $P.Logs -Filter *.log | Sort-Object LastWriteTime -Descending | Format-Table Name, Length, LastWriteTime -AutoSize
            Write-Host 'usage: startup.bat logs <service>   (follows the log; Ctrl+C to stop)'
            exit 0
        }
        $f = Join-Path $P.Logs "$Service.log"
        if (-not (Test-Path $f)) { Write-Host "no log for '$Service' ($f)" -ForegroundColor Red; exit 1 }
        Get-Content -LiteralPath $f -Tail 60 -Wait
        exit 0
    }
    'stop' {
        Stop-All
        exit 0
    }
}

if ($Command -eq 'restart') { Stop-All }
$script:LogFile = Join-Path $P.Logs ('startup-{0:yyyyMMdd-HHmmss}.log' -f (Get-Date))
Write-Log ('Keelson + Merxa - native (no Docker) {0}. Log: {1}' -f $Command, $script:LogFile) 'Cyan'
if ($Command -eq 'fetch') { $script:StepTotal = 5 }

Step-Prerequisites
Step-Secrets
Step-Tools
Step-Python
Step-Web
function Write-BlockedSummary {
    if ($script:Blocked.Count -eq 0) { return }
    Write-Log ''
    Write-Log 'Blocked by Windows Smart App Control (skipped):' 'Yellow'
    foreach ($k in $script:Blocked.Keys) { Write-Log ('  {0,-12} {1}' -f $k, $script:Blocked[$k]) 'Yellow' }
    if ($script:Blocked.ContainsKey('optimizer')) { Write-Log '  without the optimizer, cases stop at the impact / option step (investigation and chat still work)' 'Yellow' }
    Write-Log '  Options: run that service from Docker (docker compose up -d optimizer), or decide yourself whether to change' 'Yellow'
    Write-Log '  Smart App Control (Windows Security > App & browser control). Details: Event Viewer > CodeIntegrity/Operational.' 'Yellow'
}

if ($Command -eq 'fetch') {
    Write-Log ('      step finished in {0}' -f (Get-Elapsed $script:StepT0)) 'DarkGray'
    Write-BlockedSummary
    Write-Log ('Everything is downloaded, installed and built in {0}. Start with: startup.bat' -f (Get-Elapsed)) 'Green'
    exit $(if ($script:Blocked.Count -gt 0) { 2 } else { 0 })
}
Step-Postgres
Step-Edge
Step-Core
Step-Jobs
Step-Apps

Write-Step 'Status'
$bad = Show-Status -Final
Write-Log ''
Write-Log 'Public URLs (HTTP, via Caddy on port 80):' 'Cyan'
Write-Log '  http://keelson.localhost   Keelson (engine UI + /api)'
Write-Log '  http://merxa.localhost     Merxa Retail Suite (mock ERP)'
Write-Log '  http://auth.localhost      Keycloak (realms keelson, merxa)'
Write-Log '  http://gateway.localhost   LiteLLM console (platform-admin)'
Write-Log 'Demo password: Select-String -Path .env -Pattern ''^DEMO_USER_PASSWORD='''
Write-Log 'Manage: startup.bat status | startup.bat logs <service> | startup.bat stop'
Write-Log ("Startup log: {0}" -f $script:LogFile)
Write-BlockedSummary
$blockedSvcs = @($Svc.Keys | Where-Object { Get-BlockedReason $_ }).Count
if ($bad - $blockedSvcs -gt 0) { Write-Log ("{0} service(s) not healthy after {1}" -f ($bad - $blockedSvcs), (Get-Elapsed)) 'Red'; exit 1 }
if ($blockedSvcs -gt 0) { Write-Log ("Native stack is up in {0}, except {1} blocked service(s)." -f (Get-Elapsed), $blockedSvcs) 'Yellow'; exit 2 }
Write-Log ("Native stack is up in {0}." -f (Get-Elapsed)) 'Green'
exit 0
