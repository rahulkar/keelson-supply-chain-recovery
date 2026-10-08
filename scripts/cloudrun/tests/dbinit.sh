#!/usr/bin/env bash
# Integration test for the Cloud SQL init job image (infra/cloudrun/images/db-init) against
# throwaway Postgres 18 servers (no volumes, own network, everything removed on exit):
#   1. superuser path on the pg-core image: roles + databases for keelson adk keycloak litellm merxa,
#      owners, CONNECT revoked from PUBLIC, extensions at the pinned versions, idempotent re-run,
#      password rotation
#   2. version drift: Cloud SQL pins vs. the versions pg-core already installed -> exit 1
#   3. Cloud SQL emulation: a NON-superuser admin with CREATEROLE/CREATEDB (like Cloud SQL's
#      "postgres" user) creates roles, databases owned by them and btree_gist (PG16+ rules)
#   4. a pinned extension the server does not offer -> exit 1 naming the offered versions
# Exit status = number of failed checks. Usage: scripts/cloudrun/tests/dbinit.sh
# shellcheck disable=SC2319  # "[ cond ]; result <name> $?" reports the condition status on purpose
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1; fi
P="keelson_m3_cr_dbi"
NET="${P}_net"
IMG="${P}/db-init:test"
fail=0

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  docker rm -fv "${P}_core" "${P}_plain" >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
  docker image rm "$IMG" >/dev/null 2>&1 || true
}
trap cleanup EXIT

result() {  # name, ok(0/1), detail
  local ok=true; [ "$2" -eq 0 ] || { ok=false; fail=$((fail + 1)); }
  printf '{"check":"%s","ok":%s,"detail":"%s"}\n' "$1" "$ok" "$(printf '%s' "$3" | tr '\n"' ' ' | cut -c1-300)"
}

docker build -q -t "$IMG" "$ROOT/infra/cloudrun/images/db-init" >/dev/null || { echo "build failed" >&2; exit 99; }
docker network create "$NET" >/dev/null
docker run -d --name "${P}_core" --network "$NET" --memory 384m --tmpfs /var/lib/postgresql:size=512m \
  -e POSTGRES_PASSWORD=su-pw -e KEELSON_DB_PASSWORD=k-pw -e ADK_DB_PASSWORD=a-pw -e KEYCLOAK_DB_PASSWORD=kc-pw \
  keelson-pg-core:latest >/dev/null || exit 99
docker run -d --name "${P}_plain" --network "$NET" --memory 256m --tmpfs /var/lib/postgresql:size=256m \
  -e POSTGRES_PASSWORD=su-pw postgres:18-trixie >/dev/null || exit 99
for _ in $(seq 1 120); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' "${P}_core")" = healthy ] &&
    docker exec "${P}_plain" pg_isready -h 127.0.0.1 -q && break
  sleep 1
done

LOCAL_PINS=(-e PIN_POSTGIS=3.6.4 -e PIN_VECTOR=0.8.7 -e PIN_BTREE_GIST=1.8 -e PIN_PGCRYPTO=1.4)
mapfile -t CLOUD_PINS < <(sed -n 's/^\(PIN_[A-Z_]*=[^[:space:]]*\)[[:space:]]*$/--env=\1/p' "$ROOT/infra/cloudrun/versions.env")
[ "${#CLOUD_PINS[@]}" -eq 4 ] || { echo "expected 4 PIN_* lines in versions.env" >&2; exit 99; }
PWS=(-e KEELSON_DB_PASSWORD=k-pw -e ADK_DB_PASSWORD=a-pw -e KEYCLOAK_DB_PASSWORD=kc-pw
  -e LITELLM_DB_PASSWORD=l-pw -e MERXA_DB_PASSWORD=m-pw)
dbinit() {  # host user password [docker -e args...]
  local host="$1" user="$2" pw="$3"; shift 3
  docker run --rm --network "$NET" --memory 128m -e "PGHOST=$host" -e "PGUSER=$user" -e "PGPASSWORD=$pw" \
    -e PGSSLMODE=disable "$@" "$IMG" 2>&1
}
sql() { docker exec "$1" psql -U postgres -d "${3:-postgres}" -tAc "$2" 2>&1; }
login() {  # container role password db
  docker exec -e PGPASSWORD="$3" "$1" psql -h 127.0.0.1 -U "$2" -d "$4" -tAc 'select current_user' 2>&1
}

