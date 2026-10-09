#!/usr/bin/env bash
# Generate .env + secrets/ idempotently (existing values are never overwritten).
# Runs scripts/init_secrets.py inside python:3.14-slim-trixie with cryptography==50.0.2,
# so nothing is installed on the host. Usage: scripts/init.sh [--check]
# Progress is logged as "[HH:MM:SS +elapsed] [n/5] ...": checking docker, the container
# image (local or pulled), installing cryptography, running init_secrets.py, result.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# Docker Desktop on Windows (Git Bash): convert /d/primark -> D:/primark and stop
# MSYS from rewriting container paths.
if command -v cygpath >/dev/null 2>&1; then
  ROOT="$(cygpath -m "$ROOT")"
  export MSYS_NO_PATHCONV=1
fi

IMAGE="python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
CRYPTOGRAPHY_VERSION="50.0.2"
CHECK=0
for arg in "$@"; do if [ "$arg" = "--check" ]; then CHECK=1; fi; done

# -- logging: "[HH:MM:SS +elapsed] [n/5] message" --
T0=$SECONDS
STEP=0
STEP_TOTAL=5
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_STEP=$'\033[36m' C_OK=$'\033[32m' C_WARN=$'\033[33m' C_ERR=$'\033[31m' C_RESET=$'\033[0m'
else
  C_STEP="" C_OK="" C_WARN="" C_ERR="" C_RESET=""
fi
log_c() { # COLOUR MESSAGE...
  local color=$1 tag="" now secs elapsed
  shift
  now="$(date +%H:%M:%S)"
  secs=$((SECONDS - T0))
  if [ "$secs" -lt 60 ]; then elapsed="${secs}s"; else printf -v elapsed '%dm%02ds' $((secs / 60)) $((secs % 60)); fi
  if [ "$STEP" -gt 0 ]; then tag=" [$STEP/$STEP_TOTAL]"; fi
  printf '%s[%s +%s]%s %s%s\n' "$color" "$now" "$elapsed" "$tag" "$*" "${color:+$C_RESET}"
}
log() { log_c "" "$*"; }
step() { STEP=$((STEP + 1)); log_c "$C_STEP" "==> $*"; }
warn() { log_c "$C_WARN" "WARN: $*"; }

if [ "$CHECK" = 1 ]; then
  log "Checking (read-only) .env and secrets/ in $ROOT"
else
  log "Completing (existing values are kept) .env and secrets/ in $ROOT"
fi

# -- 1. docker --
step "Checking docker"
if ! DOCKER_BIN="$(command -v docker)"; then
  log_c "$C_ERR" "ERROR: docker CLI not found - install/start Docker Desktop first."
  exit 127
fi
log "docker CLI: $DOCKER_BIN"
ENGINE_UP=1
if SERVER_VERSION="$(docker version --format '{{.Server.Version}}' 2>/dev/null)"; then
  log "Docker engine: $SERVER_VERSION"
else
  ENGINE_UP=0
  warn "Docker engine is not reachable - start Docker Desktop (docker run below will fail)."
fi

# -- 2. image --
step "Container image $IMAGE"
if [ "$ENGINE_UP" = 1 ]; then
  if IMG="$(docker image inspect --format '{{.Id}}|{{.Size}}|{{.Created}}' "$IMAGE" 2>/dev/null)"; then
    IFS='|' read -r IMG_ID IMG_SIZE IMG_CREATED <<<"$IMG"
    case "$IMG_SIZE" in '' | *[!0-9]*) IMG_SIZE="?" ;; *) IMG_SIZE="$(((IMG_SIZE + 524288) / 1048576)) MB" ;; esac
    log "Using local image (${IMG_ID:0:19}, $IMG_SIZE, created $IMG_CREATED)"
  else
    log "Not present locally - pulling it (one-time download) ..."
    if ! docker pull "$IMAGE" 2>&1 | while IFS= read -r line || [ -n "$line" ]; do log "    ${line%$'\r'}"; done; then
      warn "docker pull failed; docker run will retry."
    fi
  fi
fi

# -- 3./4. pip install + init_secrets.py (one container; '::step::' lines become step headers) --
log "Mount ${ROOT} -> /work, pip cache volume keelson_pipcache, --memory 256m"
set +e
docker run --rm --memory 256m \
  -v "${ROOT}:/work" \
  -v keelson_pipcache:/root/.cache/pip \
  -w /work \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_ROOT_USER_ACTION=ignore \
  "${IMAGE}" \
  sh -c 'echo "::step::Installing cryptography==$1 in the container (pip cache volume keelson_pipcache)" &&
    pip install -q "cryptography==$1" && shift &&
    echo "::step::Running init_secrets.py --root /work $* (values are never printed)" &&
    exec python scripts/init_secrets.py --root /work "$@"' \
  init "${CRYPTOGRAPHY_VERSION}" "$@" 2>&1 | while IFS= read -r line || [ -n "$line" ]; do
  line=${line%$'\r'}
  case "$line" in
    ::step::*) step "${line#::step::}" ;;
    *[![:space:]]*) log "    $line" ;;
  esac
done
code=${PIPESTATUS[0]}
set -e

# -- 5. result --
STEP=$((STEP_TOTAL - 1))
step "Result"
if [ "$code" -ne 0 ]; then
  if [ "$CHECK" = 1 ]; then what="check found missing/invalid values"; else what="failed"; fi
  log_c "$C_ERR" "init_secrets.py $what (exit $code)"
  exit "$code"
fi
log_c "$C_OK" "OK: $ROOT/.env and $ROOT/secrets/ are ready (values not shown)."
exit 0
