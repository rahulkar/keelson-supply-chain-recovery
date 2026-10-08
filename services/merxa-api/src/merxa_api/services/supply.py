"""M2 read-side queries: ports, lanes, rate cards, sailings, capacity bookings, shipments,
lane options, factory lines / production orders / schedules, online orders, promotions.
Shared by REST (/v1) and MCP tools; all return JSON-ready dicts."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from merxa_api import tables as t
from merxa_api.calendar import is_iso_week, iso_week, week_monday
from merxa_api.errors import Problem, not_found
from merxa_api.services.catalog import sim_today
from merxa_api.services.common import paginate, parse_date, row_dict

Page = dict[str, Any]
ACTIVE_BOOKING = ("held", "confirmed")
# Origin port per factory country (where goods enter the main leg).
ORIGIN_PORT: dict[str, str] = {"BD": "BDCGP", "VN": "VNSGN", "KH": "KHPNH", "TR": "TRIST", "MA": "MATNG"}


def _page(items: list[dict[str, Any]], next_cursor: str | None) -> Page:
    return {"items": items, "next_cursor": next_cursor}


def _period_cols(col: Any, start: str, end: str) -> list[Any]:
    """daterange → inclusive ISO start/end columns."""
    return [func.lower(col).label(start), (func.upper(col) - 1).label(end)]


# ───────────────────────── network ─────────────────────────
async def list_ports(conn: AsyncConnection, *, limit: int, cursor: str | None) -> Page:
    p = t.port
    q = select(p.c.unlocode, p.c.name, p.c.country, p.c.lat, p.c.lon, p.c.kind, p.c.version)
    rows, nxt = await paginate(conn, q, [p.c.unlocode], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


def _lane_query() -> Any:
    ln, c = t.lane, t.carrier
    return select(
        ln.c.code,
        ln.c.origin,
        ln.c.dest,
        ln.c.mode,
        ln.c.carrier_code,
        c.c.name.label("carrier_name"),
        ln.c.transit_days,
        ln.c.via,
        ln.c.alt_lane_code,
        ln.c.version,
    ).select_from(ln.join(c, c.c.code == ln.c.carrier_code))


async def list_lanes(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    origin: str | None = None,
    dest: str | None = None,
    mode: str | None = None,
) -> Page:
    ln = t.lane
    q = _lane_query()
    if origin:
        q = q.where(ln.c.origin == origin)
    if dest:
        q = q.where(ln.c.dest == dest)
    if mode:
        q = q.where(ln.c.mode == mode)
    rows, nxt = await paginate(conn, q, [ln.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


def _rate_subquery() -> Any:
    rc = t.rate_card
    return select(
        rc.c.lane_code,
        *_period_cols(rc.c.valid, "valid_from", "valid_to"),
        rc.c.basis,
        rc.c.rate_cents,
        rc.c.currency,
        rc.c.weekly_capacity,
        rc.c.version,
    ).subquery()


async def list_rate_cards(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    lane_code: str | None = None,
    valid_on: str | None = None,
) -> Page:
    sq = _rate_subquery()
    q = select(sq)
    if lane_code:
        q = q.where(sq.c.lane_code == lane_code)
    if (d := parse_date(valid_on, "valid_on")) is not None:
        q = q.where(sq.c.valid_from <= d, sq.c.valid_to >= d)
    rows, nxt = await paginate(
        conn, q, [sq.c.lane_code, sq.c.valid_from], limit=limit, cursor=cursor, key_types=(str, date)
    )
    return _page([row_dict(r) for r in rows], nxt)


async def rate_on(conn: AsyncConnection, lane_code: str, day: date) -> Any:
    """The rate card of ``lane_code`` valid on ``day`` (or ``None``)."""
    rc = t.rate_card
    q = select(
        rc.c.basis,
        rc.c.rate_cents,
        rc.c.currency,
        rc.c.weekly_capacity,
        func.lower(rc.c.valid).label("valid_from"),
    ).where(rc.c.lane_code == lane_code, rc.c.valid.op("@>")(day))
    return (await conn.execute(q)).first()


async def booked_units(conn: AsyncConnection, lane_code: str, week: str) -> int:
    cb = t.capacity_booking
    q = select(func.coalesce(func.sum(cb.c.qty_units), 0)).where(
        cb.c.lane_code == lane_code, cb.c.iso_week == week, cb.c.status.in_(ACTIVE_BOOKING), cb.c.basis == "per_unit"
    )
    return int((await conn.execute(q)).scalar_one())


async def remaining_capacity(conn: AsyncConnection, lane_code: str, week: str) -> tuple[int | None, Any]:
    """(remaining weekly capacity, rate card) — ``None`` capacity when no rate card is valid."""
    rate = await rate_on(conn, lane_code, week_monday(week))
    if rate is None:
        return None, None
    if rate.basis != "per_unit":
        return int(rate.weekly_capacity), rate
    return int(rate.weekly_capacity) - await booked_units(conn, lane_code, week), rate


async def list_sailings(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    lane_code: str | None = None,
    status: str | None = None,
    etd_from: str | None = None,
    etd_to: str | None = None,
) -> Page:
    s, ln = t.sailing, t.lane
    q = select(
        s.c.code,
        s.c.lane_code,
        ln.c.origin,
        ln.c.dest,
        s.c.vessel,
        s.c.cutoff,
        s.c.etd,
        s.c.eta,
        s.c.capacity_teu,
        s.c.status,
        s.c.version,
    ).select_from(s.join(ln, ln.c.code == s.c.lane_code))
    if lane_code:
        q = q.where(s.c.lane_code == lane_code)
    if status:
        q = q.where(s.c.status == status)
    if (d := parse_date(etd_from, "etd_from")) is not None:
        q = q.where(s.c.etd >= d)
    if (d := parse_date(etd_to, "etd_to")) is not None:
        q = q.where(s.c.etd <= d)
    rows, nxt = await paginate(conn, q, [s.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def list_capacity_bookings(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    lane_code: str | None = None,
    iso_week: str | None = None,
    status: str | None = None,
) -> Page:
    cb = t.capacity_booking
    q = select(*[c for c in cb.c if c.key not in ("id", "updated_at")])
    if lane_code:
        q = q.where(cb.c.lane_code == lane_code)
    if iso_week:
        q = q.where(cb.c.iso_week == iso_week)
    if status:
        q = q.where(cb.c.status == status)
    rows, nxt = await paginate(conn, q, [cb.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def list_shipments(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    po_code: str | None = None,
    status: str | None = None,
    dest_dc: str | None = None,
    sailing_code: str | None = None,
) -> Page:
    sh = t.shipment
    q = select(*[c for c in sh.c if c.key not in ("id", "updated_at")])
    if po_code:
        q = q.where(sh.c.po_code == po_code)
    if status:
        q = q.where(sh.c.status == status)
    if dest_dc:
        q = q.where(sh.c.dest_dc_code == dest_dc)
    if sailing_code:
        q = q.where(sh.c.sailing_code == sailing_code)
    rows, nxt = await paginate(conn, q, [sh.c.code], limit=limit, cursor=cursor)
    items = [row_dict(r) for r in rows]
    codes = [i["code"] for i in items]
    lines: dict[str, list[dict[str, Any]]] = {c: [] for c in codes}
    if codes:
        sl = t.shipment_line
        for r in await conn.execute(
            select(sl.c.shipment_code, sl.c.line_no, sl.c.po_line_ref, sl.c.sku_code.label("sku"), sl.c.qty)
            .where(sl.c.shipment_code.in_(codes))
            .order_by(sl.c.shipment_code, sl.c.line_no)
        ):
            lines[r.shipment_code].append(
                {"line_no": r.line_no, "po_line_ref": r.po_line_ref, "sku": r.sku, "qty": r.qty}
            )
    for item in items:
        item["dest_dc"] = item["dest_dc_code"]
        item["lines"] = lines[item["code"]]
        item["total_qty"] = sum(ln["qty"] for ln in item["lines"])
    return _page(items, nxt)


# ───────────────────────── lane options ─────────────────────────
async def lane_options(conn: AsyncConnection, *, origin: str, dest: str, week: str | None = None) -> dict[str, Any]:
    """Direct lanes ``origin → dest`` plus two-leg routes (main leg to a port + road leg)."""
    if not origin or not dest:
        raise Problem(400, "invalid_parameter", "origin and dest are required")
    if week is None:
        week = iso_week(await sim_today(conn))
    elif not is_iso_week(week):
        raise Problem(400, "invalid_parameter", "week must be an ISO week (YYYY-Www)")
    lanes = {r.code: r for r in await conn.execute(_lane_query())}
    routes: list[list[Any]] = []
    for ln in lanes.values():
        if ln.origin != origin:
            continue
        if ln.dest == dest:
            routes.append([ln])
            continue
        for leg in lanes.values():
            if leg.origin == ln.dest and leg.dest == dest and leg.mode == "road":
                routes.append([ln, leg])
    options = []
    for route in routes:
        main = route[0]
        remaining, rate = await remaining_capacity(conn, main.code, week)
        legs = []
        for leg in route:
            leg_rate = await rate_on(conn, leg.code, week_monday(week))
            legs.append(
                {
                    "lane_code": leg.code,
                    "origin": leg.origin,
                    "dest": leg.dest,
                    "mode": leg.mode,
                    "carrier_code": leg.carrier_code,
                    "transit_days": leg.transit_days,
                    "rate_cents": int(leg_rate.rate_cents) if leg_rate else None,
                    "basis": leg_rate.basis if leg_rate else None,
                }
            )
        via = list(main.via or [])
        if len(route) > 1:
            via.append(main.dest)
        options.append(
            {
                "lane_code": main.code,
                "mode": main.mode,
                "transit_days": sum(leg.transit_days for leg in route),
                "via": via,
                "rate_cents": int(rate.rate_cents) if rate else None,
                "basis": rate.basis if rate else None,
                "currency": rate.currency if rate else None,
                "remaining_capacity_week": remaining,
                "alt_lane_code": main.alt_lane_code,
                "legs": legs,
            }
        )
    options.sort(key=lambda o: (o["transit_days"], o["lane_code"]))
    return {"origin": origin, "dest": dest, "week": week, "options": options}


async def fastest_transit(conn: AsyncConnection, origin: str, dest: str) -> tuple[int, str] | None:
    opts = (await lane_options(conn, origin=origin, dest=dest))["options"]
    return (opts[0]["transit_days"], opts[0]["lane_code"]) if opts else None


# ───────────────────────── production ─────────────────────────
async def list_factory_lines(
    conn: AsyncConnection, *, limit: int, cursor: str | None, factory_code: str | None = None
) -> Page:
    fl = t.factory_line
    q = select(fl.c.code, fl.c.factory_code, fl.c.capabilities, fl.c.std_minutes_day, fl.c.version)
    if factory_code:
        q = q.where(fl.c.factory_code == factory_code)
    rows, nxt = await paginate(conn, q, [fl.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


def _po_cols() -> list[Any]:
    po = t.production_order
    return [
        po.c.code,
        po.c.po_line_ref,
        po.c.line_code,
        po.c.seq,
        po.c.planned_start,
        po.c.planned_end,
        po.c.status,
        po.c.hold_reason,
        po.c.version,
    ]


async def list_production_orders(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    line_code: str | None = None,
    factory_code: str | None = None,
    status: str | None = None,
    po_code: str | None = None,
) -> Page:
    po = t.production_order
    q = select(*_po_cols())
    if line_code:
        q = q.where(po.c.line_code == line_code)
    if factory_code:
        q = q.where(
            po.c.line_code.in_(select(t.factory_line.c.code).where(t.factory_line.c.factory_code == factory_code))
        )
    if status:
        q = q.where(po.c.status == status)
    if po_code:
        q = q.where(po.c.po_line_ref.like(po_code.replace("%", "") + "#%"))
    rows, nxt = await paginate(conn, q, [po.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def factory_schedule(
    conn: AsyncConnection, *, factory_code: str, date_from: str | None = None, date_to: str | None = None
) -> dict[str, Any]:
    exists = (await conn.execute(select(t.factory.c.code).where(t.factory.c.code == factory_code))).first()
    if exists is None:
        raise not_found("factory", factory_code)
    today = await sim_today(conn)
    d_from = parse_date(date_from, "from") or today - timedelta(days=7)
    d_to = parse_date(date_to, "to") or today + timedelta(days=42)
    if d_to < d_from:
        raise Problem(400, "invalid_parameter", "to must not be before from")
    if (d_to - d_from).days > 366:
        raise Problem(400, "invalid_parameter", "the window is limited to 366 days")
    fl, lc, po = t.factory_line, t.line_calendar, t.production_order
    lines = list(
        await conn.execute(
            select(fl.c.code, fl.c.capabilities, fl.c.std_minutes_day, fl.c.version)
            .where(fl.c.factory_code == factory_code)
            .order_by(fl.c.code)
        )
    )
    codes = [ln.code for ln in lines]
    calendar: dict[str, list[dict[str, Any]]] = defaultdict(list)
    orders: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if codes:
        for r in await conn.execute(
            select(lc.c.line_code, lc.c.day, lc.c.available_minutes, lc.c.status)
            .where(lc.c.line_code.in_(codes), lc.c.day >= d_from, lc.c.day <= d_to)
            .order_by(lc.c.line_code, lc.c.day)
        ):
            calendar[r.line_code].append(
                {"day": r.day.isoformat(), "available_minutes": r.available_minutes, "status": r.status}
            )
        for r in await conn.execute(
            select(*_po_cols())
            .where(po.c.line_code.in_(codes), po.c.planned_end >= d_from, po.c.planned_start <= d_to)
            .order_by(po.c.line_code, po.c.seq)
        ):
            orders[r.line_code].append(row_dict(r))
    return {
        "factory_code": factory_code,
        "from": d_from.isoformat(),
        "to": d_to.isoformat(),
        "lines": [
            {
                "code": ln.code,
                "capabilities": list(ln.capabilities or []),
                "std_minutes_day": ln.std_minutes_day,
                "version": ln.version,
                "calendar": calendar[ln.code],
                "orders": orders[ln.code],
            }
            for ln in lines
        ],
    }


# ───────────────────────── online orders ─────────────────────────
async def list_online_orders(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    market: str | None = None,
    status: str | None = None,
    promise_before: str | None = None,
    at_risk: bool | None = None,
) -> Page:
    oo, ol = t.online_order, t.online_order_line
    risk = select(ol.c.order_code).where(ol.c.reserved_qty < ol.c.qty).group_by(ol.c.order_code)
    q = select(
        oo.c.code,
        oo.c.market,
        oo.c.channel,
        oo.c.store_code,
        oo.c.promise_date,
        oo.c.status,
        oo.c.created_at,
        oo.c.version,
    )
    if market:
        q = q.where(oo.c.market == market)
    if status:
        q = q.where(oo.c.status == status)
    if (d := parse_date(promise_before, "promise_before")) is not None:
        q = q.where(oo.c.promise_date < d)
    if at_risk is True:
        q = q.where(oo.c.code.in_(risk), oo.c.status.in_(("open", "picking")))
    elif at_risk is False:
        q = q.where(oo.c.code.not_in(risk))
    rows, nxt = await paginate(conn, q, [oo.c.code], limit=limit, cursor=cursor)
    items = [row_dict(r) for r in rows]
    codes = [i["code"] for i in items]
    lines: dict[str, list[dict[str, Any]]] = {c: [] for c in codes}
    if codes:
        for r in await conn.execute(
            select(ol.c.order_code, ol.c.line_no, ol.c.sku_code.label("sku"), ol.c.qty, ol.c.reserved_qty, ol.c.version)
            .where(ol.c.order_code.in_(codes))
            .order_by(ol.c.order_code, ol.c.line_no)
        ):
            lines[r.order_code].append(
                {"line_no": r.line_no, "sku": r.sku, "qty": r.qty, "reserved_qty": r.reserved_qty, "version": r.version}
            )
    for item in items:
        item["lines"] = lines[item["code"]]
        item["shortfall"] = sum(ln["qty"] - ln["reserved_qty"] for ln in item["lines"])
        item["at_risk"] = item["shortfall"] > 0
    return _page(items, nxt)


# ───────────────────────── promotions ─────────────────────────
async def list_promotions(
    conn: AsyncConnection, *, limit: int, cursor: str | None, active_on: str | None = None
) -> Page:
    pr = t.promotion
    q = select(
        pr.c.code,
        pr.c.name,
        pr.c.markets,
        pr.c.grades,
        pr.c.style_codes,
        *_period_cols(pr.c.period, "period_start", "period_end"),
        pr.c.discount_bp,
        pr.c.uplift_bp,
        pr.c.version,
    )
    if (d := parse_date(active_on, "active_on")) is not None:
        q = q.where(pr.c.period.op("@>")(d))
    rows, nxt = await paginate(conn, q, [pr.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def get_promotion(conn: AsyncConnection, code: str) -> dict[str, Any] | None:
    pr = t.promotion
    row = (
        await conn.execute(
            select(pr.c.code, *_period_cols(pr.c.period, "period_start", "period_end"), pr.c.version).where(
                pr.c.code == code
            )
        )
    ).first()
    return row_dict(row) if row else None


__all__ = [
    "ORIGIN_PORT",
    "booked_units",
    "factory_schedule",
    "fastest_transit",
    "lane_options",
    "list_capacity_bookings",
    "list_factory_lines",
    "list_lanes",
    "list_online_orders",
    "list_ports",
    "list_production_orders",
    "list_promotions",
    "list_rate_cards",
    "list_sailings",
    "list_shipments",
    "rate_on",
    "remaining_capacity",
]
