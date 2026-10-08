# syntax=docker/dockerfile:1.7
# Keycloak for Cloud Run: build-time options baked in with `kc.sh build`, started with
# `start --optimized --cache=local` (single instance: the service runs with min = max = 1).
# Hostname, proxy headers, DB URL and credentials are runtime options set by the service YAML.
# Build context: this directory (no files are copied).
#   docker build -f infra/cloudrun/images/keycloak.Dockerfile infra/cloudrun/images
ARG KEYCLOAK_IMAGE=quay.io/keycloak/keycloak:26.8.0

FROM ${KEYCLOAK_IMAGE} AS builder
ENV KC_DB=postgres \
    KC_HEALTH_ENABLED=true \
    KC_CACHE=local
RUN /opt/keycloak/bin/kc.sh build

FROM ${KEYCLOAK_IMAGE}
COPY --from=builder /opt/keycloak/ /opt/keycloak/
ENV KC_DB=postgres \
    KC_HEALTH_ENABLED=true \
    KC_CACHE=local
USER 1000
ENTRYPOINT ["/opt/keycloak/bin/kc.sh"]
CMD ["start", "--optimized", "--cache=local"]
