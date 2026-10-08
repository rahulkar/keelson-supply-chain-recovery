#!/usr/bin/env bash
# First-boot initialisation of pg-core (runs once, on an empty data directory).
#
# Creates one login role + one database per consumer, each DB owned by its own role:
#   keelson  (engine domain data; PostGIS, pgvector, btree_gist, pgcrypto)
#   adk      (Google ADK session store)
#   keycloak (Keycloak)
# CONNECT is revoked from PUBLIC on every database, so roles can only reach their own.
# Passwords come from the environment and are passed as psql variables (never
# interpolated into SQL text by the shell, never echoed).
set -Eeuo pipefail

log() { printf '{"level":"info","component":"pg-core-init","msg":"%s"}\n' "$*"; }

: "${KEELSON_DB_PASSWORD:?KEELSON_DB_PASSWORD is required}"
: "${ADK_DB_PASSWORD:?ADK_DB_PASSWORD is required}"
: "${KEYCLOAK_DB_PASSWORD:?KEYCLOAK_DB_PASSWORD is required}"

psql_su() {
  psql -v ON_ERROR_STOP=1 --no-psqlrc --username "${POSTGRES_USER:-postgres}" "$@"
}

create_role_and_db() {
  local name="$1" password="$2"
  log "creating role and database ${name}"
  psql_su --dbname postgres -v role="${name}" -v pw="${password}" <<'SQL'
SELECT format('CREATE ROLE %I LOGIN PASSWORD %L NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS',
              :'role', :'pw')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'role') \gexec
SELECT format('ALTER ROLE %I PASSWORD %L', :'role', :'pw') \gexec
SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L TEMPLATE template0', :'role', :'role', 'UTF8')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'role') \gexec
SELECT format('REVOKE ALL ON DATABASE %I FROM PUBLIC', :'role') \gexec
SELECT format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO %I', :'role', :'role') \gexec
SQL
}

create_role_and_db keelson  "${KEELSON_DB_PASSWORD}"
create_role_and_db adk      "${ADK_DB_PASSWORD}"
create_role_and_db keycloak "${KEYCLOAK_DB_PASSWORD}"

log "hardening default databases"
psql_su --dbname postgres <<'SQL'
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_database WHERE datname = 'template_postgis') THEN
    EXECUTE 'REVOKE CONNECT ON DATABASE template_postgis FROM PUBLIC';
  END IF;
END $$;
SQL

# Extensions need superuser (PostGIS is untrusted); the schema stays owned by the DB
# owner (PG15+: public schema owner = pg_database_owner).
log "enabling extensions in keelson"
psql_su --dbname keelson <<'SQL'
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
SQL

log "pg-core initialised"
