"""Constants shared by the platform tests and their fixtures."""

from __future__ import annotations

from pathlib import Path

KEELSON_ISS = "http://auth.localhost/realms/keelson"
MERXA_ISS = "http://auth.localhost/realms/merxa"
MERXA_MCP = "http://merxa.localhost/mcp"
PUBLIC = "http://keelson.localhost"
LITELLM = "http://litellm:4000"
MASTER_KEY = "sk-test-master"
TENANT_A = "demo-retail"
TENANT_B = "northwind-test"

REPO_PACKS = Path(__file__).resolve().parents[5] / "data" / "packs"
HAVE_PACKS = (REPO_PACKS / "blank" / "pack.yaml").is_file()
"""The ``--target test`` image copies ``data/packs`` to ``/src/data/packs`` (repo layout); skip only if absent."""
