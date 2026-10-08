"""Transactional outbox: domain events + scn/1.0 entity changes, the event feed
(``list_events``) and ``scn.changes_since``.

Writers call :func:`emit` inside the transaction that changes the data. ``emit`` first
takes a transaction-scoped advisory lock, so outbox rows are numbered (``seq``) **in commit
order** without gaps: a reader that has seen ``seq ≤ N`` has seen every change up to N.
That makes ``ox:<seq>`` a safe watermark for snapshots and ``changes_since``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Text, cast, func, insert, select, text
from sqlalchemy.ext.asyncio import AsyncConnection

from merxa_api import tables as t
from merxa_api.errors import Problem
from merxa_api.services.common import jsonable

OUTBOX_LOCK = 0x4D58_4F42  # "MXOB"

# Event types delivered by webhooks (docs/contracts/22-merxa-m2.md §5).
M2_WEBHOOK_EVENTS: tuple[str, ...] = (
    "change_set.applied",
    "change_set.rolled_back",
    "po_line.eta_changed",
    "supplier_message.received",
    "inventory.adjusted",
    "sailing.updated",
)
# M5 (docs/contracts/95 §2/§4): manual changes made in the Merxa UI / REST (Guide mode
# auto-tick reads them back) and the lane capacity demo inject.
M5_WEBHOOK_EVENTS: tuple[str, ...] = (
    "transfer_order.created",
    "allocation.updated",
    "po_line.updated",
    "capacity_booking.created",
    "capacity_booking.cancelled",
    "lane.capacity_changed",
)
WEBHOOK_EVENTS: tuple[str, ...] = (*M2_WEBHOOK_EVENTS, *M5_WEBHOOK_EVENTS)
# Internal events (feed + changes_since only).
INTERNAL_EVENTS: tuple[str, ...] = ("entity.updated", "scenario.reset")
# Outbox events delivered by webhooks under a different (namespaced) type
# (docs/contracts/90 §10: a demo reset reaches subscribers as ``merxa.scenario.reset`` so
# consumers mark their snapshot stale). The feed and ``changes_since`` keep the outbox type.
WEBHOOK_TYPE_ALIASES: dict[str, str] = {"scenario.reset": "merxa.scenario.reset"}
# Every webhook ``type`` a subscription can receive ("all events" = these).
SUBSCRIBABLE_EVENTS: tuple[str, ...] = (*WEBHOOK_EVENTS, *WEBHOOK_TYPE_ALIASES.values())


def webhook_type(event_type: str) -> str:
    """The ``type`` a webhook delivery carries for an outbox ``event_type``."""
    return WEBHOOK_TYPE_ALIASES.get(event_type, event_type)


# Free-text payload fields per event type (caller-written text, not system data): an event
# carrying any of them is ``content_trust: untrusted_text`` in the feed (docs/contracts/90 §4).
UNTRUSTED_FIELDS: dict[str, tuple[str, ...]] = {
    "supplier_message.received": ("subject", "body"),
    "inventory.adjusted": ("reason",),  # demo director inject: caller's free text
    "lane.capacity_changed": ("carrier_note",),  # carrier's notice text (the numbers are facts)
}

_WATERMARK_RE = re.compile(r"^(?:ox:)?(\d{1,18})$")

# (scn entity, key) of the rows an audit entry touched; see :func:`changes_for`.
Change = tuple[str, str]


async def emit(
    conn: AsyncConnection,
    event_type: str,
    payload: dict[str, Any],
    changes: Sequence[dict[str, Any]] | None = None,
) -> None:
    await conn.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": OUTBOX_LOCK})
    await conn.execute(
        insert(t.outbox).values(
            event_type=event_type, payload=jsonable(payload), changes=jsonable(list(changes)) if changes else None
        )
    )


def parse_watermark(value: str | int | None) -> int:
    if value is None or value == "":
        return 0
    m = _WATERMARK_RE.match(str(value).strip())
    if not m:
        raise Problem(400, "invalid_watermark", "watermark must look like 'ox:<number>'")
    return int(m.group(1))


def format_watermark(seq: int) -> str:
    return f"ox:{seq}"


async def current_watermark(conn: AsyncConnection) -> int:
    return int((await conn.execute(select(func.coalesce(func.max(t.outbox.c.seq), 0)))).scalar_one())


# ───────────────────────── audit entry → scn changes ─────────────────────────
async def changes_for(conn: AsyncConnection, entries: Iterable[tuple[str, str, str, Any, Any]]) -> list[dict[str, Any]]:
    """Map audit entries ``(entity_type, entity_ref, action, before, after)`` to scn/1.0
    change records ``{entity, op, key, version}`` with the rows' current versions."""
    wanted: dict[str, set[str]] = {}

    def add(entity: str, key: str) -> None:
        wanted.setdefault(entity, set()).add(key)

    to_codes: set[str] = set()
    po_codes: set[str] = set()
    shipment_codes: set[str] = set()
    order_codes: set[str] = set()
    style_codes: set[str] = set()
    for entity_type, ref, *_ in entries:
        if entity_type == "inventory_position":
            add("inventory_position", ref)
        elif entity_type in ("allocation", "production_order", "promotion", "sailing"):
            add(entity_type, ref)
        elif entity_type == "po_line":
            add("po_line", ref)
        elif entity_type == "purchase_order":
            po_codes.add(ref)
        elif entity_type == "transfer_order":
            to_codes.add(ref)
        elif entity_type == "shipment":
            shipment_codes.add(ref)
        elif entity_type == "online_order":
            order_codes.add(ref)
        elif entity_type == "demand_forecast":
            add("demand_forecast", ref)
        elif entity_type in ("dc", "store"):  # scn `location`, source_id '<kind>:<code>'
            add("location", f"{entity_type}:{ref}")
        elif entity_type == "style":  # scn `item`: the style row and its SKUs (they carry its price)
            style_codes.add(ref)
        elif entity_type == "rate_card":  # scn `lane_rate`, source_id '<lane>|<valid_from>'
            add("lane_rate", ref)
    out: list[dict[str, Any]] = []
    if style_codes:
        st, sk = t.style, t.sku
        for r in await conn.execute(
            select(st.c.code, st.c.version).where(st.c.code.in_(sorted(style_codes))).order_by(st.c.code)
        ):
            out.append(_change("item", f"style:{r.code}", r.version))
        for r in await conn.execute(
            select(sk.c.code, sk.c.version).where(sk.c.style_code.in_(sorted(style_codes))).order_by(sk.c.code)
        ):
            out.append(_change("item", f"sku:{r.code}", r.version))
    if to_codes:
        tl, to = t.transfer_order_line, t.transfer_order
        for r in await conn.execute(
            select(tl.c.to_code, tl.c.line_no, (tl.c.version + to.c.version - 1).label("v"))
            .select_from(tl.join(to, to.c.code == tl.c.to_code))
            .where(tl.c.to_code.in_(sorted(to_codes)))
        ):
            out.append(_change("transfer_order_line", f"{r.to_code}#{r.line_no}", r.v))
    if po_codes:
        for r in await conn.execute(
            select(t.po_line.c.po_code, t.po_line.c.line_no).where(t.po_line.c.po_code.in_(sorted(po_codes)))
        ):
            add("po_line", f"{r.po_code}#{r.line_no}")
    if shipment_codes:
        for r in await conn.execute(
            select(t.shipment_line.c.po_line_ref).where(t.shipment_line.c.shipment_code.in_(sorted(shipment_codes)))
        ):
            add("po_line", r.po_line_ref)
    if order_codes:
        ol, oo = t.online_order_line, t.online_order
        for r in await conn.execute(
            select(ol.c.order_code, ol.c.line_no, (ol.c.version + oo.c.version - 1).label("v"))
            .select_from(ol.join(oo, oo.c.code == ol.c.order_code))
            .where(ol.c.order_code.in_(sorted(order_codes)))
        ):
            out.append(_change("online_order_line", f"{r.order_code}#{r.line_no}", r.v))
    for entity, keys in sorted(wanted.items()):
        versions = await _versions(conn, entity, sorted(keys))
        for key in sorted(keys):
            if entity == "lane_rate" and key not in versions:  # the rate card was split / replaced
                out.append(_change(entity, key, None, op="delete"))
            else:
                out.append(_change(entity, key, versions.get(key)))
    return out


