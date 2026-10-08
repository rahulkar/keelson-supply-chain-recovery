"""Deployment settings from the environment (``docs/contracts/122`` §6).

Behaviour (tables, entity SQL, ops, caps) lives in the YAML file named by ``SIDECAR_CONFIG``;
the environment only says *where* things are and holds the secrets. Secrets are read here once
and never logged.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from adapter_sidecar.errors import ConfigError

MIN_SECRET_CHARS = 32


def _bool(value: str | None, default: bool = False) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _int(env: Mapping[str, str], name: str, default: int, lo: int, hi: int) -> int:
    raw = env.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer") from None
    if not lo <= value <= hi:
        raise ConfigError(f"{name} must be within {lo}..{hi}")
    return value


def origin_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.netloc.rsplit('@', 1)[-1]}"


@dataclass(frozen=True)
class Settings:
    config_path: Path
    public_url: str
    mcp_resource: str
    mcp_allowed_hosts: list[str]
    state_db_url: str
    data_dir: Path | None
    snapshot_dir: Path
    snapshot_secret: str | None
    admin_key: str | None
    keelson_jwks_url: str
    trusted_receipt_issuers: list[str]
    receipt_leeway_s: int = 30
    receipt_jwks_ttl_s: int = 300
    snapshot_ttl_s: int | None = None
    """``SNAPSHOT_TTL_S`` overrides the config's ``snapshot.ttl_s`` (deployment concern)."""
    outbound_proxy: str | None = None
    demo_mode: bool = False
    demo_golden_dir: Path | None = None
    log_level: str = "INFO"
    port: int = 8400
    idempotency_stale_s: int = 120
    idempotency_retention_h: int = 24
    env: Mapping[str, str] = field(default_factory=dict, repr=False)
    """The process environment (secret lookups by ``*_env`` names from the config)."""

    @property
    def mcp_path(self) -> str:
        return urlsplit(self.mcp_resource).path or "/mcp"

    @property
    def public_origin(self) -> str:
        return origin_of(self.public_url)

    def secret(self, name: str) -> str | None:
        """Value of the environment variable ``name`` (``None`` when unset or empty)."""
        value = self.env.get(name)
        return value if value else None

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        env = dict(os.environ if env is None else env)
        public_url = (env.get("PUBLIC_URL") or "").rstrip("/")
        if not public_url.startswith(("http://", "https://")) or not urlsplit(public_url).hostname:
            raise ConfigError("PUBLIC_URL must be the absolute http(s) origin Keelson registers (e.g. http://fernhill.localhost)")
        if urlsplit(public_url).path not in ("", "/") or "@" in urlsplit(public_url).netloc:
            raise ConfigError("PUBLIC_URL must be an origin without path or userinfo")
        resource = (env.get("MCP_RESOURCE") or f"{public_url}/mcp").rstrip("/")
        if origin_of(resource) != origin_of(public_url):
            raise ConfigError("MCP_RESOURCE must be on the PUBLIC_URL origin (Keelson only downloads parts from it)")
        hosts_raw = env.get("MCP_ALLOWED_HOSTS") or ""
        hosts = [h.strip() for h in hosts_raw.split(",") if h.strip()]
        public_host = urlsplit(public_url).netloc
        if not hosts:
            hosts = [public_host]
        elif public_host not in hosts and urlsplit(public_url).hostname not in hosts:
            raise ConfigError(f"MCP_ALLOWED_HOSTS must include the PUBLIC_URL host {public_host!r}")
        config_path = env.get("SIDECAR_CONFIG") or ""
        if not config_path:
            raise ConfigError("SIDECAR_CONFIG must name the sidecar YAML file")
        secret = env.get("SIDECAR_SNAPSHOT_SECRET") or None
        if secret is not None and len(secret) < MIN_SECRET_CHARS:
            raise ConfigError(f"SIDECAR_SNAPSHOT_SECRET must have at least {MIN_SECRET_CHARS} characters")
        admin = env.get("SIDECAR_ADMIN_KEY") or None
        if admin is not None and len(admin) < MIN_SECRET_CHARS:
            raise ConfigError(f"SIDECAR_ADMIN_KEY must have at least {MIN_SECRET_CHARS} characters")
        demo = _bool(env.get("DEMO_MODE"))
        golden = env.get("DEMO_GOLDEN_DIR")
        issuers = [i.strip().rstrip("/") for i in (env.get("TRUSTED_RECEIPT_ISSUERS") or "").split(",") if i.strip()]
        data_dir = env.get("DATA_DIR")
        ttl = env.get("SNAPSHOT_TTL_S")
        return cls(
            config_path=Path(config_path),
            public_url=public_url,
            mcp_resource=resource,
            mcp_allowed_hosts=hosts,
            state_db_url=env.get("STATE_DB_URL") or "sqlite+aiosqlite:////data/state/sidecar.db",
            data_dir=Path(data_dir) if data_dir else None,
            snapshot_dir=Path(env.get("SNAPSHOT_DIR") or "/data/snapshots"),
            snapshot_secret=secret,
            admin_key=admin,
            keelson_jwks_url=env.get("KEELSON_JWKS_URL") or "http://keelson.localhost/api/.well-known/jwks.json",
            trusted_receipt_issuers=issuers or ["http://keelson.localhost"],
            receipt_leeway_s=_int(env, "RECEIPT_LEEWAY_S", 30, 0, 300),
            receipt_jwks_ttl_s=_int(env, "RECEIPT_JWKS_TTL_S", 300, 10, 86_400),
            snapshot_ttl_s=_int(env, "SNAPSHOT_TTL_S", 7200, 60, 86_400) if ttl else None,
            outbound_proxy=env.get("OUTBOUND_PROXY") or None,
            demo_mode=demo,
            demo_golden_dir=Path(golden) if golden else None,
            log_level=(env.get("LOG_LEVEL") or "INFO").upper(),
            port=_int(env, "PORT", 8400, 1, 65_535),
            idempotency_stale_s=_int(env, "IDEMPOTENCY_STALE_S", 120, 5, 3600),
            env=env,
        )