# 1. superuser path (pg-core image; its own first-boot init already created keelson/adk/keycloak)
out="$(dbinit "${P}_core" postgres su-pw -e "DB_ROLES=keelson adk keycloak litellm merxa" "${PWS[@]}" "${LOCAL_PINS[@]}")"; rc=$?
[ $rc -eq 0 ]; result superuser_run_ok $? "rc=$rc $(echo "$out" | tail -n 2)"
owners="$(sql "${P}_core" "select string_agg(datname||':'||pg_get_userbyid(datdba), ',' order by datname) from pg_database where datname in ('keelson','adk','keycloak','litellm','merxa')")"
[ "$owners" = "adk:adk,keelson:keelson,keycloak:keycloak,litellm:litellm,merxa:merxa" ]; result db_owned_by_own_role $? "$owners"
pub="$(sql "${P}_core" "select count(*) from pg_database d, aclexplode(d.datacl) a where d.datname in ('litellm','merxa') and a.grantee = 0 and a.privilege_type = 'CONNECT'")"
[ "$pub" = 0 ]; result connect_revoked_from_public $? "public connect grants=$pub"
x="$(login "${P}_core" litellm l-pw merxa)"
[[ "$x" == *"permission denied"* ]]; result cross_db_connect_denied $? "$x"
[ "$(login "${P}_core" merxa m-pw merxa)" = merxa ]; result role_password_works $? "merxa login"
exts="$(sql "${P}_core" "select string_agg(extname||'='||extversion, ',' order by extname) from pg_extension where extname <> 'plpgsql'" merxa)"
[ "$exts" = "btree_gist=1.8" ]; result merxa_btree_gist $? "$exts"
exts="$(sql "${P}_core" "select string_agg(extname||'='||extversion, ',' order by extname) from pg_extension where extname <> 'plpgsql'" keelson)"
[ "$exts" = "btree_gist=1.8,pgcrypto=1.4,postgis=3.6.4,vector=0.8.7" ]; result keelson_extensions_pinned $? "$exts"
out="$(dbinit "${P}_core" postgres su-pw -e "DB_ROLES=keelson adk keycloak litellm merxa" "${PWS[@]}" "${LOCAL_PINS[@]}")"; rc=$?
[ $rc -eq 0 ]; result idempotent_rerun $? "rc=$rc"
out="$(dbinit "${P}_core" postgres su-pw -e "DB_ROLES=merxa" -e MERXA_DB_PASSWORD=m-pw-2 "${LOCAL_PINS[@]}")"; rc=$?
[ $rc -eq 0 ] && [ "$(login "${P}_core" merxa m-pw-2 merxa)" = merxa ]; result password_rotation $? "rc=$rc"
[[ "$out" != *m-pw-2* ]]; result password_not_logged $? "output checked"
out="$(dbinit "${P}_core" postgres su-pw -e "DB_ROLES=litellm" "${LOCAL_PINS[@]}")"; rc=$?
[ $rc -eq 1 ] && [[ "$out" == *"LITELLM_DB_PASSWORD is required"* ]]; result missing_password_fails $? "rc=$rc"
out="$(dbinit "${P}_core" postgres su-pw -e "DB_ROLES=bad-name" "${LOCAL_PINS[@]}")"; rc=$?
[ $rc -eq 1 ] && [[ "$out" == *"invalid role name"* ]]; result invalid_role_rejected $? "rc=$rc"

# 2. Cloud SQL pins against pg-core's newer extensions -> drift detected
out="$(dbinit "${P}_core" postgres su-pw -e "DB_ROLES=keelson" -e KEELSON_DB_PASSWORD=k-pw "${CLOUD_PINS[@]}")"; rc=$?
[ $rc -eq 1 ] && [[ "$out" == *'"extension version drift"'*'"extension":"postgis"'* ]]; result version_drift_detected $? "rc=$rc $(echo "$out" | grep -m1 drift)"

# 3. Cloud SQL emulation: non-superuser admin with CREATEROLE + CREATEDB
sql "${P}_plain" "CREATE ROLE cloudadmin LOGIN PASSWORD 'ca-pw' CREATEROLE CREATEDB" >/dev/null
out="$(dbinit "${P}_plain" cloudadmin ca-pw -e "DB_ROLES=litellm merxa" -e LITELLM_DB_PASSWORD=l-pw -e MERXA_DB_PASSWORD=m-pw "${CLOUD_PINS[@]}")"; rc=$?
[ $rc -eq 0 ]; result nonsuperuser_run_ok $? "rc=$rc $(echo "$out" | tail -n 3)"
owners="$(sql "${P}_plain" "select string_agg(datname||':'||pg_get_userbyid(datdba), ',' order by datname) from pg_database where datname in ('litellm','merxa')")"
[ "$owners" = "litellm:litellm,merxa:merxa" ]; result nonsuperuser_db_owners $? "$owners"
pub="$(sql "${P}_plain" "select count(*) from pg_database d, aclexplode(d.datacl) a where d.datname in ('litellm','merxa') and a.grantee = 0")"
[ "$pub" = 0 ]; result nonsuperuser_revoke_public $? "public grants=$pub"
[ "$(sql "${P}_plain" "select extversion from pg_extension where extname='btree_gist'" merxa)" = 1.8 ]; result nonsuperuser_btree_gist $? "btree_gist in merxa"
out="$(dbinit "${P}_plain" cloudadmin ca-pw -e "DB_ROLES=litellm merxa" -e LITELLM_DB_PASSWORD=l-pw -e MERXA_DB_PASSWORD=m-pw "${CLOUD_PINS[@]}")"; rc=$?
[ $rc -eq 0 ]; result nonsuperuser_idempotent $? "rc=$rc $(echo "$out" | tail -n 2)"

# 4. pinned extension not offered by the server (plain postgres has no PostGIS)
out="$(dbinit "${P}_plain" postgres su-pw -e "DB_ROLES=keelson" -e KEELSON_DB_PASSWORD=k-pw "${CLOUD_PINS[@]}")"; rc=$?
[ $rc -eq 1 ] && [[ "$out" == *'"extension version not offered"'*'"extension":"postgis"'* ]]; result missing_extension_detected $? "rc=$rc"

echo "dbinit: $fail failed check(s)" >&2
exit "$fail"
