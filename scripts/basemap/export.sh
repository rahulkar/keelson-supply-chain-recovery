#!/usr/bin/env bash
# Copy the installed base map out of the `keelson_basemap` volume into DIR (for the Cloud Run
# bucket upload: scripts/cloudrun/deploy.sh --enable-basemap --basemap-dir DIR). Checks the volume
# first (basemap-fetch --check); hidden staging directories (dotfiles) are not copied.
#
# Usage: scripts/basemap/export.sh DIR      (DIR must be missing or empty; ~30-200 MB)
set -euo pipefail
usage() { sed -n '2,6p' "$0" | sed 's/^# \{0,1\}//'; }
case "${1:-}" in -h | --help) usage; exit 0 ;; "") usage >&2; exit 2 ;; esac
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEST="$1"
mkdir -p "$DEST"
[ -z "$(ls -A "$DEST")" ] || { echo "export.sh: $DEST is not empty" >&2; exit 2; }
DEST="$(cd "$DEST" && pwd)"
VOLUME="${BASEMAP_VOLUME:-keelson_basemap}"
if command -v cygpath >/dev/null 2>&1; then DEST="$(cygpath -m "$DEST")"; export MSYS_NO_PATHCONV=1; fi
cd "$ROOT"
docker compose --profile basemap run --rm --build basemap-fetch --check
IMAGE="$(docker compose --profile basemap config --images basemap-fetch | head -n1)"
docker run --rm --memory 128m --user "$(id -u):$(id -g)" -v "$VOLUME:/src:ro" -v "$DEST:/dst" \
  --entrypoint sh "$IMAGE" -c 'cd /src && tar -cf - -- * | tar -xmf - --no-same-owner -C /dst'
echo "export.sh: base map copied to $DEST"
