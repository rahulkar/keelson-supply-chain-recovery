"""Last-seen tracking of OAuth clients (``azp``) for the "API & MCP Clients" page.

Updated at most once a minute per client, off the request path (fire-and-forget task).
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncEngine

from merxa_api import tables as t
from merxa_api.logs import get_logger
from merxa_api.security.principal import Principal
from merxa_api.services.common import row_dict

log = get_logger(__name__)
TOUCH_INTERVAL_S = 60.0


class ClientsSeen:
    def __init__(self, engine: AsyncEngine) -> None:
        self.engine = engine
        self._last: dict[str, float] = {}
        self._tasks: set[asyncio.Task[Any]] = set()

    def touch_soon(self, principal: Principal, surface: str) -> None:
        cid = principal.client_id
        if not cid or principal.kind != "bearer":
            return
        now = time.monotonic()
        if now - self._last.get(cid, float("-inf")) < TOUCH_INTERVAL_S:
            return
        self._last[cid] = now
        try:
            task = asyncio.get_running_loop().create_task(self._upsert(cid, principal, surface))
        except RuntimeError:  # pragma: no cover - no loop
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _upsert(self, cid: str, principal: Principal, surface: str) -> None:
        tbl = t.oauth_client_seen
        stmt = insert(tbl).values(
            client_id=cid, last_sub=principal.sub, last_username=principal.username, surface=surface
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[tbl.c.client_id],
            set_={
                "last_seen_at": stmt.excluded.last_seen_at,
                "last_sub": stmt.excluded.last_sub,
                "last_username": stmt.excluded.last_username,
                "surface": stmt.excluded.surface,
            },
        )
        try:
            async with self.engine.begin() as conn:
                await conn.execute(stmt)
        except Exception:
            log.warning("could not record oauth client last-seen", extra={"client_id": cid})

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def list(self) -> dict[str, Any]:
        tbl = t.oauth_client_seen
        async with self.engine.connect() as conn:
            rows = await conn.execute(select(tbl).order_by(tbl.c.last_seen_at.desc(), tbl.c.client_id))
            return {"items": [row_dict(r) for r in rows]}
