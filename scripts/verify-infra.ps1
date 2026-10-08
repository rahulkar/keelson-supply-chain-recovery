<#
.SYNOPSIS
  Smoke-verify the running Keelson infra services from inside the compose networks.
.PARAMETER SkipLlm
  Skip the LiteLLM temporary-model call (no Gemini request is made).
.NOTES
  Checks: OIDC discovery (issuer), demo password grants + token claims, Keycloak admin
  API (private_key_jwt clients, service-account roles, built-ins preserved), public admin
  blocking, LiteLLM readiness + smoke call, Squid egress policy, *.localhost resolution.
  Exit code = number of failed checks.
#>
[CmdletBinding()]
param([switch]$SkipLlm)
# Native tools (docker) write progress/logs to stderr; with 'Stop' Windows PowerShell 5.1
# would turn that into terminating errors. Exit codes are checked explicitly instead.
$ErrorActionPreference = 'Continue'
$root = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$mount = $root.Replace([char]92, [char]47)   # D:\primark -> D:/primark
$image = 'python:3.14-slim-trixie'
$name = "keelson-verify-$PID"
$extra = @(); if ($SkipLlm) { $extra += '--skip-llm' }
$rc = 0
try {
    & docker create --name $name --memory 128m --network keelson_app --env-file "$root\.env" `
        -v "${mount}/scripts:/scripts:ro" $image python /scripts/verify_infra.py all @extra | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'docker create failed (are the infra services up?)' }
    & docker network connect keelson_ai $name
    & docker start -a $name
    $rc += $LASTEXITCODE
} finally {
    & docker rm -f $name 2>$null | Out-Null
}
foreach ($net in @('app', 'edge')) {
    & docker run --rm --memory 64m --network "keelson_$net" -e "VERIFY_NETWORK=$net" `
        -v "${mount}/scripts:/scripts:ro" $image python /scripts/verify_infra.py dns
    if ($LASTEXITCODE -ne 0) { $rc += 1 }
}
if ($rc -eq 0) { Write-Host 'All infra checks passed.' -ForegroundColor Green }
else { Write-Host "$rc infra check(s) failed." -ForegroundColor Red }
exit $rc
