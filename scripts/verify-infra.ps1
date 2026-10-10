<#
.SYNOPSIS
  Smoke-verify the running Keelson infra services from inside the compose networks.
.PARAMETER SkipLlm
  Skip the LiteLLM temporary-model call (no Gemini request is made).
.NOTES
  Checks: OIDC discovery (issuer), demo password grants + token claims, Keycloak admin
  API (private_key_jwt clients, service-account roles, built-ins preserved), public admin
  blocking, LiteLLM readiness + smoke call, Squid egress policy, *.localhost resolution.
  Each JSON result line of verify_infra.py is shown as "PASS name (ms)" / "FAIL name -> error"
  with a "[HH:mm:ss +elapsed] [step/3]" prefix; the raw JSON is kept in
  logs/verify-infra-<yyyyMMdd-HHmmss>.log.
  Exit code = number of failed checks.
#>
[CmdletBinding()]
param([switch]$SkipLlm)
# Native tools (docker) write progress/logs to stderr; with 'Stop' Windows PowerShell 5.1
# would turn that into terminating errors. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$mount = $root.Replace([char]92, [char]47)   # D:\primark -> D:/primark
$image = 'python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170'
$name = "keelson-verify-$PID"
$extra = @(); if ($SkipLlm) { $extra += '--skip-llm' }
$rc = 0

# -- logging: "[HH:mm:ss +elapsed] [n/3] message" to the console and logs/verify-infra-<ts>.log --
$inv = [Globalization.CultureInfo]::InvariantCulture
$script:t0 = Get-Date
$script:stepNo = 0
$script:stepTotal = 3
$script:log = $null
$script:passed = 0
$script:failedChecks = 0
$script:lastLineAt = Get-Date
$logFile = Join-Path (Join-Path $root 'logs') ('verify-infra-{0}.log' -f $script:t0.ToString('yyyyMMdd-HHmmss', $inv))

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
function Write-Step([string]$msg) { $script:stepNo++; $script:lastLineAt = Get-Date; Write-Log "==> $msg" 'Cyan' }
# Native output line (string or stderr ErrorRecord) -> plain text.
function ConvertTo-PlainText($item) {
    $text = if ($item -is [System.Management.Automation.ErrorRecord]) { $item.Exception.Message } else { "$item" }
    return ($text -replace '\x1b\[[0-9;?]*[ -/]*[@-~]', '').TrimEnd("`r", "`n")
}

# One output line of verify_infra.py -> "PASS name (225 ms)" / "FAIL name -> error";
# anything that is not a check result is passed through. The raw JSON goes to the log file.
function Write-CheckLine($item) {
    $text = ConvertTo-PlainText $item
    $now = Get-Date
    $delta = ($now - $script:lastLineAt).TotalSeconds
    $script:lastLineAt = $now
    $t = $text.Trim()
    if (-not $t) { return }
    $obj = $null
    if ($t.StartsWith('{')) { try { $obj = $t | ConvertFrom-Json -ErrorAction Stop } catch { $obj = $null } }
    if ($obj -and $obj.PSObject.Properties['check']) {
        if ($script:log) { $script:log.WriteLine('        json: ' + $t) }
        $checkName = [string]$obj.check
        if ($obj.PSObject.Properties['network']) { $checkName += '@' + $obj.network }
        if ($obj.ok -eq $true) {
            $script:passed++
            $ms = $obj.PSObject.Properties['ms']
            if (-not $ms) { $ms = $obj.PSObject.Properties['chat_ms'] }
            $took = if ($ms -and $ms.Value -is [ValueType]) { "($($ms.Value) ms)" } else { '(~{0} s)' -f $delta.ToString('0.0', $inv) }
            Write-Log ("  PASS  {0}  {1}" -f $checkName, $took) 'Green'
        } else {
            $script:failedChecks++
            $errProp = $obj.PSObject.Properties['error']
            $detail = if ($errProp -and $errProp.Value) { [string]$errProp.Value }
            else { ($t -replace '^\{\s*"check":\s*"[^"]*",\s*"ok":\s*(true|false),?\s*', '') -replace '\}\s*$', '' }
            $detail = ($detail -replace '\s+', ' ').Trim()
            if ($detail.Length -gt 200) { $detail = $detail.Substring(0, 197) + '...' }
            Write-Log ("  FAIL  {0}  -> {1}" -f $checkName, $detail) 'Red'
        }
    } elseif ($obj -and $obj.PSObject.Properties['summary']) {
        if ($script:log) { $script:log.WriteLine('        json: ' + $t) }
        Write-Log ("  (verify_infra.py: {0} checks, {1} failed)" -f $obj.summary.checks, $obj.summary.failed) 'DarkGray'
    } else {
        Write-Log "  $text"
    }
}

