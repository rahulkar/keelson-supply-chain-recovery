# syntax=docker/dockerfile:1.7
# Keelson adapter sidecar (docs/contracts/122-m8-adapter-sidecar.md): tier B/C/W MCP tools in front
# of a CSV directory, a SQL database or a read-only REST API. One process (uvicorn, 1 worker) per
# data source. The Fernhill demo instance ships in /app/demo/fernhill (SIDECAR_CONFIG points at it).
# Build context = repository root (needs packages/contracts-py):
#   docker build -f services/adapter-sidecar/Dockerfile -t keelson-adapter-sidecar .
# Test image (dev deps + tests; Postgres tests need SIDECAR_TEST_PG_DSN, else skipped):
#   docker build -f services/adapter-sidecar/Dockerfile --target test -t keelson-adapter-sidecar:test .
#   docker run --rm --memory 1g keelson-adapter-sidecar:test

ARG PYTHON_IMAGE=python:3.14-slim-trixie

# ───────────── base: python + uv + locked third-party deps (cached layer) ─────────────
FROM ${PYTHON_IMAGE} AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src/services/adapter-sidecar
COPY packages/contracts-py /src/packages/contracts-py
COPY services/adapter-sidecar/pyproject.toml services/adapter-sidecar/uv.lock services/adapter-sidecar/README.md ./

# ───────────── builder: runtime venv (no dev deps, nothing editable) ─────────────
FROM base AS builder
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project
COPY services/adapter-sidecar/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ───────────── test: dev deps + tests (run: docker run --rm <image>) ─────────────
FROM base AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --no-install-project
COPY services/adapter-sidecar/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable
COPY services/adapter-sidecar/demo ./demo
COPY services/adapter-sidecar/examples ./examples
COPY services/adapter-sidecar/tests ./tests
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# ───────────── runtime (default target) ─────────────
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app \
 && mkdir -p /data/state /data/snapshots /data/fernhill \
 && chown -R app:app /data
COPY --from=builder /opt/venv /opt/venv
COPY services/adapter-sidecar/demo /app/demo
COPY services/adapter-sidecar/examples /app/examples
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8400 \
    SIDECAR_CONFIG=/app/demo/fernhill/sidecar.yaml \
    STATE_DB_URL=sqlite+aiosqlite:////data/state/sidecar.db \
    SNAPSHOT_DIR=/data/snapshots \
    DEMO_GOLDEN_DIR=/app/demo/fernhill/golden
WORKDIR /app
USER app
EXPOSE 8400
HEALTHCHECK --interval=10s --timeout=3s --retries=30 --start-period=10s \
  CMD ["python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8400/healthz',timeout=3)"]
CMD ["adapter-sidecar", "serve"]
