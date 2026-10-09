#!/usr/bin/env bash
# Fill (or check) the self-hosted base map volume: Protomaps archive + glyphs + sprites, served by
# Caddy at http://keelson.localhost/basemap/ (docs/contracts/127-m8-basemap.md).
#
# Usage: scripts/basemap/fetch.sh [--profile lite|standard|detail] [--build latest|YYYYMMDD]
#                                 [--max-mb N] [--keep N] [--dry-run | --check]
#   = docker compose --profile basemap run --rm basemap-fetch <args>  (egress proxy, non-root)
# Sizes: lite ~29 MB, standard ~59 MB (default), detail ~203 MB (Docker volume `keelson_basemap`).
# Afterwards nothing needs a restart: the map picks the manifest up on the next page load.
set -euo pipefail
case "${1:-}" in -h | --help) sed -n '2,10p' "$0"; exit 0 ;; esac
cd "$(dirname "${BASH_SOURCE[0]}")/../.."
exec docker compose --profile basemap run --rm --build basemap-fetch "$@"
