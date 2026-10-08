"""Tier-A **crawl sync** (``docs/contracts/110-m7-backoffice-generic.md`` §4): page through a plain
MCP server's mapped read tools and load the rows as a canonical ``scn/1.0`` snapshot.

A connector without ``scn.snapshot_*`` tools but with an active **read mapping**
(``connector_mapping`` kind ``read``, :mod:`keelson_engine.core.connector_mappings`) is synced by
crawling. :mod:`keelson_engine.core.scn_sync` owns the life cycle exactly as for tier B (single
flight + cross-process lock, ``loading`` row, failure record + back-off, pointer flip, GC marks,
pins, network push) and calls :func:`run_crawl` for the data part:

1. the mapping is re-checked against the approved tool catalogue (``read_mapping_violations``: only
   approved, trusted ``read`` tools are ever called — never a write tool);
2. ``source`` lookups are crawled first into memory (key → value, text keys);
3. every entity mapping pages its source tool (``ReadSource``: page numbers or a cursor, at most
   ``max_pages`` calls; hitting the cap with a full last page is recorded as **truncation**) with
   the connector's **reader** credentials over FastMCP through the egress proxy; each item is mapped
   with the safe expression evaluator (:mod:`keelson_engine.core.mapping_eval`), validated with the
   ``scn/1.0`` row model and streamed into the ``scn_<entity>`` tables under the new snapshot id in
   batches (temp table + ``INSERT … SELECT``, as RLS forbids ``COPY`` into the tables). Readers never
   see these rows: they always filter by the ``scn_current`` snapshot id, which only flips at the end;
4. one transaction: FK sanity (as tier B), ``ANALYZE``, previous ``ready`` → ``superseded``, the
   new snapshot ``ready`` with ``consistency = 'crawled'``, ``as_of_range`` = [first page, last page]
   and ``crawl`` = ``{pages, rows_seen, truncated, lookups}``, ``scn_current`` flip.

A failure deletes the rows written so far (and ``scn_sync`` records the attempt ``failed``); a
crashed crawl's rows are deleted when the next sync marks the orphan ``failed``. Plans built on a
crawled snapshot are **advisory** (``SnapshotLabel.advisory``): the runtime reads
``SnapshotRow.consistency`` / ``as_of_range``.

Failure codes (``scn_snapshot.error``): ``mapping_missing``, ``mapping_invalid``, ``auth_failed``,
``connector_error``, ``tool_error``, ``items_invalid``, ``mapping_failed`` (an expression could not be
evaluated for an item), ``data_invalid`` (a row the ``scn/1.0`` model or Postgres refuses,
duplicate ``source_id``), ``too_many_rows``, ``fk_violation``, plus scn_sync's generic ones.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final

from pydantic import ValidationError

from keelson_contracts import scn
from keelson_contracts.mapping import EntityMapping, ReadMapping, ReadSource, SourceLookup, parse_expression
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.mapping_eval import (
    Lookups,
    MappingEvalError,
    canonical_json,
    evaluate,
    get_path,
    render_template,
)
from keelson_engine.core.sql import dumps

__all__ = [
    "CrawlStats",
    "FLUSH_ROWS",
    "MAX_LOOKUP_ROWS",
    "MAX_ROWS",
    "crawl_rows",
    "preview",
    "purge_rows",
    "run_crawl",
    "session_caller",
]

log = get_logger(__name__)

FLUSH_ROWS: Final = 5_000
"""Rows per load batch (one short transaction each)."""
MAX_ROWS: Final = 10_000_000
"""Hard upper bound of rows one crawl may load (all entities); ``SCN_CRAWL_MAX_ROWS`` (default 2M) applies below it."""
MAX_LOOKUP_ROWS: Final = 200_000

ToolCaller = Callable[[str, dict[str, Any]], Awaitable[Any]]


class CrawlError(Exception):
    """A crawl failure with a ``scn_snapshot.error`` code."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(slots=True)
class CrawlStats:
    pages: int = 0
    rows_seen: int = 0
    truncated: list[str] = field(default_factory=list)
    lookups: dict[str, int] = field(default_factory=dict)
    first_at: dt.datetime | None = None
    last_at: dt.datetime | None = None

    def to_json(self) -> dict[str, Any]:
        return {"pages": self.pages, "rows_seen": self.rows_seen, "truncated": list(self.truncated),
                "lookups": dict(self.lookups)}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


