"""Back-office test helpers: spec/connector resets."""

from __future__ import annotations

from sqlalchemy import text

from e1_env import TENANT_A, TENANT_B
from keelson_engine.core.db import system_session


async def reset_specs(*tenants: str) -> None:
    async with system_session() as s:
        await s.execute(text("DELETE FROM agent_spec WHERE tenant = ANY(:t)"),
                        {"t": list(tenants or (TENANT_A, TENANT_B))})


async def drop_connector(tenant: str, slug: str) -> None:
    async with system_session() as s:
        await s.execute(text("DELETE FROM connector WHERE tenant = :t AND slug = :s"), {"t": tenant, "s": slug})
