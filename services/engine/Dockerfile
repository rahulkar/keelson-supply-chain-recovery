# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Keelson engine (engine-api, engine-worker, bootstrap). Build context: REPO ROOT
#   docker build -f services/engine/Dockerfile -t keelson/engine .
#   docker build -f services/engine/Dockerfile --target test -t keelson/engine:test .   (pytest image)
# Runtime: non-root uid 10001, venv in /opt/venv, SQL migrations in /app/migrations.

FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS builder
COPY --from=ghcr.io/astral-sh/uv:0.12.23@sha256:61d393e44e249f2e4b526b6c7ddcecce245946826e608e11c93ad4f5bba55b21 /uv /bin/uv
ENV UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /src/services/engine
# path dependency (tool.uv.sources: ../../packages/contracts-py)
COPY packages/contracts-py /src/packages/contracts-py
COPY services/engine/pyproject.toml services/engine/uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project --no-editable
COPY services/engine/README.md ./
COPY services/engine/src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ── test image: runtime deps + dev group + tests (needs E1_TEST_ADMIN_DSN / E1_TEST_VALKEY_URL) ──
FROM builder AS test
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-editable
COPY services/engine/migrations ./migrations
COPY services/engine/tests ./tests
COPY infra/tenants.yaml /src/infra/tenants.yaml
COPY models.lock /src/models.lock
# template packs (docs/contracts/70 §4): the pack tests read them from PACKS_DIR
COPY data/packs /src/data/packs
# map geometry (contracts/80 §5): runtime/geo.py finds it at <source tree>/data/geo
COPY data/geo /src/data/geo
# event taxonomy (contracts/90 §1): runtime/nodes/taxonomy.py finds it at <source tree>/data/taxonomy
COPY data/taxonomy /src/data/taxonomy
# knowledge-base seed + adversarial fixture (contracts/90 §7): test_kb checks the repo seed documents
COPY data/kb /src/data/kb
ENV PATH=/opt/venv/bin:$PATH PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PACKS_DIR=/src/data/packs
CMD ["python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/platform"]

# ── runtime (default target) ──
FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170 AS runtime
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY --from=builder /opt/venv /opt/venv
COPY services/engine/migrations /app/migrations
# map geometry for network_map@1 (sea routes + country centroids; runtime/geo.py reads /app/geo)
COPY data/geo /app/geo
# event taxonomy (contracts/90 §1; runtime/nodes/taxonomy.py reads /app/taxonomy, checked at worker start-up)
COPY data/taxonomy /app/taxonomy
# knowledge-base seed (contracts/90 §7; bootstrap ingests /app/kb/<tenant slug>/*.md into an empty tenant KB).
# Only the tenant folders: the adversarial fixture (data/kb/adversarial) is for evals and never ships.
COPY data/kb/demo-retail /app/kb/demo-retail
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8000
WORKDIR /app
USER app
EXPOSE $PORT
CMD ["sh", "-c", "exec uvicorn keelson_engine.api.main:app --host 0.0.0.0 --port \"${PORT}\" --proxy-headers --forwarded-allow-ips '*' --timeout-graceful-shutdown 5"]
