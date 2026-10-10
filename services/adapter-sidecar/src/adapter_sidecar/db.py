"""SQLAlchemy async engines for the state store and SQL backends.

SQLite (``sqlite+aiosqlite``) connections run in WAL mode with ``foreign_keys`` on and a busy
timeout. pysqlite's implicit transaction handling is switched off and the sidecar emits its own
``BEGIN`` (SQLAlchemy's documented recipe): a read transaction is then a real snapshot across
several statements, and a write transaction can take the database write lock up front with
``BEGIN IMMEDIATE`` (``begin_write``) instead of failing later on a lock upgrade.

Postgres (``postgresql+asyncpg``) uses ``REPEATABLE READ`` + ``READ ONLY`` for snapshots and
``SELECT … FOR UPDATE`` for the rows a change set touches.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

_BEGIN_OPTION = "sidecar_sqlite_begin"


def is_sqlite(engine_or_conn: AsyncEngine | AsyncConnection) -> bool:
    return engine_or_conn.dialect.name == "sqlite"


def create_engine(url: str) -> AsyncEngine:
    """An async engine for ``url`` (``sqlite+aiosqlite`` or ``postgresql+asyncpg``)."""
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite"):
        if parsed.drivername != "sqlite+aiosqlite":
            raise ValueError("SQLite URLs must use the sqlite+aiosqlite driver")
        engine = create_async_engine(url, poolclass=NullPool)

        @event.listens_for(engine.sync_engine, "connect")
        def _connect(dbapi_conn: Any, _record: Any) -> None:
            dbapi_conn.isolation_level = None  # we emit BEGIN ourselves (see module docstring)
            cur = dbapi_conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.execute("PRAGMA busy_timeout=30000")
            cur.execute("PRAGMA synchronous=FULL")
            cur.close()

        @event.listens_for(engine.sync_engine, "begin")
        def _begin(conn: Any) -> None:
            mode = conn.get_execution_options().get(_BEGIN_OPTION, "DEFERRED")
            conn.exec_driver_sql(f"BEGIN {mode}")

        return engine
    if parsed.drivername.startswith("postgresql"):
        if parsed.drivername != "postgresql+asyncpg":
            raise ValueError("Postgres URLs must use the postgresql+asyncpg driver")
        return create_async_engine(url, pool_size=5, max_overflow=5, pool_pre_ping=True)
    raise ValueError(f"unsupported database driver {parsed.drivername!r}")


@asynccontextmanager
async def begin_write(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A write transaction (SQLite: ``BEGIN IMMEDIATE``). Commits on success, rolls back on error."""
    async with engine.connect() as conn:
        if is_sqlite(engine):
            conn = await conn.execution_options(**{_BEGIN_OPTION: "IMMEDIATE"})
        async with conn.begin():
            yield conn


@asynccontextmanager
async def begin_read(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """A consistent read-only snapshot transaction (Postgres: ``REPEATABLE READ``, ``READ ONLY``;
    SQLite: a deferred ``BEGIN``, which in WAL mode reads one snapshot). Always rolled back."""
    async with engine.connect() as conn:
        if not is_sqlite(engine):
            conn = await conn.execution_options(isolation_level="REPEATABLE READ", postgresql_readonly=True)
        trans = await conn.begin()
        try:
            yield conn
        finally:
            await trans.rollback()


def quote_ident(conn: AsyncConnection | AsyncEngine, name: str) -> str:
    """Always-quoted identifier (``schema.table`` → each part quoted). Names are validated by the
    config (``[A-Za-z_][A-Za-z0-9_]*``) before they ever reach SQL."""
    prep = conn.dialect.identifier_preparer
    return ".".join(prep.quote_identifier(part) for part in name.split("."))


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def iso(value: dt.datetime | None) -> str | None:
    return value.astimezone(dt.UTC).isoformat().replace("+00:00", "Z") if value is not None else None


def jdump(value: Any) -> str | None:
    """Compact JSON text for a state column (``None`` stays NULL)."""
    if value is None:
        return None
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=True, default=_json_default)


def jload(value: Any) -> Any:
    if value is None or not isinstance(value, str | bytes):
        return value
    return json.loads(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, dt.datetime):
        return iso(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    raise TypeError(f"{type(value).__name__} is not JSON serializable")


def jsonable(value: Any) -> Any:
    """Row values as JSON (dates → ISO strings, Decimals refused, tuples → lists)."""
    if isinstance(value, dt.datetime):
        return iso(value)
    if isinstance(value, dt.date):
        return value.isoformat()
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [jsonable(v) for v in value]
    return value
