"""Engine canonical store: ``scn/1.0`` snapshot sync (``docs/contracts/80-m3-deterministic-core.md`` §1).

:func:`sync_connector` copies a Tier-B connector's canonical snapshot into the tenant's
``scn_*`` tables and flips the ``scn_current`` pointer:

1. ``scn.snapshot_open`` (every ``scn/1.0`` entity) as the connector's **reader** principal over
   the FastMCP client (egress proxy, :func:`keelson_engine.core.mcp.mcp_client`);
2. ``scn.snapshot_export`` per entity; every part URL must be on the connector's own origin
   (:func:`keelson_contracts.scn.part_uri_allowed`); parts are streamed through the egress proxy
   into a temp dir with ``sha256`` + byte-size checks against the export and the manifest;
3. one transaction: per entity the Parquet rows (pyarrow; columns = ``SCN_COLUMNS``, every row
   validated with the ``scn/1.0`` row model) are ``COPY``-ed (asyncpg binary COPY) into an
   ``ON COMMIT DROP`` temp table and moved with ``INSERT … SELECT`` into ``scn_<entity>`` —
   Postgres refuses ``COPY FROM`` into tables with row-level security, the ``INSERT`` is checked
   by the RLS policy; row counts vs. the manifest; FK sanity (every ``po_line.sku_code`` is a
   ``sku`` item, every inventory location exists with its kind); the previous ``ready`` snapshot
   becomes ``superseded``, the new one ``ready`` and ``scn_current`` points to it;
4. GC: superseded snapshots beyond the newest ``SCN_SYNC_KEEP_SNAPSHOTS`` (default 2) that no plan
   pins (``scn_pin``) are **marked** for GC (``gc_requested_at``); failed attempts are capped at
   ``SCN_SYNC_KEEP_FAILED``. The marked snapshots' rows (~300k per full-profile snapshot) are
   deleted later by the worker's background task (:func:`collect_garbage`: batches of
   ``GC_BATCH_ROWS`` rows per statement and table, one short transaction each, yielding between
   batches), so neither a sync nor a demo reset (:func:`mark_noncurrent`) waits for the deletes.
   A marked snapshot is never current or pinned, cannot be pinned any more and is hidden from
   :func:`get_snapshot` / :func:`list_snapshots`.

Concurrency: one sync per ``(tenant, connector)``. In one process concurrent callers share the
same run (single-flight); across processes a session-level Postgres advisory lock is held for
the whole sync and a second process gets :class:`SyncInProgress`. A failure (any step) marks the
attempt ``failed`` and leaves the previous snapshot current; no partial rows are ever committed.

Triggers (the worker drives them; the case path never waits for a sync):
:func:`mark_stale` (webhooks, debounced), :func:`request_sync` (admin, due now),
:func:`poll_changes` (tier C ``scn.changes_since``), :func:`due_syncs` (what to run now),
:func:`list_sync_connectors` (worker start-up).
"""

from __future__ import annotations

import asyncio
import hashlib
import shutil
import tempfile
import time
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from keelson_contracts import scn
from keelson_engine.config import get_settings
from keelson_engine.core.db import check_tenant_slug, keelson_engine, system_session, tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.sql import dumps

__all__ = [
    "CHANGES_TOOL",
    "SNAPSHOT_TOOLS",
    "RESET_EVENT_TYPES",
    "STALE_EVENT_PREFIXES",
    "DueSync",
    "FK_CHECKS",
    "GC_BATCH_ROWS",
    "GcProgress",
    "PlanNotFound",
    "ScnSyncError",
    "SnapshotNotFound",
    "SnapshotRow",
    "SyncFailed",
    "SyncInProgress",
    "SyncUnavailable",
    "collect_garbage",
    "current_snapshot",
    "due_syncs",
    "get_snapshot",
    "is_reset_event",
    "is_stale_event",
    "list_snapshots",
    "list_sync_connectors",
    "mark_noncurrent",
    "mark_stale",
    "pin_snapshot",
    "poll_changes",
    "purge_connector",
    "request_sync",
    "sync_connector",
    "sync_state",
    "unpin_plan",
]

log = get_logger(__name__)

SNAPSHOT_TOOLS = ("scn.snapshot_open", "scn.snapshot_export")
CHANGES_TOOL = "scn.changes_since"
STALE_EVENT_PREFIXES = ("change_set.", "po_line.", "inventory.", "sailing.")
"""Connector webhook event types that make the canonical snapshot stale (contract 80 §1)."""
RESET_EVENT_TYPES = frozenset({"scenario.reset"})
"""Source resets (contract 90 §10) also make it stale (plus any ``merxa.*`` reset event)."""

BATCH_ROWS = 10_000
"""Rows per Parquet batch converted (in a worker thread) and streamed into ``COPY``."""
GC_BATCH_ROWS = 20_000
"""Rows deleted per statement (and transaction) per entity table by :func:`collect_garbage`."""
_MAX_RETRY_S = 900.0
_ERROR_MAX = 500
_TABLES: dict[str, str] = {e: f"scn_{e}" for e in scn.SCN_ENTITIES}


# ───────────────────────────── types ─────────────────────────────


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    """One ``scn_snapshot`` row (a sync attempt)."""

    id: str
    tenant: str
    connector: str
    status: str
    """``loading`` | ``ready`` | ``failed`` | ``superseded``."""
    source_snapshot_id: str | None = None
    as_of: datetime | None = None
    watermark: str | None = None
    reason: str | None = None
    row_counts: dict[str, int] = field(default_factory=dict)
    bytes: int = 0
    duration_ms: int | None = None
    error: str | None = None
    created_at: datetime | None = None
    ready_at: datetime | None = None
    pins: int = 0
    consistency: str = "consistent"
    """M7: ``consistent`` (tier B export) | ``crawled`` (tier-A crawl: plans built on it are advisory)."""
    as_of_from: datetime | None = None
    as_of_to: datetime | None = None
    """Crawled snapshots: when the first / last page was read."""
    crawl: dict[str, Any] | None = None
    """Crawled snapshots: ``{pages, rows_seen, truncated, lookups}``."""

    @property
    def as_of_range(self) -> dict[str, str] | None:
        """``{from, to}`` (ISO) of a crawled snapshot, else ``None``."""
        if self.consistency != "crawled" or self.as_of_from is None or self.as_of_to is None:
            return None
        return {"from": self.as_of_from.isoformat(), "to": self.as_of_to.isoformat()}

    @property
    def label(self) -> dict[str, Any]:
        """``keelson_contracts.scn.SnapshotLabel`` JSON (``{consistency, as_of_range}``). A crawled
        snapshot always stays advisory (a missing range falls back to when it was loaded)."""
        if self.consistency != "crawled":
            return {"consistency": "consistent", "as_of_range": None}
        rng = self.as_of_range
        if rng is None:
            when = (self.as_of_to or self.as_of_from or self.ready_at or self.created_at)
            iso = when.isoformat() if when is not None else datetime.now(UTC).isoformat()
            rng = {"from": iso, "to": iso}
        return {"consistency": "crawled", "as_of_range": rng}

    @property
    def advisory(self) -> bool:
        return self.consistency == "crawled"

    def to_json(self) -> dict[str, Any]:
        def iso(v: datetime | None) -> str | None:
            return v.isoformat() if v is not None else None

        return {
            "id": self.id, "connector": self.connector, "status": self.status,
            "source_snapshot_id": self.source_snapshot_id, "as_of": iso(self.as_of), "watermark": self.watermark,
            "reason": self.reason, "row_counts": dict(self.row_counts), "rows": sum(self.row_counts.values()),
            "bytes": self.bytes, "duration_ms": self.duration_ms, "error": self.error,
            "created_at": iso(self.created_at), "ready_at": iso(self.ready_at), "pins": self.pins,
            "consistency": self.consistency, "as_of_range": self.as_of_range, "crawl": self.crawl,
        }


