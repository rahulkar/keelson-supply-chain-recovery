#!/usr/bin/env bash
# Cloud Run parity check for the Keelson/Merxa app images.
#
# Runs every app image the way Cloud Run runs it and checks that it becomes healthy:
#   * PORT=8080 (+ K_SERVICE/K_REVISION/K_CONFIGURATION), the image's own CMD (or the command the
#     Cloud Run manifest sets), listening on 0.0.0.0:$PORT;
#   * read-only root filesystem, tmpfs /tmp (plus the in-memory volumes the manifests declare),
#     all capabilities dropped, no-new-privileges, the image's USER (must not be root);
#   * own throwaway bridge network, no published port except one random loopback port for the probe;
#   * minimal environment: only what the service needs to start (throwaway pg-core + valkey for
#     the engine and merxa-api, dummy secrets).
# HTTP images: GET <health path> must answer 2xx within --timeout seconds (default 60).
# engine-worker (no port): must still be running after 25 s with a clean log.
# Any "Read-only file system" / "Permission denied" in a log is reported as a warning.
#
# Usage: scripts/cloudrun/parity-check.sh [--only a,b] [--no-build] [--with-keycloak] [--keep]
#                                         [--timeout S] [--list] [--selftest] [--image svc=ref]...
#   --only           subset of: engine-api engine-worker optimizer merxa-api keelson-web merxa-web keycloak
#   --image svc=ref  check ref instead of keelson-<svc>:latest (repeatable; never built)
#   --no-build       report a missing image as FAIL instead of building it with `docker compose build`
#   --with-keycloak  also build infra/cloudrun/images/keycloak.Dockerfile and check
#                    `start --optimized --cache=local` (needs ~1.3 GB RAM while it runs)
#   --keep           keep containers/network/images for debugging (default: removed)
#   --selftest       regression test of this checker: synthetic images that must PASS / FAIL
#                    (wrong port, write outside /tmp, root user, crash); exit 0 = checker OK
# Output: one JSON line per image, then a table. Exit status = number of FAILed images.
# Everything it creates is named keelson_m3_cr_* and removed on exit.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1; fi

PREFIX="keelson_m3_cr"
NET="${PREFIX}_net"
PG="${PREFIX}_pg"
VALKEY="${PREFIX}_valkey"
KC_IMAGE="${PREFIX}/keycloak:parity"
TIMEOUT=60
WORKER_SETTLE=25
ONLY=""
BUILD=1
WITH_KC=0
KEEP=0
MIN_FREE_GB=3
SELFTEST=0
IMAGE_OVERRIDES=()

ALL=(engine-api engine-worker optimizer merxa-api keelson-web merxa-web)

usage() { sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="${2:?}"; shift 2 ;;
    --image)  # svc=ref: check ref instead of keelson-<svc>:latest (e.g. a freshly built tag)
      case "${2:-}" in *=?*) IMAGE_OVERRIDES+=("$2") ;; *) echo "parity-check: --image needs svc=ref" >&2; exit 2 ;; esac
      shift 2 ;;
    --no-build) BUILD=0; shift ;;
    --with-keycloak) WITH_KC=1; shift ;;
    --keep) KEEP=1; shift ;;
    --timeout) TIMEOUT="${2:?}"; shift 2 ;;
    --selftest) SELFTEST=1; shift ;;
    --list) printf '%s\n' "${ALL[@]}" keycloak; exit 0 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "parity-check: unknown argument $1" >&2; exit 2 ;;
  esac
done
SELECTED=()
if [ -n "$ONLY" ]; then
  IFS=',' read -r -a SELECTED <<<"$ONLY"
  for s in "${SELECTED[@]}"; do
    case " ${ALL[*]} keycloak " in *" $s "*) ;; *) echo "parity-check: unknown image $s" >&2; exit 2 ;; esac
  done
else
  SELECTED=("${ALL[@]}")
  [ "$WITH_KC" = 1 ] && SELECTED+=(keycloak)
fi
wants() { case " ${SELECTED[*]} " in *" $1 "*) return 0 ;; *) return 1 ;; esac; }

