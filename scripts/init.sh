#!/usr/bin/env bash
# Generate .env + secrets/ idempotently (existing values are never overwritten).
# Runs scripts/init_secrets.py inside python:3.14-slim-trixie with cryptography==50.0.2,
# so nothing is installed on the host. Usage: scripts/init.sh [--check]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Docker Desktop on Windows (Git Bash): convert /d/primark -> D:/primark and stop
# MSYS from rewriting container paths.
if command -v cygpath >/dev/null 2>&1; then
  ROOT="$(cygpath -m "$ROOT")"
  export MSYS_NO_PATHCONV=1
fi

IMAGE="python:3.14-slim-trixie"
CRYPTOGRAPHY_VERSION="50.0.2"

exec docker run --rm --memory 256m \
  -v "${ROOT}:/work" \
  -v keelson_pipcache:/root/.cache/pip \
  -w /work \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_ROOT_USER_ACTION=ignore \
  "${IMAGE}" \
  sh -c 'pip install -q "cryptography==$1" && shift && exec python scripts/init_secrets.py --root /work "$@"' \
  init "${CRYPTOGRAPHY_VERSION}" "$@"