@dataclass(frozen=True, slots=True)
class DueSync:
    tenant: str
    connector: str
    reason: str | None
    stale_since: datetime | None


class ScnSyncError(Exception):
    code = "sync_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code:
            self.code = code
        self.message = message


class SyncUnavailable(ScnSyncError):
    """The connector cannot be synced (unknown, not active, or no approved snapshot tools).
    Nothing was recorded."""

    code = "sync_unavailable"


class SyncInProgress(ScnSyncError):
    """Another process holds the sync lock of this ``(tenant, connector)``."""

    code = "sync_in_progress"


class SyncFailed(ScnSyncError):
    """The attempt failed; ``snapshot`` is the ``failed`` row, the previous snapshot stays current."""

    code = "sync_failed"

    def __init__(self, snapshot: SnapshotRow, code: str) -> None:
        super().__init__(snapshot.error or code, code=code)
        self.snapshot = snapshot


class SnapshotNotFound(LookupError):
    pass


class PlanNotFound(LookupError):
    """:func:`pin_snapshot` with a plan id that is not a plan of the tenant."""


class _Fail(Exception):
    """A sync step failed; ``detail`` is safe to store/show (no secrets, no signed URLs)."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


# ───────────────────────────── row helpers ─────────────────────────────

_SNAP_COLS = (
    "s.id, s.tenant, s.connector, s.status, s.source_snapshot_id, s.as_of, s.watermark, s.reason, s.row_counts, "
    "s.bytes, s.duration_ms, s.error, s.created_at, s.ready_at, "
    "(SELECT count(*) FROM scn_pin p WHERE p.tenant = s.tenant AND p.snapshot_id = s.id) AS pins, "
    "s.consistency, s.as_of_from, s.as_of_to, s.crawl"
)


def _snap(m: Any) -> SnapshotRow:
    return SnapshotRow(
        id=str(m["id"]), tenant=m["tenant"], connector=m["connector"], status=m["status"],
        source_snapshot_id=m["source_snapshot_id"], as_of=m["as_of"], watermark=m["watermark"],
        reason=m["reason"], row_counts={str(k): int(v) for k, v in (m["row_counts"] or {}).items()},
        bytes=int(m["bytes"] or 0), duration_ms=m["duration_ms"], error=m["error"], created_at=m["created_at"],
        ready_at=m["ready_at"], pins=int(m["pins"] or 0), consistency=str(m["consistency"] or "consistent"),
        as_of_from=m["as_of_from"], as_of_to=m["as_of_to"], crawl=m["crawl"],
    )


def _uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError:
        raise SnapshotNotFound(f"snapshot {value!r} not found") from None


async def get_snapshot(tenant: str, snapshot_id: str) -> SnapshotRow | None:
    sid = _uuid(snapshot_id)
    async with tenant_session(tenant) as s:
        m = (await s.execute(text(f"SELECT {_SNAP_COLS} FROM scn_snapshot s WHERE s.tenant = :t AND s.id = :i "
                                  "AND s.gc_requested_at IS NULL"),
                             {"t": tenant, "i": sid})).mappings().first()
    return _snap(m) if m else None


async def current_snapshot(tenant: str, connector: str) -> SnapshotRow | None:
    """The snapshot readers should use (``scn_current``), or ``None`` before the first sync.
    Query the ``scn_*`` tables with ``WHERE tenant = :t AND snapshot_id = :id``."""
    async with tenant_session(tenant) as s:
        m = (
            await s.execute(
                text(
                    f"SELECT {_SNAP_COLS} FROM scn_current c JOIN scn_snapshot s "
                    "ON s.tenant = c.tenant AND s.id = c.snapshot_id WHERE c.tenant = :t AND c.connector = :c"
                ),
                {"t": tenant, "c": connector},
            )
        ).mappings().first()
    return _snap(m) if m else None


async def list_snapshots(tenant: str, connector: str, limit: int = 20) -> list[SnapshotRow]:
    """Newest first (every status; snapshots marked for GC are gone already)."""
    limit = max(1, min(int(limit), 200))
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(f"SELECT {_SNAP_COLS} FROM scn_snapshot s WHERE s.tenant = :t AND s.connector = :c "
                 "AND s.gc_requested_at IS NULL ORDER BY s.created_at DESC, s.id DESC LIMIT :n"),
            {"t": tenant, "c": connector, "n": limit},
        )
        return [_snap(m) for m in res.mappings().all()]


async def sync_state(tenant: str, connector: str) -> dict[str, Any] | None:
    async with tenant_session(tenant) as s:
        m = (
            await s.execute(
                text("SELECT stale_since, last_event_at, due_at, reason, attempts, retry_at, last_attempt_at, "
                     "last_error, last_poll_at FROM scn_sync_state WHERE tenant = :t AND connector = :c"),
                {"t": tenant, "c": connector},
            )
        ).mappings().first()
    if m is None:
        return None
    return {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in dict(m).items()}


# ───────────────────────────── pins ─────────────────────────────


async def pin_snapshot(tenant: str, snapshot_id: str, plan_id: int) -> None:
    """Keep ``snapshot_id`` while plan ``plan_id`` exists (idempotent). Only ``ready`` or
    ``superseded`` snapshots not marked for GC can be pinned (:class:`SnapshotNotFound` otherwise,
    also when GC marked or removed it concurrently). Deleting the plan row drops the pin (FK
    cascade). The snapshot row is read ``FOR KEY SHARE``: a GC pass that holds it (``FOR UPDATE``)
    makes us wait and re-check the mark on the committed row."""
    sid = _uuid(snapshot_id)
    try:
        async with tenant_session(tenant) as s:
            res = await s.execute(
                text(
                    """
                    INSERT INTO scn_pin (tenant, snapshot_id, plan_id)
                    SELECT s.tenant, s.id, :p FROM scn_snapshot s
                     WHERE s.tenant = :t AND s.id = :i AND s.status IN ('ready', 'superseded')
                       AND s.gc_requested_at IS NULL
                     FOR KEY SHARE OF s
                    ON CONFLICT (tenant, snapshot_id, plan_id) DO UPDATE SET created_at = scn_pin.created_at
                    RETURNING snapshot_id
                    """
                ),
                {"t": tenant, "i": sid, "p": int(plan_id)},
            )
            if res.first() is None:
                raise SnapshotNotFound(f"snapshot {snapshot_id} is not available for pinning")
    except IntegrityError as exc:
        # FK (tenant, plan_id) -> plan: unknown / other tenant's plan. FK (tenant, snapshot_id): GC
        # deleted the snapshot between our read and the FK check (GC holds the row, see _gc).
        if "plan" in _constraint_name(exc):
            raise PlanNotFound(f"plan {plan_id} not found") from None
        raise SnapshotNotFound(f"snapshot {snapshot_id} is not available for pinning") from None


def _constraint_name(exc: IntegrityError) -> str:
    """The violated constraint of an asyncpg-backed ``IntegrityError`` (``""`` if unknown)."""
    orig: Any = exc.orig
    for candidate in (getattr(orig, "__cause__", None), orig):
        name = getattr(candidate, "constraint_name", None)
        if name:
            return str(name)
    return ""


async def unpin_plan(tenant: str, plan_id: int) -> int:
    """Release every pin of ``plan_id`` (plan closed); returns the number of pins removed."""
    async with tenant_session(tenant) as s:
        res = await s.execute(text("DELETE FROM scn_pin WHERE tenant = :t AND plan_id = :p RETURNING snapshot_id"),
                              {"t": tenant, "p": int(plan_id)})
        return len(res.all())


# ───────────────────────────── triggers ─────────────────────────────


def is_reset_event(event_type: str) -> bool:
    """A source-side reset (Merxa ``scenario.reset`` / any ``merxa.*`` reset event, e.g.
    ``merxa.scenario.reset``): every row may have changed (contract 90 §10)."""
    if event_type in RESET_EVENT_TYPES:
        return True
    return event_type.startswith("merxa.") and (event_type.endswith(".reset") or event_type.endswith("_reset"))


def is_stale_event(event_type: str) -> bool:
    return any(event_type.startswith(p) for p in STALE_EVENT_PREFIXES) or is_reset_event(event_type)


async def mark_stale(tenant: str, connector: str, reason: str) -> None:
    """Record that ``connector``'s data changed (webhook / poll). The sync becomes due
    ``SCN_SYNC_DEBOUNCE_SECONDS`` after the **last** such mark, but at most
    ``SCN_SYNC_MAX_DELAY_SECONDS`` after the first unsynced one, and never before a failure
    back-off (``retry_at``) has passed. Cheap (one upsert); never syncs itself."""
    st = get_settings()
    async with tenant_session(tenant) as s:
        await s.execute(
            text(
                """
                INSERT INTO scn_sync_state AS st (tenant, connector, stale_since, last_event_at, due_at, reason, updated_at)
                VALUES (:t, :c, now(), now(), now() + make_interval(secs => :deb), :r, now())
                ON CONFLICT (tenant, connector) DO UPDATE SET
                  stale_since   = COALESCE(st.stale_since, now()),
                  last_event_at = now(),
                  due_at        = GREATEST(
                                    LEAST(now() + make_interval(secs => :deb),
                                          COALESCE(st.stale_since, now()) + make_interval(secs => :max)),
                                    COALESCE(st.retry_at, '-infinity'::timestamptz)),
                  reason        = EXCLUDED.reason,
                  updated_at    = now()
                """
            ),
            {"t": tenant, "c": connector, "r": reason[:200], "deb": float(st.scn_sync_debounce_seconds),
             "max": float(st.scn_sync_max_delay_seconds)},
        )


async def _sync_mode(tenant: str, connector: str) -> tuple[Any, str]:
    """``(active connector row, "snapshot" | "crawl")`` or :class:`SyncUnavailable`. Tier B
    (approved ``scn.snapshot_open`` + ``scn.snapshot_export``) wins; otherwise a connector with a
    read mapping is crawled (M7 tier A, :mod:`keelson_engine.core.scn_crawl`)."""
    from keelson_engine.core import connectors as reg

    try:
        conn = await reg.get_connector(tenant, connector)
    except reg.ConnectorNotFound:
        raise SyncUnavailable(f"connector {connector!r} not found", code="not_found") from None
    if not conn.usable:
        raise SyncUnavailable(f"connector {connector!r} is {conn.status}", code="connector_inactive")
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text("SELECT name FROM connector_tool WHERE connector = :c AND status = 'approved' AND name = ANY(:n)"),
            {"c": connector, "n": list(SNAPSHOT_TOOLS)},
        )
        have = {r[0] for r in res.all()}
        mapped = (await s.execute(text("SELECT 1 FROM connector_mapping WHERE tenant = :t AND connector = :c "
                                       "AND kind = 'read' LIMIT 1"), {"t": tenant, "c": connector})).first()
    if have == set(SNAPSHOT_TOOLS):
        return conn, "snapshot"
    if mapped is not None:
        return conn, "crawl"
    raise SyncUnavailable(f"connector {connector!r} has no approved scn snapshot tools and no read mapping",
                          code="sync_unsupported")


async def _sync_capable(tenant: str, connector: str) -> Any:
    """The active connector row if it can be synced (snapshot or crawl), else :class:`SyncUnavailable`."""
    conn, _mode = await _sync_mode(tenant, connector)
    return conn


async def request_sync(tenant: str, connector: str, reason: str = "admin") -> dict[str, Any]:
    """Make a sync due **now** (admin "Sync now", worker start-up), bypassing debounce and
    failure back-off. Raises :class:`SyncUnavailable`. Returns the sync state."""
    await _sync_capable(tenant, connector)
    async with tenant_session(tenant) as s:
        await s.execute(
            text(
                """
                INSERT INTO scn_sync_state AS st (tenant, connector, stale_since, due_at, reason, updated_at)
                VALUES (:t, :c, now(), now(), :r, now())
                ON CONFLICT (tenant, connector) DO UPDATE SET
                  stale_since = COALESCE(st.stale_since, now()), due_at = now(), retry_at = NULL,
                  reason = EXCLUDED.reason, updated_at = now()
                """
            ),
            {"t": tenant, "c": connector, "r": reason[:200]},
        )
    return await sync_state(tenant, connector) or {}


_CAPABLE_SQL = """
  c.status = 'active'
  AND ((SELECT count(DISTINCT t.name) FROM connector_tool t
         WHERE t.tenant = c.tenant AND t.connector = c.slug AND t.status = 'approved'
           AND t.name IN ('scn.snapshot_open', 'scn.snapshot_export')) = 2
       OR EXISTS (SELECT 1 FROM connector_mapping m
                   WHERE m.tenant = c.tenant AND m.connector = c.slug AND m.kind = 'read'))
