<#
.SYNOPSIS
  Full demo reset: POST /api/demo/reset (docs/contracts/95-m5-remediation.md section 6).

.DESCRIPTION
  Signs in to Keelson as a demo persona that may reset (platform-admin or demo-presenter;
  default "ava") through the persona switcher, then calls POST /api/demo/reset with the
  session cookie, X-CSRF-Token and Origin. Prints the step log; exits 1 when any step failed
  or the request was refused.

  Steps (engine side): Merxa scenario:reset (golden seed + sim clock), demo-tenant rows,
  ADK sessions, Valkey demo keys, Merxa offline sessions/consents (Keycloak admin API),
  Token Broker rows, agent spec back to the pack default, scn re-sync request.

.PARAMETER BaseUrl
  Keelson public URL (default http://keelson.localhost).

.PARAMETER Persona
  Demo persona used to sign in (default ava, platform-admin; the persona switcher only shows it
  on local demos).

.PARAMETER KeepConsents
  Rehearsal mode: keep the Merxa consents and Token Broker links (no Connect-Merxa step).

.PARAMETER DemoAccess
  X-Demo-Access secret for hosted demos (default: env DEMO_ACCESS_SECRET).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\demo-reset.ps1
  powershell -ExecutionPolicy Bypass -File scripts\demo-reset.ps1 -KeepConsents
#>
[CmdletBinding()]
param(
    [string]$BaseUrl = "http://keelson.localhost",
    [string]$Persona = "ava",
    [switch]$KeepConsents,
    [string]$DemoAccess = $env:DEMO_ACCESS_SECRET
)

$ErrorActionPreference = "Stop"
$BaseUrl = $BaseUrl.TrimEnd("/")
$session = New-Object Microsoft.PowerShell.Commands.WebRequestSession
$headers = @{ Origin = $BaseUrl }
if ($DemoAccess) { $headers["X-Demo-Access"] = $DemoAccess }

function Invoke-Json([string]$Method, [string]$Path, [hashtable]$Headers, $Body) {
    $params = @{
        Method = $Method; Uri = "$BaseUrl$Path"; WebSession = $session; Headers = $Headers
        ContentType = "application/json"; UseBasicParsing = $true; TimeoutSec = 120
    }
    if ($null -ne $Body) { $params["Body"] = ($Body | ConvertTo-Json -Compress) }
    try {
        $resp = Invoke-WebRequest @params
        return @{ Status = [int]$resp.StatusCode; Body = ($resp.Content | ConvertFrom-Json) }
    } catch {
        $r = $_.Exception.Response
        if ($null -eq $r) { throw }
        $reader = New-Object System.IO.StreamReader($r.GetResponseStream())
        $text = $reader.ReadToEnd()
        return @{ Status = [int]$r.StatusCode; Body = $text }
    }
}

$login = Invoke-Json "POST" "/api/demo/login-as" $headers @{ username = $Persona }
if ($login.Status -ne 200) {
    Write-Host "Sign-in as '$Persona' failed (HTTP $($login.Status)): $($login.Body)" -ForegroundColor Red
    exit 1
}
$csrf = $login.Body.csrf_token
if (-not $csrf) {
    $me = Invoke-Json "GET" "/api/me" @{} $null
    $csrf = $me.Body.csrf_token
}
$resetHeaders = @{ Origin = $BaseUrl; "X-CSRF-Token" = $csrf }
$res = Invoke-Json "POST" "/api/demo/reset" $resetHeaders @{ keep_consents = [bool]$KeepConsents }
if ($res.Status -ne 200) {
    Write-Host "Demo reset refused (HTTP $($res.Status)): $($res.Body)" -ForegroundColor Red
    exit 1
}
$log = $res.Body
Write-Host ("Demo reset {0} in {1} ms (keep_consents={2})" -f $(if ($log.ok) { "OK" } else { "FAILED" }), $log.duration_ms, $log.keep_consents)
foreach ($step in $log.steps) {
    $color = switch ($step.status) { "ok" { "Green" } "skipped" { "DarkGray" } default { "Red" } }
    $counts = ($step.counts.PSObject.Properties | ForEach-Object { "$($_.Name)=$($_.Value)" }) -join " "
    Write-Host ("  {0,-22} {1,-8} {2,6} ms  {3} {4}" -f $step.step, $step.status, $step.duration_ms, $step.detail, $counts) -ForegroundColor $color
}
if (-not $log.ok) { exit 1 }
if ($log.duration_ms -ge 30000) { Write-Host "  (slower than the 30 s target)" -ForegroundColor Yellow }
exit 0
