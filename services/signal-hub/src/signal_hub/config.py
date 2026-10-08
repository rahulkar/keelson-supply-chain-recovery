"""Runtime configuration from environment variables (``docs/contracts/100-m6-signals.md`` §1, §9).

Secrets follow the repo convention: ``/run/secrets/<name>`` first, then env ``<NAME>``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

SERVICE_ISSUER = "keelson-engine"
SERVICE_AUDIENCE = "signal-hub"

DEFAULT_GDACS_URL = (
    "https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH"
    "?eventlist=TC;EQ;FL&alertlevel=orange;red"
)
DEFAULT_USGS_URL = "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson"
DEFAULT_RSS_FEEDS = "rss.theloadstar=The Loadstar|https://theloadstar.com/feed/"
DEFAULT_USER_AGENT = "KeelsonSignalHub/0.1 (+https://keelson.localhost)"


def read_secret(name: str) -> str | None:
    """``/run/secrets/<name>`` first, then env ``<NAME>``; empty = unset."""
    path = Path("/run/secrets") / name.lower()
    try:
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    except OSError:
        pass
    value = os.environ.get(name.upper(), "").strip()
    return value or None


def _optional_key(value: str | None) -> str | None:
    """A key secret that may hold the placeholder ``unset``/``none`` (Cloud Run secrets cannot be empty)."""
    return None if value is None or value.strip().lower() in {"unset", "none", "-"} else value


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


@dataclass(frozen=True, slots=True)
class RssFeed:
    """One configured trade-press feed: ``connector_id`` (``rss.<slug>``), display name, URL."""

    connector_id: str
    name: str
    url: str


def parse_rss_feeds(raw: str) -> tuple[RssFeed, ...]:
    """``RSS_FEEDS``: comma-separated ``<connector_id>=<name>|<url>`` entries (``rss.`` prefix added
    when missing). Empty = no RSS adapter."""
    feeds: list[RssFeed] = []
    for entry in (e.strip() for e in raw.split(",")):
        if not entry:
            continue
        cid, _, rest = entry.partition("=")
        name, _, url = rest.partition("|")
        cid, name, url = cid.strip().lower(), name.strip(), url.strip()
        if not cid or not name or not url.startswith(("http://", "https://")):
            raise ValueError(f"RSS_FEEDS entry {entry!r} must be <id>=<name>|<http(s) url>")
        if not cid.startswith("rss."):
            cid = f"rss.{cid}"
        feeds.append(RssFeed(cid, name, url))
    ids = [f.connector_id for f in feeds]
    if len(ids) != len(set(ids)):
        raise ValueError("RSS_FEEDS connector ids must be unique")
    return tuple(feeds)


@dataclass(frozen=True, slots=True)
class Settings:
    port: int = 8300
    database_url: str = field(default="", repr=False)
    """asyncpg DSN of the ``signals`` database as role ``signals`` (RLS applies)."""
    shared_secret: str | None = field(default=None, repr=False)
    jwt_leeway_s: int = 5
    max_token_ttl_s: int = 900
    """Service JWTs must be short-lived: ``exp`` more than this far in the future is rejected."""
    log_level: str = "INFO"

    # LiteLLM (news extraction only; a dedicated virtual key, never the master key)
    litellm_base_url: str = "http://litellm:4000"
    litellm_key: str | None = field(default=None, repr=False)
    llm_key_source: str | None = None
    """Where ``litellm_key`` came from: ``env`` (``LITELLM_SIGNALS_KEY``, always wins), ``db`` (the key
    the bootstrap provisioned, :mod:`signal_hub.llm_key`) or ``None`` (no key: news stays pending)."""
    data_key: str | None = field(default=None, repr=False)
    """``SIGNALS_DATA_KEY``: secret for the at-rest encryption of ``hub_secret`` (falls back to
    ``SIGNAL_HUB_SHARED_SECRET``; :mod:`signal_hub.secretbox`)."""
    llm_key_alias: str = "keelson-signal-hub"
    llm_key_max_budget: float = 5.0
    """Small spend cap (USD, LiteLLM ``max_budget``) of the provisioned extraction key."""
    llm_key_budget_duration: str = "30d"
    llm_key_refresh_s: float = 300.0
    """The API re-reads the provisioned key this often (a bootstrap re-run may have rotated it)."""
    litellm_wait_s: float = 180.0
    """Bootstrap: how long to wait for LiteLLM readiness before skipping key provisioning."""
    extract_alias: str = "extract-fast"
    guard_alias: str = "guard-default"
    llm_timeout_s: float = 30.0
    extraction_batch: int = 8
    extraction_max_attempts: int = 8

    # outbound HTTP (feeds)
    outbound_proxy: str | None = None
    """``HTTPS_PROXY``/``HTTP_PROXY`` (compose: ``http://egress:3128``); feeds are fetched only
    through it when set. Internal calls (LiteLLM, demo feeds) never use it."""
    user_agent: str = DEFAULT_USER_AGENT
    http_connect_timeout_s: float = 10.0
    http_read_timeout_s: float = 30.0
    http_max_bytes: int = 12 * 1024 * 1024
    gdacs_url: str = DEFAULT_GDACS_URL
    gdacs_window_days: int = 7
    gdacs_fetch_geometry: bool = True
    gdacs_max_geometry_fetches: int = 6
    usgs_url: str = DEFAULT_USGS_URL
    rss_feeds: tuple[RssFeed, ...] = field(default_factory=tuple)
    rss_max_items: int = 40
    cadence_gdacs_s: int = 600
    cadence_usgs_s: int = 300
    cadence_rss_s: int = 900
    min_interval_s: float = 5.0
    """Per-source rate limit: at least this many seconds between two requests to one source."""
    enabled_adapters: tuple[str, ...] = ("gdacs", "usgs", "rss")

    # demo packs
    demo_mode: bool = False
    demo_tenants: tuple[str, ...] = ()
    """Tenants allowed to run demo packs (empty = any tenant while ``DEMO_MODE`` is on)."""
    scenarios_dir: Path = Path("/app/scenarios")
    demo_feeds_base_url: str = "http://127.0.0.1:8300/demo-feeds"
    demo_tick_s: float = 2.0

    # data
    geo_dir: Path = Path("/app/geo")

    # bootstrap (one-shot job only)
    admin_dsn: str | None = field(default=None, repr=False)
    signals_db_password: str | None = field(default=None, repr=False)
    signals_db_name: str = "signals"
    signals_role: str = "signals"
    postgis_version: str | None = None
    """Pinned PostGIS version to create (Cloud SQL: ``PIN_POSTGIS``); unset = the server default."""

    @property
    def auth_configured(self) -> bool:
        return bool(self.shared_secret)


def _default_geo_dir() -> Path:
    env = os.environ.get("GEO_DIR", "").strip()
    if env:
        return Path(env)
    image = Path("/app/geo")
    if image.is_dir():
        return image
    return Path(__file__).resolve().parents[4] / "data" / "geo"


def _default_scenarios_dir() -> Path:
    env = os.environ.get("SCENARIOS_DIR", "").strip()
    if env:
        return Path(env)
    image = Path("/app/scenarios")
    if image.is_dir():
        return image
    return Path(__file__).resolve().parents[4] / "data" / "scenarios"


def load_settings() -> Settings:
    port = _int("PORT", 8300)
    proxy = (os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
             or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "").strip()
    env_key = _optional_key(read_secret("LITELLM_SIGNALS_KEY"))
    adapters = tuple(a.strip() for a in os.environ.get("SIGNAL_ADAPTERS", "gdacs,usgs,rss").split(",") if a.strip())
    return Settings(
        port=port,
        database_url=read_secret("SIGNALS_DATABASE_URL") or "",
        shared_secret=read_secret("SIGNAL_HUB_SHARED_SECRET"),
        jwt_leeway_s=_int("SIGNAL_HUB_JWT_LEEWAY_S", 5),
        max_token_ttl_s=_int("SIGNAL_HUB_MAX_TOKEN_TTL_S", 900),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        litellm_base_url=os.environ.get("LITELLM_BASE_URL", "http://litellm:4000").rstrip("/"),
        litellm_key=env_key,
        llm_key_source="env" if env_key else None,
        data_key=read_secret("SIGNALS_DATA_KEY"),
        llm_key_alias=os.environ.get("SIGNALS_LLM_KEY_ALIAS", "keelson-signal-hub").strip() or "keelson-signal-hub",
        llm_key_max_budget=_float("SIGNALS_LLM_MAX_BUDGET", 5.0),
        llm_key_budget_duration=os.environ.get("SIGNALS_LLM_BUDGET_DURATION", "30d").strip() or "30d",
        llm_key_refresh_s=_float("SIGNALS_LLM_KEY_REFRESH_S", 300.0),
        litellm_wait_s=_float("SIGNALS_LITELLM_WAIT_S", 180.0),
        extract_alias=os.environ.get("SIGNALS_EXTRACT_ALIAS", "extract-fast"),
        guard_alias=os.environ.get("SIGNALS_GUARD_ALIAS", "guard-default"),
        llm_timeout_s=_float("SIGNALS_LLM_TIMEOUT_S", 30.0),
        extraction_batch=_int("SIGNALS_EXTRACTION_BATCH", 8),
        extraction_max_attempts=_int("SIGNALS_EXTRACTION_MAX_ATTEMPTS", 8),
        outbound_proxy=proxy or None,
        user_agent=os.environ.get("SIGNALS_USER_AGENT", DEFAULT_USER_AGENT),
        http_connect_timeout_s=_float("SIGNALS_HTTP_CONNECT_TIMEOUT_S", 10.0),
        http_read_timeout_s=_float("SIGNALS_HTTP_READ_TIMEOUT_S", 30.0),
        gdacs_url=os.environ.get("GDACS_URL", DEFAULT_GDACS_URL),
        gdacs_window_days=_int("GDACS_WINDOW_DAYS", 7),
        gdacs_fetch_geometry=_bool("GDACS_FETCH_GEOMETRY", True),
        usgs_url=os.environ.get("USGS_URL", DEFAULT_USGS_URL),
        rss_feeds=parse_rss_feeds(os.environ.get("RSS_FEEDS", DEFAULT_RSS_FEEDS)),
        rss_max_items=_int("RSS_MAX_ITEMS", 40),
        cadence_gdacs_s=_int("CADENCE_GDACS_S", 600),
        cadence_usgs_s=_int("CADENCE_USGS_S", 300),
        cadence_rss_s=_int("CADENCE_RSS_S", 900),
        min_interval_s=_float("SIGNALS_MIN_INTERVAL_S", 5.0),
        enabled_adapters=adapters,
        demo_mode=_bool("DEMO_MODE", False),
        demo_tenants=tuple(t.strip() for t in os.environ.get("DEMO_TENANTS", "").split(",") if t.strip()),
        scenarios_dir=_default_scenarios_dir(),
        demo_feeds_base_url=os.environ.get("DEMO_FEEDS_BASE_URL", f"http://127.0.0.1:{port}/demo-feeds").rstrip("/"),
        demo_tick_s=_float("DEMO_TICK_S", 2.0),
        geo_dir=_default_geo_dir(),
        admin_dsn=read_secret("SIGNALS_ADMIN_DSN"),
        signals_db_password=read_secret("SIGNALS_DB_PASSWORD"),
        signals_db_name=os.environ.get("SIGNALS_DB_NAME", "signals"),
        signals_role=os.environ.get("SIGNALS_DB_ROLE", "signals"),
        postgis_version=os.environ.get("SIGNALS_POSTGIS_VERSION", "").strip() or None,
    )
