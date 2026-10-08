"""Raw staging of tier-A crawls (``docs/contracts/121-m8-mapping-compiler.md`` §2.4).

The DuckDB crawl writes every page it fetches to ``scn_raw_page`` (one row per page: the items as
``jsonb``, one short transaction per :data:`FLUSH_PAGES` pages) under a ``scn_raw_set`` row keyed by
the crawl attempt (the ``scn_snapshot`` id). The set records the sources (key, tool, pages, items,
truncation), the size and when the pages were fetched; ``status`` is ``staging`` while fetching,
``complete`` once every source was read (even if the mapping then failed) and ``failed`` when the
fetch itself stopped.

What it is for: a **staged preview** maps an edited read mapping against the last crawl's full
data without calling the vendor (also while the connector is disabled), and an admin can fix a
``mapping_failed`` crawl against exactly the data that failed.

Retention (bounded by design): a crawl start deletes every set of the connector except the current
snapshot's; a successful flip deletes every set except the new current one. So at most the current
set, the latest failed attempt's set and the running one exist. Sets also go with their snapshot
(GC, the failed-history cap) and with the connector (``ON DELETE CASCADE``); a crashed attempt's
set is dropped with the orphan and a demo reset drops every non-current set eagerly.
``SCN_RAW_STAGING=0`` keeps raw data only inside the crawl's DuckDB session. Every table is RLS
``FORCE``-d like the rest of the tenant data.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Any, Final

from sqlalchemy import text

from keelson_contracts.common import sha256_hex
from keelson_contracts.mapping import ReadSource
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.sql import dumps

__all__ = [
    "FLUSH_PAGES",
    "StagingWriter",
    "begin",
    "drop_noncurrent",
    "drop_sets",
    "fetch_key",
    "keep_only",
    "latest_set",
    "read_pages",
]

log = get_logger(__name__)

FLUSH_PAGES: Final = 50
"""Pages per insert transaction."""
FLUSH_BYTES: Final = 8 * 1024 * 1024
"""…or fewer pages when they are large."""


def fetch_key(src: ReadSource) -> str:
    """The identity of a read regardless of its page cap (``max_pages``): a staged preview of a
    mapping whose only change is a source's ``max_pages`` still finds its pages."""
    return sha256_hex({k: v for k, v in src.model_dump(mode="json").items() if k != "max_pages"})[:16]


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


class StagingWriter:
    """Buffers fetched pages and inserts them in short transactions."""

    def __init__(self, tenant: str, sid: uuid.UUID) -> None:
        self.tenant = tenant
        self.sid = sid
        self._buffer: list[dict[str, Any]] = []
        self._buffered_bytes = 0
        self.pages = 0
        self.items = 0
        self.bytes = 0

    async def add(self, key: str, page_no: int, item_count: int, items_json: str) -> None:
        size = len(items_json.encode("utf-8"))
        self._buffer.append({"t": self.tenant, "s": self.sid, "k": key, "p": page_no, "n": item_count, "b": size,
                             "i": items_json})
        self._buffered_bytes += size
        self.pages += 1
        self.items += item_count
        self.bytes += size
        if len(self._buffer) >= FLUSH_PAGES or self._buffered_bytes >= FLUSH_BYTES:
            await self.flush()

    async def flush(self) -> None:
        if not self._buffer:
            return
        from sqlalchemy.exc import DataError

        from keelson_engine.core.scn_crawl import CrawlError

        rows, self._buffer, self._buffered_bytes = self._buffer, [], 0
        try:
            async with tenant_session(self.tenant) as s:
                await s.execute(text(
                    "INSERT INTO scn_raw_page (tenant, snapshot_id, source_key, page_no, item_count, bytes, items) "
                    "VALUES (:t, :s, :k, :p, :n, :b, CAST(:i AS jsonb))"), rows)
        except DataError as exc:  # e.g. NaN / NUL in a vendor value: not storable as Postgres JSON
            raise CrawlError("items_invalid", f"a fetched page cannot be stored as JSON ({type(exc.orig).__name__})") from None

    async def finish(self, status: str, *, sources: Sequence[dict[str, Any]], fetched_from: dt.datetime | None,
                     fetched_to: dt.datetime | None) -> None:
        """Flush what is left and record the set's outcome (``complete`` | ``failed``)."""
        if status == "complete":
            await self.flush()
        else:
            self._buffer.clear()
        async with tenant_session(self.tenant) as s:
            await s.execute(text(
                "UPDATE scn_raw_set SET status = :st, sources = CAST(:src AS jsonb), pages = :p, items = :n, bytes = :b, "
                "fetched_from = :f, fetched_to = :to WHERE tenant = :t AND snapshot_id = :s"),
                {"st": status, "src": dumps(list(sources)), "p": self.pages, "n": self.items, "b": self.bytes,
                 "f": fetched_from, "to": fetched_to, "t": self.tenant, "s": self.sid})


