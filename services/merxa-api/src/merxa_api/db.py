"""Async database engine helpers (SQLAlchemy 2.1 + asyncpg)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine


def create_engine(database_url: str, *, pool_size: int = 5) -> AsyncEngine:
    """Create the shared async engine. ``search_path`` pins every session to ``merxa``."""
    return create_async_engine(
        database_url,
        pool_size=pool_size,
        max_overflow=5,
        pool_pre_ping=True,
        connect_args={"server_settings": {"search_path": "merxa,public", "application_name": "merxa-api"}},
    )


@asynccontextmanager
async def transaction(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Open a connection with a transaction that commits on success, rolls back on error."""
    async with engine.begin() as conn:
        yield conn


async def ping(engine: AsyncEngine) -> bool:
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True
    except Exception:
        return False
