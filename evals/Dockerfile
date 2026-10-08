# syntax=docker/dockerfile:1.7
# Keelson in-app eval runner (M8, docs/contracts/128-m8-evals-ui.md §4): compose service
# `evals-runner` (profile `evals`). Build context: REPO ROOT
#   docker build -f evals/Dockerfile -t keelson/evals-runner .
# Black box over HTTP, like the CLI: the eval library and cases, the e2e HTTP helpers, and the
# adversarial knowledge-base fixture (data/kb/adversarial never ships in the engine image).
# Runtime: non-root uid 10001; compose runs it with a read-only root filesystem and no egress.

FROM python:3.14-slim-trixie
RUN groupadd --gid 10001 app \
 && useradd --uid 10001 --gid app --no-create-home --shell /usr/sbin/nologin app
COPY evals/requirements-runtime.txt /tmp/requirements-runtime.txt
RUN --mount=type=cache,target=/root/.cache/pip \
    pip install --no-compile --root-user-action=ignore -r /tmp/requirements-runtime.txt
COPY evals/keelson_evals /app/evals/keelson_evals
COPY evals/cases /app/evals/cases
COPY evals/run_live.py evals/runner_service.py /app/evals/
COPY e2e/api_thin_slice.py /app/e2e/api_thin_slice.py
COPY data/kb/adversarial /app/data/kb/adversarial
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KEELSON_E2E_DIR=/app/e2e
WORKDIR /app/evals
USER app
CMD ["python", "-u", "runner_service.py", "--repo-root", "/app"]
