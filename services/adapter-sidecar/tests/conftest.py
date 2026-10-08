"""Fixtures: the Fernhill demo sidecar on a temporary copy of its golden CSVs, served in-process
(FastAPI lifespan + FastMCP over ASGI), a fake Keelson receipt issuer (ES256 JWKS over an httpx
``MockTransport``) and MCP clients. No network is used."""

from __future__ import annotations

import asyncio
import csv
import json
import os
import shutil
import sys
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import httpx2
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from jwt.algorithms import ECAlgorithm
from keelson_contracts.receipts import ApprovalReceiptClaims, approved_by_policy, approved_by_user, receipt_header

SERVICE = Path(__file__).resolve().parents[1]
FERNHILL = SERVICE / "demo" / "fernhill"
GOLDEN = FERNHILL / "golden"
TESTS = Path(__file__).resolve().parent
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

PUBLIC = "http://fernhill.localhost"
RESOURCE = f"{PUBLIC}/mcp"
API_KEY = "fh_test_api_key_0123456789abcdefghijklmn"
ADMIN_KEY = "admin_test_key_0123456789abcdefghijklmno"
SNAPSHOT_SECRET = "snapshot_test_secret_0123456789abcdefghij"
KEELSON_ISS = "http://keelson.localhost"
KEELSON_JWKS = "http://keelson.localhost/api/.well-known/jwks.json"
TENANT = "northwind-test"
EXT_SUB = "connector:fernhill"


@dataclass
class FakeKeelson:
    """Signs approval receipts like the engine (``ApprovalReceiptClaims.issue`` + ES256, kid ``ar-1``)
    and serves the JWKS through an httpx ``MockTransport``."""

    key: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    jwks_status: int = 200
    jwks_fetches: int = 0

    def jwks(self) -> dict[str, Any]:
        jwk = json.loads(ECAlgorithm.to_jwk(self.key.public_key()))
        jwk.update({"kid": "ar-1", "use": "sig", "alg": "ES256"})
        return {"keys": [jwk]}

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == KEELSON_JWKS:
                self.jwks_fetches += 1
                if self.jwks_status != 200:
                    return httpx.Response(self.jwks_status, json={"error": "down"})
                return httpx.Response(200, json=self.jwks())
            return httpx.Response(404)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def receipt(self, *, change_set_ref: str, change_set_hash: str, approval_id: str, policy: bool = True,
                aud: str = RESOURCE, ext_iss: str = PUBLIC, tenant: str = TENANT, ext_sub: str = EXT_SUB,
                header: dict[str, Any] | None = None, co_approved_by: tuple[str, ...] = (),
                claims_update: dict[str, Any] | None = None, ttl_s: int = 300, now: int | None = None,
                key: Any = None) -> str:
        claims = ApprovalReceiptClaims.issue(
            approval_id=approval_id, change_set_ref=change_set_ref, change_set_hash=change_set_hash, tenant=tenant,
            keelson_sub="sub-nora", ext_sub=ext_sub,
            approved_by=approved_by_policy("auto-small-transfer", "p-1") if policy else approved_by_user("marco"),
            approver_name=None if policy else "Marco Rossi", initiated_by="Nora Test", policy_version="p-1",
            co_approved_by=co_approved_by, aud=aud, ext_iss=ext_iss, ttl_s=ttl_s, now=now,
        ).to_claims()
        claims.update(claims_update or {})
        return jwt.encode(claims, key or self.key, algorithm="ES256", headers=header or receipt_header())


def sidecar_env(tmp: Path, data_dir: Path | None, config: Path | None = None, **extra: str) -> dict[str, str]:
    env = {
        "SIDECAR_CONFIG": str(config or FERNHILL / "sidecar.yaml"),
        "PUBLIC_URL": PUBLIC,
        "STATE_DB_URL": f"sqlite+aiosqlite:///{tmp / 'state' / 'sidecar.db'}",
        "SNAPSHOT_DIR": str(tmp / "snapshots"),
        "SIDECAR_API_KEY": API_KEY,
        "SIDECAR_ADMIN_KEY": ADMIN_KEY,
        "SIDECAR_SNAPSHOT_SECRET": SNAPSHOT_SECRET,
        "KEELSON_JWKS_URL": KEELSON_JWKS,
        "TRUSTED_RECEIPT_ISSUERS": KEELSON_ISS,
        "DEMO_MODE": "1",
        "DEMO_GOLDEN_DIR": str(GOLDEN),
    }
    if data_dir is not None:
        env["DATA_DIR"] = str(data_dir)
    env.update(extra)
    return env


@pytest.fixture
def data_dir(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    for f in GOLDEN.glob("*.csv"):
        shutil.copyfile(f, d / f.name)
    return d


@pytest.fixture
def keelson() -> FakeKeelson:
    return FakeKeelson()


@dataclass
class Running:
    app: Any
    sidecar: Any
    data_dir: Path | None
    tmp: Path

    def http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=PUBLIC)

    def client(self, api_key: str | None = API_KEY, *, token: str | None = None,
               headers: dict[str, str] | None = None) -> Client:
        def factory(**kwargs: Any) -> httpx2.AsyncClient:
            kwargs.pop("follow_redirects", None)
            hdrs = dict(kwargs.pop("headers", None) or {})
            if api_key is not None:
                hdrs["X-Adapter-Key"] = api_key
            hdrs.update(headers or {})
            return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), follow_redirects=True,
                                      headers=hdrs, **kwargs)

        return Client(StreamableHttpTransport(RESOURCE, auth=token, httpx_client_factory=factory))


