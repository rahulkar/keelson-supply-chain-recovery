<#
.SYNOPSIS
  Fill (or check) the self-hosted base map volume (docs/contracts/127-m8-basemap.md).

.DESCRIPTION
  Runs the one-shot basemap-fetch container (compose profile "basemap"): a Protomaps archive
  (world extract), Noto Sans glyphs, sprites, licence notices and manifest.json into the Docker
  volume keelson_basemap, which Caddy serves at http://keelson.localhost/basemap/. Downloads go
  through the egress proxy to the pinned public hosts only. Nothing needs a restart afterwards.

  Sizes: lite ~29 MB, standard ~59 MB (default), detail ~203 MB.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File scripts\basemap\fetch.ps1
  powershell -ExecutionPolicy Bypass -File scripts\basemap\fetch.ps1 --profile lite
  powershell -ExecutionPolicy Bypass -File scripts\basemap\fetch.ps1 --dry-run
  powershell -ExecutionPolicy Bypass -File scripts\basemap\fetch.ps1 --check
#>
$ErrorActionPreference = "Stop"
$root = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Push-Location $root
try {
    & docker compose --profile basemap run --rm --build basemap-fetch @args
    exit $LASTEXITCODE
} finally {
    Pop-Location
}
