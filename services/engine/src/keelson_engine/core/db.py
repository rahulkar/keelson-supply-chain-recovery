"""Async Postgres access for the ``keelson`` database (SQLAlchemy 2.1 + asyncpg).

Row-level security model (migration ``0001``):

* every tenant table has ``ENABLE`` + ``FORCE ROW LEVEL SECURITY`` and the policy
  ``tenant = current_setting('app.tenant_id', true) OR current_setting('app.system', true) = 'on'``;
* :func:`tenant_session` opens a transaction and runs ``set_config('app.tenant_id', <tenant>, true)``
  (transaction-local, i.e. ``SET LOCAL``) — every statement in it sees only that tenant;
* :func:`system_session` sets ``app.system = 'on'`` for the transaction — used only for
  infrastructure queries that are cross-tenant by nature (runs queue + leases, reaper,
  bootstrap, BFF/console session tables).

Why not a separate ``keelson_system`` role with ``BYPASSRLS`` (the 55-engine-internals
wording)? pg-core's init only creates the login role ``keelson`` (``NOCREATEROLE``,
``NOBYPASSRLS``), so the engine cannot create a second role itself. Both session kinds
therefore use the same role; ``FORCE`` makes the policies apply to the table owner too,
and the ``app.system`` switch is only ever set by this module. Because the GUC is set with
``is_local = true`` it is reset at commit/rollback and can never leak to the next user of
a pooled connection.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from keelson_engine.config import Settings, get_settings

__all__ = [
    "close_db",
    "init_db",
    "keelson_engine",
    "system_session",
    "tenant_session",
]

_TENANT_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")

_engine: AsyncEngine | None = None
_sessions: async_sessionmaker[AsyncSession] | None = None


def _make_engine(settings: Settings) -> AsyncEngine:
    return create_async_engine(
        settings.keelson_database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,
        pool_recycle=1800,
        connect_args={"server_settings": {"application_name": "keelson-engine"}},
    )


async def init_db(settings: Settings | None = None) -> None:
    """Create the engine for the ``keelson`` DB (idempotent). The ADK DB is owned by the
    ADK ``DatabaseSessionService`` (runtime), not by this module."""
    global _engine, _sessions
    if _engine is not None:
        return
    _engine = _make_engine(settings or get_settings())
    _sessions = async_sessionmaker(_engine, expire_on_commit=False, autoflush=False)


async def close_db() -> None:
    global _engine, _sessions
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessions = None


def keelson_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("init_db() has not been awaited")
    return _engine


def _sessionmaker() -> async_sessionmaker[AsyncSession]:
    if _sessions is None:
        raise RuntimeError("init_db() has not been awaited")
    return _sessions


def check_tenant_slug(tenant: str) -> str:
    if not isinstance(tenant, str) or not _TENANT_RE.match(tenant):
        raise ValueError("invalid tenant slug")
    return tenant


@asynccontextmanager
async def tenant_session(tenant: str, *, snapshot: bool = False) -> AsyncIterator[AsyncSession]:
    """``BEGIN; SET LOCAL app.tenant_id = :tenant`` — RLS restricts every statement to
    ``tenant``. Commits on normal exit, rolls back on exception.

    ``snapshot=True``: ``REPEATABLE READ READ ONLY`` — every statement of the transaction sees
    the same snapshot (taken at its first statement), for multi-table reads that must be
    mutually consistent (``GET /api/cases/{id}``)."""
    check_tenant_slug(tenant)
    async with _sessionmaker()() as session:
        async with session.begin():
            if snapshot:
                await session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
            await session.execute(text("SELECT set_config('app.tenant_id', :t, true)"), {"t": tenant})
            yield session


@asynccontextmanager
async def system_session() -> AsyncIterator[AsyncSession]:
    """Cross-tenant infrastructure transaction (``SET LOCAL app.system = 'on'``)."""
    async with _sessionmaker()() as session:
        async with session.begin():
            await session.execute(text("SELECT set_config('app.system', 'on', true)"))
            yield session