CREATED=()
BUILT_KC=0
ERRDIR="$(mktemp -d)"  # docker run stderr per container; removed on exit
EXTRA_IMAGES=()
# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  rm -rf "$ERRDIR"
  [ "$KEEP" = 1 ] && { echo "parity-check: --keep: leaving ${CREATED[*]} $NET" >&2; return; }
  for c in "${CREATED[@]}"; do docker rm -fv "$c" >/dev/null 2>&1 || true; done
  docker network rm "$NET" >/dev/null 2>&1 || true
  [ "$BUILT_KC" = 1 ] && EXTRA_IMAGES+=("$KC_IMAGE")
  for i in "${EXTRA_IMAGES[@]}"; do docker image rm "$i" >/dev/null 2>&1 || true; done
}
trap cleanup EXIT

free_gb() { df -Pk "$ROOT" 2>/dev/null | awk 'NR==2 {printf "%d", $4 / 1048576}'; }
ensure_disk() {
  local f; f="$(free_gb)"
  if [ -n "$f" ] && [ "$f" -lt "$MIN_FREE_GB" ]; then
    echo "parity-check: only ${f} GB free on the Docker disk host volume (< ${MIN_FREE_GB} GB): not building" >&2
    return 1
  fi
}

# image name compose gives a service: <project>-<service>
image_of() {
  local o
  for o in "${IMAGE_OVERRIDES[@]}"; do
    [ "${o%%=*}" = "$1" ] && { printf '%s' "${o#*=}"; return; }
  done
  printf 'keelson-%s:latest' "$1"
}

ensure_image() {  # svc -> 0 if the image exists (building it with compose if allowed)
  local svc="$1" img; img="$(image_of "$svc")"
  docker image inspect "$img" >/dev/null 2>&1 && return 0
  [ "$BUILD" = 1 ] && [ "$img" = "keelson-${svc}:latest" ] || return 1  # --image refs are never built
  ensure_disk || return 1
  echo "parity-check: building $img (docker compose build $svc)" >&2
  GEMINI_API_KEY="${GEMINI_API_KEY:-parity-unused}" env -u LITELLM_MASTER_KEY \
    docker compose -f "$ROOT/compose.yaml" --project-directory "$ROOT" build "$svc" >&2 || return 1
  docker image inspect "$img" >/dev/null 2>&1
}

RESULTS=()
emit() {  # image status seconds detail fix
  local img="$1" st="$2" secs="$3" detail="$4" fix="$5"
  # one line per result, no field separator inside a field
  detail="$(printf '%s' "$detail" | tr '\n\r|' '  /')"; fix="$(printf '%s' "$fix" | tr '\n\r|' '  /')"
  RESULTS+=("$img|$st|$secs|$detail|$fix")
  printf '{"image":"%s","status":"%s","seconds":%s,"detail":"%s","fix":"%s"}\n' \
    "$img" "$st" "${secs:-null}" "$(json_escape "$detail")" "$(json_escape "$fix")"
}
json_escape() { printf '%s' "$1" | tr '\n\r\t' '   ' | sed 's/\\/\\\\/g; s/"/\\"/g'; }

fs_errors() { docker logs "$1" 2>&1 | grep -iE "read-only file system|EROFS|permission denied|EACCES" | head -n 3; }

suggest() {  # container log excerpt -> suggested fix
  local logs="$1"
  case "$logs" in
    *[Rr]ead-only\ file\ system* | *EROFS*)
      echo "the process writes outside /tmp: point that path at /tmp via env (e.g. a *_DIR / cache setting) or declare an in-memory volume for it in infra/cloudrun; better, make the image not write there" ;;
    *[Pp]ermission\ denied* | *EACCES*)
      echo "a path the non-root user must write is root-owned: chown it in the Dockerfile or move it under /tmp" ;;
    *[Aa]ddress\ already\ in\ use*) echo "port clash inside the container" ;;
    *) echo "see the log excerpt; the server must listen on 0.0.0.0:\$PORT (8080) and start without optional dependencies" ;;
  esac
}

user_check() {  # image -> "" if non-root, else reason
  local u; u="$(docker image inspect -f '{{.Config.User}}' "$1" 2>/dev/null)"
  case "$u" in
    "" | root | 0 | 0:* | root:*) echo "image USER is '${u:-<unset>}' (root)" ;;
    *) echo "" ;;
  esac
}

