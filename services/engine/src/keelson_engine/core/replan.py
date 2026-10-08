"""Plan dependencies and webhook re-plan routing (``docs/contracts/95-m5-remediation.md`` §4).

**Dependencies.** Every saved plan version records what its assumptions fingerprint depends on
(``plan_dependency``): the **lanes** its options use (``lane_code`` / ``alt_lane_code`` /
``allowed_lanes`` values and DC pairs ``from_dc``/``to_dc`` → ``FROM>TO``), the inbound
**receipts** (PO lines ``PO#line``: ``po_line_ref``, ``replaces_po_line_ref``, ``receipt_ref`` and
``target_ref`` values of that shape) it relies on and the **connectors** it writes to.
:func:`extract_dependencies` walks the stored JSON generically (no scenario-specific code);
``repo.save_plan`` (``source='plan'``) and ``repo.create_change_set`` (``source='change_set'``)
call :func:`record_dependencies`.

**Routing.** :func:`route_event` (called by ``core.webhooks.receive`` for a newly stored event)
handles the plan-relevant structured event types in :data:`ROUTERS` that are enabled by
``REPLAN_EVENT_TYPES`` (default ``lane.capacity_changed``): it reads only structured fields
(``data.lane_code``, ``data.factor_bp``, ``data.weeks``, ``data.origin``/``data.dest`` — never free
text), finds the cases whose **latest** plan version is open or committed (not rolled back /
superseded, case not closed) and depends on the lane, and enqueues one ``signal`` run per case.

A plan depends on the event's lane when one of its ``lane`` dependencies is

* the lane **code** itself (inbound lanes, bookings; ``matched_by`` ``lane``), or
* the **transfer pseudo-lane** ``ORIGIN>DEST`` of the lane's (origin, dest) pair — plans record
  DC-to-DC transfers by their DC pair while the connector's lane events carry the lane's own code
  (``matched_by`` ``lane_pair``). The pair comes from the event (``origin``/``dest`` or
  ``origin_code``/``dest_code``) unless the connector's current canonical snapshot has the lane
  (``scn_lane``): then that row's pair is used (authoritative; a contradicting event pair is
  dropped from the signal). Events without the pair still match (:func:`lane_pairs`);

or one of its **receipts** sails on the lane in that snapshot (``matched_by`` ``receipt``).
``RunRequest.payload = {signal_id}`` where ``signal_id`` is a :data:`case_signal` row holding
``{event_id, kind: "replan", lane_code, factor_bp, weeks, connector, event_type, plan_version,
matched_by}`` (+ ``origin``/``dest`` when the event carries them and the snapshot does not
contradict them, + ``plan_lane_code`` — the pseudo-lane the plan uses — when the pair matched)
(:func:`get_signal`). The run's actor is the connector's webhook principal (no roles), so the
policy never auto-approves what the re-plan proposes. Idempotent per (case, event).
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from keelson_engine.config import get_settings
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger
from keelson_engine.core.sql import dumps

__all__ = [
    "CLOSED_PLAN_STATUSES",
    "DEPENDENCY_KINDS",
    "ROUTERS",
    "PlanDependencies",
    "ReplanTarget",
    "dependencies_of",
    "enabled_event_types",
    "extract_dependencies",
    "find_dependent_plans",
    "get_signal",
    "lane_pairs",
    "pair_ref",
    "record_dependencies",
    "route_event",
    "signal_actor",
]

log = get_logger(__name__)

DEPENDENCY_KINDS = ("lane", "receipt", "connector", "dc")
CLOSED_PLAN_STATUSES = frozenset({"rolled_back", "superseded", "discarded", "cancelled", "rejected"})
"""Plan statuses that no longer need re-planning (anything else is open or committed)."""
CLOSED_CASE_STATUSES = frozenset({"closed"})

LANE_KEYS = frozenset({"lane_code", "alt_lane_code", "lane", "inbound_lane_code", "inbound_lane"})
LANE_LIST_KEYS = frozenset({"allowed_lanes", "lanes", "lane_codes"})
RECEIPT_KEYS = frozenset({"po_line_ref", "replaces_po_line_ref", "receipt_ref"})
RECEIPT_REF_KEYS = frozenset({"target_ref", "ref"})
CONNECTOR_KEYS = frozenset({"connector"})
CONNECTOR_LIST_KEYS = frozenset({"connectors"})
_PO_LINE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,120}#\d{1,6}$")
_REF_MAX = 200
_MAX_REFS = 2000
_MAX_DEPTH = 16


@dataclass(slots=True)
class PlanDependencies:
    lanes: set[str] = field(default_factory=set)
    receipts: set[str] = field(default_factory=set)
    connectors: set[str] = field(default_factory=set)
    dcs: set[str] = field(default_factory=set)

    def rows(self) -> list[tuple[str, str]]:
        out = [("lane", r) for r in sorted(self.lanes)]
        out += [("receipt", r) for r in sorted(self.receipts)]
        out += [("connector", r) for r in sorted(self.connectors)]
        out += [("dc", r) for r in sorted(self.dcs)]
        return out[:_MAX_REFS]

    def update(self, other: PlanDependencies) -> None:
        self.lanes |= other.lanes
        self.receipts |= other.receipts
        self.connectors |= other.connectors
        self.dcs |= other.dcs

    def __bool__(self) -> bool:
        return bool(self.lanes or self.receipts or self.connectors or self.dcs)

    @classmethod
    def from_fingerprint(cls, fingerprint: Mapping[str, Any]) -> PlanDependencies:
        """The runtime's fingerprint ``{lanes, dcs, po_lines, connectors, committed_lanes}``."""

        def refs(*keys: str) -> set[str]:
            out: set[str] = set()
            for key in keys:
                raw = fingerprint.get(key)
                if isinstance(raw, (list, tuple, set, frozenset)):
                    out.update(v for v in (_ok(x) for x in raw) if v)
            return out

        return cls(lanes=refs("lanes", "committed_lanes"), receipts=refs("po_lines", "receipts"),
                   connectors=refs("connectors"), dcs=refs("dcs"))


