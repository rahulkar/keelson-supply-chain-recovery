#!/usr/bin/env bash
# All Cloud Run artifact tests (no GCP project needed). Each part runs in a throwaway container
# and cleans up after itself. Exit status = number of failed parts.
#   1. shellcheck (koalaman/shellcheck:stable) on every script
#   2. pytest scripts/cloudrun/tests (python:3.14-slim-trixie + pyyaml): manifests, deploy.sh
#      --dry-run / --render-only (fake gcloud/docker on PATH), secret wiring, overlay rewrites
#   3. scripts/cloudrun/tests/dbinit.sh: db-init job image against throwaway Postgres 18
#   4. scripts/cloudrun/parity-check.sh --selftest: the parity checker's own verdicts
# Usage: scripts/cloudrun/tests/run.sh [--skip-docker-tests]   (the latter runs 1+2 only)
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
if command -v cygpath >/dev/null 2>&1; then ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1; fi
failed=0
part() { echo "== $1" >&2; }

part shellcheck
docker run --rm --memory 128m -v "$ROOT:/mnt:ro" -w /mnt koalaman/shellcheck:stable -x \
  scripts/cloudrun/deploy.sh scripts/cloudrun/parity-check.sh scripts/cloudrun/tests/run.sh \
  scripts/cloudrun/tests/dbinit.sh scripts/cloudrun/tests/overlays.sh infra/cloudrun/images/rewrite-hosts.sh \
  infra/cloudrun/images/db-init/db-init.sh || failed=$((failed + 1))

part pytest
docker run --rm --memory 384m -v "$ROOT:/work:ro" -v keelson_pipcache:/root/.cache/pip -w /work \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_ROOT_USER_ACTION=ignore -e PYTHONDONTWRITEBYTECODE=1 \
  python:3.14-slim-trixie sh -c 'pip install -q "pyyaml==6.*" "pytest>=8" && \
    python -m pytest -q -p no:cacheprovider scripts/cloudrun/tests' || failed=$((failed + 1))

if [ "${1:-}" != --skip-docker-tests ]; then
  part dbinit
  bash "$ROOT/scripts/cloudrun/tests/dbinit.sh" || failed=$((failed + 1))
  part parity-selftest
  bash "$ROOT/scripts/cloudrun/parity-check.sh" --selftest || failed=$((failed + 1))
fi
echo "cloudrun tests: $failed failed part(s)" >&2
exit "$failed"