# shellcheck disable=SC2054  # the commas belong to the --tmpfs option value
COMMON=(--read-only --tmpfs /tmp:rw,nosuid,nodev,size=256m,mode=1777
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 512 --cpus 1
  --network "$NET" -e PORT=8080)

start_deps() {
  docker network create "$NET" >/dev/null 2>&1 || true
  docker image inspect keelson-pg-core:latest >/dev/null 2>&1 || {
    [ "$BUILD" = 1 ] && ensure_disk &&
      GEMINI_API_KEY="${GEMINI_API_KEY:-parity-unused}" env -u LITELLM_MASTER_KEY \
        docker compose -f "$ROOT/compose.yaml" --project-directory "$ROOT" build pg-core >&2
  } || { echo "parity-check: keelson-pg-core image missing" >&2; return 1; }
  CREATED+=("$PG" "$VALKEY")
  docker run -d --name "$PG" --network "$NET" --memory 384m \
    --tmpfs /var/lib/postgresql:rw,size=768m \
    -e POSTGRES_PASSWORD=parity-super -e KEELSON_DB_PASSWORD=parity-keelson \
    -e ADK_DB_PASSWORD=parity-adk -e KEYCLOAK_DB_PASSWORD=parity-keycloak \
    keelson-pg-core:latest postgres -c shared_buffers=32MB -c max_connections=80 >/dev/null || return 1
  docker run -d --name "$VALKEY" --network "$NET" --memory 64m valkey/valkey:8.1.10-alpine@sha256:081c2f5cb575efc901aa80ff9cdbd1ec6a301682fd35e1ebb4b0990a4a4a8507 \
    valkey-server --save "" --appendonly no >/dev/null || return 1

  for _ in $(seq 1 120); do
    [ "$(docker inspect -f '{{.State.Health.Status}}' "$PG" 2>/dev/null)" = healthy ] && break
    sleep 1
  done
  [ "$(docker inspect -f '{{.State.Health.Status}}' "$PG")" = healthy ] || { echo "parity-check: pg-core not healthy" >&2; return 1; }
  # merxa database (Cloud SQL layout: one role + database per consumer; btree_gist pre-created).
  docker exec "$PG" psql -U postgres -v ON_ERROR_STOP=1 -q \
    -c "CREATE ROLE merxa LOGIN PASSWORD 'parity-merxa'" \
    -c "CREATE DATABASE merxa OWNER merxa" >/dev/null || return 1
  docker exec "$PG" psql -U postgres -d merxa -v ON_ERROR_STOP=1 -q -c "CREATE EXTENSION IF NOT EXISTS btree_gist" >/dev/null || return 1
}

ENGINE_ENV=(-e "KEELSON_DATABASE_URL=postgresql+asyncpg://keelson:parity-keelson@${PG}:5432/keelson"
  -e "ADK_DATABASE_URL=postgresql+asyncpg://adk:parity-adk@${PG}:5432/adk"
  -e "VALKEY_URL=redis://${VALKEY}:6379/0" -e "SESSION_SECRET=parity-session-secret-0123456789"
  -e "OPTIMIZER_URL=http://${PREFIX}_optimizer:8080" -e "OPTIMIZER_SHARED_SECRET=parity-optimizer-secret"
  -e "CONNECTOR_PROXY_URL=" -e "KEELSON_DATA_KEY=$(printf 'parity-data-key-32-bytes-long!!!' | base64)")

migrate_engine() {  # apply the engine migrations to the throwaway DB (what keelson-bootstrap does)
  docker run --rm --network "$NET" --memory 384m "${ENGINE_ENV[@]}" "$(image_of engine-api)" python -c '
import asyncio, os
from sqlalchemy.ext.asyncio import create_async_engine
from keelson_engine.core.migrate import migrate
async def main():
    e = create_async_engine(os.environ["KEELSON_DATABASE_URL"])
    try:
        print("applied", len(await migrate(e)), "migrations")
    finally:
        await e.dispose()
asyncio.run(main())' >&2
}

