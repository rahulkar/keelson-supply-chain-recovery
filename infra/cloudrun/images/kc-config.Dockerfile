# syntax=docker/dockerfile:1.7
# Cloud Run overlay for keycloak-config-cli (Cloud Run Job "keelson-kc-config"): the realm files
# from infra/keycloak/config baked in (compose bind-mounts them), dev origins rewritten to the
# public domain. Secrets stay $(env:...) references resolved at run time from Secret Manager.
# Build context: REPOSITORY ROOT.
#   docker build -f infra/cloudrun/images/kc-config.Dockerfile --build-arg PUBLIC_BASE_DOMAIN=demo.example.com .
ARG KC_CONFIG_IMAGE=adorsys/keycloak-config-cli:6.5.1-26
ARG REWRITE_IMAGE=python:3.14-slim-trixie

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
