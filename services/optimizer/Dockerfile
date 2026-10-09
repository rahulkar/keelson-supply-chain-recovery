# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Keelson optimizer. Build context = repository root (needs packages/contracts-py):
#   docker build -f services/optimizer/Dockerfile -t keelson-optimizer .
# Test image (dev deps + tests, same base):
#   docker build -f services/optimizer/Dockerfile --target test -t keelson-optimizer:test .
#   docker run --rm --memory 512m --cpus 1.5 keelson-optimizer:test
# Only manylinux wheels are used for third-party packages (no compiler in the image;
# see [tool.uv] no-build-package in pyproject.toml).

ARG PYTHON_IMAGE=python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170

# ───────────── base: python + uv + locked third-party deps (cached layer) ─────────────
FROM ${PYTHON_IMAGE} AS base
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /bin/uv
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
