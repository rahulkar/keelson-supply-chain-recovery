# Cloud SQL for Keelson (POSTGRES_18)

Counterpart of the local `pg-core`, `pg-litellm` and `pg-merxa` containers. Created by
`scripts/cloudrun/deploy.sh`; this page explains the choices.

## Layout

| Mode | Instances | Databases (one LOGIN role each, owner of its database) |
|---|---|---|
| default (smoke deploy) | `keelson-pg` | `keelson`, `adk`, `keycloak`, `litellm`, `merxa` (+ `phoenix` with `--enable-phoenix`) |
| `--sql-isolation` (production) | `keelson-pg` | `keelson`, `adk`, `keycloak` (+ `phoenix`) |
| | `keelson-pg-litellm` | `litellm` (isolated, as in compose: LiteLLM's CVE history) |
| | `merxa-pg` | `merxa` (the third party's own data) |

In the default mode the databases are isolated by role: `CONNECT` is revoked from `PUBLIC` on
every database, so e.g. the `litellm` role cannot open `keelson` (verified by
`scripts/cloudrun/tests/dbinit.sh`).

Instance settings: `--edition=ENTERPRISE --tier=$SQL_TIER` (default `db-custom-1-3840`),
**private IP only** (`--no-assign-ip`, Private Services Access range `keelson-psa` on `keelson-vpc`),
`--ssl-mode=ENCRYPTED_ONLY` (clients use `ssl=require` / `sslmode=require`), zonal, automatic
storage increase, daily backup at 03:00, deletion protection on.

## Users and the db-init job

Cloud SQL's `postgres` user is **not** a superuser (`cloudsqlsuperuser`: CREATEROLE, CREATEDB,
extension creation). The job `keelson-db-init-<instance>` (image `images/db-init`, script
`images/db-init/db-init.sh`) runs as `postgres` on every deploy and:

1. creates/updates each role with its password from Secret Manager (rotation = new secret
   version, re-run the job, redeploy the consumer);
2. grants itself `SET` + `INHERIT` membership in each role (PG16+: required to create a database
   owned by another role and to act as its owner);
3. creates each database `OWNER <role>`, `REVOKE ALL ... FROM PUBLIC`, `GRANT CONNECT, TEMPORARY`
   to the owner;
4. creates the extensions with **pinned versions** and verifies them.

## Extension pins

`versions.env` pins Cloud SQL's versions for POSTGRES_18 (source:
<https://docs.cloud.google.com/sql/docs/postgres/extensions>, page updated 2026-09-30):

| Extension | Database | Cloud SQL (pinned) | local pg-core today |
|---|---|---|---|
| `postgis` | keelson | 3.6.0 | 3.6.4 |
| `vector` (pgvector) | keelson | 0.8.5 | 0.8.7 |
| `btree_gist` | keelson, merxa | 1.8 | 1.8 |
| `pgcrypto` | keelson | 1.3 | 1.4 |

The job runs `CREATE EXTENSION ... VERSION '<pin>'` only if that version is offered
(`pg_available_extension_versions`) and fails on drift (installed version differs from the pin) or
when the pin is not offered, printing what the server offers. When Cloud SQL updates its versions:
re-check the page, update `versions.env`, and align `infra/postgres/core` (local dev must not use
features the cloud does not have).