def _ok(value: Any) -> str | None:
    if isinstance(value, str):
        v = value.strip()
        if 0 < len(v) <= _REF_MAX and not any(ord(ch) < 0x20 for ch in v):
            return v
    return None


def extract_dependencies(value: Any, *, connectors: Iterable[str] = ()) -> PlanDependencies:
    """Lanes, receipts and connectors referenced anywhere in ``value`` (plan options, change set
    items; models or JSON). Bounded in depth and size."""
    deps = PlanDependencies(connectors={c for c in (_ok(c) for c in connectors) if c})

    def walk(node: Any, depth: int) -> None:
        if depth > _MAX_DEPTH or len(deps.lanes) + len(deps.receipts) > _MAX_REFS:
            return
        if hasattr(node, "model_dump"):
            node = node.model_dump(mode="json")
        if isinstance(node, Mapping):
            frm, to = _ok(node.get("from_dc")), _ok(node.get("to_dc"))
            if frm and to and frm != to:
                deps.lanes.add(f"{frm}>{to}")
            for key, child in node.items():
                k = str(key)
                if k in LANE_KEYS and (v := _ok(child)):
                    deps.lanes.add(v)
                elif k in LANE_LIST_KEYS and isinstance(child, list):
                    deps.lanes.update(v for v in (_ok(c) for c in child) if v)
                elif k in RECEIPT_KEYS and (v := _ok(child)):
                    deps.receipts.add(v)
                elif k in RECEIPT_REF_KEYS and (v := _ok(child)) and _PO_LINE_RE.match(v):
                    deps.receipts.add(v)
                elif k in CONNECTOR_KEYS and (v := _ok(child)):
                    deps.connectors.add(v)
                elif k in CONNECTOR_LIST_KEYS and isinstance(child, list):
                    deps.connectors.update(v for v in (_ok(c) for c in child) if v)
                if isinstance(child, (Mapping, list, tuple)) or hasattr(child, "model_dump"):
                    walk(child, depth + 1)
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child, depth + 1)

    walk(value, 0)
    return deps


async def record_dependencies(session: AsyncSession, *, tenant: str, plan_id: int, case_id: str, plan_version: int,
                              deps: PlanDependencies, source: str = "plan", replace: bool = True) -> int:
    """Store ``deps`` for a plan row inside an open tenant transaction (``replace`` drops the rows
    of the same ``source`` first: re-saving a plan version replaces its fingerprint)."""
    if replace:
        await session.execute(
            text("DELETE FROM plan_dependency WHERE tenant = :t AND plan_id = :p AND source = :src"),
            {"t": tenant, "p": plan_id, "src": source},
        )
    rows = deps.rows()
    if not rows:
        return 0
    await session.execute(
        text(
            """
            INSERT INTO plan_dependency (tenant, plan_id, case_id, plan_version, kind, ref, source)
            SELECT :t, :p, :c, :v, d->>0, d->>1, :src FROM jsonb_array_elements(CAST(:rows AS jsonb)) AS d
            ON CONFLICT DO NOTHING
            """
        ),
        {"t": tenant, "p": plan_id, "c": case_id, "v": plan_version, "src": source, "rows": dumps(rows)},
    )
    return len(rows)


