"""Shared fixtures. Database tests (marker ``postgis``) need ``SIGNAL_HUB_TEST_ADMIN_DSN`` — a
PostGIS superuser DSN of a throwaway server (e.g. ``postgis/postgis:18-3.6``); they create the role
``signals`` and a fresh database ``signals_test`` with the real bootstrap + migrations."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import jwt
import pytest

from signal_hub.bootstrap import ensure_database
from signal_hub.config import Settings
from signal_hub.db import create_pool
from signal_hub.gazetteer import Gazetteer, load_gazetteer
from signal_hub.migrate import migrate

HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
SCENARIOS = FIXTURES / "scenarios"
REPO = HERE.parents[2]
GEO_DIR = Path(os.environ.get("GEO_DIR") or REPO / "data" / "geo")
SECRET = "test-secret-" + "s" * 40
# Overridable so parallel runs against one Postgres server do not drop each other's database.
TEST_DB = os.environ.get("SIGNAL_HUB_TEST_DB") or "signals_test"
TEST_PASSWORD = "signals-test-pw"

T0 = dt.datetime(2026, 10, 7, tzinfo=dt.UTC)


def load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


def make_token(tenant: str = "demo-retail", *, secret: str = SECRET, ttl: int = 120, **over: Any) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {"iss": "keelson-engine", "aud": "signal-hub", "tenant": tenant, "iat": now, "exp": now + ttl}
    claims.update(over)
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, secret, algorithm="HS256")


@pytest.fixture(scope="session")
def gaz() -> Gazetteer:
    return load_gazetteer(GEO_DIR)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        shared_secret=SECRET,
        geo_dir=GEO_DIR,
        scenarios_dir=SCENARIOS,
        demo_mode=True,
        demo_feeds_base_url="http://signal-hub-api:8300/demo-feeds",
        litellm_key=None,
    )


def _with_db(dsn: str, database: str, user: str | None = None, password: str | None = None) -> str:
    parts = urlsplit(dsn)
    netloc = parts.netloc
    if user is not None:
        host = netloc.rsplit("@", 1)[-1]
        netloc = f"{user}:{password}@{host}"
    return urlunsplit((parts.scheme, netloc, f"/{database}", parts.query, ""))


@pytest.fixture(scope="session")
async def db_dsn() -> str:
    admin = os.environ.get("SIGNAL_HUB_TEST_ADMIN_DSN")
    if not admin:
        pytest.skip("SIGNAL_HUB_TEST_ADMIN_DSN not set (PostGIS tests)")
    conn = await asyncpg.connect(admin)
    try:
        await conn.execute(f"DROP DATABASE IF EXISTS {TEST_DB} WITH (FORCE)")
    finally:
        await conn.close()
    await ensure_database(admin, role="signals", password=TEST_PASSWORD, database=TEST_DB)
    # idempotent: a second run changes nothing and does not fail
    await ensure_database(admin, role="signals", password=TEST_PASSWORD, database=TEST_DB)
    dsn = _with_db(admin, TEST_DB, "signals", TEST_PASSWORD)
    conn = await asyncpg.connect(dsn)
    try:
        await migrate(conn)
        assert await migrate(conn) == []
    finally:
        await conn.close()
    return dsn


@pytest.fixture(scope="session")
async def pool(db_dsn: str) -> AsyncIterator[asyncpg.Pool]:
    p = await create_pool(db_dsn, max_size=8)
    yield p
    await p.close()


TABLES = ("alert_change", "alert", "exposure", "event", "signal", "news_item", "network_node", "network_lane",
          "alert_rule_set", "demo_run", "tenant_registry", "source_cursor", "source_subscription", "source_schedule",
          "watch_region", "hub_secret")


@pytest.fixture
async def clean(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        await conn.execute(f"TRUNCATE {', '.join(TABLES)}")


SEA_ROUTE_SGN_HAM = None


def sea_route(origin: str, dest: str, via: str | None = None) -> list[list[float]]:
    import json

    data = json.loads((GEO_DIR / "sea_routes.json").read_text(encoding="utf-8"))
    for r in data["routes"]:
        if r["from"] == origin and r["to"] == dest and (via is None or r["via"] == via):
            return r["path"]
    raise KeyError((origin, dest, via))


def demo_network() -> dict[str, Any]:
    """A small tenant network around HCMC + North Europe (shape of what the engine pushes)."""
    return {
        "nodes": [
            {"type": "port", "code": "VNSGN", "name": "Ho Chi Minh City (Cat Lai)", "lat": 10.7626, "lon": 106.788},
            {"type": "factory", "code": "F-VN-01", "name": "Factory VN 1", "lat": 10.95, "lon": 106.70},
            {"type": "supplier", "code": "SUP-VN-01", "name": "Supplier VN 1", "lat": 10.85, "lon": 106.62},
            {"type": "port", "code": "DEHAM", "name": "Hamburg", "lat": 53.5395, "lon": 9.9663},
            {"type": "dc", "code": "DE1", "name": "DC Germany", "lat": 53.4, "lon": 10.2},
            {"type": "port", "code": "BDCGP", "name": "Chittagong", "lat": 22.3089, "lon": 91.8},
        ],
        "lanes": [
            {"code": "SGN-HAM-SEA", "mode": "sea", "path": sea_route("VNSGN", "DEHAM", "SUEZ")},
            {"code": "CGP-FXT-SEA", "mode": "sea", "path": sea_route("BDCGP", "GBFXT", "SUEZ")},
        ],
    }


Factory = Callable[..., Any]


@asynccontextmanager
async def running_app(application: Any) -> AsyncIterator[Any]:
    """Run the app lifespan (incl. the mounted MCP session manager) in one dedicated task:
    anyio cancel scopes must be entered and exited by the same task."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def runner() -> None:
        async with application.router.lifespan_context(application):
            started.set()
            await stop.wait()

    task = asyncio.create_task(runner())
    await asyncio.wait({task, asyncio.create_task(started.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()
    try:
        yield application
    finally:
        stop.set()
        await task
