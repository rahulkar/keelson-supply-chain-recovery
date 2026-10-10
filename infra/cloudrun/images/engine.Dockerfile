# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Cloud Run overlay for the engine image (engine-api, engine-worker pool, bootstrap job).
# compose bind-mounts models.lock, data/packs, infra/tenants.yaml and (M3) data/geo into the
# container; Cloud Run has no bind mounts, so they are baked in here, with the dev origins in
# tenants.yaml / pack.yaml rewritten to the public domain. Build context: REPOSITORY ROOT.
#   docker build -f infra/cloudrun/images/engine.Dockerfile \
#     --build-arg ENGINE_IMAGE=<engine image> --build-arg PUBLIC_BASE_DOMAIN=demo.example.com .
ARG ENGINE_IMAGE=keelson-engine-api:latest
ARG REWRITE_IMAGE=python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

FROM ${REWRITE_IMAGE} AS rewrite
ARG PUBLIC_BASE_DOMAIN
COPY infra/cloudrun/images/rewrite-hosts.sh /usr/local/bin/rewrite-hosts
COPY models.lock /out/models.lock
COPY infra/tenants.yaml /out/tenants.yaml
COPY data/packs /out/packs
COPY data/geo /out/geo
RUN sh /usr/local/bin/rewrite-hosts "${PUBLIC_BASE_DOMAIN}" /out/tenants.yaml /out/packs

FROM ${ENGINE_IMAGE}
# root-owned, world-readable: the app user (10001) only reads them.
COPY --from=rewrite /out/models.lock /app/models.lock
COPY --from=rewrite /out/tenants.yaml /app/tenants.yaml
COPY --from=rewrite /out/packs /app/packs
COPY --from=rewrite /out/geo /app/geo
ENV MODELS_LOCK=/app/models.lock TENANTS_FILE=/app/tenants.yaml PACKS_DIR=/app/packs GEO_DIR=/app/geo
USER 10001
