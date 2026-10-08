"""Read models for REST and MCP (always inside a tenant's RLS context)."""

from __future__ import annotations

import datetime as dt
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

import asyncpg

from keelson_contracts.signals import (
    Alert,
    AlertDetail,
    AlertStatusUpdate,
    DisruptionEvent,
    DisruptionSignal,
    Exposure,
    ExposureSummary,
    NodeCounts,
)
from signal_hub.alerts import alert_from_row

__all__ = [
    "MAX_MEMBERS",
    "AlertConflict",
    "alert_changes",
    "alert_detail",
    "event_bundle",
    "hazards_near_node",
    "list_alerts",
    "load_event",
    "load_exposures",
    "load_signals",
    "search_signals",
    "set_alert_status",
]

MAX_MEMBERS: Final = 1000


class AlertConflict(ValueError):
    pass


def _signal_from_row(row: asyncpg.Record) -> DisruptionSignal:
    doc = dict(row["doc"])
    doc["event_id"] = str(row["event_id"]) if row["event_id"] is not None else None
    return DisruptionSignal.model_validate(doc)


async def load_event(conn: asyncpg.Connection, event_id: str) -> DisruptionEvent | None:
    row = await conn.fetchrow("SELECT * FROM event WHERE id = $1", event_id)
    if row is None:
        return None
    members = [str(r["id"]) for r in await conn.fetch(
        """SELECT id FROM signal WHERE event_id = $1
           ORDER BY (id = $2) DESC, is_current DESC, observed_at DESC, id LIMIT $3""",
        event_id, row["anchor_signal_id"], MAX_MEMBERS,
    )]
    if not members:
        return None
    anchor = str(row["anchor_signal_id"]) if row["anchor_signal_id"] is not None else None
    if anchor is not None and anchor not in members:
        anchor = None
    return DisruptionEvent.model_validate({
        "id": str(row["id"]),
        "tenant": row["tenant"],
        "kind": row["kind"],
        "subtype": row["subtype"],
        "title": row["title"],
        "summary": row["summary"],
        "anchor_signal_id": anchor,
        "member_signal_ids": members,
        "geometry": row["geometry"],
        "window": {"starts_at": row["window_start"], "ends_at": row["window_end"]},
        "severity_bp": row["severity_bp"],
        "confidence_bp": row["confidence_bp"],
        "status": row["status"],
        "taxonomy_key": row["taxonomy_key"],
        "updated_at": row["updated_at"],
    })


async def load_signals(conn: asyncpg.Connection, ids: list[str]) -> list[DisruptionSignal]:
    if not ids:
        return []
    rows = await conn.fetch("SELECT event_id, doc FROM signal WHERE id = ANY($1::uuid[])", ids)
    by_id = {r["doc"]["id"]: _signal_from_row(r) for r in rows}
    return [by_id[i] for i in ids if i in by_id]


async def load_exposures(conn: asyncpg.Connection, tenant: str, event_id: str) -> list[Exposure]:
    rows = await conn.fetch(
        """SELECT node_type, node_code, method, distance_km, weight_bp FROM exposure
           WHERE tenant = $1 AND event_id = $2 ORDER BY node_type, node_code""",
        tenant, event_id,
    )
    return [Exposure(event_id=event_id, tenant=tenant, **dict(r)) for r in rows]


async def event_bundle(conn: asyncpg.Connection, tenant: str, event_id: str) -> dict[str, Any] | None:
    """``{event, signals, exposures, exposure_summary}`` for ``GET /v1/events/{id}`` / ``get_event``."""
    event = await load_event(conn, event_id)
    if event is None:
        return None
    signals = await load_signals(conn, list(event.member_signal_ids))
    exposures = await load_exposures(conn, tenant, event_id)
    summary = ExposureSummary(nodes_by_type=NodeCounts.of(exposures), open_po_lines=None, units_inbound=None)
    return {
        "event": event.model_dump(mode="json"),
        "signals": [s.model_dump(mode="json") for s in signals],
        "exposures": [e.model_dump(mode="json") for e in exposures],
        "exposure_summary": summary.model_dump(mode="json"),
    }


async def list_alerts(
    conn: asyncpg.Connection, tenant: str, *, statuses: list[str] | None, severities: list[str] | None,
    limit: int, updated_since: dt.datetime | None,
) -> list[Alert]:
    rows = await conn.fetch(
        """SELECT * FROM alert WHERE tenant = $1
             AND ($2::text[] IS NULL OR status = ANY($2::text[]))
             AND ($3::text[] IS NULL OR severity = ANY($3::text[]))
             AND ($4::timestamptz IS NULL OR updated_at >= $4)
           ORDER BY updated_at DESC, id LIMIT $5""",
        tenant, statuses, severities, updated_since, limit,
    )
    return [alert_from_row(r) for r in rows]


async def alert_changes(conn: asyncpg.Connection, tenant: str, *, after: int, limit: int) -> dict[str, Any]:
    rows = await conn.fetch(
        """SELECT c.seq AS change_seq, c.change AS change_kind, c.action AS change_action, c.at AS change_at, a.*
           FROM alert_change c JOIN alert a ON a.id = c.alert_id
           WHERE c.tenant = $1 AND c.seq > $2 ORDER BY c.seq LIMIT $3""",
        tenant, after, limit,
    )
    # aliased: alert.* has its own (current) `action`; the feed reports the action of each change
    items = [
        {"seq": r["change_seq"], "change": r["change_kind"], "action": r["change_action"],
         "at": r["change_at"].isoformat(), "alert": alert_from_row(r).model_dump(mode="json")}
        for r in rows
    ]
    return {"items": items, "next_after": items[-1]["seq"] if items else after}


