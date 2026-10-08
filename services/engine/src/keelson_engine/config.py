"""Engine settings (pydantic-settings).

Every variable of ``compose.yaml`` ``*engine-env`` is a field here (names are the env var
names, case-insensitive). Secrets are ``SecretStr`` so they never end up in logs or reprs.
``get_settings()`` is cached; tests call :func:`set_settings` to install their own.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["Settings", "get_settings", "reset_settings", "set_settings"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="", extra="ignore", case_sensitive=False)

    # ── process ──────────────────────────────────────────────────────────────
    port: int = 8000
    log_level: str = "INFO"
    log_json: bool = True

    # ── data stores ──────────────────────────────────────────────────────────
    keelson_database_url: str = "postgresql+asyncpg://keelson:keelson@pg-core:5432/keelson"
    adk_database_url: str = "postgresql+asyncpg://adk:adk@pg-core:5432/adk"
    valkey_url: SecretStr = SecretStr("redis://valkey:6379/0")
    db_pool_size: int = 5
    db_max_overflow: int = 10
    migrations_dir: Path | None = None
    """Directory with ``NNNN_name.sql`` files; default ``/app/migrations`` or the source tree."""

    # ── identity ─────────────────────────────────────────────────────────────
    oidc_issuer: str = "http://auth.localhost/realms/keelson"
    oidc_client_id: str = "keelson-bff"
    keelson_bff_client_secret: SecretStr = SecretStr("")
    demo_client_id: str = "keelson-demo"
    keelson_demo_client_secret: SecretStr = SecretStr("")
    demo_user_password: SecretStr = SecretStr("")
    keycloak_internal_url: str = "http://keycloak:8080"
    kc_admin_password: SecretStr = SecretStr("")
    merxa_issuer: str = "http://auth.localhost/realms/merxa"
    merxa_mcp_url: str = "http://merxa.localhost/mcp"

    # ── public surface ───────────────────────────────────────────────────────
    public_url: str = "http://keelson.localhost"
    public_base_domain: str = "localhost"
    session_secret: SecretStr = SecretStr("")
    demo_access_secret: SecretStr = SecretStr("")
    demo_mode: bool = False
    brand_engine_name: str = "Keelson"
    brand_vendor_name: str = "Merxa"
    session_idle_seconds: int = 7200
    session_max_seconds: int = 36000
    console_session_seconds: int = 8 * 3600
    console_ticket_seconds: int = 60

    # ── connectors / services ────────────────────────────────────────────────
    connector_proxy_url: str | None = "http://egress:3128"
    """Egress proxy for third-party connector traffic (Merxa MCP + merxa realm)."""
    optimizer_url: str = "http://optimizer:8200"
    optimizer_shared_secret: SecretStr = SecretStr("")
    litellm_base_url: str = "http://litellm:4000"
    litellm_master_key: SecretStr = SecretStr("")
    llm_request_timeout_s: float = 180.0
    """Upper bound of ONE model request through the LiteLLM proxy; the client then retries it
    (OpenAI SDK, 2 retries). A provider call that never answers stalled a run for 10 min at the
    previous 600 s (M3 integration: Gemini Pro hung once on the recommender); normal calls take
    2–40 s."""
    models_lock: Path = Path("/app/models.lock")
    tenants_file: Path = Path("/app/tenants.yaml")
    packs_dir: Path = Path("/app/packs")
    """Template packs ``<packs_dir>/<pack>/pack.yaml`` (compose mounts ``data/packs``)."""
    secrets_dir: Path = Path("/run/secrets")

    # ── back-office (docs/contracts/70-backoffice-m2.md) ─────────────────────
    connector_dev_hosts: str = "merxa.localhost"
    """Comma-separated hosts a connector URL may use although they do not resolve to a public
    address (dev only). Everything else must resolve to public IPs only."""
    connector_test_timeout_seconds: float = 20.0
    platform_max_llm_calls: int = 120
    platform_max_total_tokens: int = 2_000_000
    platform_max_rpm_limit: int = 1_000
    platform_max_tpm_limit: int = 2_000_000
    webhook_max_body_bytes: int = 256 * 1024
    webhook_tolerance_seconds: int = 300
    webhook_rate_per_minute: int = 600
    """Per ``(tenant, connector)``; Valkey fixed window (fails open when Valkey is down)."""

    # ── scn/1.0 canonical store sync (docs/contracts/80-m3-deterministic-core.md §1) ──
    scn_sync_debounce_seconds: float = 30.0
    """A webhook stale mark makes the connector due this long after the *last* event …"""
    scn_sync_max_delay_seconds: float = 300.0
    """… but never later than this after the first unsynced event (a steady event stream cannot
    postpone the sync forever)."""
    scn_sync_retry_seconds: float = 60.0
    """Back-off after a failed sync: ``retry * 2^(failures-1)``, capped at 15 min."""
    scn_sync_timeout_seconds: float = 600.0
    """Whole sync (open + export + download + load); a timeout fails it, the previous snapshot stays."""
    scn_sync_download_timeout_seconds: float = 120.0
    scn_sync_download_concurrency: int = 4
    scn_sync_max_part_bytes: int = 256 * 1024 * 1024
    scn_sync_max_total_bytes: int = 2 * 1024 * 1024 * 1024
    scn_sync_keep_snapshots: int = 2
    """GC keeps the newest N ready/superseded snapshots per connector (+ every pinned one)."""
    scn_sync_keep_failed: int = 20
    """Failed attempts kept in ``scn_snapshot`` for the back-office history (they hold no rows)."""
    scn_sync_tmp_dir: Path | None = None
    """Where downloaded Parquet parts are staged (default: the system temp dir)."""

    # ── knowledge base (docs/contracts/90-m4-investigation.md §7) ─────────────
    kb_embed_alias: str = "embed-default"
    """models.lock alias the bootstrap seeds the embedding profile from (``params.dimensions`` = dims)."""
    kb_seed_dir: Path | None = None
    """Seed documents ``<dir>/<tenant slug>/*.md`` (bootstrap, only when the tenant KB is empty);
    default ``/app/kb`` or the source tree's ``data/kb``."""
    kb_seed_name: str = "Playbooks & policies"
    kb_max_upload_bytes: int = 5 * 1024 * 1024
    kb_chunk_tokens: int = 600
    kb_chunk_overlap_tokens: int = 80
    kb_max_chunks_per_document: int = 2000
    kb_max_pdf_pages: int = 500
    kb_pdf_timeout_s: float = 60.0
    """PDF text extraction runs in a child process (``core/kb_pdf.py``) killed after this long."""
    kb_pdf_memory_mb: int = 384
    """Address-space limit (RLIMIT_AS) of that child; a PDF bomb fails there, not in engine-api."""
    kb_embed_batch: int = 32
    """Texts per LiteLLM ``/embeddings`` request."""
    kb_reembed_stale_seconds: int = 900
    """A ``running`` re-embed job without progress for this long is treated as interrupted."""

    # ── M5 remediation (docs/contracts/95-m5-remediation.md) ──────────────────
    replan_event_types: str = "lane.capacity_changed"
    """Comma-separated webhook types that re-plan dependent cases (``core.replan.ROUTERS``:
    ``lane.capacity_changed``, ``sailing.updated``)."""
    checklist_poll_seconds: float = 60.0
    """Read-back poll of open Guide checklists (sooner when a connector event arrived)."""
    demo_tenants: str = "demo-retail"
    """Tenants ``POST /api/demo/reset`` resets (DEMO_MODE only; the first one is the demo tenant)."""
    demo_merxa_users: str = "marco.rossi,priya.shah"
    """Merxa usernames whose offline sessions/consents the demo reset always revokes (plus every
    username linked to the tenant through the Token Broker)."""
    merxa_api_url: str = "http://merxa-api:8100"
    """Merxa REST base the demo reset calls (``/v1/auth/demo/login-as``, ``/v1/admin/scenario:reset``)."""
    merxa_public_url: str = "http://merxa.localhost"
    """Merxa's public origin (sent as ``Origin`` on its cookie-authenticated demo endpoints)."""
    demo_merxa_admin: str = "merxa.admin"
    demo_reset_merxa_timeout_s: float = 25.0

    # ── M6 Signal Hub (docs/contracts/100-m6-signals.md §6) ───────────────────
    signal_hub_url: str = ""
    """Internal base URL of ``signal-hub-api`` (compose: ``http://signal-hub-api:8300``). Empty =
    the hub integration is off (no network push, no forwarding, no alerts, no internal connector)."""
    signal_hub_shared_secret: SecretStr = SecretStr("")
    """HS256 key of the engine's service JWTs for the hub (``iss keelson-engine``, ``aud signal-hub``)."""
    signal_hub_connector_slug: str = "signal-hub"
    """Slug of the internal first-party connector (auth kind ``service_jwt``) bootstrap registers."""
    signal_hub_timeout_s: float = 10.0
    signal_hub_token_ttl_s: int = 60
    signal_hub_forward_event_types: str = (
        "supplier_message.received,lane.capacity_changed,sailing.updated,po_line.updated"
    )
    """Comma-separated connector webhook types forwarded to ``POST /v1/ingest/connector-event``."""
    signal_hub_forward_inline_timeout_s: float = 3.0
    """Inline forward from the webhook receiver (a slower hub is retried by the alert watcher)."""
    alerts_watch: bool = True
    """engine-api runs the alert watcher (polls the hub per tenant, SSE fan-out, auto-open)."""
    alerts_poll_seconds: float = 5.0
    alerts_list_limit: int = 200
    alerts_stream_maxlen: int = 500
    alerts_tools_refresh_seconds: float = 900.0
    """How often the watcher re-snapshots the internal connector's ``tools/list`` (first-party)."""

    # ── M7 back-office (110) ─────────────────────────────────────────────────
    evals_results_dir: Path | None = None
    """``GET /api/admin/evals``: the eval results directory, mounted read-only (``evals/results``);
    unset = ``/app/evals/results`` when present, else the source tree's ``evals/results``."""
    evals_list_limit: int = Field(default=10, ge=1, le=50)
    scn_crawl_max_rows: int = Field(default=2_000_000, ge=1000, le=10_000_000)
    """Upper bound of the rows one tier-A crawl may load."""

    # ── M8 mapping compiler + proposed mappings (121) ─────────────────────────
    scn_mapping_engine: Literal["duckdb", "interpreter"] = "duckdb"
    """Tier-A crawl engine: the DuckDB mapping compiler, or ``interpreter`` = the exact M7 path
    (same rows; kept as the reference and a kill switch)."""
    scn_raw_staging: bool = True
    """Keep the raw pages of a crawl (current snapshot + latest failed attempt) for staged previews;
    off = raw data lives only inside the crawl's DuckDB session."""
    scn_raw_max_bytes: int = Field(default=512 * 1024 * 1024, ge=1024 * 1024, le=8 * 1024 * 1024 * 1024)
    """Raw JSON one crawl may fetch (all sources); above it the crawl fails ``too_many_rows``."""
    duckdb_memory_limit_mb: int = Field(default=256, ge=32, le=4096)
    """Per DuckDB session (one crawl / preview); it spills to a private temp dir beyond it."""
    duckdb_threads: int = Field(default=2, ge=1, le=8)
    mapping_preview_max_items: int = Field(default=20_000, ge=1, le=50_000)
    """Upper bound of items per source a staged preview maps in engine-api."""
    mapping_proposer: Literal["auto", "heuristic", "off"] = "auto"
    """``auto``: rule-based drafts refined by the LLM when available; ``heuristic``: rules only (no
    model call); ``off``: proposals disabled (409)."""
    mapping_proposer_alias: str = "fast-default"
    """LiteLLM alias of the ``mapping_proposer`` agent (Flash / Flash-Lite only; Pro is refused)."""
    mapping_propose_max_calls: int = Field(default=24, ge=1, le=200)
    """Read-tool calls one proposal may make while sampling."""
    mapping_propose_sample_size: int = Field(default=20, ge=1, le=200)
    """Items per sampled page."""
    mapping_propose_timeout_s: float = Field(default=120.0, ge=5.0, le=600.0)
    mapping_propose_llm_timeout_s: float = Field(default=60.0, ge=5.0, le=300.0)
    mapping_propose_max_profile_chars: int = Field(default=24_000, ge=2_000, le=200_000)
    """Upper bound of the profile data block sent to the model."""

    # ── runs ─────────────────────────────────────────────────────────────────
    run_lease_seconds: int = 60
    event_stream_maxlen: int = 1000
    event_stream_ttl_seconds: int = 24 * 3600

    http_timeout_seconds: float = 15.0

    @field_validator("public_url", "oidc_issuer", "merxa_issuer", "litellm_base_url", "optimizer_url",
                     "merxa_api_url", "merxa_public_url", "signal_hub_url")
    @classmethod
    def _strip_slash(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("connector_proxy_url")
    @classmethod
    def _empty_proxy(cls, v: str | None) -> str | None:
        return v or None

    # ── derived ──────────────────────────────────────────────────────────────
    @property
    def public_origin(self) -> str:
        """``scheme://host[:port]`` of :attr:`public_url` (the only accepted ``Origin``)."""
        parts = urlsplit(self.public_url)
        return f"{parts.scheme}://{parts.netloc}"

    @property
    def public_scheme(self) -> str:
        return urlsplit(self.public_url).scheme or "http"

    @property
    def cookie_secure(self) -> bool:
        return self.public_scheme == "https"

    @property
    def signal_hub_enabled(self) -> bool:
        """The hub integration is configured (URL + shared secret)."""
        return bool(self.signal_hub_url and self.signal_hub_shared_secret.get_secret_value())

    @property
    def dev_hosts(self) -> frozenset[str]:
        return frozenset(h.strip().lower().rstrip(".") for h in self.connector_dev_hosts.split(",") if h.strip())

    @property
    def keelson_realm(self) -> str:
        return self.oidc_issuer.rstrip("/").rsplit("/", 1)[-1]

    @property
    def is_local_demo(self) -> bool:
        """True when the public base domain is ``localhost`` (persona switcher without secret)."""
        return self.public_base_domain.strip().lower() == "localhost"

    @property
    def bff_redirect_uri(self) -> str:
        return f"{self.public_url}/api/auth/callback"

    def connector_redirect_uri(self, slug: str) -> str:
        return f"{self.public_url}/api/connectors/{slug}/callback"

    def console_host(self, target: str) -> str:
        return f"{target}.{self.public_base_domain}"

    def console_origin(self, target: str) -> str:
        """``scheme://<target>.<domain>[:port]`` — same scheme/port as :attr:`public_url` (Caddy
        serves every host on one port, e.g. ``HTTP_PORT=8080``)."""
        port = urlsplit(self.public_url).port
        return f"{self.public_scheme}://{self.console_host(target)}" + (f":{port}" if port else "")


_override: Settings | None = None


@lru_cache(maxsize=1)
def _cached() -> Settings:
    return Settings()


def get_settings() -> Settings:
    """Process-wide settings (env parsed once)."""
    return _override if _override is not None else _cached()


def set_settings(settings: Settings | None) -> None:
    """Install explicit settings (tests); ``None`` restores env-based settings."""
    global _override
    _override = settings


def reset_settings() -> None:
    set_settings(None)
    _cached.cache_clear()

