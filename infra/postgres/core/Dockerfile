# pg-core: Postgres 18 + PostGIS 3.6 + pgvector (PGDG packages).
# Databases/roles are created on first init by init/10-keelson-init.sh
# (env: POSTGRES_PASSWORD, KEELSON_DB_PASSWORD, ADK_DB_PASSWORD, KEYCLOAK_DB_PASSWORD).
FROM postgis/postgis:18-3.6

# The base image already ships the PGDG apt repo (trixie-pgdg, component 18).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends "postgresql-${PG_MAJOR}-pgvector"; \
    rm -rf /var/lib/apt/lists/*

# Runs after the base image's 10_postgis.sh (lexical order of /docker-entrypoint-initdb.d).
COPY --chmod=0755 init/10-keelson-init.sh /docker-entrypoint-initdb.d/20-keelson-init.sh

# Probe over TCP: during first-boot init the entrypoint runs a temporary server that
# listens on the unix socket only, so a socket probe reports "ready" before the init
# scripts (roles, databases, extensions) have finished.
HEALTHCHECK --interval=5s --timeout=3s --retries=30 \
  CMD pg_isready -h 127.0.0.1 -U postgres -d keelson || exit 1
