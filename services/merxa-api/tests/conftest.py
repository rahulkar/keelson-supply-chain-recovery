"""Shared fixtures. Requires a throwaway Postgres 18 reachable via ``MERXA_TEST_ADMIN_DSN``
(e.g. ``postgresql://postgres:test@merxa-test-pg:5432/postgres``); see README › Tests.

Each test session creates its own database ``merxa_t_<hex>`` and drops it afterwards.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

import asyncpg
import httpx
import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from sqlalchemy.ext.asyncio import AsyncEngine

from merxa_api.app import create_app
from merxa_api.config import Settings
from merxa_api.db import create_engine
from merxa_api.seed.loader import seed
from merxa_api.state import AppState
from support import (
    ISSUER,
    KEELSON_ISS,
    KEELSON_JWKS,
    RESOURCE,
    WEBHOOK_SECRET,
    WEBHOOK_URL,
    FakeKeelson,
    FakeRealm,
    FakeReceiver,
    httpx2_client,
    httpx_client,
)

ADMIN_DSN = os.environ.get("MERXA_TEST_ADMIN_DSN", "postgresql://postgres:test@localhost:5432/postgres")


def _db_url(dsn: str, name: str) -> str:
    base = dsn.rsplit("/", 1)[0]
    return base.replace("postgresql://", "postgresql+asyncpg://", 1) + f"/{name}"


async def create_database() -> tuple[str, str]:
    name = f"merxa_t_{uuid.uuid4().hex[:12]}"
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    return name, _db_url(ADMIN_DSN, name)


async def drop_database(name: str) -> None:
    conn = await asyncpg.connect(ADMIN_DSN)
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture(scope="session")
def realm() -> FakeRealm:
    r = FakeRealm()
    r.users = {
        "priya.shah": {"sub": "priya-merxa-sub", "password": "pw", "roles": ["merch-planner"], "name": "Priya Shah"},
        "marco.rossi": {"sub": "marco-merxa-sub", "password": "pw", "roles": ["ops-director"], "name": "Marco Rossi"},
        "merxa.admin": {"sub": "admin-merxa-sub", "password": "pw", "roles": ["merxa-admin"], "name": "Merxa Admin"},
    }
    return r


@pytest.fixture(scope="session")
def keelson() -> FakeKeelson:
    return FakeKeelson()


@pytest.fixture(scope="session")
def receiver() -> FakeReceiver:
    return FakeReceiver()


@pytest.fixture(scope="session")
async def database() -> AsyncIterator[str]:
    name, url = await create_database()
    try:
        yield url
    finally:
        await drop_database(name)


@pytest.fixture(scope="session")
def settings(database: str, tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        database_url=database,
        public_url="http://merxa.localhost",
        oidc_issuer=ISSUER,
        mcp_resource=RESOURCE,
        trusted_receipt_issuers=[KEELSON_ISS],
        keelson_jwks_url=KEELSON_JWKS,
        web_client_secret="test-web-secret",
        demo_client_secret="test-demo-secret",
        demo_user_password="pw",
        session_secret="test-session-secret",
        demo_mode=True,
        seed_profile="small",
        seed_on_startup=True,
        snapshot_dir=str(tmp_path_factory.mktemp("snapshots")),
        snapshot_secret="test-snapshot-secret",
        keelson_webhook_url=WEBHOOK_URL,
        webhook_secret=WEBHOOK_SECRET,
        webhook_worker=False,  # tests drive the worker with run_once()
    )


@pytest.fixture(scope="session")
async def state(
    settings: Settings, realm: FakeRealm, keelson: FakeKeelson, receiver: FakeReceiver
) -> AsyncIterator[AppState]:
    st = AppState.build(
        settings,
        idp_http_client=httpx2_client(realm, keelson),
        http_client=httpx_client(realm, keelson),
        webhook_http_client=receiver.client(),
    )
    yield st


@pytest.fixture(scope="session")
async def app(state: AppState) -> AsyncIterator[Any]:
    """Run the FastAPI lifespan (incl. the mounted MCP session manager) in one dedicated task:
    anyio cancel scopes must be entered and exited by the same task."""
    application = create_app(state=state)
    started, stop = asyncio.Event(), asyncio.Event()

    async def runner() -> None:
        async with application.router.lifespan_context(application):
            started.set()
            await stop.wait()

    task = asyncio.create_task(runner())
    await asyncio.wait({task, asyncio.create_task(started.wait())}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        task.result()  # surface startup errors
    try:
        yield application
    finally:
        stop.set()
        await task


@pytest.fixture(scope="session")
def engine(app: Any, state: AppState) -> AsyncEngine:
    return state.engine


@pytest.fixture
async def http(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://merxa.localhost") as c:
        yield c


@pytest.fixture
async def reseed(engine: AsyncEngine, settings: Settings) -> None:
    """Restore the golden seed (domain tables only; the audit log is append-only)."""
    await seed(engine, settings.seed_profile, reset_first=True)


@pytest.fixture
def mcp_client(app: Any) -> Callable[[str | None], Client]:
    def factory(token: str | None) -> Client:
        def http_factory(**kwargs: Any) -> httpx2.AsyncClient:
            kwargs.pop("follow_redirects", None)
            return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), follow_redirects=True, **kwargs)

        transport = StreamableHttpTransport(RESOURCE, auth=token, httpx_client_factory=http_factory)
        return Client(transport)

    return factory


LITE_URL = "http://merxa.localhost/mcp-lite"


@pytest.fixture
def lite_client(app: Any) -> Callable[..., Client]:
    """``/mcp-lite`` client: ``lite_client(token=...)`` (Bearer) or ``lite_client(api_key=...)``."""

    def factory(token: str | None = None, api_key: str | None = None) -> Client:
        def http_factory(**kwargs: Any) -> httpx2.AsyncClient:
            kwargs.pop("follow_redirects", None)
            headers = dict(kwargs.pop("headers", None) or {})
            if api_key is not None:
                headers["X-Merxa-Api-Key"] = api_key
            return httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), follow_redirects=True, headers=headers, **kwargs
            )

        transport = StreamableHttpTransport(LITE_URL, auth=token, httpx_client_factory=http_factory)
        return Client(transport)

    return factory


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def fresh_engine(url: str) -> AsyncEngine:
    return create_engine(url, pool_size=2)
