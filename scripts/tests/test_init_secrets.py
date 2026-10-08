"""Tests for scripts/init_secrets.py (run inside python:3.14-slim-trixie, see docs/infra.md)."""

from __future__ import annotations

import base64
import importlib.util
import os
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "init_secrets", Path(__file__).resolve().parent.parent / "init_secrets.py"
)
assert _SPEC and _SPEC.loader
init_secrets = importlib.util.module_from_spec(_SPEC)
sys.modules["init_secrets"] = init_secrets
_SPEC.loader.exec_module(init_secrets)

CONTRACT_VARS = {
    # docs/contracts/00-overview.md (minus GEMINI_API_KEY)
    "PUBLIC_BASE_DOMAIN", "HTTP_PORT", "DEMO_MODE", "PG_CORE_SUPERPASSWORD", "KEELSON_DB_PASSWORD",
    "ADK_DB_PASSWORD", "KEYCLOAK_DB_PASSWORD", "LITELLM_DB_PASSWORD", "LITELLM_MASTER_KEY",
    "LITELLM_SALT_KEY", "MERXA_DB_PASSWORD", "VALKEY_PASSWORD", "KC_ADMIN_PASSWORD",
    "KEELSON_BFF_CLIENT_SECRET", "KEELSON_DEMO_CLIENT_SECRET", "MERXA_WEB_CLIENT_SECRET",
    "MERXA_DEMO_CLIENT_SECRET", "DEMO_USER_PASSWORD", "SESSION_SECRET", "DEMO_ACCESS_SECRET",
    # extras required by compose.yaml / the infra task
    "OPTIMIZER_SHARED_SECRET", "LITELLM_UI_PASSWORD", "PHOENIX_SECRET", "DEMO_ENABLED", "SEED_PROFILE",
}


def _run(root: Path, *extra: str) -> int:
    return init_secrets.main(["--root", str(root), *extra])


def test_spec_covers_contract_and_never_gemini() -> None:
    names = {v.name for v in init_secrets.ENV_SPEC}
    assert CONTRACT_VARS <= names
    assert "GEMINI_API_KEY" not in names
    assert len(names) == len(init_secrets.ENV_SPEC), "duplicate names in ENV_SPEC"


def test_fresh_generation(tmp_path: Path) -> None:
    assert _run(tmp_path) == 0
    env = init_secrets.parse_env((tmp_path / ".env").read_text())
    assert CONTRACT_VARS <= env.keys()
    assert "GEMINI_API_KEY" not in env
    assert env["LITELLM_MASTER_KEY"].startswith("sk-") and len(env["LITELLM_MASTER_KEY"]) > 40
    assert env["DEMO_ENABLED"] == "true" and env["DEMO_MODE"] == "1" and env["SEED_PROFILE"] == "full"
    assert env["PUBLIC_BASE_DOMAIN"] == "localhost" and env["HTTP_PORT"] == "80"
    # secrets are random, distinct and URL/shell safe
    secret_names = [v.name for v in init_secrets.ENV_SPEC if v.secret]
    values = [env[n] for n in secret_names]
    assert len(set(values)) == len(values)
    assert all(v.removeprefix("sk-").removeprefix("whsec_").isalnum() for v in values)

    sdir = tmp_path / "secrets"
    for name in init_secrets.EC_KEY_FILES:
        assert init_secrets.is_valid_ec_p256_pem((sdir / name).read_bytes())
    raw = base64.b64decode((sdir / init_secrets.DATA_KEY_FILE).read_bytes().strip())
    assert len(raw) == 32
    assert (sdir / init_secrets.EC_KEY_FILES[0]).read_bytes() != (sdir / init_secrets.EC_KEY_FILES[1]).read_bytes()


def test_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("# mine\nKEELSON_DB_PASSWORD=keepme\nCUSTOM=1\nVALKEY_PASSWORD=\n")
    assert _run(tmp_path) == 0
    first_env = (tmp_path / ".env").read_text()
    first_keys = {p.name: p.read_bytes() for p in (tmp_path / "secrets").iterdir()}
    env = init_secrets.parse_env(first_env)
    assert env["KEELSON_DB_PASSWORD"] == "keepme"
    assert env["CUSTOM"] == "1"
    assert env["VALKEY_PASSWORD"], "empty values are regenerated"
    assert first_env.startswith("# mine\n")
    assert first_env.count("VALKEY_PASSWORD=") == 1

    assert _run(tmp_path) == 0
    assert (tmp_path / ".env").read_text() == first_env
    assert {p.name: p.read_bytes() for p in (tmp_path / "secrets").iterdir()} == first_keys


def test_check_mode_reports_missing_without_writing(tmp_path: Path) -> None:
    assert _run(tmp_path, "--check") == 1
    assert not (tmp_path / ".env").exists()
    assert not (tmp_path / "secrets").exists()
    assert _run(tmp_path) == 0
    assert _run(tmp_path, "--check") == 0


def test_invalid_existing_material_is_reported_not_replaced(tmp_path: Path) -> None:
    sdir = tmp_path / "secrets"
    sdir.mkdir()
    (sdir / "keelson_data_key").write_text("not-base64-32-bytes")
    assert _run(tmp_path) == 1
    assert (sdir / "keelson_data_key").read_text() == "not-base64-32-bytes"


def test_bad_master_key_and_gemini_in_env_are_problems(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("LITELLM_MASTER_KEY=nope\nGEMINI_API_KEY=x\n")
    assert _run(tmp_path) == 1
    problems = init_secrets.validate_env(init_secrets.parse_env((tmp_path / ".env").read_text()))
    assert any("sk-" in p for p in problems)
    assert any("GEMINI_API_KEY" in p for p in problems)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A=1\n# c\n\nB='two words'\nexport C=\"3\"\n", {"A": "1", "B": "two words", "C": "3"}),
        ("bad line\nD=x=y\n", {"D": "x=y"}),
    ],
)
def test_parse_env(text: str, expected: dict[str, str]) -> None:
    assert init_secrets.parse_env(text) == expected


def test_rejects_non_p256_and_pkcs1(tmp_path: Path) -> None:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    p384 = ec.generate_private_key(ec.SECP384R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    sec1 = ec.generate_private_key(ec.SECP256R1()).private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
    )
    assert not init_secrets.is_valid_ec_p256_pem(p384)
    assert not init_secrets.is_valid_ec_p256_pem(sec1)
    assert init_secrets.is_valid_ec_p256_pem(init_secrets.generate_ec_p256_pem())


def test_env_rewrite_is_atomic(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A failed rewrite must leave the existing .env untouched and no temp files behind."""
    original = "LITELLM_SALT_KEY=irreplaceable\n"
    (tmp_path / ".env").write_text(original)

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(init_secrets.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        init_secrets.ensure_env(tmp_path / ".env")
    assert (tmp_path / ".env").read_text() == original
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env"]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() != 0, reason="needs root (init container)")
def test_generated_files_belong_to_repo_owner(tmp_path: Path) -> None:
    """Run as root in the init container, files must belong to the bind-mounted repo's owner."""
    os.chown(tmp_path, 4321, 4322)
    assert _run(tmp_path) == 0
    paths = [tmp_path / ".env", tmp_path / "secrets", *(tmp_path / "secrets").iterdir()]
    assert {(p.stat().st_uid, p.stat().st_gid) for p in paths} == {(4321, 4322)}
    assert (tmp_path / ".env").stat().st_mode & 0o777 == 0o600
    for name in init_secrets.EC_KEY_FILES:  # read through compose secrets by uid 10001
        assert (tmp_path / "secrets" / name).stat().st_mode & 0o004