check_http() {  # name image path mem timeout [docker args...] -- [command...]
  local name="$1" img="$2" path="$3" mem="$4" limit="$5"; shift 5
  local args=() cmd=() c="${PREFIX}_${name//-/_}" port="" t0 now code="" st="" root
  while [ $# -gt 0 ] && [ "$1" != -- ]; do args+=("$1"); shift; done
  [ $# -gt 0 ] && shift
  cmd=("$@")
  root="$(user_check "$img")"
  if [ -n "$root" ]; then emit "$name" FAIL "" "$root" "add a non-root USER to the image (Cloud Run runs the image USER)"; return; fi
  CREATED+=("$c")
  t0=$(date +%s)
  if ! docker run -d --name "$c" "${COMMON[@]}" --memory "$mem" \
      -e "K_SERVICE=$name" -e "K_REVISION=${name}-parity" -e "K_CONFIGURATION=$name" \
      -p 127.0.0.1::8080 "${args[@]}" "$img" "${cmd[@]}" >/dev/null 2>"$ERRDIR/${c}.err"; then
    emit "$name" FAIL "" "docker run failed: $(cat "$ERRDIR/${c}.err")" "fix the run options/image"; return
  fi
  port=""
  while :; do
    now=$(date +%s)
    if [ "$(docker inspect -f '{{.State.Running}}' "$c" 2>/dev/null)" != true ]; then
      local ex logs; ex="$(docker inspect -f '{{.State.ExitCode}}' "$c")"; logs="$(docker logs --tail 15 "$c" 2>&1)"
      emit "$name" FAIL "$((now - t0))" "exited with code $ex: $(printf '%s' "$logs" | tail -n 6)" "$(suggest "$logs")"; return
    fi
    # Re-read every round: Docker Desktop may report the published port late. No
    # `-o /dev/null`: with MSYS path conversion off, a native curl.exe cannot open it.
    port="$(docker port "$c" 8080/tcp 2>/dev/null | head -n1 | sed 's/.*://' | tr -d '\r')"
    code=""
    if [ -n "$port" ]; then
      code="$(curl -s -m 3 -w '\n%{http_code}' "http://127.0.0.1:${port}${path}" 2>/dev/null | tail -n1)"
    fi
    case "$code" in 2??) break ;; esac
    if [ $((now - t0)) -ge "$limit" ]; then
      local logs; logs="$(docker logs --tail 15 "$c" 2>&1)"
      emit "$name" FAIL "$((now - t0))" "no 2xx on ${path} within ${limit}s (last status ${code:-none}): $(printf '%s' "$logs" | tail -n 6)" "$(suggest "$logs")"; return
    fi
    sleep 1
  done
  local secs=$(( $(date +%s) - t0 )) uid fse
  uid="$(docker exec "$c" id -u 2>/dev/null || echo '?')"
  [ "$uid" != 0 ] || { emit "$name" FAIL "$secs" "process runs as uid 0" "set a non-root USER"; return; }
  sleep 2
  fse="$(fs_errors "$c")"
  if [ -n "$fse" ]; then st=WARN; else st=PASS; fi
  emit "$name" "$st" "$secs" "GET ${path} -> ${code} as uid ${uid}${fse:+; log: $fse}" "$( [ -n "$fse" ] && suggest "$fse")"
  [ "$KEEP" = 1 ] || { docker rm -fv "$c" >/dev/null 2>&1; }
}

check_worker() {  # name image mem [docker args...] -- command...
  local name="$1" img="$2" mem="$3"; shift 3
  local args=() c="${PREFIX}_${name//-/_}" root logs fse
  while [ $# -gt 0 ] && [ "$1" != -- ]; do args+=("$1"); shift; done
  [ $# -gt 0 ] && shift
  root="$(user_check "$img")"
  if [ -n "$root" ]; then emit "$name" FAIL "" "$root" "add a non-root USER to the image"; return; fi
  CREATED+=("$c")
  docker run -d --name "$c" "${COMMON[@]}" --memory "$mem" "${args[@]}" "$img" "$@" >/dev/null || {
    emit "$name" FAIL "" "docker run failed" "fix the run options/image"; return; }
  sleep "$WORKER_SETTLE"
  logs="$(docker logs --tail 15 "$c" 2>&1)"
  if [ "$(docker inspect -f '{{.State.Running}}' "$c")" != true ]; then
    emit "$name" FAIL "$WORKER_SETTLE" "exited with code $(docker inspect -f '{{.State.ExitCode}}' "$c"): $(printf '%s' "$logs" | tail -n 6)" "$(suggest "$logs")"; return
  fi
  fse="$(fs_errors "$c")"
  if [ -n "$fse" ]; then
    emit "$name" WARN "$WORKER_SETTLE" "running, but: $fse" "$(suggest "$fse")"
  else
    emit "$name" PASS "$WORKER_SETTLE" "running after ${WORKER_SETTLE}s as uid $(docker exec "$c" id -u 2>/dev/null), no filesystem errors" ""
  fi
  [ "$KEEP" = 1 ] || docker rm -fv "$c" >/dev/null 2>&1
}

selftest() {  # synthetic images with known outcomes; returns the number of wrong verdicts
  local img="${PREFIX}/selftest:1" base="python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170" wrong=0 r name st want
  local srv='import http.server,os
class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
http.server.ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("BIND_PORT") or os.environ["PORT"])), H).serve_forever()'
  docker image inspect "$base" >/dev/null 2>&1 || { echo "selftest needs $base" >&2; return 99; }
  printf 'FROM %s\nUSER 65534:65534\n' "$base" | docker build -q -t "$img" - >/dev/null || return 99
  docker network create "$NET" >/dev/null 2>&1 || true
  check_http st-good "$img" /healthz 128m 20 -- python -c "$srv"
  check_http st-wrong-port "$img" /healthz 128m 8 -e BIND_PORT=8000 -- python -c "$srv"
  check_http st-writes-root "$img" /healthz 128m 20 -- python -c "open('/var/cache/state.db','w'); $srv"
  check_http st-root-user "$base" /healthz 128m 20 -- python -c "$srv"
  check_http st-crash "$img" /healthz 128m 20 -- python -c "raise SystemExit(3)"
  check_worker st-worker-ok "$img" 128m -- python -c "import time; time.sleep(600)"
  EXTRA_IMAGES+=("$img")  # removed by cleanup, after its containers
  for r in "${RESULTS[@]}"; do
    IFS='|' read -r name st _ _ fix <<<"$r"
    case "$name" in
      st-good | st-worker-ok) want=PASS ;;
      *) want=FAIL ;;
    esac
    if [ "$st" != "$want" ]; then
      echo "selftest: $name: got $st, want $want" >&2; wrong=$((wrong + 1))
    fi
    if [ "$name" = st-writes-root ] && [[ "$fix" != *"writes outside /tmp"* ]]; then
      echo "selftest: st-writes-root: missing read-only filesystem hint ($fix)" >&2; wrong=$((wrong + 1))
    fi
  done
  echo "selftest: ${#RESULTS[@]} cases, $wrong wrong verdict(s)" >&2
  return "$wrong"
}

