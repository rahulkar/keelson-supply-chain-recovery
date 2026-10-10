"""Shared helpers for the supply-chain tooling tests (scripts/supply-chain/tests/run.sh).

The tools are stdlib scripts in scripts/supply-chain/ (not a package): the directory is put on
sys.path so the tests import them as modules. No test touches the network or Docker.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
TOOLS = ROOT / "scripts" / "supply-chain"
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

D = {k: "sha256:" + k * 64 for k in "abcdef0123456789"}
PY = f"python:3.14.8-slim-trixie@{D['a']}"
UV = f"ghcr.io/astral-sh/uv:0.12.23@{D['b']}"
SYNTAX = f"docker/dockerfile:1.7.1@{D['c']}"
VALKEY = f"valkey/valkey:8.1.10-alpine@{D['d']}"
CHECKOUT = "actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"

GOOD_TREE: dict[str, str] = {
    "services/app/Dockerfile": (
        f"# syntax={SYNTAX}\n"
        f"ARG PYTHON_IMAGE={PY}\n"
        "FROM ${PYTHON_IMAGE} AS base\n"
        f"COPY --from={UV} /uv /bin/uv\n"
        "FROM base AS test\n"
        "RUN --mount=type=cache,target=/root/.cache/uv true\n"
        "FROM ${PYTHON_IMAGE} AS runtime\n"
        "COPY --from=base /bin/uv /bin/uv\n"
    ),
    "compose.yaml": (
        "name: t\n"
        "services:\n"
        "  app:\n"
        "    build:\n"
        "      context: .\n"
        "      dockerfile: services/app/Dockerfile\n"
        "  cache:\n"
        f"    image: {VALKEY}   # comment\n"
    ),
    "infra/cloudrun/versions.env": "# pins\nVALKEY_ENGINE_VERSION=VALKEY_8_0\n",
    "scripts/run.sh": (
        "#!/usr/bin/env bash\n"
        "# docker run python:3.14-slim-trixie is only a comment\n"
        f'IMAGE="{PY}"\n'
        f"docker run --rm {VALKEY} valkey-cli --version\n"
        'docker run --rm "${PREFIX}/selftest:1" true\n'
        "psql postgresql://postgres:test@db:5432/postgres\n"
    ),
    "scripts/run.ps1": (
        "<#\n  Runs inside python:3.14-slim-trixie\n#>\n"
        f"$image = '{PY}'\n"
    ),
    ".github/workflows/ci.yml": (
        "name: ci\n"
        "on: [push]\n"
        "permissions: {}\n"
        "jobs:\n"
        "  a:\n"
        "    runs-on: ubuntu-24.04\n"
        "    steps:\n"
        f"      - uses: {CHECKOUT} # v7.0.1\n"
        "      - uses: ./.github/actions/local\n"
        f"      - run: docker run --rm {PY} python -V\n"
    ),
    "infra/supply-chain/images.json": json.dumps({
        "schema": "keelson.images/1",
        "images": [{"name": "app", "dockerfile": "services/app/Dockerfile", "context": ".", "target": "runtime",
                    "release": False, "suites": [{"id": "app", "target": "test"}]}],
    }, indent=1),
}


FAKE_COSIGN = r"""#!/bin/sh
# Records argv (one call per line); fails when an argument contains $FAKE_COSIGN_FAIL.
echo "$*" >> "$FAKE_COSIGN_LOG"
if [ -n "${FAKE_COSIGN_FAIL:-}" ]; then
  for a; do case "$a" in *"$FAKE_COSIGN_FAIL"*) echo "Error: no matching signatures: fake" >&2; exit 1 ;; esac; done
fi
exit 0
"""


def fake_cosign(d: Path) -> tuple[Path, Path]:
    """A cosign stand-in: returns (binary, log of calls)."""
    d.mkdir(parents=True, exist_ok=True)
    binary, log = d / "cosign", d / "cosign.log"
    binary.write_text(FAKE_COSIGN, encoding="utf-8")
    binary.chmod(0o755)
    return binary, log


def write_tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return root


@pytest.fixture()
def good_tree(tmp_path: Path) -> Path:
    return write_tree(tmp_path / "repo", GOOD_TREE)