def _change(entity: str, key: str, version: int | None, op: str = "upsert") -> dict[str, Any]:
    return {"entity": entity, "op": op, "key": key, "version": version}


async def _versions(conn: AsyncConnection, entity: str, keys: list[str]) -> dict[str, int]:
    if entity == "inventory_position":
        ip = t.inventory_position
        parts = [k.split("/", 2) for k in keys if k.count("/") >= 2]
        if not parts:
            return {}
        rows = await conn.execute(
            select(ip.c.location_type, ip.c.location_code, ip.c.sku_code, ip.c.version).where(
                (ip.c.location_type + "/" + ip.c.location_code + "/" + ip.c.sku_code).in_(keys)
            )
        )
        return {f"{r.location_type}/{r.location_code}/{r.sku_code}": r.version for r in rows}
    if entity == "po_line":
        pl = t.po_line
        rows = await conn.execute(
            select(pl.c.po_code, pl.c.line_no, pl.c.version).where(
                (pl.c.po_code + "#" + cast(pl.c.line_no, Text)).in_(keys)
            )
        )
        return {f"{r.po_code}#{r.line_no}": r.version for r in rows}
    if entity == "demand_forecast":
        df = t.demand_forecast
        rows = await conn.execute(
            select(df.c.style_code, df.c.store_code, df.c.iso_week, df.c.version).where(
                (df.c.style_code + "|" + func.coalesce(df.c.store_code, df.c.cluster_code) + "|" + df.c.iso_week).in_(
                    keys
                )
            )
        )
        return {f"{r.style_code}|{r.store_code}|{r.iso_week}": r.version for r in rows}
    if entity == "lane_rate":
        rc = t.rate_card
        lanes = sorted({k.split("|", 1)[0] for k in keys})
        rows = await conn.execute(
            select(rc.c.lane_code, func.lower(rc.c.valid).label("valid_from"), rc.c.version).where(
                rc.c.lane_code.in_(lanes)
            )
        )
        found = {f"{r.lane_code}|{r.valid_from.isoformat() if r.valid_from else ''}": r.version for r in rows}
        return {k: v for k, v in found.items() if k in keys}
    if entity == "location":
        found: dict[str, int] = {}
        for kind, table in (("dc", t.dc), ("store", t.store)):
            codes = [k.split(":", 1)[1] for k in keys if k.startswith(f"{kind}:")]
            if codes:
                for r in await conn.execute(select(table.c.code, table.c.version).where(table.c.code.in_(codes))):
                    found[f"{kind}:{r.code}"] = r.version
        return found
    table = {
        "allocation": t.allocation,
        "production_order": t.production_order,
        "promotion": t.promotion,
        "sailing": t.sailing,
    }[entity]
    rows = await conn.execute(select(table.c.code, table.c.version).where(table.c.code.in_(keys)))
    return {r.code: r.version for r in rows}


