#!/usr/bin/env bash
# Supply-chain tooling tests (docs/supply-chain.md). Each part runs in a throwaway container; nothing
# needs the network except pulling the pinned tool images once. Exit status = number of failed parts.
#   1. check_pins.py on the repository (pins, caps, consistency, images.json coverage)
#   2. shellcheck (v0.11.0, severity warning) on every *.sh of the repository
#   3. pytest scripts/supply-chain/tests + scripts/cloudrun/tests + scripts/tests/test_init_secrets.py
#      (python 3.14, pyyaml, pytest, cryptography; fake cosign/gcloud/docker on PATH)
#   4. actionlint (1.7.12) and zizmor (1.30.1, offline) on .github/
#   5. --with-renovate: renovate-config-validator --strict + the Renovate extraction test (~1.3 GB image)
#   6. --with-docker: scripts/supply-chain/tests/e2e_sign_verify.sh (local registry + real cosign)
# Usage: scripts/supply-chain/tests/run.sh [--with-renovate] [--with-docker]
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
HOST_ROOT="$ROOT"
if command -v cygpath >/dev/null 2>&1; then HOST_ROOT="$(cygpath -m "$ROOT")"; export MSYS_NO_PATHCONV=1; fi
PY_IMAGE="python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
SHELLCHECK_IMAGE="koalaman/shellcheck:v0.11.0@sha256:61862eba1fcf09a484ebcc6feea46f1782532571a34ed51fedf90dd25f925a8d"
ACTIONLINT_IMAGE="rhysd/actionlint:1.7.12@sha256:b1934ee5f1c509618f2508e6eb47ee0d3520686341fec936f3b79331f9315667"
ZIZMOR_IMAGE="ghcr.io/zizmorcore/zizmor:1.30.1@sha256:a2eb396d886c053073405c7a980f2139ba2248ec172243cfa3841e57196e8101"
RENOVATE_IMAGE="ghcr.io/renovatebot/renovate:44.145.1@sha256:6f1f3e2d9d0c3f99aa1f61f7509d302185de7deb173ac8c5ff88a5ce283ec435"
WITH_RENOVATE=0; WITH_DOCKER=0
for a in "$@"; do
  case "$a" in
    --with-renovate) WITH_RENOVATE=1 ;;
    --with-docker) WITH_DOCKER=1 ;;
    *) echo "run.sh: unknown argument $a" >&2; exit 2 ;;
  esac
done
failed=0
part() { echo "== $1" >&2; }
py() { docker run --rm --memory 512m -v "$HOST_ROOT:/work:ro" -v keelson_pipcache:/root/.cache/pip -w /work \
  -e PIP_DISABLE_PIP_VERSION_CHECK=1 -e PIP_ROOT_USER_ACTION=ignore -e PYTHONDONTWRITEBYTECODE=1 "$PY_IMAGE" sh -c "$1"; }

part check_pins
py "python scripts/supply-chain/check_pins.py" || failed=$((failed + 1))

part shellcheck
mapfile -t scripts < <(cd "$ROOT" && find scripts infra e2e -name '*.sh' -not -path '*/node_modules/*' | sort)
docker run --rm --memory 768m -v "$HOST_ROOT:/mnt:ro" -w /mnt "$SHELLCHECK_IMAGE" -S warning -x "${scripts[@]}" || failed=$((failed + 1))

part pytest
py "pip install -q pyyaml==6.0.3 pytest==9.1.1 cryptography==50.0.2 && python -m pytest -q -p no:cacheprovider \
  scripts/supply-chain/tests scripts/cloudrun/tests scripts/tests/test_init_secrets.py" || failed=$((failed + 1))

part actionlint+zizmor
docker run --rm --memory 256m -v "$HOST_ROOT:/repo:ro" -w /repo "$ACTIONLINT_IMAGE" || failed=$((failed + 1))
docker run --rm --memory 256m -v "$HOST_ROOT:/repo:ro" -w /repo "$ZIZMOR_IMAGE" --no-online-audits --no-progress -q .github ||
  failed=$((failed + 1))

if [ "$WITH_RENOVATE" = 1 ]; then
  part renovate
  docker run --rm --memory 1g -v "$HOST_ROOT:/usr/src/app:ro" -w /usr/src/app "$RENOVATE_IMAGE" \
    renovate-config-validator --strict || failed=$((failed + 1))
  bash "$ROOT/scripts/supply-chain/tests/renovate_extract.sh" || failed=$((failed + 1))
fi
if [ "$WITH_DOCKER" = 1 ]; then
  part e2e_sign_verify
  bash "$ROOT/scripts/supply-chain/tests/e2e_sign_verify.sh" || failed=$((failed + 1))
fi
echo "supply-chain tests: $failed failed part(s)" >&2
exit "$failed"