# ───────────────────────────── paging ─────────────────────────────


async def _pages(call: ToolCaller, src: ReadSource, where: str, stats: CrawlStats, *,
                 max_pages: int | None = None) -> AsyncIterator[list[Any]]:
    """The item lists of each page of ``src`` (see ``ReadSource`` for the paging rules)."""
    try:
        base = render_template(src.args, None)
    except MappingEvalError as exc:
        raise CrawlError("mapping_invalid", f"{where}: {exc}") from None
    limit = min(src.max_pages, max_pages) if max_pages is not None else src.max_pages

    async def fetch(args: dict[str, Any], n: int) -> tuple[Any, list[Any]]:
        if stats.first_at is None:
            stats.first_at = _now()
        result = await call(src.tool, args)
        stats.last_at = _now()
        stats.pages += 1
        items = get_path(result, src.items_path)
        if items is None:
            if n == 0:
                raise CrawlError("items_invalid", f"{where}: {src.items_path} selects nothing in the {src.tool} result")
            return result, []
        if type(items) is not list:
            raise CrawlError("items_invalid", f"{where}: {src.items_path} is a {type(items).__name__}, not a list")
        return result, items

    if src.page_param is None:
        _result, items = await fetch(dict(base), 0)
        yield items
        return
    size_args = {src.page_size_param: src.page_size} if src.page_size_param else {}
    if src.cursor_path is None:
        page = src.first_page
        for n in range(limit):
            _result, items = await fetch({**base, **size_args, src.page_param: page}, n)
            yield items
            if len(items) < src.page_size:
                return
            page += 1
        if max_pages is None:
            stats.truncated.append(where)
        return
    cursor: Any = None
    seen: set[str] = set()
    for n in range(limit):
        args = {**base, **size_args}
        if cursor is not None:
            args[src.page_param] = cursor
        result, items = await fetch(args, n)
        yield items
        nxt = get_path(result, src.cursor_path)
        if nxt is None or nxt == "":
            return
        if isinstance(nxt, bool) or not isinstance(nxt, (str, int)):
            raise CrawlError("items_invalid", f"{where}: the cursor at {src.cursor_path} is not text")
        key = str(nxt)
        if key in seen:
            return
        seen.add(key)
        cursor = nxt
    if max_pages is None:
        stats.truncated.append(where)


# ───────────────────────────── mapping ─────────────────────────────


@dataclass(slots=True)
class _Compiled:
    where: str
    em: EntityMapping
    fields: dict[str, Any]
    model: type[scn.ScnRow]
    columns: tuple[str, ...]


def _compile(i: int, em: EntityMapping) -> _Compiled:
    return _Compiled(f"entities[{i}] ({em.entity})", em, em.parsed_fields(), scn.SCN_ROW_MODELS[em.entity],
                     scn.SCN_COLUMNS[em.entity])


def _summary(exc: ValidationError) -> str:
    errs = exc.errors()[:3]
    return "; ".join(f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg', '')}" for e in errs)[:300]


def map_item(c: _Compiled, item: Any, lookups: Lookups, index: int) -> tuple[Any, ...]:
    """One source item → a validated row tuple in ``SCN_COLUMNS`` order."""
    row: dict[str, Any] = {}
    for column, ast in c.fields.items():
        try:
            value = evaluate(ast, item, lookups)
        except MappingEvalError as exc:
            raise CrawlError("mapping_failed", f"{c.where} item {index}: {column}: {exc}") from None
        if value is not None:
            row[column] = value
    try:
        model = c.model.model_validate(row)
    except ValidationError as exc:
        sid = row.get("source_id")
        raise CrawlError("data_invalid", f"{c.where} item {index} ({str(sid)[:60]!r}): {_summary(exc)}") from None
    return tuple(getattr(model, col) for col in c.columns)


async def _lookups(call: ToolCaller, mapping: ReadMapping, stats: CrawlStats, *,
                   max_pages: int | None = None) -> Lookups:
    """Static tables as-is; ``source`` tables crawled **once per distinct source** (several tables
    over the same tool + args — e.g. a style's family, colour and price — share one paging run)."""
    tables: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[tuple[str, SourceLookup]]] = {}
    for name, table in mapping.lookups.items():
        if not isinstance(table, SourceLookup):
            tables[name] = dict(table.values)
            continue
        groups.setdefault(canonical_json(table.source.model_dump(mode="json")), []).append((name, table))
    for members in groups.values():
        compiled = [(name, f"lookups.{name}", parse_expression(t.key), parse_expression(t.value), dict[str, Any]())
                    for name, t in members]
        where = compiled[0][1] if len(compiled) == 1 else f"lookups.{{{','.join(m[0] for m in compiled)}}}"
        async for items in _pages(call, members[0][1].source, where, stats, max_pages=max_pages):
            for item in items:
                for _name, at, key_ast, value_ast, values in compiled:
                    try:
                        key = evaluate(key_ast, item)
                        value = evaluate(value_ast, item)
                    except MappingEvalError as exc:
                        raise CrawlError("mapping_failed", f"{at}: {exc}") from None
                    if key is None:
                        continue
                    if isinstance(value, (dict, list)):
                        raise CrawlError("mapping_failed", f"{at}: lookup values are text, numbers or booleans")
                    values.setdefault(str(key), value)
                    if len(values) > MAX_LOOKUP_ROWS:
                        raise CrawlError("too_many_rows", f"{at}: more than {MAX_LOOKUP_ROWS} entries")
        for name, _at, _k, _v, values in compiled:
            tables[name] = values
            stats.lookups[name] = len(values)
    return Lookups(tables)


