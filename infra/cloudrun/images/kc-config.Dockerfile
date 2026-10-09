# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Cloud Run overlay for keycloak-config-cli (Cloud Run Job "keelson-kc-config"): the realm files
# from infra/keycloak/config baked in (compose bind-mounts them), dev origins rewritten to the
# public domain. Secrets stay $(env:...) references resolved at run time from Secret Manager.
# Build context: REPOSITORY ROOT.
#   docker build -f infra/cloudrun/images/kc-config.Dockerfile --build-arg PUBLIC_BASE_DOMAIN=demo.example.com .
ARG KC_CONFIG_IMAGE=adorsys/keycloak-config-cli:6.5.1-26@sha256:1b22dfaa9ae0c71f74b0342f9221a6510f272da5def683dbba26a98e6b1b1411
ARG REWRITE_IMAGE=python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

FROM ${REWRITE_IMAGE} AS rewrite
ARG PUBLIC_BASE_DOMAIN
COPY infra/cloudrun/images/rewrite-hosts.sh /usr/local/bin/rewrite-hosts
COPY infra/keycloak/config /out/config
RUN sh /usr/local/bin/rewrite-hosts "${PUBLIC_BASE_DOMAIN}" /out/config
# The dev realms allow plain HTTP (sslRequired: none, Caddy serves http://*.localhost). On Cloud
# Run every request reaches Keycloak over TLS (the ALB, or run.app) with X-Forwarded-Proto: https,
# so require it for external clients, as Keycloak's own production default does.
RUN find /out/config -type f \( -name '*.yaml' -o -name '*.yml' \) -exec \
      sed -E -i 's/^([[:space:]]*sslRequired:[[:space:]]*)["'\'']?none["'\'']?[[:space:]]*$/\1external/' {} + \
 && ! grep -rnE '^[[:space:]]*sslRequired:[[:space:]]*["'\'']?none' /out/config

FROM ${KC_CONFIG_IMAGE}
COPY --from=rewrite /out/config /config
ENV IMPORT_FILES_LOCATIONS=/config/*.yaml
