#!/usr/bin/env bash
# Routing/security test for infra/caddy/Caddyfile without the full stack.
#
# Starts, on a throwaway docker network: a stub upstream (every port the Caddyfile
# proxies to), Caddy with the real Caddyfile (upstreams overridden via the *_UPSTREAM env
# vars) and a throwaway `basemap` volume holding a synthetic base map (make_basemap_fixture.py),
# and a client container running check_routes.py. Then repeats the admin check with
# DEV_ALLOW_KC_ADMIN=1 on a Caddy without a base map (the "nothing installed" answers), and
# the base map checks on a Caddy whose BASEMAP_ROOT lies below a dot directory (native mode).
# Cleans up everything it created. Exit status = failed checks.
#
# Usage (Git Bash or Linux): scripts/tests/caddy/run.sh
#   CADDYFILE=/path/to/Caddyfile overrides the file under test (default: infra/caddy/Caddyfile).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then
  HERE="$(cygpath -m "$HERE")"; ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1
fi
PY_IMAGE="python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
CADDY_IMAGE="caddy:2.11.7-alpine@sha256:d8542f48d34a9cf4e4c11a478865229840e87e4c96ea3f439101f31a5d35f75f"
CADDYFILE="${CADDYFILE:-$ROOT/infra/caddy/Caddyfile}"
if command -v cygpath >/dev/null 2>&1; then CADDYFILE="$(cygpath -m "$CADDYFILE")"; fi
ID="keelson-caddytest-$$"
NET="$ID-net"
BASEMAP_VOL="$ID-basemap"
rc=0

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  docker rm -f "$ID-stub" "$ID-caddy" "$ID-caddy-admin" "$ID-caddy-native" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker volume rm "$BASEMAP_VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker network create "$NET" >/dev/null
docker run -d --name "$ID-stub" --network "$NET" --network-alias stub --memory 64m \
  -v "$HERE:/t:ro" "$PY_IMAGE" python /t/stub_upstreams.py >/dev/null

upstream_env=(
  -e ENGINE_API_UPSTREAM=stub:8000 -e KEELSON_WEB_UPSTREAM=stub:3000
  -e MERXA_API_UPSTREAM=stub:8100 -e MERXA_WEB_UPSTREAM=stub:3001
  -e KEYCLOAK_UPSTREAM=stub:8080 -e LITELLM_UPSTREAM=stub:4000 -e PHOENIX_UPSTREAM=stub:6006
)

docker volume create "$BASEMAP_VOL" >/dev/null
docker run --rm --memory 64m -v "$BASEMAP_VOL:/b" -v "$HERE:/t:ro" "$PY_IMAGE" python /t/make_basemap_fixture.py /b

start_caddy() {  # name, DEV_ALLOW_KC_ADMIN, [extra docker run args...]
  local name="$1" admin="$2"; shift 2
  docker run -d --name "$name" --network "$NET" --memory 64m "${upstream_env[@]}" \
    -e DEV_ALLOW_KC_ADMIN="$admin" "$@" \
    -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" "$CADDY_IMAGE" >/dev/null
  # wait for the admin endpoint (= config loaded)
  for _ in $(seq 1 30); do
    docker exec "$name" wget -qO- http://127.0.0.1:2019/config/ >/dev/null 2>&1 && return 0
    sleep 0.5
  done
  docker logs "$name" >&2; return 1
}

# Validate first (adapt + provision): a syntax error fails fast with a readable message.
docker run --rm --memory 64m "${upstream_env[@]}" -v "$CADDYFILE:/etc/caddy/Caddyfile:ro" \
  "$CADDY_IMAGE" caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -1

start_caddy "$ID-caddy" 0 -v "$BASEMAP_VOL:/srv/basemap:ro"
start_caddy "$ID-caddy-admin" 1
# Native mode's root sits below a dot directory (<repo>/.native/data/basemap).
start_caddy "$ID-caddy-native" 0 -e BASEMAP_ROOT=/srv/.native/data/basemap -v "$BASEMAP_VOL:/srv/.native/data/basemap:ro"
sleep 1  # stub threads

docker run --rm --network "$NET" --memory 64m -v "$HERE:/t:ro" "$PY_IMAGE" \
  python /t/check_routes.py "$ID-caddy" default || rc=$((rc + $?))
docker run --rm --network "$NET" --memory 64m -v "$HERE:/t:ro" "$PY_IMAGE" \
  python /t/check_routes.py "$ID-caddy-admin" kc-admin-allowed || rc=$((rc + $?))
docker run --rm --network "$NET" --memory 64m -v "$HERE:/t:ro" "$PY_IMAGE" \
  python /t/check_routes.py "$ID-caddy-native" basemap-dotted-root || rc=$((rc + $?))

# Access-log redaction: the probe request (log_probe_sent) must be logged, its secrets not.
logs="$(docker logs "$ID-caddy" 2>&1)"
if grep -q '/api/auth/callback' <<<"$logs" && ! grep -q 'LOGPROBE' <<<"$logs"; then
  echo '{"check": "access_log_redaction", "ok": true}'
else
  leaked="$(grep -o '[A-Z]*-LOGPROBE' <<<"$logs" | sort -u | tr '\n' ' ')"
  echo "{\"check\": \"access_log_redaction\", \"ok\": false, \"leaked\": \"${leaked}\"}"
  rc=$((rc + 1))
fi

if [ "$rc" -eq 0 ]; then echo "caddy routing tests: all passed"; else echo "caddy routing tests: $rc failed" >&2; fi
exit "$rc"
