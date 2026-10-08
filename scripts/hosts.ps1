<#
.SYNOPSIS
  OPTIONAL: map the Keelson *.localhost hostnames to 127.0.0.1 in the Windows hosts file.

.DESCRIPTION
  Browsers (Chrome, Edge, Firefox) and curl.exe already resolve any *.localhost name to
  loopback, so this is NOT needed for normal use. It is needed for tools that use the
  Windows resolver directly - e.g. Node.js or Python running on the host
  (dns.lookup / getaddrinfo return ENOTFOUND / 11001 for keelson.localhost).

  Without -Apply/-Remove the script only prints the entries and instructions; it never
  changes anything automatically. -Apply / -Remove need an elevated PowerShell
  ("Run as administrator"). The block is delimited by markers so it can be removed
  cleanly, and a timestamped backup of the hosts file is written first.

.PARAMETER Apply
  Add (or refresh) the Keelson block in the hosts file.

.PARAMETER Remove
  Remove the Keelson block from the hosts file.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\hosts.ps1            # show only
  # in an elevated PowerShell:
  powershell -ExecutionPolicy Bypass -File scripts\hosts.ps1 -Apply
  powershell -ExecutionPolicy Bypass -File scripts\hosts.ps1 -Remove
#>
[CmdletBinding()]
param([switch]$Apply, [switch]$Remove)

$ErrorActionPreference = 'Stop'
$hostnames = @('keelson.localhost', 'merxa.localhost', 'auth.localhost', 'gateway.localhost', 'traces.localhost')
$hostsPath = Join-Path $env:SystemRoot 'System32\drivers\etc\hosts'
$begin = '# >>> keelson (scripts/hosts.ps1) >>>'
$end = '# <<< keelson (scripts/hosts.ps1) <<<'
$block = @($begin) + ($hostnames | ForEach-Object { "127.0.0.1`t$_" }) + @($end)

function Test-Admin {
    $id = [Security.Principal.WindowsIdentity]::GetCurrent()
    return (New-Object Security.Principal.WindowsPrincipal($id)).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Get-HostsWithoutBlock([string[]]$lines) {
    $out = New-Object System.Collections.Generic.List[string]
    $inside = $false
    foreach ($l in $lines) {
        if ($l -eq $begin) { $inside = $true; continue }
        if ($l -eq $end) { $inside = $false; continue }
        if (-not $inside) { $out.Add($l) }
    }
    return , $out.ToArray()
}

if (-not $Apply -and -not $Remove) {
    Write-Host 'Entries this script manages (not applied - nothing was changed):'
    $block | ForEach-Object { Write-Host "  $_" }
    Write-Host ''
    $current = Get-Content -LiteralPath $hostsPath -ErrorAction SilentlyContinue
    if ($current -contains $begin) { Write-Host "Status: the Keelson block is present in $hostsPath." -ForegroundColor Green }
    else { Write-Host "Status: no Keelson block in $hostsPath." -ForegroundColor Yellow }
    Write-Host ''
    Write-Host 'Only needed for host-side Node/Python tooling; browsers resolve *.localhost already.'
    Write-Host 'To apply, open PowerShell as Administrator and run:'
    Write-Host '  powershell -ExecutionPolicy Bypass -File scripts\hosts.ps1 -Apply'
    exit 0
}

if (-not (Test-Admin)) {
    Write-Host 'This action edits the hosts file and needs an elevated PowerShell (Run as administrator).' -ForegroundColor Red
    exit 1
}

$lines = @(Get-Content -LiteralPath $hostsPath)
$backup = "$hostsPath.keelson-$(Get-Date -Format yyyyMMddHHmmss).bak"
Copy-Item -LiteralPath $hostsPath -Destination $backup
$clean = Get-HostsWithoutBlock $lines
if ($Apply) {
    $new = @($clean) + @('') + $block
    $action = 'added'
} else {
    $new = $clean
    $action = 'removed'
}
# Trim trailing blank lines, then write ASCII (the hosts file must not get a BOM).
$list = New-Object System.Collections.Generic.List[string]
$list.AddRange([string[]]$new)
while ($list.Count -gt 0 -and [string]::IsNullOrWhiteSpace($list[$list.Count - 1])) { $list.RemoveAt($list.Count - 1) }
[IO.File]::WriteAllLines($hostsPath, $list.ToArray(), [Text.Encoding]::ASCII)
& ipconfig /flushdns | Out-Null
Write-Host "Keelson hosts block $action (backup: $backup)." -ForegroundColor Green
