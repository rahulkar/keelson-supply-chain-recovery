#!/usr/bin/env bash
# First-boot test for the pg-core image (infra/postgres/core) without touching the
# compose volume: builds the image, starts a throwaway container (no volume, dummy
# passwords) and asserts
#   * the image HEALTHCHECK only turns healthy after the init scripts have finished
#   * roles/databases keelson, adk, keycloak exist, each DB owned by its own role
#   * CONNECT is revoked from PUBLIC (cross-DB and template1/postgres connects fail)
#   * extensions postgis, vector, btree_gist, pgcrypto are installed in keelson
#   * the keelson role can create tables in its own database
# Exit status = number of failed checks. Usage: scripts/tests/pgcore/run.sh
# shellcheck disable=SC2319  # "[ cond ]; result <name> $?" reports the condition status on purpose
set -uo pipefail   # no -e: every check reports its own result

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1; fi
NAME="keelson-pgcore-test-$$"
fail=0

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

result() {  # name, ok(0/1), detail
  local ok=true; [ "$2" -eq 0 ] || { ok=false; fail=$((fail + 1)); }
  printf '{"check":"%s","ok":%s,"detail":"%s"}\n' "$1" "$ok" "$3"
}

IMAGE="$(docker build -q "$ROOT/infra/postgres/core")" || { echo "image build failed" >&2; exit 99; }
docker run -d --name "$NAME" --memory 384m --health-interval 1s --health-start-period 0s \
  -e POSTGRES_PASSWORD=test-super -e KEELSON_DB_PASSWORD=test-keelson \
  -e ADK_DB_PASSWORD=test-adk -e KEYCLOAK_DB_PASSWORD=test-keycloak "$IMAGE" >/dev/null

# 1. health gating: never "healthy" while the init script has not logged completion.
early=0; healthy=0
for _ in $(seq 1 120); do
  h="$(docker inspect -f '{{.State.Health.Status}}' "$NAME")"
  done_init="$(docker logs "$NAME" 2>&1 | grep -c '"pg-core initialised"' || true)"
  if [ "$h" = healthy ]; then
    healthy=1
    [ "$done_init" -ge 1 ] || early=1
    break
  fi
  sleep 0.5
done
result healthcheck_waits_for_init $(( early || !healthy )) "healthy=$healthy early=$early"

# Wait for the final server (TCP) - the init server is socket-only.
for _ in $(seq 1 60); do docker exec "$NAME" pg_isready -h 127.0.0.1 -q && break; sleep 0.5; done

su_sql() { docker exec "$NAME" psql -U postgres -tAc "$1"; }
as_role() {  # role password db sql
  docker exec -e PGPASSWORD="$2" "$NAME" psql -h 127.0.0.1 -U "$1" -d "$3" -tAc "$4" 2>&1
}

owners="$(su_sql "select string_agg(datname||':'||pg_get_userbyid(datdba), ',' order by datname) from pg_database where datname in ('keelson','adk','keycloak')")"
[ "$owners" = "adk:adk,keelson:keelson,keycloak:keycloak" ]; result db_owned_by_own_role $? "$owners"

exts="$(docker exec "$NAME" psql -U postgres -d keelson -tAc "select string_agg(extname, ',' order by extname) from pg_extension where extname in ('postgis','vector','btree_gist','pgcrypto')")"
[ "$exts" = "btree_gist,pgcrypto,postgis,vector" ]; result keelson_extensions $? "$exts"

out="$(as_role keelson test-keelson keelson "create table t(g geometry(Point,4326), e vector(3)); drop table t; select 'ok'")"
[ "$(echo "$out" | tail -1)" = ok ]; result keelson_can_create_tables $? "$(echo "$out" | tail -1)"

denied=0; detail=""
for pair in "keelson:test-keelson:adk" "adk:test-adk:keycloak" "keycloak:test-keycloak:keelson" \
            "keelson:test-keelson:postgres" "keelson:test-keelson:template1"; do
  IFS=: read -r role pw db <<<"$pair"
  out="$(as_role "$role" "$pw" "$db" "select 1")"  # psql fails here by design (pipefail)
  if grep -q "does not have CONNECT privilege" <<<"$out"; then
    denied=$((denied + 1))
  else
    detail="$detail $role->$db"
  fi
done
[ "$denied" -eq 5 ]; result connect_revoked_from_public $? "denied=$denied/5${detail:+ allowed:$detail}"

for role_pw in "keelson:test-keelson" "adk:test-adk" "keycloak:test-keycloak"; do
  IFS=: read -r role pw <<<"$role_pw"
  [ "$(as_role "$role" "$pw" "$role" "select 1" | tail -1)" = 1 ]; result "login_${role}_own_db" $? ""
done

super="$(su_sql "select count(*) from pg_roles where rolname in ('keelson','adk','keycloak') and (rolsuper or rolcreatedb or rolcreaterole or rolbypassrls)")"
[ "$super" = 0 ]; result roles_unprivileged $? "privileged=$super"

exit "$fail"