try {
    New-Item -ItemType Directory -Force -Path (Split-Path $logFile) | Out-Null
    $script:log = New-Object System.IO.StreamWriter($logFile, $true, (New-Object System.Text.UTF8Encoding($false)))
    $script:log.AutoFlush = $true
    Write-Log "Infra smoke checks - log file: $logFile" 'Cyan'
    Write-Log ("LLM smoke check: {0}" -f $(if ($SkipLlm) { 'skipped (-SkipLlm)' } else { 'included (one tiny Gemini call through LiteLLM)' }))
    & docker image inspect $image *> $null
    if ($LASTEXITCODE -eq 0) { Write-Log "Image $image is present locally." }
    else {
        Write-Log "Image $image not present locally - pulling it ..."
        & docker pull $image 2>&1 | ForEach-Object { Write-Log ('    ' + (ConvertTo-PlainText $_)) }
    }

    Write-Step 'Running main checks on keelson_app + keelson_ai ...'
    try {
        Write-Log "Creating container $name (--memory 128m, network keelson_app, --env-file .env)"
        & docker create --name $name --memory 128m --network keelson_app --env-file "$root\.env" `
            -v "${mount}/scripts:/scripts:ro" $image python /scripts/verify_infra.py all @extra 2>&1 |
            ForEach-Object { $l = ConvertTo-PlainText $_; if ($l -notmatch '^[0-9a-f]{64}$') { Write-Log "    $l" } }
        if ($LASTEXITCODE -ne 0) { throw 'docker create failed (are the infra services up?)' }
        Write-Log 'Attaching it to keelson_ai (LiteLLM) and starting verify_infra.py all ...'
        & docker network connect keelson_ai $name 2>&1 | ForEach-Object { Write-Log ('    ' + (ConvertTo-PlainText $_)) 'Yellow' }
        $script:lastLineAt = Get-Date
        & docker start -a $name 2>&1 | ForEach-Object { Write-CheckLine $_ }
        $rc += $LASTEXITCODE
    } finally {
        & docker rm -f $name 2>$null | Out-Null
    }
    foreach ($net in @('app', 'edge')) {
        Write-Step "DNS resolution on keelson_$net (public hostnames must resolve to Caddy) ..."
        $script:lastLineAt = Get-Date
        & docker run --rm --memory 64m --network "keelson_$net" -e "VERIFY_NETWORK=$net" `
            -v "${mount}/scripts:/scripts:ro" $image python /scripts/verify_infra.py dns 2>&1 |
            ForEach-Object { Write-CheckLine $_ }
        if ($LASTEXITCODE -ne 0) { $rc += 1 }
    }
    $total = Format-Elapsed ((Get-Date) - $script:t0).TotalSeconds
    if ($rc -eq 0) { Write-Log 'All infra checks passed.' 'Green' }
    else { Write-Log "$rc infra check(s) failed." 'Red' }
    Write-Log ("{0} passed, {1} failed, total {2}" -f $script:passed, $script:failedChecks, $total) $(if ($rc -eq 0) { 'Green' } else { 'Red' })
} catch {
    Write-Log "ERROR: $($_.Exception.Message)" 'Red'
    throw
} finally {
    if ($script:log) {
        Write-Log "Log file (incl. raw JSON): $logFile" 'Cyan'
        $script:log.Dispose()
        $script:log = $null
    }
}
exit $rc