# ───────────────────────────── main ─────────────────────────────
if [ "$SELFTEST" = 1 ]; then
  WORKER_SETTLE=5
  selftest
  exit $?
fi
for svc in "${SELECTED[@]}"; do
  [ "$svc" = keycloak ] && continue
  ensure_image "$svc" || emit "$svc" FAIL "" "image $(image_of "$svc") missing and not built" "docker compose build $svc"
done
if ! start_deps; then
  echo "parity-check: could not start the throwaway dependencies" >&2
  exit 99
fi
if wants engine-api || wants engine-worker; then
  migrate_engine || echo "parity-check: engine migrations failed (engine checks continue without schema)" >&2
fi

have() { docker image inspect "$(image_of "$1")" >/dev/null 2>&1; }
if wants optimizer && have optimizer; then
  check_http optimizer "$(image_of optimizer)" /healthz 512m "$TIMEOUT" \
    -e OPTIMIZER_SHARED_SECRET=parity-optimizer-secret -e CPSAT_NUM_WORKERS=1
fi
if wants engine-api && have engine-api; then
  check_http engine-api "$(image_of engine-api)" /api/healthz 768m "$TIMEOUT" "${ENGINE_ENV[@]}"
fi
if wants engine-worker && have engine-worker; then
  check_worker engine-worker "$(image_of engine-worker)" 768m "${ENGINE_ENV[@]}" -- python -m keelson_engine.worker
