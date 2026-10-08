"""Port to the engine's canonical ``scn/1.0`` store (``80-m3-deterministic-core.md`` §1).

The platform side (``core/scn_sync.py``, E1) owns the tables, the sync and the snapshot pointer.
The runtime uses:

* ``current_snapshot(tenant, connector)`` → the ready snapshot ``scn_current`` points to (or ``None``);
* ``pin_snapshot(tenant, snapshot_id, plan_id)`` → plans pin the snapshot they were built from
  (never garbage-collected while pinned); ``unpin_plan(tenant, plan_id)`` releases the pins of a
  superseded plan version (it can no longer be applied, so it no longer needs its snapshot);
* ``mark_stale(tenant, connector, reason=…)`` → after the engine itself changed the source system
  (an applied change set), so the next ``due_syncs`` schedules a resync;
* ``due_syncs()`` → ``[DueSync(tenant, connector, reason)]`` due now (debounced stale marks from
  webhooks and change-feed polls, failure retries, admin / nightly requests);
* ``poll_changes(tenant, connector)`` → the tier-C ``scn.changes_since`` poll (marks stale on any
  change; :class:`SyncUnavailable`-style errors for connectors without a change feed);
* ``request_sync(tenant, connector, reason)`` → make a sync due now (nightly);
* ``list_sync_connectors()`` → every active connector with approved snapshot tools (start-up) or,
  M7, a read mapping (tier A: the platform crawls it, ``core/scn_crawl.py``);
* ``sync_connector(tenant, connector, reason=…)`` → one full sync (single-flight per connector; M7:
  a tier-A connector is crawled into a ``crawled`` snapshot);
* ``get_snapshot(tenant, snapshot_id)`` → any snapshot with its M7 label (``consistency``,
  ``as_of_range``): a plan pinned to a crawled snapshot stays advisory on later runs;
* reads of ``scn_*`` rows through ``core.db.tenant_session`` (RLS) — :meth:`ScnPort.reader`.

:class:`CoreScnAdapter` binds these to the E1 modules lazily and degrades gracefully while the
platform side is not deployed yet (no snapshot → the M2 MCP gatherer, labelled advisory).
"""

from __future__ import annotations

import contextlib
import datetime as dt
import inspect
from collections.abc import AsyncIterator, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

import structlog

from .errors import RuntimeConfigError

__all__ = [
    "CoreScnAdapter",
    "snapshot_label_of",
    "DueSync",
    "ScnPort",
    "SessionReader",
    "SnapshotRef",
    "SqlReader",
]

log = structlog.get_logger(__name__)


def _get(row: Any, name: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, Mapping):
        return row.get(name, default)
    return getattr(row, name, default)


def _date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    try:
        return dt.datetime.fromisoformat(str(value)).date()
    except ValueError:
        return None


@dataclass(frozen=True)
class SnapshotRef:
    """The ready snapshot a ProblemData is built from (``scn_snapshot`` row)."""

    id: str
    connector: str
    as_of: dt.date | None = None
    watermark: str | None = None
    source_snapshot_id: str | None = None
    status: str = "ready"
    row_counts: dict[str, int] = field(default_factory=dict)
    consistency: str = "consistent"
    """M7 (``110`` §4): ``consistent`` (tier-B export at ``as_of``) or ``crawled`` (tier-A paging)."""
    as_of_range: dict[str, str] | None = None
    """``{"from": iso, "to": iso}`` when the data was read by a crawl (``None`` otherwise)."""

    @property
    def label(self) -> dict[str, Any]:
        """The ``SnapshotLabel`` JSON (``{consistency, as_of_range}``) plans carry in their KPIs."""
        crawled = self.consistency == "crawled" and self.as_of_range is not None
        return {"consistency": "crawled" if crawled else "consistent",
                "as_of_range": dict(self.as_of_range) if crawled and self.as_of_range else None}

    @property
    def advisory(self) -> bool:
        return self.consistency == "crawled"

    @classmethod
    def from_row(cls, row: Any, *, connector: str) -> SnapshotRef:
        counts = _get(row, "row_counts") or {}
        consistency, as_of_range = snapshot_label_of(row)
        return cls(
            id=str(_get(row, "id") or _get(row, "snapshot_id")),
            connector=str(_get(row, "connector") or connector),
            as_of=_date(_get(row, "as_of")),
            watermark=None if _get(row, "watermark") is None else str(_get(row, "watermark")),
            source_snapshot_id=None if _get(row, "source_snapshot_id") is None else str(_get(row, "source_snapshot_id")),
            status=str(_get(row, "status") or "ready"),
            row_counts={str(k): int(v) for k, v in dict(counts).items()} if isinstance(counts, Mapping) else {},
            consistency=consistency,
            as_of_range=as_of_range,
        )


