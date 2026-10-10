#!/usr/bin/env bash
# Builds the Cloud Run overlay images (infra/cloudrun/images) locally and checks them:
#   engine     /app/{models.lock,tenants.yaml,packs,geo} baked in, dev origins rewritten, parsed by
#              the engine's own loaders, read-only root FS + non-root
#   litellm    /app/config.yaml baked in
#   kc-config  /config/*.yaml baked in with no .localhost origin left
#   --with-keycloak: keycloak overlay (kc.sh build; start --optimized --cache=local) on a throwaway
#              pg-core, then the kc-config overlay imports both realms into it (exit 0) and the
#              OIDC issuers are https://auth.<domain>/realms/<realm>   (~1.5 GB RAM, ~5 min)
# Base images: the local compose images (keelson-engine-api:latest; env ENGINE_BASE_IMAGE overrides,
# e.g. a freshly built tag) and the pinned third-party images. Everything is named keelson_m3_cr_ov* and removed on exit.
# Exit status = number of failed checks. Usage: scripts/cloudrun/tests/overlays.sh [--with-keycloak]
# shellcheck disable=SC2319  # "[ cond ]; result <name> $?" reports the condition status on purpose
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1; fi
P="keelson_m3_cr_ov"
DOMAIN="parity.example.com"
NET="${P}_net"
WITH_KC=0; [ "${1:-}" = --with-keycloak ] && WITH_KC=1
fail=0
IMAGES=("$P/engine:test" "$P/litellm:test" "$P/kc-config:test" "$P/keycloak:test")
TMP="$(mktemp -d)"
# docker.exe needs a Windows path under Git Bash (MSYS path conversion is off)
command -v cygpath >/dev/null 2>&1 && TMP="$(cygpath -m "$TMP")"

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  docker rm -fv "${P}_pg" "${P}_kc" "${P}_cfg" "${P}_kcc" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  for i in "${IMAGES[@]}"; do docker image rm "$i" >/dev/null 2>&1 || true; done
  rm -rf "$TMP"
}
trap cleanup EXIT

result() {
  local ok=true; [ "$2" -eq 0 ] || { ok=false; fail=$((fail + 1)); }
  printf '{"check":"%s","ok":%s,"detail":"%s"}\n' "$1" "$ok" "$(printf '%s' "$3" | tr '\n"' ' ' | cut -c1-300)"
}
# shellcheck disable=SC1091  # KEY=value pins
. "$ROOT/infra/cloudrun/versions.env" 2>/dev/null || true

# ── engine ──
docker build -q -f "$ROOT/infra/cloudrun/images/engine.Dockerfile" --build-arg "ENGINE_IMAGE=${ENGINE_BASE_IMAGE:-keelson-engine-api:latest}" \
  --build-arg "PUBLIC_BASE_DOMAIN=$DOMAIN" -t "$P/engine:test" "$ROOT" >/dev/null
result engine_overlay_builds $? "FROM ${ENGINE_BASE_IMAGE:-keelson-engine-api:latest}"
out="$(docker run --rm --read-only --tmpfs /tmp --cap-drop ALL --memory 256m "$P/engine:test" python -c '
import os, pathlib
from keelson_engine.bootstrap import load_tenants, load_models_lock
from keelson_engine.core.packs import list_packs
t = load_tenants(pathlib.Path("/app/tenants.yaml"))
a = load_models_lock(pathlib.Path("/app/models.lock"))
p = list_packs()
urls = [c.get("mcp_url") for x in t for c in (x.get("connectors") or [])]
print("uid", os.getuid(), "tenants", len(t), "aliases", len(a), "packs", len(p), "mcp", ",".join(u for u in urls if u))
try:
    from keelson_engine.runtime.geo import get_geo
    print("geo", getattr(get_geo(), "source", "?"))
except ImportError:
    print("geo n/a (engine image predates runtime.geo)")
