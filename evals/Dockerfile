# syntax=docker/dockerfile:1.7.1@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e
# Keelson in-app eval runner (M8, docs/contracts/128-m8-evals-ui.md §4): compose service
# `evals-runner` (profile `evals`). Build context: REPO ROOT
#   docker build -f evals/Dockerfile -t keelson/evals-runner .
# Black box over HTTP, like the CLI: the eval library and cases, the e2e HTTP helpers, and the
# adversarial knowledge-base fixture (data/kb/adversarial never ships in the engine image).
# Runtime: non-root uid 10001; compose runs it with a read-only root filesystem and no egress.

FROM python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170
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