async def crawl_rows(call: ToolCaller, mapping: ReadMapping, *, max_pages: int | None = None,
                     stats: CrawlStats | None = None) -> AsyncIterator[tuple[str, list[tuple[Any, ...]]]]:
    """``(entity, rows)`` batches of mapped + validated rows (``dedupe`` applied per mapping)."""
    stats = stats if stats is not None else CrawlStats()
    lookups = await _lookups(call, mapping, stats, max_pages=max_pages)
    for i, em in enumerate(mapping.entities):
        c = _compile(i, em)
        seen: set[str] = set()
        index = 0
        async for items in _pages(call, em.source, c.where, stats, max_pages=max_pages):
            batch: list[tuple[Any, ...]] = []
            for item in items:
                row = map_item(c, item, lookups, index)
                index += 1
                stats.rows_seen += 1
                if em.dedupe:
                    sid = str(row[0])
                    if sid in seen:
                        continue
                    seen.add(sid)
                batch.append(row)
            if batch:
                yield em.entity, batch


# ───────────────────────────── loading ─────────────────────────────


async def _flush(tenant: str, sid: uuid.UUID, entity: str, rows: list[tuple[Any, ...]]) -> int:
    import asyncpg

    table = f"scn_{entity}"
    cols = scn.SCN_COLUMNS[entity]
    collist = ", ".join(cols)
    tmp = f"scn_crawl_{entity}"
    async with tenant_session(tenant) as s:
        pg = (await (await s.connection()).get_raw_connection()).driver_connection
        await pg.execute(f"CREATE TEMP TABLE {tmp} ON COMMIT DROP AS SELECT {collist} FROM {table} WITH NO DATA")
        try:
            await pg.copy_records_to_table(tmp, records=rows, columns=list(cols))
            status = await pg.execute(
                f"INSERT INTO {table} (tenant, snapshot_id, {collist}) SELECT $1::text, $2::uuid, {collist} FROM {tmp}",
                tenant, sid)
        except asyncpg.UniqueViolationError:
            raise CrawlError("data_invalid", f"{entity}: duplicate source_id (mark derived rows dedupe)") from None
        except asyncpg.NotNullViolationError as exc:
            raise CrawlError("data_invalid", f"{entity}: missing {exc.column_name or 'value'}") from None
        except asyncpg.DataError as exc:
            raise CrawlError("data_invalid", f"{entity}: {type(exc).__name__} ({exc.sqlstate})") from None
    return int(status.rsplit(" ", 1)[-1])


async def purge_rows(tenant: str, ids: list[uuid.UUID]) -> None:
    """Delete the entity rows of failed / orphaned crawl attempts."""
    if not ids:
        return
    for entity in scn.SCN_ENTITIES:
        async with tenant_session(tenant) as s:
            pg = (await (await s.connection()).get_raw_connection()).driver_connection
            await pg.execute(f"DELETE FROM scn_{entity} WHERE tenant = $1 AND snapshot_id = ANY($2::uuid[])",
                             tenant, ids)