def snapshot_label_of(row: Any) -> tuple[str, dict[str, str] | None]:
    """``(consistency, as_of_range)`` of a snapshot row (``core.scn_sync.SnapshotRow``, its JSON or
    a mapping): ``crawled`` only with a complete range ``{from, to}``; anything else is treated
    as ``consistent`` (tier B). Accepts ``as_of_range`` as ``{from, to}`` / ``{from_, to}`` or an
    object with ``from_``/``to`` attributes, or flat ``as_of_from`` / ``as_of_to`` columns."""
    consistency = str(_get(row, "consistency") or "consistent")
    raw = _get(row, "as_of_range")
    start = end = None
    if raw is not None:
        start = _aware_iso(_get(raw, "from") if isinstance(raw, Mapping) and "from" in raw else _get(raw, "from_"))
        end = _aware_iso(_get(raw, "to"))
    else:
        start, end = _aware_iso(_get(row, "as_of_from")), _aware_iso(_get(row, "as_of_to"))
    if consistency == "crawled" and start and end:
        return "crawled", {"from": start, "to": end}
    if consistency == "crawled":
        # still advisory: fall back to when the snapshot was taken
        when = (_aware_iso(_get(row, "as_of") or _get(row, "ready_at") or _get(row, "created_at"))
                or dt.datetime.now(dt.UTC).isoformat())
        log.warning("crawled snapshot without a complete as_of_range; using its timestamp", at=when)
        return "crawled", {"from": start or when, "to": end or start or when}
    return "consistent", None


