# syntax=docker/dockerfile:1.7
# Keelson optimizer. Build context = repository root (needs packages/contracts-py):
#   docker build -f services/optimizer/Dockerfile -t keelson-optimizer .
# Test image (dev deps + tests, same base):
#   docker build -f services/optimizer/Dockerfile --target test -t keelson-optimizer:test .
#   docker run --rm --memory 512m --cpus 1.5 keelson-optimizer:test
# Only manylinux wheels are used for third-party packages (no compiler in the image;
# see [tool.uv] no-build-package in pyproject.toml).

ARG PYTHON_IMAGE=python:3.14-slim-trixie

# ───────────── base: python + uv + locked third-party deps (cached layer) ─────────────
FROM ${PYTHON_IMAGE} AS base
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv
WORKDIR /src/services/optimizer
COPY packages/contracts-py /src/packages/contracts-py
COPY services/optimizer/pyproject.toml services/optimizer/uv.lock services/optimizer/README.md ./

# ───────────── builder: runtime venv (no dev deps, nothing editable) ─────────────
FROM base AS builder
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project
COPY services/optimizer/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ───────────── test: dev deps + tests (run: docker run --rm <image>) ─────────────
FROM base AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable --no-install-project
COPY services/optimizer/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable
COPY services/optimizer/tests ./tests
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
CMD ["pytest", "-q", "-p", "no:cacheprovider"]

# ───────────── runtime (default target) ─────────────
FROM ${PYTHON_IMAGE} AS runtime
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8200
USER app
EXPOSE $PORT
HEALTHCHECK --interval=10s --timeout=3s --retries=20 \
  CMD ["python", "-c", "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8200\")}/healthz',timeout=3)"]
CMD ["keelson-optimizer"]