"""


async def due_syncs(limit: int = 20) -> list[DueSync]:
    """Connectors (any tenant) whose sync is due now, oldest first — only active connectors with
    approved snapshot tools. The worker runs ``sync_connector(d.tenant, d.connector,
    reason=d.reason or "stale")`` for each (catch :class:`ScnSyncError`)."""
    async with system_session() as s:
        res = await s.execute(
            text(
                f"""
                SELECT st.tenant, st.connector, st.reason, st.stale_since
                  FROM scn_sync_state st
                  JOIN connector c ON c.tenant = st.tenant AND c.slug = st.connector
                 WHERE st.due_at IS NOT NULL AND st.due_at <= now() AND {_CAPABLE_SQL}
                 ORDER BY st.due_at, st.tenant, st.connector
                 LIMIT :n
                """
            ),
            {"n": max(1, int(limit))},
        )
        return [DueSync(r[0], r[1], r[2], r[3]) for r in res.all()]


async def list_sync_connectors() -> list[tuple[str, str]]:
    """``(tenant, connector)`` of every active connector with approved snapshot tools (start-up)."""
    async with system_session() as s:
        res = await s.execute(text(f"SELECT c.tenant, c.slug FROM connector c WHERE {_CAPABLE_SQL} "
                                   "ORDER BY c.tenant, c.slug"))
        return [(r[0], r[1]) for r in res.all()]


async def poll_changes(tenant: str, connector: str) -> bool:
    """Tier C: ``scn.changes_since(<current watermark>)``; any change (or the source's
    ``full_resync_required``) → :func:`mark_stale` and ``True``. No current snapshot yet → stale.
    Raises :class:`SyncUnavailable` (no approved ``scn.changes_since``) or :class:`ScnSyncError`
    (call failed)."""
    from keelson_engine.core import connectors as reg

    conn = await _sync_capable(tenant, connector)
    async with tenant_session(tenant) as s:
        ok = (await s.execute(text("SELECT 1 FROM connector_tool WHERE connector = :c AND name = :n "
                                   "AND status = 'approved'"), {"c": connector, "n": CHANGES_TOOL})).first()
        await s.execute(text("UPDATE scn_sync_state SET last_poll_at = now() WHERE tenant = :t AND connector = :c"),
                        {"t": tenant, "c": connector})
    if ok is None:
        raise SyncUnavailable(f"connector {connector!r} has no approved {CHANGES_TOOL}", code="sync_unsupported")
    current = await current_snapshot(tenant, connector)
    if current is None or not current.watermark:
        await mark_stale(tenant, connector, "poll:no_snapshot")
        return True
    try:
        headers = await reg.headers_for(conn, "reader")
        data = await _call_tools(conn.url, headers, [(CHANGES_TOOL, {"watermark": current.watermark})])
        changes = scn.ChangesSince.model_validate(data[0])
    except _Fail as exc:
        raise ScnSyncError(exc.detail, code=exc.code) from None
    except ValidationError as exc:
        raise ScnSyncError(_validation_summary(exc), code="invalid_changes") from None
    except Exception as exc:  # noqa: BLE001 - token/transport errors: never leak details
        raise ScnSyncError(f"{CHANGES_TOOL} failed ({type(exc).__name__})", code="connector_error") from None
    if changes.requires_sync:
        reason = "poll:full_resync" if changes.full_resync_required else f"poll:{len(changes.changes)}_changes"
        await mark_stale(tenant, connector, reason)
        return True
    return False


# ───────────────────────────── sync ─────────────────────────────

_inflight: dict[tuple[str, str], asyncio.Future[SnapshotRow]] = {}


async def sync_connector(tenant: str, connector: str, *, reason: str) -> SnapshotRow:
    """Sync ``connector``'s canonical snapshot for ``tenant`` (see the module docstring).

    Returns the new ``ready`` :class:`SnapshotRow`. Raises :class:`SyncUnavailable` (nothing
    recorded), :class:`SyncInProgress` (another process is syncing) or :class:`SyncFailed`
    (attempt recorded as ``failed``; the previous snapshot stays current). Concurrent calls in
    one process share one run and get the same result."""
    check_tenant_slug(tenant)
    key = (tenant, connector)
    fut = _inflight.get(key)
    if fut is None:
        fut = asyncio.ensure_future(_sync_once(tenant, connector, reason))
        _inflight[key] = fut

        def _done(f: asyncio.Future[SnapshotRow], key: tuple[str, str] = key) -> None:
            if _inflight.get(key) is f:
                del _inflight[key]
            if not f.cancelled():
                f.exception()  # retrieved: no "exception was never retrieved" noise

        fut.add_done_callback(_done)
    return await asyncio.shield(fut)


_LOCK_SQL = "SELECT pg_try_advisory_lock(hashtextextended($1, 0))"
_UNLOCK_SQL = "SELECT pg_advisory_unlock(hashtextextended($1, 0))"


@asynccontextmanager
async def _sync_lock(tenant: str, connector: str) -> AsyncIterator[bool]:
    """Session-level advisory lock on a dedicated pooled connection (cross-process).

    A session lock outlives the transaction: if the unlock does not complete (error, cancellation)
    the physical connection is terminated and dropped from the pool, so the lock dies with the
    backend instead of staying held by an idle pooled connection (which would block every later
    sync of this connector with SyncInProgress)."""
    key = f"keelson:scn_sync|{tenant}|{connector}"
    async with keelson_engine().connect() as conn:
        pg = (await conn.get_raw_connection()).driver_connection
        got = bool(await pg.fetchval(_LOCK_SQL, key))
        try:
            yield got
        finally:
            if got:
                released = False
                try:
                    released = bool(await pg.fetchval(_UNLOCK_SQL, key))
                except Exception as exc:  # noqa: BLE001 - the sync result stands; the lock is dropped below
                    log.warning("scn sync unlock failed", tenant=tenant, connector=connector,
                                error=type(exc).__name__)
                finally:
                    if not released:
                        pg.terminate()  # synchronous: cannot be interrupted by a second cancellation
                        with suppress(Exception):
                            await conn.invalidate()


async def _sync_once(tenant: str, connector: str, reason: str) -> SnapshotRow:
    conn, mode = await _sync_mode(tenant, connector)
    async with _sync_lock(tenant, connector) as acquired:
        if not acquired:
            raise SyncInProgress(f"a sync of {connector!r} is already running")
        await _fail_orphans(tenant, connector)
        sid, started_at = await _insert_loading(tenant, connector, reason)
        t0 = time.monotonic()
        log.info("scn sync started", tenant=tenant, connector=connector, snapshot=str(sid), reason=reason)
        try:
            async with asyncio.timeout(get_settings().scn_sync_timeout_seconds):
                if mode == "crawl":
                    from keelson_engine.core import scn_crawl

                    row = await scn_crawl.run_crawl(conn, sid, t0)
                else:
                    row = await _run(conn, sid, t0)
        except BaseException as exc:
            code, detail = _classify(exc, tenant, connector, sid)
            failed = await asyncio.shield(_record_failure(tenant, connector, sid, code, detail, t0))
            log.warning("scn sync failed", tenant=tenant, connector=connector, snapshot=str(sid), code=code,
                        detail=detail)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise SyncFailed(failed, code) from None
        await _after_success(tenant, connector, started_at)
    try:
        await _gc(tenant, connector)
    except Exception as exc:  # noqa: BLE001 - GC failure never fails a good sync
        log.warning("scn gc failed", tenant=tenant, connector=connector, error=type(exc).__name__)
    log.info("scn sync ready", tenant=tenant, connector=connector, snapshot=row.id,
             rows=sum(row.row_counts.values()), bytes=row.bytes, duration_ms=row.duration_ms)
    # M6: push the tenant network (nodes + lane paths) to the Signal Hub; best effort, never raises
    from keelson_engine.core.network import on_sync_ready

    await on_sync_ready(tenant, connector)
    return await get_snapshot(tenant, row.id) or row


def _classify(exc: BaseException, tenant: str, connector: str, sid: uuid.UUID) -> tuple[str, str]:
    from keelson_engine.public_errors import report_exception

    if isinstance(exc, _Fail):
        return exc.code, exc.detail
    from keelson_engine.core.scn_crawl import CrawlError

    if isinstance(exc, CrawlError):
        return exc.code, exc.detail
    if isinstance(exc, TimeoutError):
        return "timeout", f"the sync exceeded {get_settings().scn_sync_timeout_seconds:.0f} s"
    if isinstance(exc, asyncio.CancelledError):
        return "cancelled", "the sync was cancelled"
    error_id = report_exception(log, "scn sync crashed", exc, tenant=tenant, connector=connector, snapshot=str(sid))
    return "internal_error", f"unexpected {type(exc).__name__} (error ID {error_id})"


async def _fail_orphans(tenant: str, connector: str) -> None:
    """``loading`` rows left by a crashed process (we hold the lock, so none is live). A crawl
    streams its rows before the flip, so an orphaned crawl's rows are deleted too."""
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text("UPDATE scn_snapshot SET status = 'failed', error = 'interrupted: the syncing process stopped' "
                 "WHERE tenant = :t AND connector = :c AND status = 'loading' RETURNING id"),
            {"t": tenant, "c": connector},
        )
        orphans = [r[0] for r in res.all()]
    if orphans:
        from keelson_engine.core.scn_crawl import purge_rows

        await purge_rows(tenant, orphans)


