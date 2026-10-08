-- Native Postgres initialisation (startup.bat -> scripts/native/keelson-native.ps1), run as the
-- superuser on every start (idempotent: passwords follow .env).
--
-- Mirrors infra/postgres/core/init/10-keelson-init.sh (keelson, adk, keycloak + extensions) and
-- adds the litellm and merxa databases that compose keeps in their own clusters (pg-litellm,
-- pg-merxa). One login role + one database per consumer, each database owned by its role, CONNECT
-- revoked from PUBLIC. Passwords are read from the environment (\getenv), never from the command
-- line or this file.
\set ON_ERROR_STOP on

\getenv pw KEELSON_DB_PASSWORD
\set role keelson
\ir pg-role-db.sql

\getenv pw ADK_DB_PASSWORD
\set role adk
\ir pg-role-db.sql

\getenv pw KEYCLOAK_DB_PASSWORD
\set role keycloak
\ir pg-role-db.sql

\getenv pw LITELLM_DB_PASSWORD
\set role litellm
\ir pg-role-db.sql

\getenv pw MERXA_DB_PASSWORD
\set role merxa
\ir pg-role-db.sql

REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
REVOKE CONNECT ON DATABASE template1 FROM PUBLIC;

-- Extensions need superuser (PostGIS is untrusted); the schema stays owned by the DB owner.
\connect keelson
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS btree_gist;
CREATE EXTENSION IF NOT EXISTS pgcrypto;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

-- LiteLLM's Prisma migrations create tables in public: its role owns the database, and PG15+
-- gives the public schema to pg_database_owner, so nothing else is needed for litellm / merxa.
