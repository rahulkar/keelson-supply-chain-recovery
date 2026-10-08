<#
.SYNOPSIS
  Generate .env and secrets/ for the Keelson stack (idempotent).

.DESCRIPTION
  Runs scripts/init_secrets.py inside python:3.14-slim-trixie (with cryptography==50.0.2)
  so nothing is installed on the host. Existing values in .env and existing key files
  in secrets/ are never overwritten. GEMINI_API_KEY is never written to disk.

  Progress is logged as "[HH:mm:ss +elapsed] [n/5] ...": checking docker, the container
  image (local or pulled), installing cryptography, running init_secrets.py, result.

.PARAMETER Check
  Validate only; exit code 1 when anything is missing or invalid.

.EXAMPLE
  pwsh scripts/init.ps1
  powershell -ExecutionPolicy Bypass -File scripts\init.ps1 -Check
#>
[CmdletBinding()]
param([switch]$Check)

# Native tools (docker) write progress/logs to stderr; with 'Stop' Windows PowerShell 5.1
# would turn that into terminating errors. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$mount = $root.Replace([char]92, [char]47)   # D:\primark -> D:/primark (Docker Desktop)
$image = 'python:3.14-slim-trixie'
$cryptographyVersion = '50.0.2'
$extra = if ($Check) { '--check' } else { '' }

# -- logging: "[HH:mm:ss +elapsed] [n/5] message" ------------------------------------------------
$inv = [Globalization.CultureInfo]::InvariantCulture
$script:t0 = Get-Date
$script:stepNo = 0
$script:stepTotal = 5

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
}
function Write-Step([string]$msg) { $script:stepNo++; Write-Log "==> $msg" 'Cyan' }
# Native output line (string or stderr ErrorRecord) -> plain text.
function ConvertTo-PlainText($item) {
    $text = if ($item -is [System.Management.Automation.ErrorRecord]) { $item.Exception.Message } else { "$item" }
    return ($text -replace '\x1b\[[0-9;?]*[ -/]*[@-~]', '').TrimEnd("`r", "`n")
}

Write-Log ("{0} .env and secrets/ in {1}" -f $(if ($Check) { 'Checking (read-only)' } else { 'Completing (existing values are kept)' }), $root)

# -- 1. docker ---------------------------------------------------------------------------
Write-Step 'Checking docker'
$dockerCmd = Get-Command docker -ErrorAction SilentlyContinue
if (-not $dockerCmd) {
    Write-Log 'ERROR: docker CLI not found' 'Red'
    throw 'docker CLI not found - install/start Docker Desktop first.'
}
Write-Log "docker CLI: $($dockerCmd.Source)"
$serverVersion = @(& docker version --format '{{.Server.Version}}' 2>$null)
$engineUp = ($LASTEXITCODE -eq 0)
if ($engineUp) { Write-Log "Docker engine: $($serverVersion[0])" }
else { Write-Log 'WARN: Docker engine is not reachable - start Docker Desktop (docker run below will fail).' 'Yellow' }

# -- 2. image ----------------------------------------------------------------------------
Write-Step "Container image $image"
if ($engineUp) {
    $img = @(& docker image inspect --format '{{.Id}}|{{.Size}}|{{.Created}}' $image 2>$null)
    if ($LASTEXITCODE -eq 0 -and $img.Count -gt 0) {
        $p = "$($img[0])".Split('|')
        $sizeMb = if ($p.Count -gt 1 -and $p[1] -match '^\d+$') { ([double]$p[1] / 1MB).ToString('0', $inv) + ' MB' } else { '?' }
        $id = if ($p[0].Length -gt 19) { $p[0].Substring(0, 19) } else { $p[0] }
        Write-Log ("Using local image ({0}, {1}, created {2})" -f $id, $sizeMb, $(if ($p.Count -gt 2) { $p[2] } else { '?' }))
    } else {
        Write-Log 'Not present locally - pulling it (one-time download) ...'
        & docker pull $image 2>&1 | ForEach-Object { Write-Log ('    ' + (ConvertTo-PlainText $_)) }
        if ($LASTEXITCODE -ne 0) { Write-Log "WARN: docker pull failed (exit $LASTEXITCODE); docker run will retry." 'Yellow' }
    }
}

# -- 3./4. pip install + init_secrets.py (one container; '::step::' lines become step headers) --
Write-Log "Mount ${mount} -> /work, pip cache volume keelson_pipcache, --memory 256m"
$cmd = "echo '::step::Installing cryptography==$cryptographyVersion in the container (pip cache volume keelson_pipcache)'" +
    " && pip install -q cryptography==$cryptographyVersion" +
    " && echo '::step::Running init_secrets.py --root /work $extra (values are never printed)'" +
    " && python scripts/init_secrets.py --root /work $extra"
& docker run --rm --memory 256m `
    -v "${mount}:/work" `
    -v keelson_pipcache:/root/.cache/pip `
    -w /work `
    -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_ROOT_USER_ACTION=ignore `
    $image sh -c $cmd 2>&1 | ForEach-Object {
    $text = ConvertTo-PlainText $_
    if ($text -match '^::step::(.*)$') { Write-Step $Matches[1] } elseif ($text.Trim()) { Write-Log "    $text" }
}
$code = $LASTEXITCODE

# -- 5. result ---------------------------------------------------------------------------
$script:stepNo = $script:stepTotal - 1
Write-Step 'Result'
if ($code -ne 0) {
    $what = if ($Check) { 'check found missing/invalid values' } else { 'failed' }
    Write-Log "init_secrets.py $what (exit $code)" 'Red'
    exit $code
}
Write-Log "OK: $root\.env and $root\secrets\ are ready (values not shown)." 'Green'
exit 0
