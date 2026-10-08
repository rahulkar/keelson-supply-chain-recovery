# syntax=docker/dockerfile:1.7
# Keelson Signal Hub (docs/contracts/100-m6-signals.md). One image, three roles:
#   signal-hub-api        (default CMD: uvicorn on :8300)
#   signal-hub-poller     python -m signal_hub.poller [--once --adapter gdacs|usgs|rss|extraction|sweep]
#   signal-hub-bootstrap  python -m signal_hub.bootstrap
# Build context = repository root (needs packages/contracts-py, data/geo, data/scenarios):
#   docker build -f services/signal-hub/Dockerfile -t keelson-signal-hub .
# Test image (dev deps + tests; PostGIS tests need SIGNAL_HUB_TEST_ADMIN_DSN, else skipped):
#   docker build -f services/signal-hub/Dockerfile --target test -t keelson-signal-hub:test .
#   docker run --rm keelson-signal-hub:test

ARG PYTHON_IMAGE=python:3.14-slim-trixie

# ───────────── base: python + uv + locked third-party deps (cached layer) ─────────────
FROM ${PYTHON_IMAGE} AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src/services/signal-hub
COPY packages/contracts-py /src/packages/contracts-py
COPY services/signal-hub/pyproject.toml services/signal-hub/uv.lock services/signal-hub/README.md ./

# ───────────── builder: runtime venv (no dev deps, nothing editable) ─────────────
FROM base AS builder
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project
COPY services/signal-hub/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ───────────── test: dev deps + tests (run: docker run --rm <image>) ─────────────
FROM base AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --no-install-project
COPY services/signal-hub/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable
COPY services/signal-hub/migrations /app/migrations
COPY data/geo /app/geo
COPY data/scenarios /app/scenarios
COPY services/signal-hub/tests ./tests
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 \
    GEO_DIR=/app/geo SCENARIOS_DIR=/app/scenarios
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# ───────────── runtime (default target) ─────────────
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
COPY services/signal-hub/migrations /app/migrations
COPY data/geo /app/geo
COPY data/scenarios /app/scenarios
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8300 \
    GEO_DIR=/app/geo \
    SCENARIOS_DIR=/app/scenarios
WORKDIR /app
USER app
EXPOSE 8300
# No image HEALTHCHECK: the poller/bootstrap roles serve no HTTP; compose probes the API's /healthz.
CMD ["signal-hub-api"]