fi
if wants merxa-api && have merxa-api; then
  # /data/snapshots = the in-memory volume of infra/cloudrun/services/merxa-api.yaml
  check_http merxa-api "$(image_of merxa-api)" /healthz 768m "$TIMEOUT" \
    --tmpfs /data/snapshots:rw,size=256m,uid=10001,gid=10001,mode=0700 \
    -e "MERXA_DATABASE_URL=postgresql+asyncpg://merxa:parity-merxa@${PG}:5432/merxa?ssl=disable" \
    -e SEED_PROFILE=small -e DEMO_MODE=1 -e SESSION_SECRET=parity-session-secret-0123456789 \
    -e MERXA_SNAPSHOT_SECRET=parity-snapshot-secret -e PUBLIC_URL=https://merxa.parity.example
fi
if wants keelson-web && have keelson-web; then
  check_http keelson-web "$(image_of keelson-web)" /healthz 384m "$TIMEOUT" \
    -e HOSTNAME=0.0.0.0 -e "ENGINE_API_INTERNAL_URL=http://${PREFIX}_engine_api:8080"
fi
if wants merxa-web && have merxa-web; then
  check_http merxa-web "$(image_of merxa-web)" /healthz 384m "$TIMEOUT" \
    -e HOSTNAME=0.0.0.0 -e "MERXA_API_INTERNAL_URL=http://${PREFIX}_merxa_api:8080"
fi
if wants keycloak; then
  if ensure_disk && docker build -q -f "$ROOT/infra/cloudrun/images/keycloak.Dockerfile" -t "$KC_IMAGE" "$ROOT/infra/cloudrun/images" >/dev/null; then
    BUILT_KC=1
    # Cloud Run's root filesystem is writable (in memory); with --read-only Keycloak needs its
    # data dir (tmp + transaction logs) writable, as a tmpfs here.
    # First boot creates the schema (Liquibase), which takes minutes on one CPU; Keycloak answers
    # 503 on every path until it is bootstrapped, so /realms/master is a true readiness probe.
    check_http keycloak "$KC_IMAGE" /realms/master 1280m 300 \
      --tmpfs /opt/keycloak/data:rw,size=128m,uid=1000,gid=0,mode=0770 \
      -e "KC_DB_URL=jdbc:postgresql://${PG}:5432/keycloak" -e KC_DB_USERNAME=keycloak \
      -e KC_DB_PASSWORD=parity-keycloak -e KC_HOSTNAME=https://auth.parity.example \
      -e KC_HOSTNAME_BACKCHANNEL_DYNAMIC=true -e KC_HTTP_ENABLED=true -e KC_HTTP_PORT=8080 \
      -e KC_PROXY_HEADERS=xforwarded -e KC_BOOTSTRAP_ADMIN_USERNAME=admin \
      -e KC_BOOTSTRAP_ADMIN_PASSWORD=parity-admin \
      -e "JAVA_OPTS_KC_HEAP=-XX:MaxRAMPercentage=60 -XX:MinHeapFreeRatio=10 -XX:MaxHeapFreeRatio=20" \
      -- start --optimized --cache=local
  else
    emit keycloak FAIL "" "could not build infra/cloudrun/images/keycloak.Dockerfile" "see docker build output"
  fi
fi

fails=0
printf '\n%-14s %-5s %5s  %s\n' IMAGE RESULT SECS DETAIL
for r in "${RESULTS[@]}"; do
  IFS='|' read -r img st secs detail fix <<<"$r"
  printf '%-14s %-5s %5s  %s\n' "$img" "$st" "${secs:--}" "$(printf '%s' "$detail" | cut -c1-160)"
  [ -n "$fix" ] && printf '%-26s fix: %s\n' "" "$fix"
  [ "$st" = FAIL ] && fails=$((fails + 1))
done
exit "$fails"