async def _insert_loading(tenant: str, connector: str, reason: str) -> tuple[uuid.UUID, datetime]:
    async with tenant_session(tenant) as s:
        r = (
            await s.execute(
                text("INSERT INTO scn_snapshot (id, tenant, connector, status, reason) "
                     "VALUES (:i, :t, :c, 'loading', :r) RETURNING id, created_at"),
                {"i": uuid.uuid7(), "t": tenant, "c": connector, "r": reason[:200]},
            )
        ).one()
    return r[0], r[1]


async def _record_failure(tenant: str, connector: str, sid: uuid.UUID, code: str, detail: str,
                          t0: float) -> SnapshotRow:
    st = get_settings()
    error = f"{code}: {detail}"[:_ERROR_MAX]
    duration_ms = int((time.monotonic() - t0) * 1000)
    async with tenant_session(tenant) as s:
        marked = (await s.execute(
            text("UPDATE scn_snapshot SET status = 'failed', error = :e, duration_ms = :d "
                 "WHERE tenant = :t AND id = :i AND status = 'loading' RETURNING id"),
            {"t": tenant, "i": sid, "e": error, "d": duration_ms},
        )).first()
        if marked is None:
            # the attempt row is gone (connector deleted mid-sync: purge_connector): no retry state
            # for a connector that no longer exists
            return SnapshotRow(id=str(sid), tenant=tenant, connector=connector, status="failed", error=error,
                               duration_ms=duration_ms)
        await s.execute(
            text(
                """
                INSERT INTO scn_sync_state AS st (tenant, connector, stale_since, due_at, reason, attempts,
                                                  retry_at, last_attempt_at, last_error, updated_at)
                VALUES (:t, :c, now(), now() + make_interval(secs => :b), 'retry', 1,
                        now() + make_interval(secs => :b), now(), :e, now())
                ON CONFLICT (tenant, connector) DO UPDATE SET
                  stale_since = COALESCE(st.stale_since, now()),
                  attempts = st.attempts + 1,
                  retry_at = now() + make_interval(secs => LEAST(:b * power(2, LEAST(st.attempts, 10)), :cap)),
                  due_at = now() + make_interval(secs => LEAST(:b * power(2, LEAST(st.attempts, 10)), :cap)),
                  last_attempt_at = now(), last_error = :e, updated_at = now()
                """
            ),
            {"t": tenant, "c": connector, "e": error, "b": float(st.scn_sync_retry_seconds), "cap": _MAX_RETRY_S},
        )
    row = await get_snapshot(tenant, str(sid))
    return row or SnapshotRow(id=str(sid), tenant=tenant, connector=connector, status="failed", error=error,
                              duration_ms=duration_ms)


