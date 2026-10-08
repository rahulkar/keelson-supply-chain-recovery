# syntax=docker/dockerfile:1.7
# Keelson engine (engine-api, engine-worker, bootstrap). Build context: REPO ROOT
#   docker build -f services/engine/Dockerfile -t keelson/engine .
#   docker build -f services/engine/Dockerfile --target test -t keelson/engine:test .   (pytest image)
# Runtime: non-root uid 10001, venv in /opt/venv, SQL migrations in /app/migrations.

FROM python:3.14-slim-trixie AS builder
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
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
FROM python:3.14-slim-trixie AS runtime
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
