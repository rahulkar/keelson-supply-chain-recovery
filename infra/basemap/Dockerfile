# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# basemap-fetch: one-shot tool that fills the `basemap` volume with a self-hosted Protomaps base map
# (archive + glyphs + sprites + licences + manifest.json). docs/contracts/127-m8-basemap.md.
#
# Build context: the repository root (compose.yaml, profile `basemap`). `Dockerfile.dockerignore`
# next to this file limits it to scripts/basemap and the shared manifest fixture.
#
#   docker compose --profile basemap run --rm basemap-fetch [--profile lite|standard|detail] [--dry-run|--check]
#   docker build -f infra/basemap/Dockerfile --target test .     # node --test scripts/basemap/tests
#
# go-pmtiles (BSD-3-Clause, static binary) comes from the upstream image pinned by digest; the
# script runs on Node 24 (no npm dependencies) as uid 10001.

ARG PMTILES_IMAGE=protomaps/go-pmtiles:v1.31.2@sha256:06574f01f55a78f78f887bc7ebf729a5c093c0d6e17d9876300cfcb0758b59d3
ARG NODE_IMAGE=node:24.21.0-slim@sha256:d6aa754f16b3197301076f047b5def2f02ea1dbbc2ca920407d46d7ec7f87b20

FROM ${PMTILES_IMAGE} AS pmtiles

FROM ${NODE_IMAGE} AS base
# go-pmtiles is a static Go binary: it needs CA roots, which node:*-slim does not ship.
COPY --from=pmtiles /go-pmtiles /usr/local/bin/pmtiles
COPY --from=pmtiles /etc/ssl/certs/ca-certificates.crt /etc/ssl/certs/ca-certificates.crt
ENV SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt PMTILES_BIN=/usr/local/bin/pmtiles \
    BASEMAP_DIR=/srv/basemap NODE_ENV=production
COPY scripts/basemap/ /opt/basemap/
# An empty named volume mounted here inherits this ownership (non-root writes into it).
RUN mkdir -p /srv/basemap && chown 10001:10001 /srv/basemap && pmtiles --help >/dev/null

# ── tests (not part of the runtime image): docker build --target test … ──
FROM base AS test
COPY packages/viz/tests/fixtures/basemap-manifest.json /opt/basemap/tests/basemap-manifest.fixture.json
ENV BASEMAP_MANIFEST_FIXTURE=/opt/basemap/tests/basemap-manifest.fixture.json
RUN node --test "/opt/basemap/tests/*.test.mjs"

# ── runtime ──
FROM base AS runtime
USER 10001:10001
WORKDIR /srv/basemap
ENTRYPOINT ["node", "/opt/basemap/fetch-basemap.mjs"]