async def begin(tenant: str, connector: str, sid: uuid.UUID, *, mapping_version: int | None,
                mapping_hash: str | None, enabled: bool) -> StagingWriter | None:
    """Start the raw set of crawl attempt ``sid`` (``None`` with staging off). Drops every other set
    of the connector except the current snapshot's (a previous failed attempt included)."""
    async with tenant_session(tenant) as s:
        await s.execute(text(
            "DELETE FROM scn_raw_set WHERE tenant = :t AND connector = :c AND snapshot_id IS DISTINCT FROM "
            "(SELECT snapshot_id FROM scn_current WHERE tenant = :t AND connector = :c)"), {"t": tenant, "c": connector})
        if not enabled:
            return None
        await s.execute(text(
            "INSERT INTO scn_raw_set (tenant, snapshot_id, connector, status, mapping_version, mapping_hash) "
            "VALUES (:t, :s, :c, 'staging', :v, :h)"),
            {"t": tenant, "s": sid, "c": connector, "v": mapping_version, "h": mapping_hash})
    return StagingWriter(tenant, sid)


async def keep_only(tenant: str, connector: str, sid: uuid.UUID | str) -> None:
    """After a successful flip: every set of the connector but ``sid``'s is deleted."""
    async with tenant_session(tenant) as s:
        await s.execute(text("DELETE FROM scn_raw_set WHERE tenant = :t AND connector = :c AND snapshot_id <> :s"),
                        {"t": tenant, "c": connector, "s": sid if isinstance(sid, uuid.UUID) else uuid.UUID(sid)})


async def drop_sets(tenant: str, snapshot_ids: Sequence[Any]) -> None:
    """Delete the raw sets of these attempts (orphaned crawls)."""
    if not snapshot_ids:
        return
    async with tenant_session(tenant) as s:
        await s.execute(text("DELETE FROM scn_raw_set WHERE tenant = :t AND snapshot_id = ANY(:ids)"),
                        {"t": tenant, "ids": list(snapshot_ids)})


async def drop_noncurrent(tenant: str) -> int:
    """Demo reset: delete every finished set that is not a current snapshot's (a running crawl's set stays)."""
    async with tenant_session(tenant) as s:
        res = await s.execute(text(
            "DELETE FROM scn_raw_set r WHERE r.tenant = :t AND NOT EXISTS (SELECT 1 FROM scn_current c "
            "WHERE c.tenant = r.tenant AND c.snapshot_id = r.snapshot_id) AND r.status <> 'staging'"), {"t": tenant})
    return int(res.rowcount or 0)


async def latest_set(tenant: str, connector: str) -> dict[str, Any] | None:
    """The newest ``complete`` raw set (the current snapshot's or a failed attempt's), or ``None``."""
    async with tenant_session(tenant) as s:
        m = (await s.execute(text(
            "SELECT r.snapshot_id, r.status, r.mapping_version, r.mapping_hash, r.sources, r.pages, r.items, r.bytes, "
            "r.fetched_from, r.fetched_to, r.created_at, sn.status AS snapshot_status FROM scn_raw_set r "
            "JOIN scn_snapshot sn ON sn.tenant = r.tenant AND sn.id = r.snapshot_id "
            "WHERE r.tenant = :t AND r.connector = :c AND r.status = 'complete' "
            "ORDER BY r.created_at DESC, r.snapshot_id DESC LIMIT 1"), {"t": tenant, "c": connector})).mappings().first()
    if m is None:
        return None
    return {"snapshot_id": str(m["snapshot_id"]), "status": m["status"], "snapshot_status": m["snapshot_status"],
            "mapping_version": m["mapping_version"], "mapping_hash": m["mapping_hash"], "sources": m["sources"] or [],
            "pages": int(m["pages"]), "items": int(m["items"]), "bytes": int(m["bytes"]),
            "fetched_from": _iso(m["fetched_from"]), "fetched_to": _iso(m["fetched_to"]),
            "created_at": _iso(m["created_at"])}


async def read_pages(tenant: str, sid: str, source_key: str, *, max_pages: int | None = None
                     ) -> AsyncIterator[list[Any]]:
    """The staged pages of one source in page order (items decoded), streamed with a server-side
    cursor."""
    async with tenant_session(tenant) as s:
        pg = (await (await s.connection()).get_raw_connection()).driver_connection
        sql = ("SELECT page_no, items::text FROM scn_raw_page WHERE tenant = $1 AND snapshot_id = $2 "
               "AND source_key = $3 " + ("AND page_no < $4 " if max_pages is not None else "") + "ORDER BY page_no")
        args: list[Any] = [tenant, uuid.UUID(sid), source_key] + ([max_pages] if max_pages is not None else [])
        async for record in pg.cursor(sql, *args, prefetch=4):
            items = json.loads(record[1])
            yield items if isinstance(items, list) else []