async def alert_detail(conn: asyncpg.Connection, tenant: str, alert_id: str) -> AlertDetail | None:
    row = await conn.fetchrow("SELECT * FROM alert WHERE id = $1 AND tenant = $2", alert_id, tenant)
    if row is None:
        return None
    event = await load_event(conn, str(row["event_id"]))
    if event is None:
        return None
    return AlertDetail(
        alert=alert_from_row(row),
        event=event,
        signals=await load_signals(conn, list(event.member_signal_ids)),
        exposures=await load_exposures(conn, tenant, str(row["event_id"])),
    )


async def set_alert_status(
    conn: asyncpg.Connection, tenant: str, alert_id: str, update: AlertStatusUpdate, now: dt.datetime
) -> Alert | None:
    """Status transitions (``100`` §5). ``opened`` is idempotent for the same ``case_id`` (one case per
    alert); an opened alert can only be resolved; a resolved one only (re)opened or resolved."""
    row = await conn.fetchrow("SELECT * FROM alert WHERE id = $1 AND tenant = $2 FOR UPDATE", alert_id, tenant)
    if row is None:
        return None
    current = row["status"]
    if update.status == "opened":
        if row["case_id"] is not None and row["case_id"] != update.case_id:
            raise AlertConflict(f"alert already opened as case {row['case_id']}")
        if current == "opened" and row["case_id"] == update.case_id:
            return alert_from_row(row)
    elif current == "opened" and update.status != "resolved":
        raise AlertConflict("alert has an open case; it can only be resolved")
    elif current == "resolved" and update.status != "resolved":
        raise AlertConflict("alert is resolved")
    if current == update.status and update.status != "opened":
        return alert_from_row(row)
    case_id = update.case_id if update.status == "opened" else row["case_id"]
    updated = await conn.fetchrow(
        """UPDATE alert SET status = $3, case_id = $4, updated_at = greatest($5, updated_at)
           WHERE id = $1 AND tenant = $2 RETURNING *""",
        alert_id, tenant, update.status, case_id, now,
    )
    assert updated is not None  # locked above
    await conn.execute(
        "INSERT INTO alert_change (tenant, alert_id, change, action, at) VALUES ($1, $2, 'status', $3, $4)",
        tenant, alert_id, row["action"], now,
    )
    return alert_from_row(updated)


def _km(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


async def search_signals(
    conn: asyncpg.Connection, *, query: str | None, kinds: list[str] | None, since: dt.datetime | None,
    near: tuple[float, float, float] | None, limit: int,
) -> list[DisruptionSignal]:
    """Current signal versions visible to the tenant (global + own), newest first."""
    rows = await conn.fetch(
        """SELECT event_id, doc FROM signal WHERE is_current
             AND ($1::text IS NULL OR title ILIKE $1 OR coalesce(doc->>'summary', '') ILIKE $1)
             AND ($2::text[] IS NULL OR kind = ANY($2::text[]))
             AND ($3::timestamptz IS NULL OR observed_at >= $3)
             AND ($4::float8 IS NULL OR (geog IS NOT NULL AND
                  ST_DWithin(geog, ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography, $6 * 1000.0)))
           ORDER BY observed_at DESC, id LIMIT $7""",
        _like(query) if query else None, kinds, since,
        near[0] if near else None, near[1] if near else None, near[2] if near else None, limit,
    )
    return [_signal_from_row(r) for r in rows]


async def node_position(conn: asyncpg.Connection, tenant: str, node_type: str, node_code: str) -> str | None:
    """``node`` / ``lane`` selector SQL fragment availability check (returns the table name)."""
    if node_type == "lane":
        found = await conn.fetchval("SELECT 1 FROM network_lane WHERE tenant = $1 AND code = $2", tenant, node_code)
        return "lane" if found else None
    found = await conn.fetchval(
        "SELECT 1 FROM network_node WHERE tenant = $1 AND type = $2 AND code = $3", tenant, node_type, node_code
    )
    return "node" if found else None


async def hazards_near_node(
    conn: asyncpg.Connection, tenant: str, node_type: str, node_code: str, radius_km: float
) -> list[dict[str, Any]] | None:
    """Open events within ``radius_km`` of a tenant node or lane (None when the node is unknown)."""
    kind = await node_position(conn, tenant, node_type, node_code)
    if kind is None:
        return None
    if kind == "lane":
        sql = """SELECT e.id, ST_Distance(e.geog, l.path) / 1000.0 AS km FROM event e, network_lane l
                 WHERE l.tenant = $1 AND l.code = $3 AND e.status <> 'resolved' AND e.geog IS NOT NULL
                   AND ST_DWithin(e.geog, l.path, $4 * 1000.0) AND $2::text = 'lane'
                 ORDER BY km, e.id LIMIT 20"""
    else:
        sql = """SELECT e.id, ST_Distance(e.geog, n.geog) / 1000.0 AS km FROM event e, network_node n
                 WHERE n.tenant = $1 AND n.type = $2 AND n.code = $3 AND e.status <> 'resolved' AND e.geog IS NOT NULL
                   AND ST_DWithin(e.geog, n.geog, $4 * 1000.0)
                 ORDER BY km, e.id LIMIT 20"""
    out: list[dict[str, Any]] = []
    for r in await conn.fetch(sql, tenant, node_type, node_code, radius_km):
        event = await load_event(conn, str(r["id"]))
        if event is None:
            continue
        out.append({
            "event_id": event.id, "title": event.title, "kind": event.kind, "subtype": event.subtype,
            "status": event.status, "severity_bp": event.severity_bp, "confidence_bp": event.confidence_bp,
            "taxonomy_key": event.taxonomy_key, "distance_km": _km(float(r["km"])),
            "window": event.window.model_dump(mode="json"),
        })
    return out