' 2>&1)"; rc=$?
[ $rc -eq 0 ] && [[ "$out" == *"mcp https://merxa.${DOMAIN}/mcp"* ]] && [[ "$out" != *"uid 0 "* ]]
result engine_overlay_files_parse $? "$out"
leftover="$(docker run --rm --entrypoint sh "$P/engine:test" -c 'grep -rl "\.localhost" /app/tenants.yaml /app/packs || true')"
[ -z "$leftover" ]; result engine_overlay_no_localhost $? "${leftover:-none}"
[ -n "$(docker run --rm --entrypoint sh "$P/engine:test" -c 'ls /app/geo/sea_routes.json')" ]
result engine_overlay_geo $? "/app/geo"

# ── litellm ──
docker build -q -f "$ROOT/infra/cloudrun/images/litellm.Dockerfile" --build-arg "LITELLM_IMAGE=${LITELLM_IMAGE:-ghcr.io/berriai/litellm-non_root:1.104.0@sha256:37066242e6b8327fdd627682a66a9ae2a599553b4d5a84c905c6d8b9eb35c01e}" \
  -t "$P/litellm:test" "$ROOT" >/dev/null
result litellm_overlay_builds $? ""
docker create --name "${P}_cfg" "$P/litellm:test" >/dev/null && docker cp "${P}_cfg:/app/config.yaml" "$TMP/litellm.yaml" >/dev/null
cmp -s "$TMP/litellm.yaml" "$ROOT/infra/litellm/config.yaml"; result litellm_overlay_config $? "/app/config.yaml"
docker rm -fv "${P}_cfg" >/dev/null 2>&1

# ── kc-config ──
docker build -q -f "$ROOT/infra/cloudrun/images/kc-config.Dockerfile" --build-arg "KC_CONFIG_IMAGE=${KC_CONFIG_IMAGE:-adorsys/keycloak-config-cli:6.5.1-26@sha256:1b22dfaa9ae0c71f74b0342f9221a6510f272da5def683dbba26a98e6b1b1411}" \
  --build-arg "PUBLIC_BASE_DOMAIN=$DOMAIN" -t "$P/kc-config:test" "$ROOT" >/dev/null
result kcconfig_overlay_builds $? ""
docker create --name "${P}_cfg" "$P/kc-config:test" >/dev/null && docker cp "${P}_cfg:/config" "$TMP/config" >/dev/null
docker rm -fv "${P}_cfg" >/dev/null 2>&1
n="$(find "$TMP/config" -name '*.yaml' | wc -l)"; left="$(grep -rl '\.localhost' "$TMP/config" || true)"
[ "$n" -ge 2 ] && [ -z "$left" ] && grep -q "https://keelson.${DOMAIN}/api/auth/callback" "$TMP/config/keelson.yaml"
result kcconfig_overlay_realms $? "files=$n leftover=${left:-none}"
ssl="$(grep -rhE '^[[:space:]]*sslRequired:' "$TMP/config" | tr -d ' ' | sort | uniq -c | tr '\n' ' ')"
! grep -rqE '^[[:space:]]*sslRequired:[[:space:]]*["'\'']?none' "$TMP/config" && [[ "$ssl" == *"sslRequired:external"* ]]
result kcconfig_overlay_ssl_required $? "$ssl"
if docker build -q -f "$ROOT/infra/cloudrun/images/kc-config.Dockerfile" --build-arg "PUBLIC_BASE_DOMAIN=bad.localhost" "$ROOT" >/dev/null 2>&1; then
  result kcconfig_overlay_refuses_localhost 1 "PUBLIC_BASE_DOMAIN=bad.localhost must fail the build"
else
  result kcconfig_overlay_refuses_localhost 0 "build refused"
fi