async def _after_success(tenant: str, connector: str, started_at: datetime) -> None:
    """Clear the stale mark unless it was touched after this sync began (then stay due): an event
    (``mark_stale``) -> due ``debounce`` after it; a ``request_sync`` (admin "Sync now", start-up)
    after the last event -> keeps its "due now". While a sync runs only mark_stale / request_sync
    bump ``updated_at`` (failures are recorded by the syncing process itself, after the run)."""
    async with tenant_session(tenant) as s:
        await s.execute(
            text(
                """
                UPDATE scn_sync_state SET
                  attempts = 0, retry_at = NULL, last_error = NULL, last_attempt_at = now(),
                  stale_since = CASE WHEN last_event_at > :s THEN last_event_at
                                     WHEN updated_at > :s THEN updated_at END,
                  due_at = CASE WHEN updated_at > :s AND (last_event_at IS NULL OR updated_at > last_event_at)
                                  THEN due_at
                                WHEN last_event_at > :s THEN last_event_at + make_interval(secs => :deb) END,
                  updated_at = now()
                WHERE tenant = :t AND connector = :c
                """
            ),
            {"t": tenant, "c": connector, "s": started_at, "deb": float(get_settings().scn_sync_debounce_seconds)},
        )


# ───────────────────────────── the pipeline ─────────────────────────────


@dataclass(slots=True)
class _Part:
    entity: str
    index: int
    uri: str
    sha256: str
    rows: int
    bytes: int
    path: Path | None = None


def _origin(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc.rsplit('@', 1)[-1]}"


def _where(uri: str) -> str:
    """Host + path of a part URL for messages (never the signed query string)."""
    p = urlsplit(uri)
    return f"{p.hostname or '?'}{p.path}"


def _validation_summary(exc: ValidationError) -> str:
    errs = exc.errors()[:3]
    parts = [f"{'.'.join(str(x) for x in e.get('loc', ()))}: {e.get('msg', '')}" for e in errs]
    return "; ".join(parts)[:300]


