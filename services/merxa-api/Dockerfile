# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# merxa-api — Merxa Retail Suite backend. Build context: services/merxa-api
#   docker build -t keelson/merxa-api services/merxa-api
#   docker build --target test -t keelson/merxa-api:test services/merxa-api   (pytest image)

FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project
COPY README.md ./
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ── test image: runtime deps + dev group + tests (needs MERXA_TEST_ADMIN_DSN) ──
FROM builder AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable
COPY tests ./tests
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]

# ── runtime ──
FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime
RUN groupadd --gid 10001 app && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8100
WORKDIR /app
# scn/1.0 Parquet snapshots (compose mounts the named volume merxa_snapshots here; a fresh
# named volume inherits this ownership).
RUN mkdir -p /data/snapshots && chown app:app /data/snapshots
ENV SNAPSHOT_DIR=/data/snapshots
USER app
EXPOSE $PORT
CMD ["python", "-m", "merxa_api"]
