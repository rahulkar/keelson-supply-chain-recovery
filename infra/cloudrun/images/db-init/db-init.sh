#!/usr/bin/env bash
# Cloud SQL initialisation for Keelson (Cloud Run Job "keelson-db-init-<instance>").
#
# The Cloud Run counterpart of infra/postgres/core/init/10-keelson-init.sh. It runs on every deploy
# and is idempotent:
#   * one LOGIN role + one database per consumer, each database owned by its own role,
#     CONNECT revoked from PUBLIC (roles only reach their own database);
#   * role passwords are (re)set from Secret Manager on every run, so rotating a password is
#     "add a secret version, re-run the job, redeploy the consumer";
#   * extensions are created with the versions pinned in infra/cloudrun/versions.env (Cloud SQL's
#     own versions for POSTGRES_18) and verified; a mismatch fails the job.
#
# Cloud SQL's "postgres" user is NOT a superuser (cloudsqlsuperuser: CREATEROLE, CREATEDB,
# extension creation). Since PG16 a CREATEROLE user only administers the roles it created and
# needs explicit SET/INHERIT membership to create a database owned by another role, so this script
# grants itself that membership (it is the instance administrator anyway).
#
# Environment (passwords come from Secret Manager, never from arguments):
#   PGHOST, PGPORT (5432), PGUSER (postgres), PGPASSWORD, PGSSLMODE (require)
#   DB_ROLES          space separated, e.g. "keelson adk keycloak litellm merxa"
#   <ROLE>_DB_PASSWORD for every role in DB_ROLES (e.g. KEELSON_DB_PASSWORD)
#   PIN_POSTGIS, PIN_VECTOR, PIN_BTREE_GIST, PIN_PGCRYPTO   extension versions
#   SKIP_EXTENSIONS=1 only for tests against a server without the pinned versions
# Exit status: 0 ok, 1 failure (message on stderr, never a password).
set -Eeuo pipefail

log() { printf '{"level":"info","component":"keelson-db-init","msg":"%s"}\n' "$*"; }
die() { printf '{"level":"error","component":"keelson-db-init","msg":"%s"}\n' "$*" >&2; exit 1; }

: "${PGHOST:?PGHOST is required}"
: "${PGPASSWORD:?PGPASSWORD is required}"
: "${DB_ROLES:?DB_ROLES is required}"
export PGPORT="${PGPORT:-5432}" PGUSER="${PGUSER:-postgres}" PGSSLMODE="${PGSSLMODE:-require}"
export PGCONNECT_TIMEOUT="${PGCONNECT_TIMEOUT:-10}" PGAPPNAME=keelson-db-init

# database -> extensions it needs (keelson: engine domain; merxa: its own migration 0002 needs
# btree_gist and the merxa role is not allowed to create extensions on Cloud SQL).
extensions_for() {
  case "$1" in
    keelson) echo "postgis vector btree_gist pgcrypto" ;;
    merxa) echo "btree_gist" ;;
    *) echo "" ;;
  esac
}

pin_for() {
  case "$1" in
    postgis) echo "${PIN_POSTGIS:?PIN_POSTGIS is required}" ;;
    vector) echo "${PIN_VECTOR:?PIN_VECTOR is required}" ;;
    btree_gist) echo "${PIN_BTREE_GIST:?PIN_BTREE_GIST is required}" ;;
    pgcrypto) echo "${PIN_PGCRYPTO:?PIN_PGCRYPTO is required}" ;;
    *) die "no version pin for extension $1" ;;
  esac
}

psql_db() {  # db, psql args...
  local db="$1"; shift
  psql -v ON_ERROR_STOP=1 --no-psqlrc -X -q --dbname "$db" "$@"
}

# Wait for the instance (a fresh Cloud SQL instance can refuse connections for a moment).
for i in $(seq 1 30); do
  if psql_db postgres -tAc 'select 1' >/dev/null 2>&1; then break; fi
  [ "$i" -eq 30 ] && die "cannot connect to ${PGHOST}:${PGPORT} as ${PGUSER}"
  sleep 2
done
log "connected to $(psql_db postgres -tAc 'show server_version')"