async def _tool(client: Any, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """``structuredContent`` of one tool call (``_Fail`` on a tool error)."""
    from mcp import types as mcp_types

    result = await client.call_tool_mcp(name, args)
    if result.is_error:
        msg = " ".join(b.text for b in result.content if isinstance(b, mcp_types.TextContent))
        raise _Fail("tool_error", f"{name} failed: {msg[:200]}")
    data = result.structured_content
    if not isinstance(data, dict):
        raise _Fail("tool_error", f"{name} returned no structured content")
    return data


async def _call_tools(url: str, headers: dict[str, str],
                      calls: Sequence[tuple[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    """Call tools in order on one MCP session (``_Fail`` on errors; transport errors by class name only)."""
    from keelson_engine.core.mcp import mcp_client

    out: list[dict[str, Any]] = []
    try:
        async with mcp_client(url, headers) as client:
            for name, args in calls:
                out.append(await _tool(client, name, args))
    except (_Fail, asyncio.CancelledError, TimeoutError):
        raise
    except Exception as exc:  # noqa: BLE001 - transport/auth: class name only (URLs may carry secrets)
        raise _Fail("connector_error", f"MCP call failed ({type(exc).__name__})") from None
    return out


async def _open_and_export(conn: Any, sid: uuid.UUID) -> tuple[scn.SnapshotManifest, list[_Part]]:
    from keelson_engine.core.connectors import headers_for

    try:
        headers = await headers_for(conn, "reader")
    except Exception as exc:  # noqa: BLE001 - TokenError etc.; never the token
        raise _Fail("auth_failed", f"reader credentials unavailable ({type(exc).__name__})") from None
    from keelson_engine.core.mcp import mcp_client

    entities = list(scn.SCN_ENTITIES)
    parts: list[_Part] = []
    try:
        async with mcp_client(conn.url, headers) as client:
            raw = await _tool(client, "scn.snapshot_open", {"entities": entities})
            try:
                manifest = scn.SnapshotManifest.model_validate(raw)
            except ValidationError as exc:
                raise _Fail("manifest_invalid", _validation_summary(exc)) from None
            missing = [e for e in entities if e not in manifest.entities]
            if missing:
                raise _Fail("manifest_incomplete", f"the snapshot lacks {', '.join(missing)}")
            await _note_source(conn.tenant, sid, manifest)
            allowed = [_origin(conn.url)]
            for entity in entities:
                ent = manifest.entities[entity]
                raw = await _tool(client, "scn.snapshot_export", {"snapshot_id": manifest.snapshot_id, "entity": entity})
                try:
                    export = scn.SnapshotExport.model_validate(raw)
                except ValidationError as exc:
                    raise _Fail("export_invalid", f"{entity}: {_validation_summary(exc)}") from None
                if export.snapshot_id not in (None, manifest.snapshot_id) or export.entity not in (None, entity):
                    raise _Fail("export_mismatch", f"{entity}: the export answers another snapshot/entity")
                if len(export.parts) != ent.parts:
                    raise _Fail("manifest_mismatch", f"{entity}: {len(export.parts)} parts, manifest says {ent.parts}")
                if export.rows != ent.rows:
                    raise _Fail("row_count_mismatch", f"{entity}: export lists {export.rows} rows, manifest {ent.rows}")
                for i, p in enumerate(export.parts):
                    if not scn.part_uri_allowed(p.uri, allowed):
                        raise _Fail("foreign_host",
                                    f"{entity} part {i + 1} is on {urlsplit(p.uri).hostname!s}, not the connector host")
                    if ent.sha256 and ent.sha256[i] != p.sha256:
                        raise _Fail("manifest_mismatch", f"{entity} part {i + 1}: sha256 differs from the manifest")
                    parts.append(_Part(entity, i + 1, p.uri, p.sha256, p.rows, p.bytes))
    except (_Fail, asyncio.CancelledError, TimeoutError):
        raise
    except Exception as exc:  # noqa: BLE001 - transport/auth: class name only (URLs may carry secrets)
        raise _Fail("connector_error", f"MCP call failed ({type(exc).__name__})") from None
    return manifest, parts


async def _note_source(tenant: str, sid: uuid.UUID, manifest: scn.SnapshotManifest) -> None:
    """Record the source's snapshot id, as-of and watermark. M8 (``122`` §8): a source that is not
    point-in-time (an adapter sidecar crawling a REST API) says ``consistency: crawled`` with its
    ``as_of_range``; the snapshot is then labelled like a tier-A crawl (plans on it are advisory)."""
    rng = manifest.as_of_range
    async with tenant_session(tenant) as s:
        await s.execute(
            text("UPDATE scn_snapshot SET source_snapshot_id = :src, as_of = :a, watermark = :w, consistency = :k, "
                 "as_of_from = :f, as_of_to = :to WHERE tenant = :t AND id = :i"),
            {"t": tenant, "i": sid, "src": manifest.snapshot_id[:200], "a": manifest.as_of,
             "w": manifest.watermark[:200], "k": manifest.consistency, "f": rng.from_ if rng else None,
             "to": rng.to if rng else None},
        )


async def _download(client: httpx.AsyncClient, part: _Part, dest: Path) -> None:
    st = get_settings()
    if part.bytes > st.scn_sync_max_part_bytes:
        raise _Fail("part_too_large", f"{part.entity} part {part.index}: {part.bytes} bytes")
    digest = hashlib.sha256()
    size = 0
    try:
        async with client.stream("GET", part.uri) as resp:
            if resp.status_code != 200:
                raise _Fail("download_failed", f"HTTP {resp.status_code} from {_where(part.uri)}")
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > part.bytes:
                        raise _Fail("size_mismatch", f"{part.entity} part {part.index} is larger than declared")
                    digest.update(chunk)
                    fh.write(chunk)
    except (_Fail, asyncio.CancelledError):
        raise
    except httpx.HTTPError as exc:
        raise _Fail("download_failed", f"{_where(part.uri)}: {type(exc).__name__}") from None
    if size != part.bytes:
        raise _Fail("size_mismatch", f"{part.entity} part {part.index}: {size} bytes, declared {part.bytes}")
    if digest.hexdigest() != part.sha256:
        raise _Fail("sha256_mismatch", f"{part.entity} part {part.index}: sha256 does not match")
    part.path = dest


async def _download_all(parts: list[_Part], workdir: Path) -> int:
    st = get_settings()
    total = sum(p.bytes for p in parts)
    if total > st.scn_sync_max_total_bytes:
        raise _Fail("snapshot_too_large", f"{total} bytes")
    sem = asyncio.Semaphore(max(1, st.scn_sync_download_concurrency))
    timeout = httpx.Timeout(st.scn_sync_download_timeout_seconds, connect=min(10.0, st.scn_sync_download_timeout_seconds))
    async with httpx.AsyncClient(proxy=st.connector_proxy_url, timeout=timeout, trust_env=False,
                                 follow_redirects=False) as client:

        async def one(p: _Part) -> None:
            async with sem:
                await _download(client, p, workdir / f"{p.entity}-{p.index:04d}.parquet")

        try:
            async with asyncio.TaskGroup() as tg:
                for p in parts:
                    tg.create_task(one(p))
        except BaseExceptionGroup as eg:
            raise _first_error(eg) from None
    return total


def _first_error(eg: BaseExceptionGroup[Any]) -> BaseException:
    for exc in eg.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            return _first_error(exc)
        if isinstance(exc, _Fail):
            return exc
    first = eg.exceptions[0]
    return _first_error(first) if isinstance(first, BaseExceptionGroup) else first


def _part_batches(path: Path, entity: str, expected_rows: int) -> Iterator[list[tuple[Any, ...]]]:
    """Validated row tuples (``SCN_COLUMNS`` order) of one Parquet part, ``BATCH_ROWS`` at a time.
    Runs in a worker thread (pyarrow + pydantic are CPU-bound)."""
    import pyarrow.parquet as pq

    cols = scn.SCN_COLUMNS[entity]
    model = scn.SCN_ROW_MODELS[entity]
    try:
        pf = pq.ParquetFile(path)
    except Exception as exc:  # noqa: BLE001 - corrupt file
        raise _Fail("parquet_invalid", f"{entity}: unreadable Parquet ({type(exc).__name__})") from None
    with pf:
        names = list(pf.schema_arrow.names)
        missing = [c for c in cols if c not in names]
        extra = [c for c in names if c not in cols]
        if missing or extra:
            raise _Fail("schema_mismatch", f"{entity}: missing {missing or '-'}, unknown {extra or '-'}")
        if pf.metadata.num_rows != expected_rows:
            raise _Fail("row_count_mismatch", f"{entity}: part has {pf.metadata.num_rows} rows, export says {expected_rows}")
        for batch in pf.iter_batches(batch_size=BATCH_ROWS, columns=list(cols)):
            out: list[tuple[Any, ...]] = []
            for raw in batch.to_pylist():
                try:
                    m = model.model_validate(raw)
                except ValidationError as exc:
                    sid = raw.get("source_id")
                    raise _Fail("data_invalid", f"{entity} {str(sid)[:80]!r}: {_validation_summary(exc)}") from None
                out.append(tuple(getattr(m, c) for c in cols))
            yield out


async def _records(entity: str, parts: list[_Part]) -> AsyncIterator[tuple[Any, ...]]:
    for p in parts:
        assert p.path is not None
        it = _part_batches(p.path, entity, p.rows)
        while True:
            batch = await asyncio.to_thread(next, it, None)
            if batch is None:
                break
            for rec in batch:
                yield rec


async def _load_entity(pg: Any, tenant: str, sid: uuid.UUID, entity: str, parts: list[_Part]) -> int:
    import asyncpg

    table = _TABLES[entity]
    tmp = f"scn_load_{entity}"
    cols = scn.SCN_COLUMNS[entity]
    collist = ", ".join(cols)
    await pg.execute(f"CREATE TEMP TABLE {tmp} ON COMMIT DROP AS SELECT {collist} FROM {table} WITH NO DATA")
    try:
        await pg.copy_records_to_table(tmp, records=_records(entity, parts), columns=list(cols))
        status = await pg.execute(
            f"INSERT INTO {table} (tenant, snapshot_id, {collist}) SELECT $1::text, $2::uuid, {collist} FROM {tmp}",
            tenant, sid,
        )
    except asyncpg.UniqueViolationError:
        raise _Fail("data_invalid", f"{entity}: duplicate source_id") from None
    except asyncpg.NotNullViolationError as exc:
        raise _Fail("data_invalid", f"{entity}: missing {exc.column_name or 'value'}") from None
    except asyncpg.DataError as exc:  # SQLSTATE class 22, e.g. a NUL character in a text value
        raise _Fail("data_invalid", f"{entity}: {type(exc).__name__} ({exc.sqlstate})") from None
    await pg.execute(f"DROP TABLE {tmp}")
    return int(status.rsplit(" ", 1)[-1])


FK_CHECKS: tuple[tuple[str, str], ...] = (
    # DISTINCT first: the freshly loaded rows have no planner statistics yet, and a per-row anti join
    # (81,900 inventory rows x every location) took > 10 s on the full profile; per distinct key it is ms.
    (
        "po_line.sku_code → item(sku)",
        """SELECT p.sku_code
             FROM (SELECT DISTINCT sku_code FROM scn_po_line WHERE tenant = $1 AND snapshot_id = $2) p
            WHERE NOT EXISTS (SELECT 1 FROM scn_item i WHERE i.tenant = $1 AND i.snapshot_id = $2
                               AND i.code = p.sku_code AND i.kind = 'sku')
            ORDER BY 1 LIMIT 5""",
    ),
    (
        "inventory_position.location → location",
        """SELECT v.location_kind || ':' || v.location_code
             FROM (SELECT DISTINCT location_kind, location_code FROM scn_inventory_position
                    WHERE tenant = $1 AND snapshot_id = $2) v
            WHERE NOT EXISTS (SELECT 1 FROM scn_location l WHERE l.tenant = $1 AND l.snapshot_id = $2
                               AND l.code = v.location_code AND l.kind = v.location_kind)
            ORDER BY 1 LIMIT 5""",
    ),
)


async def _run(conn: Any, sid: uuid.UUID, t0: float) -> SnapshotRow:
    tenant, connector = conn.tenant, conn.slug
    phases: dict[str, int] = {}
    mark = time.monotonic()

    def lap(name: str) -> None:
        nonlocal mark
        now = time.monotonic()
        phases[name] = int((now - mark) * 1000)
        mark = now

    manifest, parts = await _open_and_export(conn, sid)
    lap("source_ms")
    st = get_settings()
    workdir = Path(tempfile.mkdtemp(prefix="keelson-scn-", dir=st.scn_sync_tmp_dir))
    try:
        total_bytes = await _download_all(parts, workdir)
        lap("download_ms")
        by_entity: dict[str, list[_Part]] = {e: [] for e in scn.SCN_ENTITIES}
        for p in parts:
            by_entity[p.entity].append(p)
        counts: dict[str, int] = {}
        async with tenant_session(tenant) as s:
            pg = (await (await s.connection()).get_raw_connection()).driver_connection
            entity_ms: dict[str, int] = {}
            for entity, eparts in by_entity.items():
                e0 = time.monotonic()
                n = await _load_entity(pg, tenant, sid, entity, eparts)
                entity_ms[entity] = int((time.monotonic() - e0) * 1000)
                expected = manifest.entities[entity].rows
                if n != expected:
                    raise _Fail("row_count_mismatch", f"{entity}: loaded {n} rows, manifest says {expected}")
                counts[entity] = n
            lap("load_ms")
            for label, sql in FK_CHECKS:
                bad = [r[0] for r in await pg.fetch(sql, tenant, sid)]
                if bad:
                    raise _Fail("fk_violation", f"{label}: {', '.join(bad)}")
            lap("check_ms")
            # Planner statistics for the new rows BEFORE readers can see them. A fresh snapshot_id
            # is absent from the old statistics, so the planner estimates ~1 row per
            # (tenant, snapshot_id) filter and picks nested loops: on the full profile the first
            # ProblemData build after a sync took ~58 s instead of ~1 s until autovacuum caught up.
            # ANALYZE runs in this transaction (it samples our own uncommitted rows) and its
            # pg_statistic rows commit together with the pointer flip.
            for table in _TABLES.values():
                await pg.execute(f"ANALYZE {table}")
            lap("analyze_ms")
            # pointer flip (same transaction as the rows)
            await pg.execute(
                "UPDATE scn_snapshot SET status = 'superseded' "
                "WHERE tenant = $1 AND connector = $2 AND status = 'ready' AND id <> $3",
                tenant, connector, sid,
            )
            flipped = await pg.fetchval(
                "UPDATE scn_snapshot SET status = 'ready', ready_at = now(), row_counts = $3::jsonb, bytes = $4, "
                "duration_ms = $5, error = NULL WHERE tenant = $1 AND id = $2 AND status = 'loading' RETURNING id",
                tenant, sid, dumps(counts), total_bytes, int((time.monotonic() - t0) * 1000),
            )
            if flipped is None:
                raise _Fail("interrupted", "the attempt was marked failed by another process")
            await pg.execute(
                "INSERT INTO scn_current (tenant, connector, snapshot_id, updated_at) VALUES ($1, $2, $3, now()) "
                "ON CONFLICT (tenant, connector) DO UPDATE SET snapshot_id = EXCLUDED.snapshot_id, updated_at = now()",
                tenant, connector, sid,
            )
        lap("commit_ms")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    log.info("scn sync phases", tenant=tenant, connector=connector, snapshot=str(sid), **phases,
             entity_ms=entity_ms)
    return SnapshotRow(id=str(sid), tenant=tenant, connector=connector, status="ready",
                       source_snapshot_id=manifest.snapshot_id, as_of=manifest.as_of, watermark=manifest.watermark,
                       row_counts=counts, bytes=total_bytes, duration_ms=int((time.monotonic() - t0) * 1000))


# ───────────────────────────── GC / purge ─────────────────────────────


async def _delete_rows(s: Any, tenant: str, ids: list[uuid.UUID]) -> None:
    for table in _TABLES.values():
        await s.execute(text(f"DELETE FROM {table} WHERE tenant = :t AND snapshot_id = ANY(:ids)"),
                        {"t": tenant, "ids": ids})


_GC_GUARD = """
   AND NOT EXISTS (SELECT 1 FROM scn_pin p WHERE p.tenant = s.tenant AND p.snapshot_id = s.id)
   AND NOT EXISTS (SELECT 1 FROM scn_current c WHERE c.tenant = s.tenant AND c.snapshot_id = s.id)
"""
"""SQL fragment (snapshot alias ``s``): neither pinned nor current."""


async def _mark(s: Any, tenant: str, candidates: list[Any]) -> list[Any]:
    """Second step of marking (the caller locked the candidates ``FOR UPDATE``): re-check pins and
    the current pointer in a NEW statement (READ COMMITTED: fresh snapshot). The locking statement
    evaluated its ``NOT EXISTS`` with the snapshot taken before it waited, so a pin committed
    meanwhile would otherwise be marked — and its snapshot's rows deleted — silently."""
    if not candidates:
        return []
    res = await s.execute(
        text(
            f"""
            UPDATE scn_snapshot s SET gc_requested_at = now()
             WHERE s.tenant = :t AND s.id = ANY(:ids) AND s.gc_requested_at IS NULL {_GC_GUARD}
            RETURNING s.id
            """
        ),
        {"t": tenant, "ids": candidates},
    )
    return [r[0] for r in res.all()]


async def _gc(tenant: str, connector: str) -> list[str]:
    """Mark the superseded snapshots beyond the newest ``SCN_SYNC_KEEP_SNAPSHOTS`` (ready +
    superseded, unmarked) that are neither current nor pinned for GC — :func:`collect_garbage`
    deletes them in the background — and cap the failed history (failed attempts hold no rows:
    their load transaction rolled back). Returns the marked ids. Only registry rows are touched,
    so this is fast at any data volume."""
    st = get_settings()
    keep = max(1, st.scn_sync_keep_snapshots)
    async with tenant_session(tenant) as s:
        # 1) lock the candidates (in id order, as mark_noncurrent: a concurrent demo reset and a
        #    sync GC never deadlock on each other). A pin being inserted holds a key-share lock on
        #    its snapshot row (pin_snapshot / FK check), so this waits for it; a pin attempted after
        #    this point waits for us and then sees the mark (pin_snapshot -> SnapshotNotFound).
        candidates = [
            r[0]
            for r in (
                await s.execute(
                    text(
                        f"""
                        SELECT s.id FROM scn_snapshot s
                         WHERE s.tenant = :t AND s.connector = :c AND s.status = 'superseded'
                           AND s.gc_requested_at IS NULL
                           AND s.id NOT IN (SELECT k.id FROM scn_snapshot k
                                             WHERE k.tenant = :t AND k.connector = :c
                                               AND k.status IN ('ready', 'superseded')
                                               AND k.gc_requested_at IS NULL
                                             ORDER BY k.created_at DESC, k.id DESC LIMIT :keep)
                           {_GC_GUARD}
                         ORDER BY s.id
                         FOR UPDATE OF s
                        """
                    ),
                    {"t": tenant, "c": connector, "keep": keep},
                )
            ).all()
        ]
        # 2) mark, re-checking pins with a fresh snapshot
        victims = await _mark(s, tenant, candidates)
        await s.execute(
            text(
                """
                DELETE FROM scn_snapshot WHERE tenant = :t AND connector = :c AND status = 'failed'
                   AND id NOT IN (SELECT id FROM scn_snapshot WHERE tenant = :t AND connector = :c AND status = 'failed'
                                   ORDER BY created_at DESC, id DESC LIMIT :kf)
                """
            ),
            {"t": tenant, "c": connector, "kf": max(0, st.scn_sync_keep_failed)},
        )
    if victims:
        log.info("scn gc marked", tenant=tenant, connector=connector, snapshots=[str(v) for v in victims])
    return [str(v) for v in victims]


async def mark_noncurrent(tenant: str) -> int:
    """Demo reset (M5, ``95`` §6): mark every snapshot of ``tenant`` that is not current, not
    loading and not pinned for GC (the reset deletes the cases first, so their plans' pins are
    gone); returns how many. Only registry rows are updated — the reset never waits for the
    deletes (:func:`collect_garbage` in the worker). The current snapshots stay, so planning keeps
    working until the requested re-sync replaces them."""
    async with tenant_session(tenant) as s:
        candidates = [
            r[0]
            for r in (
                await s.execute(
                    text(
                        f"""
                        SELECT s.id FROM scn_snapshot s
                         WHERE s.tenant = :t AND s.status <> 'loading' AND s.gc_requested_at IS NULL
                           {_GC_GUARD}
                         ORDER BY s.id
                         FOR UPDATE OF s
                        """
                    ),
                    {"t": tenant},
                )
            ).all()
        ]
        marked = await _mark(s, tenant, candidates)
    if marked:
        log.info("scn snapshots marked for gc", tenant=tenant, snapshots=len(marked))
    return len(marked)


@dataclass(slots=True)
class GcProgress:
    """What one :func:`collect_garbage` call did."""

    snapshots: int = 0
    """Registry rows deleted (snapshots fully collected)."""
    rows: int = 0
    """Entity rows deleted."""
    batches: int = 0
    """Delete statements run (one short transaction each)."""
    pending: int = 0
    """Marked snapshots left for a later call (budget used up, or another process is on them)."""
    skipped: int = 0
    """Marked snapshots that are current or pinned after all (never touched; logged as an error)."""

    @property
    def done(self) -> bool:
        return self.pending == 0


async def _gc_guard(s: Any, tenant: str, sid: Any) -> str:
    """``ok`` (still marked, not current, not pinned) | ``gone`` | ``unsafe``."""
    row = (await s.execute(
        text(f"SELECT s.gc_requested_at IS NOT NULL {_GC_GUARD} FROM scn_snapshot s WHERE s.tenant = :t AND s.id = :i"),
        {"t": tenant, "i": sid},
    )).first()
    if row is None:
        return "gone"
    return "ok" if row[0] else "unsafe"


async def _collect_one(tenant: str, sid: Any, *, batch_rows: int, budget: int | None, pause_s: float,
                       progress: GcProgress) -> str:
    """Delete one marked snapshot's rows batch by batch, then its registry row.
    Returns ``done`` | ``pending`` (budget used up / collected elsewhere right now) | ``skipped``.
    The tables still holding rows of the snapshot are probed first (one index probe each), so a
    resumed collection never spends its budget on tables emptied by an earlier pass."""
    probe = " UNION ALL ".join(
        f"SELECT '{table}' WHERE EXISTS (SELECT 1 FROM {table} WHERE tenant = :t AND snapshot_id = :i)"
        for table in _TABLES.values()
    )
    async with tenant_session(tenant) as s:
        tables = [str(r[0]) for r in (await s.execute(text(probe), {"t": tenant, "i": sid})).all()]
    for table in tables:
        while True:
            if budget is not None and progress.batches >= budget:
                return "pending"
            async with tenant_session(tenant) as s:
                locked = (await s.execute(text("SELECT pg_try_advisory_xact_lock(hashtextextended(:k, 0))"),
                                          {"k": f"keelson:scn_gc|{sid}"})).scalar_one()
                if not locked:
                    return "pending"  # another worker is collecting this snapshot
                state = await _gc_guard(s, tenant, sid)
                if state == "gone":
                    return "done"  # collected by another process meanwhile
                if state == "unsafe":
                    log.error("scn gc refused: a snapshot marked for GC is current or pinned", tenant=tenant,
                              snapshot=str(sid))
                    return "skipped"
                res = await s.execute(
                    text(f"DELETE FROM {table} WHERE ctid = ANY(ARRAY("
                         f"SELECT ctid FROM {table} WHERE tenant = :t AND snapshot_id = :i LIMIT :n))"),
                    {"t": tenant, "i": sid, "n": batch_rows},
                )
                n = int(res.rowcount or 0)
            progress.batches += 1
            progress.rows += n
            await asyncio.sleep(pause_s)  # yield: syncs, readers and runs share the database
            if n < batch_rows:
                break
    async with tenant_session(tenant) as s:
        deleted = (await s.execute(
            text(f"DELETE FROM scn_snapshot s WHERE s.tenant = :t AND s.id = :i AND s.gc_requested_at IS NOT NULL "
                 f"{_GC_GUARD} RETURNING s.id"),
            {"t": tenant, "i": sid},
        )).first()
    if deleted is not None:
        progress.snapshots += 1
    return "done"


async def collect_garbage(*, batch_rows: int = GC_BATCH_ROWS, max_batches: int | None = None, pause_s: float = 0.0,
                          tenant: str | None = None) -> GcProgress:
    """Delete the snapshots marked for GC (every tenant, or only ``tenant``), oldest mark first:
    each entity table in batches of ``batch_rows`` rows (one statement and one short transaction
    per batch, ``asyncio.sleep(pause_s)`` between batches), then the registry row. Stops after
    ``max_batches`` statements (``GcProgress.pending`` > 0: call again). Every batch re-checks,
    under a per-snapshot advisory lock (two workers never collect the same snapshot at once), that
    the snapshot is still marked and neither current nor pinned. Safe to stop at any point: the
    mark stays until the registry row is deleted, so the next call continues."""
    batch_rows = max(1, int(batch_rows))
    budget = None if max_batches is None else max(1, int(max_batches))
    async with system_session() as s:
        res = await s.execute(
            text("SELECT tenant, id FROM scn_snapshot WHERE gc_requested_at IS NOT NULL "
                 "AND (CAST(:t AS text) IS NULL OR tenant = CAST(:t AS text)) ORDER BY gc_requested_at, id"),
            {"t": tenant},
        )
        marked = [(str(r[0]), r[1]) for r in res.all()]
    progress = GcProgress()
    for t, sid in marked:
        if budget is not None and progress.batches >= budget:
            progress.pending += 1
            continue
        outcome = await _collect_one(t, sid, batch_rows=batch_rows, budget=budget, pause_s=pause_s,
                                     progress=progress)
        if outcome == "pending":
            progress.pending += 1
        elif outcome == "skipped":
            progress.skipped += 1
    if progress.rows or progress.snapshots:
        log.info("scn gc", snapshots=progress.snapshots, rows=progress.rows, batches=progress.batches,
                 pending=progress.pending)
    return progress


async def purge_connector(s: Any, tenant: str, connector: str) -> None:
    """Delete every scn row of ``connector`` inside the caller's tenant transaction ``s``
    (connector deletion). Pins go with their snapshots."""
    ids = [r[0] for r in (await s.execute(text("SELECT id FROM scn_snapshot WHERE tenant = :t AND connector = :c"),
                                          {"t": tenant, "c": connector})).all()]
    await s.execute(text("DELETE FROM scn_current WHERE tenant = :t AND connector = :c"), {"t": tenant, "c": connector})
    await s.execute(text("DELETE FROM scn_sync_state WHERE tenant = :t AND connector = :c"),
                    {"t": tenant, "c": connector})
    if ids:
        await _delete_rows(s, tenant, ids)
        await s.execute(text("DELETE FROM scn_snapshot WHERE tenant = :t AND id = ANY(:ids)"), {"t": tenant, "ids": ids})
