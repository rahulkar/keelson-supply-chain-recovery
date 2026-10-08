"""Runtime configuration for merxa-api (environment driven, see docs/contracts/20-merxa.md)."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEV_SESSION_SECRET = "dev-insecure-session-secret"
SECRETS_DIR = Path(os.environ.get("MERXA_SECRETS_DIR", "/run/secrets"))


def read_secret(name: str, default: str | None = None) -> str | None:
    """Read a secret from ``/run/secrets/<name>``, falling back to env ``<NAME>``.

    Values are stripped of surrounding whitespace. Never log the return value.
    """
    path = SECRETS_DIR / name
    try:
        if path.is_file():
            value = path.read_text(encoding="utf-8").strip()
            if value:
                return value
    except OSError:
        pass
    value = os.environ.get(name.upper())
    if value is not None and value.strip():
        return value.strip()
    return default


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: str) -> list[str]:
    return [p.strip() for p in _env(name, default).split(",") if p.strip()]


@dataclass(slots=True)
class Settings:
    """All tunables. Construct with :meth:`from_env`; tests build it directly."""

    database_url: str = "postgresql+asyncpg://merxa:merxa@localhost:5432/merxa"
    port: int = 8100
    public_url: str = "http://merxa.localhost"
    oidc_issuer: str = "http://auth.localhost/realms/merxa"
    # Optional override; by default discovered from the issuer's openid-configuration.
    oidc_jwks_uri: str | None = None
    oidc_token_alg: str = "RS256"
    mcp_resource: str = "http://merxa.localhost/mcp"
    trusted_receipt_issuers: list[str] = field(default_factory=lambda: ["http://keelson.localhost"])
    keelson_jwks_url: str = "http://keelson.localhost/api/.well-known/jwks.json"
    receipt_jwks_ttl_s: int = 300
    receipt_leeway_s: int = 30
    web_client_id: str = "merxa-web"
    web_client_secret: str | None = None
    demo_client_id: str = "merxa-demo"
    demo_client_secret: str | None = None
    demo_user_password: str | None = None
    demo_access_secret: str | None = None
    session_secret: str = DEV_SESSION_SECRET
    session_ttl_s: int = 8 * 3600
    demo_mode: bool = False
    seed_profile: str = "full"
    seed_on_startup: bool = True
    mcp_json_response: bool = True
    mcp_allowed_hosts: list[str] = field(
        default_factory=lambda: ["merxa.localhost", "merxa-api", "localhost", "127.0.0.1"]
    )
    mcp_allowed_origins: list[str] = field(default_factory=list)
    brand_vendor_name: str = "Merxa"
    log_level: str = "INFO"
    idempotency_retention_h: int = 24
    idempotency_stale_in_flight_s: int = 120
    # scn/1.0 snapshots (Parquet) and signed download URLs
    snapshot_dir: str = "/data/snapshots"
    snapshot_secret: str | None = None
    snapshot_ttl_s: int = 7200
    # Standard Webhooks delivery (seeded subscription when both are set)
    keelson_webhook_url: str | None = None
    webhook_secret: str | None = None
    webhook_worker: bool = True
    webhook_poll_s: float = 1.0
    webhook_timeout_s: float = 10.0
    # Key material for secrets at rest (webhook secrets); falls back to SESSION_SECRET.
    data_key: str | None = None

    @classmethod
    def from_env(cls) -> Settings:
        public_url = _env("PUBLIC_URL", "http://merxa.localhost").rstrip("/")
        host = urlsplit(public_url).hostname or "merxa.localhost"
        default_hosts = ",".join(dict.fromkeys([host, "merxa-api", "localhost", "127.0.0.1"]))
        return cls(
            database_url=_env("MERXA_DATABASE_URL", "postgresql+asyncpg://merxa:merxa@localhost:5432/merxa"),
            port=int(_env("PORT", "8100")),
            public_url=public_url,
            oidc_issuer=_env("OIDC_ISSUER", "http://auth.localhost/realms/merxa").rstrip("/"),
            oidc_jwks_uri=os.environ.get("OIDC_JWKS_URI") or None,
            oidc_token_alg=_env("OIDC_TOKEN_ALG", "RS256"),
            mcp_resource=_env("MCP_RESOURCE", "http://merxa.localhost/mcp"),
            trusted_receipt_issuers=_env_list("TRUSTED_RECEIPT_ISSUERS", "http://keelson.localhost"),
            keelson_jwks_url=_env("KEELSON_JWKS_URL", "http://keelson.localhost/api/.well-known/jwks.json"),
            receipt_jwks_ttl_s=int(_env("RECEIPT_JWKS_TTL_S", "300")),
            web_client_id=_env("MERXA_WEB_CLIENT_ID", "merxa-web"),
            web_client_secret=read_secret("merxa_web_client_secret"),
            demo_client_id=_env("MERXA_DEMO_CLIENT_ID", "merxa-demo"),
            demo_client_secret=read_secret("merxa_demo_client_secret"),
            demo_user_password=read_secret("demo_user_password"),
            demo_access_secret=read_secret("demo_access_secret"),
            session_secret=read_secret("session_secret") or DEV_SESSION_SECRET,
            demo_mode=_env_bool("DEMO_MODE", False),
            seed_profile=_env("SEED_PROFILE", "full"),
            seed_on_startup=_env_bool("SEED_ON_STARTUP", True),
            mcp_json_response=_env_bool("MCP_JSON_RESPONSE", True),
            mcp_allowed_hosts=_env_list("MCP_ALLOWED_HOSTS", default_hosts),
            mcp_allowed_origins=_env_list("MCP_ALLOWED_ORIGINS", ""),
            brand_vendor_name=_env("BRAND_VENDOR_NAME", "Merxa"),
            log_level=_env("LOG_LEVEL", "INFO"),
            snapshot_dir=_env("SNAPSHOT_DIR", "/data/snapshots"),
            snapshot_secret=read_secret("merxa_snapshot_secret"),
            snapshot_ttl_s=int(_env("SNAPSHOT_TTL_S", "7200")),
            keelson_webhook_url=os.environ.get("KEELSON_WEBHOOK_URL") or None,
            webhook_secret=read_secret("merxa_webhook_secret"),
            webhook_worker=_env_bool("WEBHOOK_WORKER", True),
            webhook_poll_s=float(_env("WEBHOOK_POLL_S", "1.0")),
            data_key=read_secret("merxa_data_key"),
        )

    @property
    def public_origin(self) -> str:
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def public_host(self) -> str:
        return urlsplit(self.public_url).hostname or ""

    @property
    def is_localhost_deployment(self) -> bool:
        host = self.public_host
        return host == "localhost" or host.endswith(".localhost")

    @property
    def mcp_resource_base_and_path(self) -> tuple[str, str]:
        """Split MCP_RESOURCE into (base_url, path) e.g. ('http://merxa.localhost', '/mcp')."""
        parts = urlsplit(self.mcp_resource)
        return f"{parts.scheme}://{parts.netloc}", (parts.path or "/mcp")