# ───────────────────────── readers ─────────────────────────
def render_event(row: Any) -> dict[str, Any]:
    item = {
        "event_id": str(row.id),
        "seq": int(row.seq),
        "type": row.event_type,
        "created_at": jsonable(row.created_at),
        "data": jsonable(row.payload),
    }
    data = row.payload if isinstance(row.payload, dict) else {}
    untrusted = [f for f in UNTRUSTED_FIELDS.get(row.event_type, ()) if f in data]
    if untrusted:
        item["content_trust"] = "untrusted_text"
        item["untrusted_fields"] = [f"data.{f}" for f in untrusted]
    else:
        item["content_trust"] = "trusted_data"
    return item


async def list_events(
    conn: AsyncConnection, *, after_cursor: str | None, limit: int = 100, types: Sequence[str] | None = None
) -> dict[str, Any]:
    after = parse_watermark(after_cursor)
    ob = t.outbox
    q = select(ob.c.id, ob.c.seq, ob.c.event_type, ob.c.created_at, ob.c.payload).where(ob.c.seq > after)
    if types:
        q = q.where(ob.c.event_type.in_(list(types)))
    rows = list(await conn.execute(q.order_by(ob.c.seq).limit(limit + 1)))
    more = len(rows) > limit
    rows = rows[:limit]
    items = [render_event(r) for r in rows]
    last = items[-1]["seq"] if items else after
    return {"items": items, "next_cursor": format_watermark(last), "has_more": more}


async def changes_since(conn: AsyncConnection, watermark: str | None, *, limit_events: int = 2000) -> dict[str, Any]:
    after = parse_watermark(watermark)
    ob = t.outbox
    rows = list(
        await conn.execute(
            select(ob.c.seq, ob.c.event_type, ob.c.created_at, ob.c.changes)
            .where(ob.c.seq > after)
            .order_by(ob.c.seq)
            .limit(limit_events + 1)
        )
    )
    more = len(rows) > limit_events
    rows = rows[:limit_events]
    changes: list[dict[str, Any]] = []
    resync = False
    for r in rows:
        if r.event_type == "scenario.reset":
            resync = True
        for c in r.changes or []:
            changes.append({**c, "at": jsonable(r.created_at), "event_seq": int(r.seq)})
    last = int(rows[-1].seq) if rows else after
    out: dict[str, Any] = {"changes": changes, "next_watermark": format_watermark(last), "has_more": more}
    if resync:
        out["full_resync_required"] = True
    return out
