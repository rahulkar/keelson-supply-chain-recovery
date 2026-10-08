#!/usr/bin/env python3
"""Idempotently generate the local ``.env`` file and the ``secrets/`` directory.

Contract: ``docs/contracts/00-overview.md`` (section "Env vars" and "Files in secrets/").

Behaviour
---------
* Every variable listed in :data:`ENV_SPEC` is guaranteed to exist in ``.env``.
  Values that already exist are **never** overwritten; missing ones are appended
  (random secrets are generated with :mod:`secrets`).
* ``GEMINI_API_KEY`` is deliberately **not** written: it is read from the host
  environment by ``compose.yaml`` and must never land in a file.
* ``secrets/keelson_client_assertion_key.pem`` and ``secrets/keelson_receipt_key.pem``
  are EC P-256 private keys (PKCS#8, unencrypted PEM); ``secrets/keelson_data_key`` is
  32 random bytes, base64-encoded. Existing files are validated and kept.

Usage (normally via ``scripts/init.ps1`` / ``scripts/init.sh`` which run it in
``python:3.14-slim-trixie`` with ``cryptography==50.0.2``)::

    python scripts/init_secrets.py [--root /work] [--check]

``--check`` validates without writing and exits non-zero when something is missing.
Values are never printed; only variable names and file names are logged.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import logging
import os
import re
import secrets
import string
import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

LOG = logging.getLogger("init_secrets")

_ALNUM = string.ascii_letters + string.digits


def random_token(length: int = 32) -> str:
    """Return a random alphanumeric token.

    Alphanumerics only, so the value is safe inside connection URLs
    (``postgresql://user:<pw>@host``), command lines (valkey ``--requirepass``),
    YAML scalars and Keycloak variable substitution without any escaping.
    """
    return "".join(secrets.choice(_ALNUM) for _ in range(length))


def _const(value: str) -> Callable[[], str]:
    return lambda: value


def _token(length: int, prefix: str = "") -> Callable[[], str]:
    return lambda: prefix + random_token(length)


@dataclass(frozen=True, slots=True)
class EnvVar:
    """One ``.env`` entry: name, generator for a fresh value and a comment."""

    name: str
    generate: Callable[[], str]
    comment: str
    secret: bool = True


# Order defines the order of appended lines in a fresh .env.
ENV_SPEC: tuple[EnvVar, ...] = (
    # ── general ───────────────────────────────────────────────────────────────
    EnvVar("PUBLIC_BASE_DOMAIN", _const("localhost"), "public base domain (dev: localhost)", False),
    EnvVar("HTTP_PORT", _const("80"), "Caddy listen port on the host", False),
    EnvVar("DEMO_MODE", _const("1"), "1 = demo features (persona switcher, demo director)", False),
    EnvVar("DEMO_ENABLED", _const("true"), "Keycloak demo clients/users enabled (true|false)", False),
    EnvVar("SEED_PROFILE", _const("full"), "Merxa seed profile (full = the demo | small = fast tests)", False),
    EnvVar("BRAND_ENGINE_NAME", _const("Keelson"), "engine brand string", False),
    EnvVar("BRAND_VENDOR_NAME", _const("Merxa"), "vendor brand string", False),
    EnvVar("DEV_ALLOW_KC_ADMIN", _const("0"), "1 = expose Keycloak /admin + master realm via auth.localhost", False),
    # ── pg-core ───────────────────────────────────────────────────────────────
    EnvVar("PG_CORE_SUPERPASSWORD", _token(40), "pg-core superuser (postgres) password"),
    EnvVar("KEELSON_DB_PASSWORD", _token(32), "pg-core role keelson"),
    EnvVar("ADK_DB_PASSWORD", _token(32), "pg-core role adk"),
    EnvVar("KEYCLOAK_DB_PASSWORD", _token(32), "pg-core role keycloak"),
    # ── LiteLLM ───────────────────────────────────────────────────────────────
    EnvVar("LITELLM_DB_PASSWORD", _token(32), "pg-litellm role litellm"),
    EnvVar("LITELLM_MASTER_KEY", _token(48, "sk-"), "LiteLLM master key (must start with sk-)"),
    EnvVar("LITELLM_SALT_KEY", _token(48), "LiteLLM DB encryption salt - NEVER rotate; back up with pg-litellm"),
    EnvVar("LITELLM_UI_PASSWORD", _token(24), "LiteLLM admin UI password (user admin)"),
    # ── Merxa ─────────────────────────────────────────────────────────────────
    EnvVar("MERXA_DB_PASSWORD", _token(32), "pg-merxa role merxa"),
    # ── Valkey ────────────────────────────────────────────────────────────────
    EnvVar("VALKEY_PASSWORD", _token(32), "valkey requirepass"),
    # ── Keycloak ──────────────────────────────────────────────────────────────
    EnvVar("KC_ADMIN_PASSWORD", _token(32), "Keycloak bootstrap admin (user admin, master realm)"),
    EnvVar("KEELSON_BFF_CLIENT_SECRET", _token(40), "realm keelson, client keelson-bff"),
    EnvVar("KEELSON_DEMO_CLIENT_SECRET", _token(40), "realm keelson, client keelson-demo"),
    EnvVar("MERXA_WEB_CLIENT_SECRET", _token(40), "realm merxa, client merxa-web"),
    EnvVar("MERXA_DEMO_CLIENT_SECRET", _token(40), "realm merxa, client merxa-demo"),
    EnvVar("DEMO_USER_PASSWORD", _token(20), "password of all seeded demo personas (both realms)"),
    # ── app secrets ───────────────────────────────────────────────────────────
    EnvVar("SESSION_SECRET", _token(64), "cookie/session signing secret (engine-api, merxa-api)"),
    EnvVar("DEMO_ACCESS_SECRET", _token(40), "X-Demo-Access header value for non-localhost demos"),
    EnvVar("OPTIMIZER_SHARED_SECRET", _token(48), "engine -> optimizer shared secret"),
    EnvVar("PHOENIX_SECRET", _token(48), "Arize Phoenix auth secret (profile obs)"),
    EnvVar("MERXA_WEBHOOK_SECRET", _token(32, "whsec_"), "Standard Webhooks secret: Merxa -> Keelson (connector merxa, tenant demo-retail)"),
    EnvVar("MERXA_SNAPSHOT_SECRET", _token(48), "HMAC secret for Merxa signed snapshot download URLs"),
    EnvVar("SIGNAL_HUB_SHARED_SECRET", _token(48), "engine -> signal-hub service JWT secret (HS256)"),
    EnvVar("SIGNALS_DB_PASSWORD", _token(32), "pg-core role signals (signal-hub database)"),
)

#: Never written to .env (taken from the host environment by compose).
FORBIDDEN_IN_ENV: frozenset[str] = frozenset({"GEMINI_API_KEY"})

EC_KEY_FILES: tuple[str, ...] = ("keelson_client_assertion_key.pem", "keelson_receipt_key.pem")
DATA_KEY_FILE = "keelson_data_key"

_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=(.*)$")


# ── .env handling ─────────────────────────────────────────────────────────────


def parse_env(text: str) -> dict[str, str]:
    """Parse a docker-compose style ``.env`` (``KEY=VALUE`` lines, ``#`` comments)."""
    values: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _ENV_LINE.match(line)
        if not match:
            continue
        key, value = match.group(1), match.group(2).strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def missing_env_vars(existing: dict[str, str], spec: Iterable[EnvVar] = ENV_SPEC) -> list[EnvVar]:
    """Return the spec entries that are absent or empty in ``existing``."""
    return [var for var in spec if not existing.get(var.name)]


def validate_env(values: dict[str, str]) -> list[str]:
    """Return human-readable problems with existing values (never includes values)."""
    problems: list[str] = []
    master = values.get("LITELLM_MASTER_KEY", "")
    if master and not master.startswith("sk-"):
        problems.append("LITELLM_MASTER_KEY must start with 'sk-'")
    if values.get("DEMO_ENABLED", "true") not in {"true", "false"}:
        problems.append("DEMO_ENABLED must be 'true' or 'false' (Keycloak boolean)")
    for name in FORBIDDEN_IN_ENV & values.keys():
        problems.append(f"{name} must not be stored in .env (set it in the host environment)")
    return problems


def ensure_env(env_path: Path, *, check_only: bool = False) -> list[str]:
    """Make sure ``env_path`` contains every var of :data:`ENV_SPEC`.

    Returns the names that were (or, with ``check_only``, would be) added.
    Existing content is preserved byte-for-byte; new entries are appended.
    """
    text = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
    existing = parse_env(text)
    for problem in validate_env(existing):
        LOG.warning(json.dumps({"event": "env_problem", "detail": problem}))
    missing = missing_env_vars(existing)
    names = [var.name for var in missing]
    if check_only or not missing:
        return names

    # A key present but empty (e.g. "FOO=") is replaced: drop the empty line so
    # the file never contains two definitions of the same key.
    empty = {var.name for var in missing if var.name in existing}
    kept_lines = [
        line
        for line in text.splitlines()
        if not ((m := _ENV_LINE.match(line.strip())) and m.group(1) in empty)
    ]
    out: list[str] = kept_lines
    if not text:
        out = [
            "# Generated by scripts/init_secrets.py - local secrets, NEVER commit.",
            "# Existing values are never overwritten; delete a line and re-run init to regenerate it.",
            "# GEMINI_API_KEY is intentionally absent: export it in the host environment.",
        ]
    elif out and out[-1].strip():
        out.append("")
    for var in missing:
        out.append(f"# {var.comment}")
        out.append(f"{var.name}={var.generate()}")
    # Atomic replace: .env holds values that cannot be regenerated without data loss
    # (LITELLM_SALT_KEY, DB passwords baked into volumes), so never truncate it in place.
    atomic_write(env_path, ("\n".join(out) + "\n").encode("utf-8"), mode=0o600)
    return names


# ── file helpers ──────────────────────────────────────────────────────────────


def _match_owner(path: Path, reference: Path) -> None:
    """When running as root (the init container), hand ``path`` to ``reference``'s owner.

    On a Linux/macOS host the bind-mounted repo belongs to the developer; files created
    by the root container would otherwise be root-owned, and a 0600 ``.env`` would be
    unreadable for the developer's own ``docker compose``. No-op on Windows bind mounts
    (everything appears as root) and when not running as root.
    """
    geteuid = getattr(os, "geteuid", None)
    if geteuid is None or geteuid() != 0:
        return
    try:
        ref = reference.stat()
        if ref.st_uid != 0:
            os.chown(path, ref.st_uid, ref.st_gid)
    except OSError:
        pass


def atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    """Write ``data`` to ``path`` via a temp file + ``os.replace`` (never a partial file).

    The temp file lives next to ``path`` (same filesystem) and is removed on failure.
    Ownership follows the parent directory's owner (see :func:`_match_owner`).
    """
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    try:
        with open(tmp, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if mode is not None:
            _restrict(tmp, mode)
        _match_owner(tmp, path.parent)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ── key material ──────────────────────────────────────────────────────────────


def generate_ec_p256_pem() -> bytes:
    """Return a fresh EC P-256 private key as unencrypted PKCS#8 PEM."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    key = ec.generate_private_key(ec.SECP256R1())
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def is_valid_ec_p256_pem(data: bytes) -> bool:
    """True when ``data`` is an unencrypted PKCS#8 PEM EC private key on P-256."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    if b"-----BEGIN PRIVATE KEY-----" not in data:  # PKCS#8 marker
        return False
    try:
        key = serialization.load_pem_private_key(data, password=None)
    except (ValueError, TypeError):
        return False
    return isinstance(key, ec.EllipticCurvePrivateKey) and isinstance(key.curve, ec.SECP256R1)


def generate_data_key() -> bytes:
    """Return 32 random bytes, base64 encoded (newline terminated)."""
    return base64.b64encode(secrets.token_bytes(32)) + b"\n"


def is_valid_data_key(data: bytes) -> bool:
    """True when ``data`` is base64 of exactly 32 bytes (surrounding whitespace ignored)."""
    try:
        return len(base64.b64decode(data.strip(), validate=True)) == 32
    except (binascii.Error, ValueError):
        return False


def _restrict(path: Path, mode: int = 0o600) -> None:
    """Best effort chmod (no-op on Windows bind mounts)."""
    try:
        path.chmod(mode)
    except OSError:
        pass


def _ensure_file(
    path: Path,
    make: Callable[[], bytes],
    valid: Callable[[bytes], bool],
    *,
    check_only: bool,
) -> str:
    """Create ``path`` if missing. Returns ``created``, ``kept``, ``missing`` or ``invalid``."""
    if path.exists():
        if not valid(path.read_bytes()):
            LOG.error(json.dumps({"event": "secret_invalid", "file": path.name}))
            return "invalid"
        return "kept"
    if check_only:
        return "missing"
    # Containers run as non-root (uid 10001) and read these through compose
    # `secrets:` bind mounts, so the file keeps the default (umask) read permissions.
    atomic_write(path, make())
    return "created"


def ensure_secret_files(secrets_dir: Path, *, check_only: bool = False) -> dict[str, str]:
    """Create missing key files in ``secrets_dir``; returns ``{file: status}``."""
    if not check_only and not secrets_dir.exists():
        secrets_dir.mkdir(parents=True)
        _match_owner(secrets_dir, secrets_dir.parent)
    status: dict[str, str] = {}
    for name in EC_KEY_FILES:
        status[name] = _ensure_file(
            secrets_dir / name, generate_ec_p256_pem, is_valid_ec_p256_pem, check_only=check_only
        )
    status[DATA_KEY_FILE] = _ensure_file(
        secrets_dir / DATA_KEY_FILE, generate_data_key, is_valid_data_key, check_only=check_only
    )
    return status


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="repository root (default: parent of scripts/)",
    )
    parser.add_argument("--check", action="store_true", help="validate only, do not write")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)

    root: Path = args.root.resolve()
    env_added = ensure_env(root / ".env", check_only=args.check)
    files = ensure_secret_files(root / "secrets", check_only=args.check)
    env_values = parse_env((root / ".env").read_text(encoding="utf-8")) if (root / ".env").exists() else {}
    problems = validate_env(env_values)

    LOG.info(
        json.dumps(
            {
                "event": "init_secrets",
                "mode": "check" if args.check else "apply",
                "env_file": str(root / ".env"),
                "env_added" if not args.check else "env_missing": env_added,
                "env_total": len(ENV_SPEC),
                "secret_files": files,
                "problems": problems,
            }
        )
    )
    bad_files = [n for n, s in files.items() if s in {"missing", "invalid"}]
    if problems or bad_files or (args.check and env_added):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