def _aware_iso(value: Any) -> str | None:
    """An ISO date-time with a timezone (``AsOfRange`` needs aware values; dates are UTC midnight)."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return (value if value.tzinfo else value.replace(tzinfo=dt.UTC)).isoformat()
    if isinstance(value, dt.date):
        return dt.datetime(value.year, value.month, value.day, tzinfo=dt.UTC).isoformat()
    try:
        parsed = dt.datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)).isoformat()


@dataclass(frozen=True)
class DueSync:
    tenant: str
    connector: str
    reason: str

    @classmethod
    def from_row(cls, row: Any) -> DueSync:
        """``core.scn_sync.DueSync`` (``reason`` may be ``None`` → ``stale``), a mapping or a tuple."""
        if isinstance(row, tuple):
            tenant, connector, *rest = row
            return cls(str(tenant), str(connector), str(rest[0]) if rest and rest[0] else "stale")
        return cls(str(_get(row, "tenant")), str(_get(row, "connector")), str(_get(row, "reason") or "stale"))


class SqlReader(Protocol):
    async def fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]: ...


@runtime_checkable
class ScnPort(Protocol):
    async def current_snapshot(self, *, tenant: str, connector: str) -> SnapshotRef | None: ...
    async def pin_snapshot(self, *, tenant: str, snapshot_id: str, plan_id: Any) -> None: ...
    async def unpin_plan(self, *, tenant: str, plan_id: Any) -> None: ...
    async def mark_stale(self, *, tenant: str, connector: str, reason: str) -> None: ...
    async def sync_connector(self, *, tenant: str, connector: str, reason: str) -> Any: ...
    async def due_syncs(self) -> list[DueSync]: ...
    async def poll_changes(self, *, tenant: str, connector: str) -> bool:
        """Tier-C change-feed poll; ``True`` when it marked the connector stale."""
        ...
    async def request_sync(self, *, tenant: str, connector: str, reason: str) -> None: ...
    async def startup_targets(self) -> list[tuple[str, str]]:
        """``(tenant, connector)`` of every syncable connector (active, approved snapshot tools)."""
        ...
    def reader(self, tenant: str) -> AbstractAsyncContextManager[SqlReader]: ...


class SessionReader:
    """:class:`SqlReader` over an SQLAlchemy async session or connection (``text()`` + named params)."""

    def __init__(self, session: Any) -> None:
        self._s = session

    async def fetch(self, sql: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        from sqlalchemy import text  # noqa: PLC0415

        res = await self._s.execute(text(sql), dict(params))
        return [dict(m) for m in res.mappings().all()]


async def _call(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None
    if sig is not None and not any(p.kind is p.VAR_KEYWORD for p in sig.parameters.values()):
        kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}
    result = fn(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


class CoreScnAdapter:
    """:class:`ScnPort` on ``keelson_engine.core.scn_sync`` + ``core.db.tenant_session``."""

    def __init__(self) -> None:
        try:
            from keelson_engine.core import scn_sync  # type: ignore[attr-defined]  # noqa: PLC0415
        except ImportError:
            scn_sync = None
        self._m = scn_sync

    def _fn(self, name: str) -> Any:
        fn = getattr(self._m, name, None) if self._m is not None else None
        return fn if callable(fn) else None

    async def current_snapshot(self, *, tenant: str, connector: str) -> SnapshotRef | None:
        fn = self._fn("current_snapshot")
        if fn is None:
            return None
        row = await _call(fn, tenant, connector)
        if row is None:
            return None
        ref = SnapshotRef.from_row(row, connector=connector)
        return ref if ref.status == "ready" else None

    async def pin_snapshot(self, *, tenant: str, snapshot_id: str, plan_id: Any) -> None:
        fn = self._fn("pin_snapshot")
        if fn is None:
            log.debug("pin_snapshot not available; snapshot not pinned", snapshot_id=snapshot_id)
            return
        await _call(fn, tenant, snapshot_id, plan_id)

    async def unpin_plan(self, *, tenant: str, plan_id: Any) -> None:
        fn = self._fn("unpin_plan")
        if fn is not None:
            await _call(fn, tenant, plan_id)

    async def mark_stale(self, *, tenant: str, connector: str, reason: str) -> None:
        fn = self._fn("mark_stale")
        if fn is not None:
            await _call(fn, tenant, connector, reason=reason)

    async def sync_connector(self, *, tenant: str, connector: str, reason: str) -> Any:
        fn = self._fn("sync_connector")
        if fn is None:
            raise RuntimeConfigError("core.scn_sync.sync_connector is not available")
        return await _call(fn, tenant, connector, reason=reason)

    async def due_syncs(self) -> list[DueSync]:
        fn = self._fn("due_syncs")
        if fn is None:
            return []
        return [DueSync.from_row(r) for r in await _call(fn) or []]

    async def poll_changes(self, *, tenant: str, connector: str) -> bool:
        fn = self._fn("poll_changes")
        if fn is None:
            return False
        return bool(await _call(fn, tenant, connector))

    async def request_sync(self, *, tenant: str, connector: str, reason: str) -> None:
        fn = self._fn("request_sync")
        if fn is None:
            raise RuntimeConfigError("core.scn_sync.request_sync is not available")
        await _call(fn, tenant, connector, reason=reason)

    async def collect_garbage(self, *, batch_rows: int, max_batches: int, pause_s: float) -> Any:
        """One budgeted pass of the platform's background snapshot GC (``None`` without one)."""
        fn = self._fn("collect_garbage")
        if fn is None:
            return None
        return await _call(fn, batch_rows=batch_rows, max_batches=max_batches, pause_s=pause_s)

    async def startup_targets(self) -> list[tuple[str, str]]:
        fn = self._fn("list_sync_connectors")
        if fn is None:
            return []
        return [_pair(r) for r in await _call(fn) or []]

    async def get_snapshot(self, *, tenant: str, snapshot_id: str) -> SnapshotRef | None:
        """Any snapshot of the tenant by id (``None`` if unknown, GC'd or another tenant's)."""
        fn = self._fn("get_snapshot")
        if fn is None:
            return None
        row = await _call(fn, tenant, snapshot_id)
        return SnapshotRef.from_row(row, connector=str(_get(row, "connector") or "")) if row is not None else None

    @contextlib.asynccontextmanager
    async def _session(self, tenant: str) -> AsyncIterator[SqlReader]:
        """One RLS-scoped ``REPEATABLE READ READ ONLY`` transaction: every read of one build sees
        the same database snapshot (a concurrent GC cannot remove rows half-way through)."""
        from keelson_engine.core.db import tenant_session  # noqa: PLC0415

        kwargs = {"snapshot": True} if "snapshot" in inspect.signature(tenant_session).parameters else {}
        async with tenant_session(tenant, **kwargs) as s:
            yield SessionReader(s)

    def reader(self, tenant: str) -> AbstractAsyncContextManager[SqlReader]:
        return self._session(tenant)


def _pair(row: Any) -> tuple[str, str]:
    if isinstance(row, tuple):
        return str(row[0]), str(row[1])
    return str(_get(row, "tenant")), str(_get(row, "connector") or _get(row, "slug"))