# ── keycloak + realm import ──
if [ "$WITH_KC" = 1 ]; then
  docker build -q -f "$ROOT/infra/cloudrun/images/keycloak.Dockerfile" -t "$P/keycloak:test" "$ROOT/infra/cloudrun/images" >/dev/null
  result keycloak_overlay_builds $? ""
  docker network create "$NET" >/dev/null
  docker run -d --name "${P}_pg" --network "$NET" --memory 384m --tmpfs /var/lib/postgresql:size=512m \
    -e POSTGRES_PASSWORD=su -e KEELSON_DB_PASSWORD=k -e ADK_DB_PASSWORD=a -e KEYCLOAK_DB_PASSWORD=kc-pw \
    keelson-pg-core:latest >/dev/null
  for _ in $(seq 1 90); do [ "$(docker inspect -f '{{.State.Health.Status}}' "${P}_pg")" = healthy ] && break; sleep 1; done
  docker run -d --name "${P}_kc" --network "$NET" --network-alias kc --memory 1280m --cpus 2 \
    --read-only --tmpfs /tmp --tmpfs /opt/keycloak/data:uid=1000,gid=0,mode=0770 \
    -e "KC_DB_URL=jdbc:postgresql://${P}_pg:5432/keycloak" -e KC_DB_USERNAME=keycloak -e KC_DB_PASSWORD=kc-pw \
    -e "KC_HOSTNAME=https://auth.${DOMAIN}" -e KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true -e KC_HTTP_ENABLED=true \
    -e KC_HTTP_PORT=8080 -e KC_PROXY_HEADERS=xforwarded -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
    -e KC_BOOTSTRAP_ADMIN_PASSWORD=adm-pw -e PORT=8080 -p 127.0.0.1::8080 "$P/keycloak:test" start --optimized --cache=local >/dev/null
  kcport=""
  ready=1
  for _ in $(seq 1 300); do
    # re-read every round: Docker Desktop may report the published port late
    kcport="$(docker port "${P}_kc" 8080/tcp 2>/dev/null | head -n1 | sed 's/.*://' | tr -d '\r')"
    # no `-o /dev/null`: with MSYS path conversion off, a native curl.exe cannot open it
    if [ -n "$kcport" ] && [ "$(curl -s -m 3 -w '\n%{http_code}' "http://127.0.0.1:${kcport}/realms/master" | tail -n1)" = 200 ]; then ready=0; break; fi
    sleep 2
  done
  result keycloak_optimized_ready $ready "$(docker logs --tail 3 "${P}_kc" 2>&1 | tail -n 1)"
  out="$(docker run --name "${P}_kcc" --network "$NET" --memory 768m \
    -e KEYCLOAK_URL=http://kc:8080 -e KEYCLOAK_USER=admin -e KEYCLOAK_PASSWORD=adm-pw \
    -e KEYCLOAK_AVAILABILITYCHECK_ENABLED=true -e KEYCLOAK_AVAILABILITYCHECK_TIMEOUT=120s \
    -e IMPORT_VARSUBSTITUTION_ENABLED=true -e IMPORT_REMOTESTATE_ENABLED=true \
    -e IMPORT_MANAGED_CLIENTSCOPE=no-delete -e IMPORT_BEHAVIORS_REMOVEDEFAULTROLEFROMUSER=false \
    -e DEMO_ENABLED=true -e KEELSON_BFF_CLIENT_SECRET=s1 -e KEELSON_DEMO_CLIENT_SECRET=s2 \
    -e MERXA_WEB_CLIENT_SECRET=s3 -e MERXA_DEMO_CLIENT_SECRET=s4 -e DEMO_USER_PASSWORD=pw \
    -e JAVA_TOOL_OPTIONS=-XX:MaxRAMPercentage=60 "$P/kc-config:test" 2>&1)"; rc=$?
  result kcconfig_imports_realms $rc "$(printf '%s' "$out" | tail -n 2)"
  for realm in keelson merxa; do
    iss="$(curl -s -m 5 "http://127.0.0.1:${kcport}/realms/${realm}/.well-known/openid-configuration" |
      sed -n 's/.*"issuer":"\([^"]*\)".*/\1/p')"
    [ "$iss" = "https://auth.${DOMAIN}/realms/${realm}" ]; result "issuer_${realm}" $? "$iss"
  done
fi

echo "overlays: $fail failed check(s)" >&2
exit "$fail"
