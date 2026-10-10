#!/usr/bin/env bash
# Renovate extraction test: runs the pinned Renovate image with --platform=local --dry-run=extract
# (no registry/package lookups, no token) on a copy of the working tree, then checks the extracted
# dependencies with renovate_extract_check.py (every pinned image updatable, no manager duplicates,
# uv/npm projects and GitHub Actions found, nothing from the checker fixtures).
# Needs Docker; the Renovate image is ~1.3 GB, so scripts/supply-chain/tests/run.sh only runs this
# with --with-renovate (CI's meta job always does).
# Usage: scripts/supply-chain/tests/renovate_extract.sh        Exit status = number of failed checks.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1; fi
RENOVATE_IMAGE="ghcr.io/renovatebot/renovate:44.145.1@sha256:6f1f3e2d9d0c3f99aa1f61f7509d302185de7deb173ac8c5ff88a5ce283ec435"
PY_IMAGE="python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
OUT="$(mktemp -d "${TMPDIR:-/tmp}/keelson_sc_renovate.XXXXXX")"
trap 'rm -rf "$OUT"' EXIT
OUT_MOUNT="$OUT"
if command -v cygpath >/dev/null 2>&1; then OUT_MOUNT="$(cygpath -m "$OUT")"; fi

# A throwaway git repository inside the container: Renovate's local platform lists files with git,
# and a worktree's .git file points outside the mount.
docker run --rm --memory 1500m -v "$ROOT:/src:ro" -v "$OUT_MOUNT:/out" \
  -e LOG_FORMAT=json -e LOG_LEVEL=debug -e RENOVATE_PLATFORM=local -e RENOVATE_DRY_RUN=extract \
  -e RENOVATE_ONBOARDING=false --entrypoint bash "$RENOVATE_IMAGE" -c '
    set -e
    mkdir -p /tmp/repo
    cd /src && tar --exclude=./.git --exclude=node_modules --exclude=./.native -cf - . | tar -xf - -C /tmp/repo
    cd /tmp/repo && git init -q && git add -A
    git -c user.email=ci@keelson.invalid -c user.name=ci commit -qm extract
    renovate >/out/renovate.jsonl 2>&1' || { echo "renovate extract failed" >&2; tail -c 3000 "$OUT/renovate.jsonl" >&2; exit 99; }

docker run --rm --memory 256m -v "$ROOT:/work:ro" -v "$OUT_MOUNT:/out:ro" -w /work -e PYTHONDONTWRITEBYTECODE=1 \
  "$PY_IMAGE" python scripts/supply-chain/tests/renovate_extract_check.py /out/renovate.jsonl