async def start_app(settings: Any, sidecar: Any, *, auth_http_client: Any = None) -> tuple[Any, Callable[[], Any]]:
    """Run the FastAPI lifespan in one dedicated task (anyio cancel scopes: same task)."""
    from adapter_sidecar.app import create_app

    app = create_app(settings, sidecar, auth_http_client=auth_http_client)
    started, stop = asyncio.Event(), asyncio.Event()

    async def runner() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(runner())
    waiter = asyncio.create_task(started.wait())
    await asyncio.wait({task, waiter}, return_when=asyncio.FIRST_COMPLETED)
    if task.done():
        waiter.cancel()
        task.result()

    async def shutdown() -> None:
        stop.set()
        await task

    return app, shutdown


@pytest.fixture
async def fernhill(tmp_path: Path, data_dir: Path, keelson: FakeKeelson) -> AsyncIterator[Running]:
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    settings = Settings.from_env(sidecar_env(tmp_path, data_dir))
    sidecar = Sidecar(settings, http_client=keelson.client())
    app, shutdown = await start_app(settings, sidecar)
    try:
        yield Running(app, sidecar, data_dir, tmp_path)
    finally:
        await shutdown()


def key() -> str:
    return f"test-{uuid.uuid4().hex}"


# ───────────────────────────── SQL backends ─────────────────────────────

SQL_CONFIG = SERVICE / "examples" / "sql-postgres.yaml"
SQL_SCHEMA = SERVICE / "examples" / "sql-schema.sql"
PG_ADMIN_DSN = os.environ.get("SIDECAR_TEST_PG_DSN")
PG_DB = os.environ.get("SIDECAR_TEST_PG_DB", "sidecar_test")


async def load_sql_fixture(url: str) -> None:
    """Create ``examples/sql-schema.sql`` and load the Fernhill golden rows into ``url``."""
    import datetime as dt

    from sqlalchemy import text as sql_text

    from adapter_sidecar.backends.staging import parse_cell
    from adapter_sidecar.config import load_config
    from adapter_sidecar.db import create_engine, quote_ident

    cfg = load_config(FERNHILL / "sidecar.yaml")
    engine = create_engine(url)
    pg = engine.dialect.name == "postgresql"
    try:
        async with engine.begin() as conn:
            ddl = "\n".join(ln for ln in SQL_SCHEMA.read_text(encoding="utf-8").splitlines()
                            if not ln.strip().startswith("--"))
            for stmt in ddl.split(";"):
                if stmt.strip():
                    await conn.exec_driver_sql(stmt.strip())
            for name, table in cfg.tables.items():
                types = table.column_types
                with (GOLDEN / str(table.file)).open(encoding="utf-8", newline="") as fh:
                    rows = list(csv.DictReader(fh))
                params = []
                for r in rows:
                    values = {}
                    for col, kind in types.items():
                        v = parse_cell(kind, r[col])
                        if pg and kind == "date" and v is not None:
                            v = dt.date.fromisoformat(v)
                        values[col] = v
                    params.append(values)
                cols = list(types)
                stmt_text = (f"INSERT INTO {quote_ident(conn, name)} ({', '.join(quote_ident(conn, c) for c in cols)}) "
                             f"VALUES ({', '.join(':' + c for c in cols)})")
                if params:
                    await conn.execute(sql_text(stmt_text), params)
    finally:
        await engine.dispose()


async def fresh_pg_database() -> str | None:
    """Drop + create ``SIDECAR_TEST_PG_DB`` on ``SIDECAR_TEST_PG_DSN``; its asyncpg URL (``None`` = skip)."""
    if not PG_ADMIN_DSN:
        return None
    import asyncpg

    try:
        conn = await asyncpg.connect(PG_ADMIN_DSN, timeout=5)
    except (OSError, asyncpg.PostgresError):
        return None
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{PG_DB}" WITH (FORCE)')
        await conn.execute(f'CREATE DATABASE "{PG_DB}"')
    finally:
        await conn.close()
    base = PG_ADMIN_DSN.rsplit("/", 1)[0].replace("postgresql://", "postgresql+asyncpg://", 1)
    return f"{base}/{PG_DB}"


@pytest.fixture(params=["csv", "sql-sqlite", "sql-postgres"])
async def any_backend(request: pytest.FixtureRequest, tmp_path: Path, keelson: FakeKeelson) -> AsyncIterator[Running]:
    """The Fernhill data behind each writable backend: CSV files, a SQLite database, Postgres."""
    from adapter_sidecar.service import Sidecar
    from adapter_sidecar.settings import Settings

    kind = request.param
    data: Path | None = None
    extra: dict[str, str] = {}
    config: Path | None = None
    if kind == "csv":
        data = tmp_path / "data"
        data.mkdir()
        for f in GOLDEN.glob("*.csv"):
            shutil.copyfile(f, data / f.name)
    else:
        if kind == "sql-sqlite":
            url = f"sqlite+aiosqlite:///{tmp_path / 'erp.db'}"
        else:
            maybe = await fresh_pg_database()
            if maybe is None:
                pytest.skip("SIDECAR_TEST_PG_DSN is not set / reachable")
            url = maybe
        await load_sql_fixture(url)
        extra["ERP_DATABASE_URL"] = url
        config = SQL_CONFIG
    settings = Settings.from_env(sidecar_env(tmp_path, data, config, **extra))
    sidecar = Sidecar(settings, http_client=keelson.client())
    app, shutdown = await start_app(settings, sidecar)
    try:
        yield Running(app, sidecar, data, tmp_path)
    finally:
        await shutdown()


@pytest.fixture
def anyio_backend() -> Iterator[str]:
    yield "asyncio"