async def dependencies_of(tenant: str, case_id: str, plan_version: int) -> dict[str, list[str]]:
    """``{kind: [ref, …]}`` recorded for a plan version (both sources)."""
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text("SELECT DISTINCT kind, ref FROM plan_dependency WHERE case_id = :c AND plan_version = :v "
                 "ORDER BY kind, ref"),
            {"c": case_id, "v": plan_version},
        )
        out: dict[str, list[str]] = {k: [] for k in DEPENDENCY_KINDS}
        for kind, ref in res.all():
            out.setdefault(str(kind), []).append(str(ref))
    return out


# ───────────────────────────── routing ─────────────────────────────


@dataclass(frozen=True, slots=True)
class ReplanTarget:
    case_id: str
    plan_version: int
    matched_by: tuple[str, ...]
    lanes: tuple[str, ...] = ()
    """The plan's own ``lane`` dependency refs that matched (the lane code and/or ``ORIGIN>DEST``)."""

    @property
    def plan_lane_code(self) -> str | None:
        """The transfer pseudo-lane the plan uses for the event's lane (``lane_pair`` matches only)."""
        if "lane_pair" not in self.matched_by:
            return None
        return next((ref for ref in self.lanes if ">" in ref), None)


@dataclass(frozen=True, slots=True)
class RoutedEvent:
    """What a router extracted from an event's structured fields."""

    lane_code: str
    details: dict[str, Any]
    origin: str | None = None
    dest: str | None = None


def pair_ref(origin: str, dest: str) -> str:
    """The pseudo-lane code plans record for a DC pair (as :func:`extract_dependencies`: ``FROM>TO``)."""
    return f"{origin}>{dest}"


