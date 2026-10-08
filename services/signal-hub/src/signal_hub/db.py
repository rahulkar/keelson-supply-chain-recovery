"""asyncpg pool + tenant-scoped transactions (RLS).

Every query runs inside :func:`tx`, which sets ``app.tenant_id`` for the transaction
(``set_config(..., true)``):

* ``tx(pool, None)`` — the **system** context (``app.tenant_id = ''``): reads and writes only
  global rows (``tenant IS NULL``: feed/news signals and their events). Tenant rows are invisible.
* ``tx(pool, "<tenant>")`` — a tenant context: reads global rows plus that tenant's rows, writes
  only that tenant's rows. The RLS policies (``FORCE``, migration ``0001``) enforce both, so a
  missing ``WHERE tenant = …`` can never leak or corrupt another tenant's data.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, cast

import asyncpg

__all__ = ["create_pool", "dumps", "tx"]


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


async def _init_conn(conn: asyncpg.Connection) -> None:
    await conn.set_type_codec("jsonb", encoder=dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=dumps, decoder=json.loads, schema="pg_catalog")


async def create_pool(dsn: str, *, min_size: int = 1, max_size: int = 6) -> asyncpg.Pool:
    pool = await asyncpg.create_pool(
        dsn,
        min_size=min_size,
        max_size=max_size,
        init=_init_conn,
        command_timeout=60,
        server_settings={"application_name": "signal-hub"},
    )
    assert pool is not None
    return pool


@asynccontextmanager
async def tx(pool: asyncpg.Pool, tenant: str | None) -> AsyncIterator[asyncpg.Connection]:
    """A transaction in the tenant context (``None`` = system/global context)."""
    async with pool.acquire() as conn, conn.transaction():
        await conn.execute("SELECT set_config('app.tenant_id', $1, true)", tenant or "")
        yield cast(asyncpg.Connection, conn)
