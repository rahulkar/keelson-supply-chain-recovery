<#
.SYNOPSIS
  Generate .env and secrets/ for the Keelson stack (idempotent).

.DESCRIPTION
  Runs scripts/init_secrets.py inside python:3.14-slim-trixie (with cryptography==50.0.2)
  so nothing is installed on the host. Existing values in .env and existing key files
  in secrets/ are never overwritten. GEMINI_API_KEY is never written to disk.

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

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw 'docker CLI not found - install/start Docker Desktop first.'
}

$cmd = "pip install -q cryptography==$cryptographyVersion && python scripts/init_secrets.py --root /work $extra"
& docker run --rm --memory 256m `
    -v "${mount}:/work" `
    -v keelson_pipcache:/root/.cache/pip `
    -w /work `
    -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_ROOT_USER_ACTION=ignore `
    $image sh -c $cmd
$code = $LASTEXITCODE
if ($code -ne 0) {
    $what = if ($Check) { 'check found missing/invalid values' } else { 'failed' }
    Write-Host "init_secrets.py $what (exit $code)" -ForegroundColor Red
    exit $code
}
Write-Host "OK: $root\.env and $root\secrets\ are ready (values not shown)." -ForegroundColor Green
exit 0