for role in $DB_ROLES; do
  case "$role" in
    *[!a-z0-9_]* | "") die "invalid role name: $role" ;;
  esac
  var="$(printf '%s' "$role" | tr '[:lower:]' '[:upper:]')_DB_PASSWORD"
  pw="${!var:-}"
  [ -n "$pw" ] || die "$var is required for role $role"
  log "role and database ${role}"
  psql_db postgres -v role="$role" -v pw="$pw" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS', :'role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I WITH LOGIN PASSWORD %L', :'role', :'pw') \gexec
-- PG16+: membership with SET (create a database owned by the role) and INHERIT (act as owner
-- for REVOKE/GRANT and extension creation). A no-op for a real superuser.
SELECT format('GRANT %I TO CURRENT_USER WITH INHERIT TRUE, SET TRUE', :'role')
WHERE NOT EXISTS (
  SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.roleid
  WHERE r.rolname = :'role' AND m.member = (SELECT oid FROM pg_roles WHERE rolname = current_user)
    AND m.inherit_option AND m.set_option
) \gexec
SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L TEMPLATE template0', :'role', :'role', 'UTF8')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'role') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'role') \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', :'role', :'role') \gexec
SQL
  unset pw
done

log "hardening default databases (best effort: Cloud SQL owns some of them)"
psql_db postgres <<'SQL'
DO $$
DECLARE d text;
BEGIN
  FOREACH d IN ARRAY ARRAY['postgres', 'template1', 'template_postgis'] LOOP
    IF EXISTS (SELECT 1 FROM pg_database WHERE datname = d) THEN
      BEGIN
        EXECUTE format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', d);
      EXCEPTION WHEN insufficient_privilege THEN
        RAISE NOTICE 'cannot revoke CONNECT on %', d;
      END;
    END IF;
  END LOOP;
END $$;
SQL

if [ "${SKIP_EXTENSIONS:-0}" = "1" ]; then
  log "SKIP_EXTENSIONS=1: extensions not touched"
  log "done"
  exit 0
fi

failed=0
for db in $DB_ROLES; do
  exts="$(extensions_for "$db")"
  [ -n "$exts" ] || continue
  for ext in $exts; do
    want="$(pin_for "$ext")"
    have="$(psql_db "$db" -tA -v ext="$ext" <<'SQL'
select coalesce((select extversion from pg_extension where extname = :'ext'), '')
SQL
)"
    if [ -z "$have" ]; then
      avail="$(psql_db "$db" -tA -v ext="$ext" -v ver="$want" <<'SQL'
select count(*) from pg_available_extension_versions where name = :'ext' and version = :'ver'
SQL
)"
      if [ "$avail" != "1" ]; then
        offered="$(psql_db "$db" -tA -v ext="$ext" <<'SQL'
select coalesce(string_agg(version, ',' order by version), 'none') from pg_available_extension_versions where name = :'ext'
SQL
)"
        printf '{"level":"error","component":"keelson-db-init","msg":"extension version not offered","db":"%s","extension":"%s","pinned":"%s","offered":"%s"}\n' \
          "$db" "$ext" "$want" "$offered" >&2
        failed=1
        continue
      fi
      log "creating extension ${ext} ${want} in ${db}"
      psql_db "$db" -v ext="$ext" -v ver="$want" <<'SQL'
SELECT format('CREATE EXTENSION IF NOT EXISTS %I VERSION %L', :'ext', :'ver') \gexec
SQL
      have="$want"
    fi
    if [ "$have" != "$want" ]; then
      printf '{"level":"error","component":"keelson-db-init","msg":"extension version drift","db":"%s","extension":"%s","pinned":"%s","installed":"%s"}\n' \
        "$db" "$ext" "$want" "$have" >&2
      failed=1
    fi
  done
  if [ "$db" = keelson ]; then
    # Same as pg-core: nobody but the owner creates objects in public.
    psql_db keelson -c 'REVOKE CREATE ON SCHEMA public FROM PUBLIC' >/dev/null
  fi
done
[ "$failed" -eq 0 ] || die "extension pins not satisfied (see above); update infra/cloudrun/versions.env or the instance"
log "done"