async def _load(tenant: str, sid: uuid.UUID, call: ToolCaller, mapping: ReadMapping,
                stats: CrawlStats) -> dict[str, int]:
    from keelson_engine.config import get_settings

    limit = min(MAX_ROWS, get_settings().scn_crawl_max_rows)
    counts: dict[str, int] = {e: 0 for e in scn.SCN_ENTITIES}
    pending: dict[str, list[tuple[Any, ...]]] = {}
    total = 0
    async for entity, rows in crawl_rows(call, mapping, stats=stats):
        buf = pending.setdefault(entity, [])
        buf.extend(rows)
        total += len(rows)
        if total > limit:
            raise CrawlError("too_many_rows", f"the crawl exceeds {limit} rows (SCN_CRAWL_MAX_ROWS)")
        if len(buf) >= FLUSH_ROWS:
            counts[entity] += await _flush(tenant, sid, entity, buf)
            pending[entity] = []
    for entity, buf in pending.items():
        if buf:
            counts[entity] += await _flush(tenant, sid, entity, buf)
    return counts


@asynccontextmanager
async def session_caller(conn: Any) -> AsyncIterator[ToolCaller]:
    """Reader-principal tool calls on **one** MCP session for the whole crawl (egress proxy;
    internal connectors direct). Errors become :class:`CrawlError` (class names only: URLs and
    tokens never reach a message)."""
    from mcp import types as mcp_types

    from keelson_engine.core import connectors as reg
    from keelson_engine.core.mcp import mcp_client

    try:
        headers = await reg.headers_for(conn, "reader")
    except Exception as exc:  # noqa: BLE001 - TokenError etc.; never the token
        raise CrawlError("auth_failed", f"reader credentials unavailable ({type(exc).__name__})") from None
    in_body = False
    try:
        async with mcp_client(conn.url, headers, proxy="" if conn.is_internal else None, timeout=120.0) as client:

            async def call(tool: str, args: dict[str, Any]) -> Any:
                try:
                    result = await client.call_tool_mcp(tool, args)
                except (asyncio.CancelledError, TimeoutError):
                    raise
                except Exception as exc:  # noqa: BLE001 - transport: class name only
                    raise CrawlError("connector_error", f"{tool} call failed ({type(exc).__name__})") from None
                if result.is_error:
                    msg = " ".join(b.text for b in result.content if isinstance(b, mcp_types.TextContent))
                    raise CrawlError("tool_error", f"{tool} failed: {msg[:200]}")
                if result.structured_content is None:
                    raise CrawlError("tool_error", f"{tool} returned no structured content")
                return result.structured_content

            in_body = True
            yield call
            in_body = False
    except (CrawlError, asyncio.CancelledError, TimeoutError):
        raise
    except Exception as exc:  # noqa: BLE001 - connect / close: class name only (URLs may carry secrets)
        if in_body:
            raise  # the caller's own error (database, programming): never disguised as a connector error
        raise CrawlError("connector_error", f"MCP session failed ({type(exc).__name__})") from None


