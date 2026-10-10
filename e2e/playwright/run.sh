#!/usr/bin/env bash
# Browser e2e of the M1 thin slice against the running compose stack.
#
#   e2e/playwright/run.sh                       # whole suite
#   e2e/playwright/run.sh act3-thin-slice       # golden journey only (any `playwright test` args)
#   E2E_SHELL=1 e2e/playwright/run.sh           # shell in the container instead
#
# Runs mcr.microsoft.com/playwright:v1.63.0-noble on the keelson_edge network (so *.localhost
# resolves to Caddy), with node_modules and the npm cache in named volumes. Artifacts (traces,
# screenshots, HTML report, results.json) land in e2e/playwright/artifacts/ (gitignored).
# DEMO_USER_PASSWORD comes from the environment or the repo's .env and is never printed.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
# Docker on Windows (Git Bash) wants D:/primark/..., elsewhere the plain path.
mount_dir="$(cd "$here" && (pwd -W 2>/dev/null || pwd))"

if [[ -z "${DEMO_USER_PASSWORD:-}" ]]; then
  DEMO_USER_PASSWORD="$(grep -E '^DEMO_USER_PASSWORD=' "$root/.env" | cut -d= -f2- || true)"
fi
if [[ -z "${DEMO_USER_PASSWORD:-}" ]]; then
  echo "DEMO_USER_PASSWORD is not set and not found in $root/.env" >&2
  exit 2
fi
export DEMO_USER_PASSWORD

image="mcr.microsoft.com/playwright:v1.63.0-noble@sha256:eff16c30e6f3f4af0a03fa4b706120d5e9b0891c344a27d64559aff5900a4a27"
inner='set -e
EDGE_IP="$(getent ahostsv4 keelson.localhost | awk "NR==1{print \$1}")"
[ -n "$EDGE_IP" ] || { echo "keelson.localhost does not resolve: is the stack up (caddy on keelson_edge)?" >&2; exit 2; }
export EDGE_IP
if [ ! -f node_modules/.e2e-installed ] || ! cmp -s package-lock.json node_modules/.e2e-installed; then
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
  cp package-lock.json node_modules/.e2e-installed
fi
mkdir -p artifacts/screens artifacts/traces
if [ -n "${E2E_SHELL:-}" ]; then exec bash; fi
npx tsc --noEmit
exec npx playwright test "$@"'

tty_flag=()
[[ -t 0 && -t 1 ]] && tty_flag=(-it)

MSYS_NO_PATHCONV=1 docker run --rm "${tty_flag[@]}" --init --ipc=host --memory 2g \
  --network keelson_edge \
  -e DEMO_USER_PASSWORD -e E2E_SHELL -e E2E_RUN_TIMEOUT_MS -e E2E_M6_WORKER_HELD -e E2E_NO_MODEL -e CI \
  -v "$mount_dir:/e2e" \
  -v keelson_e2e_pw_nm:/e2e/node_modules \
  -v keelson_npm_cache:/root/.npm \
  -w /e2e "$image" bash -c "$inner" bash "$@"
