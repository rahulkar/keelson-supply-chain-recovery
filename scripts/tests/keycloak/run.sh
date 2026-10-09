#!/usr/bin/env bash
# From-scratch realm import test: applies infra/keycloak/config/*.yaml with the pinned
# keycloak-config-cli to a THROWAWAY Keycloak (start-dev, in-memory/H2, own network, dummy
# secrets) - the shared compose Keycloak and its database are never touched. Then:
#   1. runs scripts/verify_infra.py realms (discovery/issuer, priya + marco.rossi password
#      grants and claims, private_key_jwt clients, service-account roles, built-ins kept)
#   2. re-runs the import (must be a no-op that still exits 0)
#   3. re-imports with DEMO_ENABLED=false and expects the demo password grants to fail
# Exit status = number of failed steps. Needs ~1.3 GB RAM while it runs (stop LiteLLM first
# on an 8 GB machine). Usage: scripts/tests/keycloak/run.sh
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1; fi
# Keep in sync with compose.yaml (keycloak / kc-config).
KC_IMAGE="quay.io/keycloak/keycloak:26.8.0@sha256:b0f60d489d51c5d113390bdf5461d4c06e6051be026c05549f2e1e10ec352bcc"
KCC_IMAGE="adorsys/keycloak-config-cli:6.5.1-26@sha256:1b22dfaa9ae0c71f74b0342f9221a6510f272da5def683dbba26a98e6b1b1411"
PY_IMAGE="python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
ID="keelson-kctest-$$"
NET="$ID-net"
BASE="http://auth.localhost:8080"   # network alias of the throwaway Keycloak
fail=0

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  docker rm -f "$ID-kc" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}
trap cleanup EXIT

rand() { head -c 48 /dev/urandom | base64 | tr -dc 'A-Za-z0-9' | head -c "${1:-32}"; }
ADMIN_PW="$(rand)"
SECRETS=(
  -e KEELSON_BFF_CLIENT_SECRET="$(rand)" -e KEELSON_DEMO_CLIENT_SECRET="$(rand)"
  -e MERXA_WEB_CLIENT_SECRET="$(rand)" -e MERXA_DEMO_CLIENT_SECRET="$(rand)"
  -e DEMO_USER_PASSWORD="$(rand 20)" -e KC_ADMIN_PASSWORD="$ADMIN_PW"
)

step() {  # name, status
  if [ "$2" -eq 0 ]; then echo "{\"step\": \"$1\", \"ok\": true}"; else echo "{\"step\": \"$1\", \"ok\": false}"; fail=$((fail + 1)); fi
}

docker network create "$NET" >/dev/null
docker run -d --name "$ID-kc" --network "$NET" --network-alias auth.localhost --memory 900m \
  -e KC_BOOTSTRAP_ADMIN_USERNAME=admin -e KC_BOOTSTRAP_ADMIN_PASSWORD="$ADMIN_PW" \
  -e KC_HOSTNAME="$BASE" -e KC_HTTP_ENABLED=true -e KC_HEALTH_ENABLED=true -e KC_CACHE=local \
  -e JAVA_OPTS_KC_HEAP="-XX:MaxRAMPercentage=60 -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=20" \
  "$KC_IMAGE" start-dev >/dev/null

ready=1
for _ in $(seq 1 90); do
  if docker exec "$ID-kc" bash -c "exec 3<>/dev/tcp/127.0.0.1/9000; printf 'GET /health/ready HTTP/1.1\r\nHost: x\r\nConnection: close\r\n\r\n' >&3; grep -q UP <&3" 2>/dev/null; then
    ready=0; break
  fi
  sleep 2
done
step keycloak_ready "$ready"
[ "$ready" -eq 0 ] || { docker logs --tail 30 "$ID-kc" >&2; exit "$fail"; }

kc_config() {  # extra docker args...
  docker run --rm --network "$NET" --memory 512m \
    -e KEYCLOAK_URL="$BASE" -e KEYCLOAK_USER=admin -e KEYCLOAK_PASSWORD="$ADMIN_PW" \
    -e KEYCLOAK_AVAILABILITYCHECK_ENABLED=true -e KEYCLOAK_AVAILABILITYCHECK_TIMEOUT=120s \
    -e IMPORT_FILES_LOCATIONS='/config/*.yaml' -e IMPORT_VARSUBSTITUTION_ENABLED=true \
    -e IMPORT_REMOTESTATE_ENABLED=true -e IMPORT_MANAGED_CLIENTSCOPE=no-delete \
    -e IMPORT_BEHAVIORS_REMOVEDEFAULTROLEFROMUSER=false -e JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=60 \
    "${SECRETS[@]}" "$@" -v "$ROOT/infra/keycloak/config:/config:ro" "$KCC_IMAGE" >"/tmp/$ID-kcc.log" 2>&1
  local rc=$?
  [ "$rc" -eq 0 ] || grep -E "ERROR|Exception" "/tmp/$ID-kcc.log" | head -5 >&2
  rm -f "/tmp/$ID-kcc.log"
  return "$rc"
}

verify() {  # mode
  docker run --rm --network "$NET" --memory 96m "${SECRETS[@]}" \
    -e AUTH_BASE_URL="$BASE" -e KEYCLOAK_INTERNAL_URL="$BASE" \
    -v "$ROOT/scripts:/scripts:ro" "$PY_IMAGE" python /scripts/verify_infra.py "$1"
}

kc_config -e DEMO_ENABLED=true;  step import_from_scratch $?
verify realms;                   step realm_checks $?
kc_config -e DEMO_ENABLED=true;  step reimport_idempotent $?
kc_config -e DEMO_ENABLED=false; step reimport_demo_disabled $?
verify demo-disabled;            step demo_grants_refused_when_disabled $?

exit "$fail"
