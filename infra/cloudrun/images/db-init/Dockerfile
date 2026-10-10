# keelson-db-init: Cloud Run Job image that creates roles, databases and pinned extensions on
# Cloud SQL (see db-init.sh). Only the psql client of the official image is used.
#   docker build -t keelson-db-init infra/cloudrun/images/db-init
ARG POSTGRES_CLIENT_IMAGE=postgres:18.6-trixie@sha256:74935e72241653ca55e0414067e6d8763aceb8a810eb51b452253ec3dcfc4336
FROM ${POSTGRES_CLIENT_IMAGE}
COPY --chmod=0755 db-init.sh /usr/local/bin/keelson-db-init
# uid 999 = "postgres" in the official image; the job never needs root.
USER 999
ENTRYPOINT ["/usr/local/bin/keelson-db-init"]
