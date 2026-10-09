# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Keycloak for Cloud Run: build-time options baked in with `kc.sh build`, started with
# `start --optimized --cache=local` (single instance: the service runs with min = max = 1).
# Hostname, proxy headers, DB URL and credentials are runtime options set by the service YAML.
# Build context: this directory (no files are copied).
#   docker build -f infra/cloudrun/images/keycloak.Dockerfile infra/cloudrun/images
ARG KEYCLOAK_IMAGE=quay.io/keycloak/keycloak:26.8.0@sha256:b0f60d489d51c5d113390bdf5461d4c06e6051be026c05549f2e1e10ec352bcc

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
