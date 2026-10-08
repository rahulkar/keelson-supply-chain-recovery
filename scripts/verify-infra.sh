#!/usr/bin/env bash
# Smoke-verify the running infra services from inside the compose networks.
# Usage: scripts/verify-infra.sh [--skip-llm]
#   --skip-llm  skip the LiteLLM temporary-model call (no Gemini request is made)
# Requires: infra services up (scripts/up.ps1 or docker compose up -d <infra services>).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1; fi
IMAGE="python:3.14-slim-trixie"
NAME="keelson-verify-$$"
rc=0

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

# Main checks need app (auth.localhost, keycloak, egress) + ai (litellm).
docker create --name "$NAME" --memory 128m --network keelson_app --env-file "$ROOT/.env" \
  -v "$ROOT/scripts:/scripts:ro" "$IMAGE" python /scripts/verify_infra.py all "$@" >/dev/null
docker network connect keelson_ai "$NAME"
docker start -a "$NAME" || rc=$?
cleanup

# Name resolution per network (S7): public hostnames must resolve to Caddy.
for net in app edge; do
  docker run --rm --memory 64m --network "keelson_$net" -e VERIFY_NETWORK="$net" \
    -v "$ROOT/scripts:/scripts:ro" "$IMAGE" python /scripts/verify_infra.py dns || rc=$((rc + 1))
done
exit "$rc"