def _endpoint(data: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        v = _ok(data.get(key))
        if v is not None and ">" not in v and len(v) <= 64:
            return v
    return None


def _pair(data: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The lane's (origin, dest) from typed event fields (both or neither)."""
    origin = _endpoint(data, "origin", "origin_code")
    dest = _endpoint(data, "dest", "dest_code")
    if origin is None or dest is None or origin == dest:
        return None, None
    return origin, dest


def _with_pair(lane: str, details: dict[str, Any], data: Mapping[str, Any]) -> RoutedEvent:
    origin, dest = _pair(data)
    if origin is not None:
        details.update(origin=origin, dest=dest)
    return RoutedEvent(lane, details, origin, dest)


_ISO_WEEK = re.compile(r"^\d{4}-W(0[1-9]|[1-4]\d|5[0-3])$")


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _lane_capacity(data: Mapping[str, Any]) -> RoutedEvent | None:
    lane = _ok(data.get("lane_code"))
    factor = _int_or_none(data.get("factor_bp"))
    if lane is None or factor is None or not 0 <= factor <= 1_000_000:
        return None
    weeks = data.get("weeks")
    if isinstance(weeks, list):
        weeks_out: Any = [w for w in (_ok(x) for x in weeks) if w][:52]
    else:
        weeks_out = _int_or_none(weeks)
    details: dict[str, Any] = {"lane_code": lane, "factor_bp": factor, "weeks": weeks_out}
    # Merxa's window (ISO weeks) and per-week booking state are facts too; only typed values pass
    for key in ("from_week", "to_week"):
        week = data.get(key)
        if isinstance(week, str) and _ISO_WEEK.match(week):
            details[key] = week
    detail = data.get("weeks_detail")
    if isinstance(detail, list):
        rows = []
        for row in detail[:60]:
            if not isinstance(row, Mapping) or not (isinstance(row.get("iso_week"), str)
                                                    and _ISO_WEEK.match(row["iso_week"])):
                continue
            clean = {"iso_week": row["iso_week"]}
            for k in ("weekly_capacity_before", "weekly_capacity", "booked_units", "overbooked_units"):
                v = _int_or_none(row.get(k))
                if v is not None and v >= 0:
                    clean[k] = v
            rows.append(clean)
        if rows:
            details["weeks_detail"] = rows
    return _with_pair(lane, details, data)


def _sailing_updated(data: Mapping[str, Any]) -> RoutedEvent | None:
    lane = _ok(data.get("lane_code"))
    if lane is None:
        return None
    return _with_pair(lane, {"lane_code": lane, "factor_bp": None, "sailing_code": _ok(data.get("code"))}, data)


ROUTERS: dict[str, Callable[[Mapping[str, Any]], RoutedEvent | None]] = {
    "lane.capacity_changed": _lane_capacity,
    "sailing.updated": _sailing_updated,
}
"""Plan-relevant structured events → lane extraction (opt-in per ``REPLAN_EVENT_TYPES``)."""


def enabled_event_types() -> frozenset[str]:
    raw = get_settings().replan_event_types
    return frozenset(t.strip() for t in raw.split(",") if t.strip() in ROUTERS)


def signal_actor(connector: str) -> dict[str, Any]:
    """The principal a webhook-originated run executes as: no roles (never auto-approved)."""
    return {"sub": f"webhook:{connector}", "username": f"webhook:{connector}", "name": f"{connector} webhook",
            "roles": []}


async def lane_pairs(tenant: str, connector: str, lane_code: str, *, origin: str | None = None,
                     dest: str | None = None) -> tuple[str, ...]:
    """The transfer pseudo-lanes (``ORIGIN>DEST``) of ``lane_code``: the (origin, dest) of its lane
    row in ``connector``'s current snapshot when the snapshot has the lane (authoritative: a
    contradicting pair in the event never widens the routing to another DC pair), else the
    event's typed pair. Empty when neither gives a usable pair."""
    async with tenant_session(tenant) as s:
        rows = (await s.execute(
            text(
                """
                SELECT ln.origin_code, ln.dest_code
                FROM scn_current cur
                JOIN scn_lane ln ON ln.tenant = cur.tenant AND ln.snapshot_id = cur.snapshot_id
                WHERE cur.connector = :conn AND ln.code = :lane
                """
            ),
            {"conn": connector, "lane": lane_code},
        )).all()
    if rows:
        return tuple(sorted({pair_ref(str(o), str(d)) for o, d in rows
                             if o and d and o != d and ">" not in f"{o}{d}"}))
    if origin is None or dest is None or origin == dest:
        return ()
    return (pair_ref(origin, dest),)


async def find_dependent_plans(tenant: str, connector: str, lane_code: str, *, origin: str | None = None,
                               dest: str | None = None, pairs: Iterable[str] | None = None) -> list[ReplanTarget]:
    """Cases whose latest, still open/committed plan version depends on ``lane_code``: by the lane
    code, by a transfer pseudo-lane ``ORIGIN>DEST`` of the lane (``pairs``, else :func:`lane_pairs`
    of the arguments and ``connector``'s current snapshot), or through a receipt on a sailing of
    that lane in ``connector``'s current snapshot."""
    if pairs is None:
        pairs = await lane_pairs(tenant, connector, lane_code, origin=origin, dest=dest)
    pair_refs = sorted({p for p in pairs if p != lane_code})
    async with tenant_session(tenant) as s:
        res = await s.execute(
            text(
                """
                WITH latest AS (
                  SELECT DISTINCT ON (p.case_id) p.id, p.case_id, p.version, p.status
                  FROM plan p ORDER BY p.case_id, p.version DESC
                ),
                live AS (
                  SELECT l.* FROM latest l JOIN "case" c ON c.id = l.case_id
                  WHERE NOT (l.status = ANY(:closed_plan)) AND NOT (c.status = ANY(:closed_case))
                ),
                lane_receipts AS (
                  SELECT pl.po_code || '#' || pl.line_no AS ref
                  FROM scn_current cur
                  JOIN scn_po_line pl ON pl.tenant = cur.tenant AND pl.snapshot_id = cur.snapshot_id
                  JOIN scn_sailing sa ON sa.tenant = cur.tenant AND sa.snapshot_id = cur.snapshot_id
                                     AND sa.code = pl.sailing_code
                  WHERE cur.connector = :conn AND sa.lane_code = :lane
                ),
                matched AS (
                  SELECT v.id, v.case_id, v.version, d.ref,
                         CASE WHEN d.kind = 'receipt' THEN 'receipt'
                              WHEN d.ref = :lane THEN 'lane' ELSE 'lane_pair' END AS how
                  FROM live v
                  JOIN plan_dependency d ON d.plan_id = v.id
                  WHERE (d.kind = 'lane' AND (d.ref = :lane OR d.ref = ANY(CAST(:pairs AS text[]))))
                     OR (d.kind = 'receipt' AND d.ref IN (SELECT ref FROM lane_receipts))
                )
                SELECT m.case_id, m.version, array_agg(DISTINCT m.how) AS kinds,
                       array_agg(DISTINCT m.ref) FILTER (WHERE m.how <> 'receipt') AS lanes
                FROM matched m
                WHERE NOT EXISTS (SELECT 1 FROM plan_dependency x WHERE x.plan_id = m.id AND x.kind = 'connector')
                   OR EXISTS (SELECT 1 FROM plan_dependency x
                              WHERE x.plan_id = m.id AND x.kind = 'connector' AND x.ref = :conn)
                GROUP BY m.case_id, m.version
                ORDER BY m.case_id
                """
            ),
            {"closed_plan": sorted(CLOSED_PLAN_STATUSES), "closed_case": sorted(CLOSED_CASE_STATUSES),
             "conn": connector, "lane": lane_code, "pairs": pair_refs},
        )
        return [ReplanTarget(str(r[0]), int(r[1]), tuple(sorted(str(k) for k in r[2])),
                             tuple(sorted(str(x) for x in (r[3] or ())))) for r in res.all()]


async def route_event(tenant: str, connector: str, event_id: str, event_type: str,
                      payload: Mapping[str, Any]) -> list[str]:
    """Enqueue a ``signal`` re-plan run on every case whose plan depends on the event's lane.
    Returns the run ids (empty for events that are not routed)."""
    from keelson_engine.core.runs import insert_run, wake_workers

    if event_type not in enabled_event_types():
        return []
    data = payload.get("data")
    routed = ROUTERS[event_type](data if isinstance(data, Mapping) else {})
    if routed is None:
        log.info("replan event without usable structured fields", tenant=tenant, connector=connector,
                 type=event_type)
        return []
    pairs = await lane_pairs(tenant, connector, routed.lane_code, origin=routed.origin, dest=routed.dest)
    if routed.origin is not None and routed.dest is not None and pair_ref(routed.origin, routed.dest) not in pairs:
        # the connector's snapshot knows the lane by another (origin, dest): the event's pair is not
        # a fact about this lane, so it is neither routed on nor handed to the run as an alias
        log.warning("replan event pair contradicts the snapshot lane", tenant=tenant, connector=connector,
                    lane=routed.lane_code)
        routed.details.pop("origin", None)
        routed.details.pop("dest", None)
    targets = await find_dependent_plans(tenant, connector, routed.lane_code, pairs=pairs)
    actor = signal_actor(connector)
    run_ids: list[str] = []
    for target in targets:
        details = {"event_id": event_id, "kind": "replan", **routed.details, "connector": connector,
                   "event_type": event_type, "plan_version": target.plan_version,
                   "matched_by": list(target.matched_by)}
        plan_lane = next((ref for ref in target.lanes if ref in pairs and ref != routed.lane_code), None)
        if "lane_pair" in target.matched_by and plan_lane is not None:
            details["plan_lane_code"] = plan_lane  # the pair as the plan knows it (the runtime's alias)
        async with tenant_session(tenant) as s:
            res = await s.execute(
                text(
                    """
                    INSERT INTO case_signal (tenant, case_id, connector, event_id, event_type, kind, payload)
                    VALUES (:t, :c, :conn, :e, :type, 'replan', CAST(:p AS jsonb))
                    ON CONFLICT (tenant, case_id, connector, event_id, kind) DO NOTHING
                    RETURNING id
                    """
                ),
                {"t": tenant, "c": target.case_id, "conn": connector, "e": event_id, "type": event_type,
                 "p": dumps(details)},
            )
            signal_id = res.scalar_one_or_none()
            if signal_id is None:
                continue  # this event was already routed to this case
            run_id, _ = await insert_run(
                s, tenant=tenant, case_id=target.case_id, kind="signal", payload={"signal_id": str(signal_id)},
                actor=actor, idempotency_key=f"signal:{connector}:{event_id}"[:200],
            )
            await s.execute(text("UPDATE case_signal SET run_id = :r WHERE id = :id"),
                            {"r": run_id, "id": signal_id})
            await s.execute(text('UPDATE "case" SET updated_at = now() WHERE id = :c'), {"c": target.case_id})
        run_ids.append(run_id)
        await wake_workers(run_id, tenant)
    log.info("replan signals enqueued", tenant=tenant, connector=connector, type=event_type,
             lane=routed.lane_code, cases=len(run_ids))
    return run_ids


async def get_signal(tenant: str, signal_id: str) -> dict[str, Any] | None:
    """See :func:`keelson_engine.core.signals.get_signal`."""
    from keelson_engine.core.signals import get_signal as _get

    return await _get(tenant=tenant, signal_id=signal_id)