async def run_crawl(conn: Any, sid: uuid.UUID, t0: float) -> Any:
    """The data part of a crawl sync for ``scn_sync`` (it holds the lock and the ``loading`` row).
    Returns the ``scn_sync.SnapshotRow`` of the ready snapshot; raises :class:`CrawlError`."""
    from keelson_engine.core import connector_mappings, scn_sync

    tenant, connector = conn.tenant, conn.slug
    mapping = await connector_mappings.get_read_mapping(tenant, connector)
    if mapping is None:
        raise CrawlError("mapping_missing", f"connector {connector!r} has no valid read mapping")
    problems = connector_mappings.violations(mapping, await connector_mappings.approved_classifications(tenant, connector))
    if problems:
        raise CrawlError("mapping_invalid", problems[0])
    stats = CrawlStats()
    try:
        async with session_caller(conn) as call:
            counts = await _load(tenant, sid, call, mapping, stats)
        started = stats.first_at or _now()
        finished = stats.last_at or started
        async with tenant_session(tenant) as s:
            pg = (await (await s.connection()).get_raw_connection()).driver_connection
            for label, sql in scn_sync.FK_CHECKS:
                bad = [r[0] for r in await pg.fetch(sql, tenant, sid)]
                if bad:
                    hint = f" (the crawl was truncated: {', '.join(stats.truncated)})" if stats.truncated else ""
                    raise CrawlError("fk_violation", f"{label}: {', '.join(bad)}{hint}")
            for entity in scn.SCN_ENTITIES:
                await pg.execute(f"ANALYZE scn_{entity}")
            await pg.execute(
                "UPDATE scn_snapshot SET status = 'superseded' "
                "WHERE tenant = $1 AND connector = $2 AND status = 'ready' AND id <> $3", tenant, connector, sid)
            flipped = await pg.fetchval(
                "UPDATE scn_snapshot SET status = 'ready', ready_at = now(), row_counts = $3::jsonb, bytes = 0, "
                "duration_ms = $4, error = NULL, consistency = 'crawled', as_of_from = $5, as_of_to = $6, "
                "as_of = $6, source_snapshot_id = $7, crawl = $8::jsonb "
                "WHERE tenant = $1 AND id = $2 AND status = 'loading' RETURNING id",
                tenant, sid, dumps(counts), int((time.monotonic() - t0) * 1000), started, finished,
                f"crawl:{sid}", dumps(stats.to_json()))
            if flipped is None:
                raise CrawlError("interrupted", "the attempt was marked failed by another process")
            await pg.execute(
                "INSERT INTO scn_current (tenant, connector, snapshot_id, updated_at) VALUES ($1, $2, $3, now()) "
                "ON CONFLICT (tenant, connector) DO UPDATE SET snapshot_id = EXCLUDED.snapshot_id, updated_at = now()",
                tenant, connector, sid)
    except BaseException:
        await asyncio.shield(_purge_quietly(tenant, sid))
        raise
    log.info("scn crawl loaded", tenant=tenant, connector=connector, snapshot=str(sid), pages=stats.pages,
             rows=sum(counts.values()), truncated=stats.truncated)
    return scn_sync.SnapshotRow(
        id=str(sid), tenant=tenant, connector=connector, status="ready", source_snapshot_id=f"crawl:{sid}",
        as_of=finished, row_counts=counts, duration_ms=int((time.monotonic() - t0) * 1000),
        consistency="crawled", as_of_from=started, as_of_to=finished, crawl=stats.to_json())


async def _purge_quietly(tenant: str, sid: uuid.UUID) -> None:
    try:
        await purge_rows(tenant, [sid])
    except Exception as exc:  # noqa: BLE001 - the next sync purges orphans again
        log.warning("scn crawl purge failed", tenant=tenant, snapshot=str(sid), error=type(exc).__name__)


# ───────────────────────────── admin preview ─────────────────────────────


async def preview(conn: Any, mapping: ReadMapping, *, max_pages: int = 1, sample: int = 5) -> dict[str, Any]:
    """``POST …/mappings/read/test``: crawl at most ``max_pages`` page(s) per source and map the
    items (nothing is stored) → per entity mapping ``{rows, sample, error}`` + paging stats."""
    stats = CrawlStats()
    try:
        async with session_caller(conn) as call:
            return await _preview(call, mapping, stats, max_pages=max_pages, sample=sample)
    except CrawlError as exc:
        return {"ok": False, "error": f"{exc.code}: {exc.detail}"[:500], "entities": [], **stats.to_json()}


async def _preview(call: ToolCaller, mapping: ReadMapping, stats: CrawlStats, *, max_pages: int,
                   sample: int) -> dict[str, Any]:
    out: list[dict[str, Any]] = []
    try:
        lookups = await _lookups(call, mapping, stats, max_pages=max_pages)
    except CrawlError as exc:
        return {"ok": False, "error": f"{exc.code}: {exc.detail}"[:500], "entities": [], **stats.to_json()}
    ok = True
    for i, em in enumerate(mapping.entities):
        c = _compile(i, em)
        entry: dict[str, Any] = {"entity": em.entity, "where": c.where, "rows": 0, "sample": [], "error": None}
        index = 0
        try:
            async for items in _pages(call, em.source, c.where, stats, max_pages=max_pages):
                for item in items:
                    row = map_item(c, item, lookups, index)
                    index += 1
                    entry["rows"] += 1
                    if len(entry["sample"]) < sample:
                        entry["sample"].append({col: (v.isoformat() if isinstance(v, dt.date) else v)
                                                for col, v in zip(c.columns, row, strict=True)})
        except CrawlError as exc:
            ok = False
            entry["error"] = f"{exc.code}: {exc.detail}"[:500]
        out.append(entry)
    return {"ok": ok, "error": None, "entities": out, **stats.to_json()}
