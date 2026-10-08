"""Read-side queries shared by REST (/v1) and MCP tools. All return JSON-ready dicts."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

from sqlalchemy import Integer, cast, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncConnection

from merxa_api import tables as t
from merxa_api.calendar import DEMO_TODAY, is_iso_week
from merxa_api.errors import Problem, not_found
from merxa_api.services.common import paginate, parse_date, parse_timestamp, row_dict

ENTITY_TYPES: tuple[str, ...] = ("sku", "style", "po", "store", "dc", "supplier", "factory")

Page = dict[str, Any]


def _page(items: list[dict[str, Any]], next_cursor: str | None) -> Page:
    return {"items": items, "next_cursor": next_cursor}


# ───────────────────────── locations ─────────────────────────
async def list_dcs(conn: AsyncConnection, *, limit: int, cursor: str | None) -> Page:
    q = select(*[c for c in t.dc.c if c.key not in ("id", "updated_at")])
    rows, nxt = await paginate(conn, q, [t.dc.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def list_clusters(conn: AsyncConnection, *, limit: int, cursor: str | None, dc_code: str | None = None) -> Page:
    count = (
        select(func.count())
        .select_from(t.store)
        .where(t.store.c.cluster_code == t.store_cluster.c.code)
        .scalar_subquery()
        .label("store_count")
    )
    q = select(
        t.store_cluster.c.code,
        t.store_cluster.c.dc_code,
        t.store_cluster.c.climate,
        t.store_cluster.c.grade_band,
        count,
        t.store_cluster.c.version,
    )
    if dc_code:
        q = q.where(t.store_cluster.c.dc_code == dc_code)
    rows, nxt = await paginate(conn, q, [t.store_cluster.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def list_locations(conn: AsyncConnection) -> dict[str, Any]:
    dcs = await conn.execute(
        select(t.dc.c.code, t.dc.c.name, t.dc.c.country, t.dc.c.lat, t.dc.c.lon).order_by(t.dc.c.code)
    )
    clusters = await list_clusters(conn, limit=10_000, cursor=None)
    return {
        "dcs": [row_dict(r) for r in dcs],
        "clusters": [
            {k: c[k] for k in ("code", "dc_code", "climate", "grade_band", "store_count")} for c in clusters["items"]
        ],
    }


def _store_cols() -> list[Any]:
    s = t.store.c
    return [
        s.code,
        s.name,
        s.market_code.label("market"),
        s.city,
        s.lat,
        s.lon,
        s.grade,
        s.cluster_code.label("cluster"),
        s.home_dc_code.label("home_dc"),
        s.sales_index,
        s.version,
    ]


async def list_stores(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    cluster_code: str | None = None,
    dc_code: str | None = None,
    market: str | None = None,
) -> Page:
    q = select(*_store_cols())
    if cluster_code:
        q = q.where(t.store.c.cluster_code == cluster_code)
    if dc_code:
        q = q.where(t.store.c.home_dc_code == dc_code)
    if market:
        q = q.where(t.store.c.market_code == market)
    rows, nxt = await paginate(conn, q, [t.store.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def _simple_list(conn: AsyncConnection, table: Any, *, limit: int, cursor: str | None, where: Any = None) -> Page:
    q = select(*[c for c in table.c if c.key not in ("id", "updated_at")])
    if where is not None:
        q = q.where(where)
    rows, nxt = await paginate(conn, q, [table.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def list_suppliers(conn: AsyncConnection, *, limit: int, cursor: str | None, kind: str | None = None) -> Page:
    return await _simple_list(
        conn, t.supplier, limit=limit, cursor=cursor, where=(t.supplier.c.kind == kind) if kind else None
    )


async def list_factories(
    conn: AsyncConnection, *, limit: int, cursor: str | None, supplier_code: str | None = None
) -> Page:
    where = (t.factory.c.supplier_code == supplier_code) if supplier_code else None
    return await _simple_list(conn, t.factory, limit=limit, cursor=cursor, where=where)


async def list_styles(conn: AsyncConnection, *, limit: int, cursor: str | None, family: str | None = None) -> Page:
    return await _simple_list(
        conn, t.style, limit=limit, cursor=cursor, where=(t.style.c.family == family) if family else None
    )


async def list_skus(conn: AsyncConnection, *, limit: int, cursor: str | None, style_code: str | None = None) -> Page:
    where = (t.sku.c.style_code == style_code) if style_code else None
    return await _simple_list(conn, t.sku, limit=limit, cursor=cursor, where=where)


# ───────────────────────── purchase orders ─────────────────────────
def _po_summary_query() -> Any:
    po, pl = t.purchase_order, t.po_line
    agg = (
        select(
            pl.c.po_code,
            func.count().label("line_count"),
            func.coalesce(func.sum(pl.c.qty_ordered), 0).label("total_qty"),
        )
        .group_by(pl.c.po_code)
        .subquery()
    )
    q = select(
        po.c.code,
        po.c.supplier_code,
        po.c.factory_code,
        po.c.dest_dc_code.label("dest_dc"),
        po.c.status,
        po.c.ex_factory_date,
        po.c.eta_dc,
        func.coalesce(agg.c.line_count, 0).label("line_count"),
        func.coalesce(agg.c.total_qty, 0).label("total_qty"),
        po.c.version,
    ).select_from(po.outerjoin(agg, agg.c.po_code == po.c.code))
    return q


async def list_purchase_orders(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    status: str | None = None,
    supplier_code: str | None = None,
    factory_code: str | None = None,
    dest_dc: str | None = None,
    eta_from: str | None = None,
    eta_to: str | None = None,
) -> Page:
    po = t.purchase_order
    q = _po_summary_query()
    if status:
        q = q.where(po.c.status == status)
    if supplier_code:
        q = q.where(po.c.supplier_code == supplier_code)
    if factory_code:
        q = q.where(po.c.factory_code == factory_code)
    if dest_dc:
        q = q.where(po.c.dest_dc_code == dest_dc)
    if (d := parse_date(eta_from, "eta_from")) is not None:
        q = q.where(po.c.eta_dc >= d)
    if (d := parse_date(eta_to, "eta_to")) is not None:
        q = q.where(po.c.eta_dc <= d)
    rows, nxt = await paginate(conn, q, [po.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def get_purchase_order(conn: AsyncConnection, code: str) -> dict[str, Any]:
    row = (await conn.execute(_po_summary_query().where(t.purchase_order.c.code == code))).first()
    if row is None:
        raise not_found("purchase order", code)
    pl = t.po_line
    lines = await conn.execute(
        select(
            pl.c.line_no,
            pl.c.sku_code.label("sku"),
            pl.c.qty_ordered,
            pl.c.qty_received,
            pl.c.eta_dc,
            pl.c.status,
            pl.c.version,
        )
        .where(pl.c.po_code == code)
        .order_by(pl.c.line_no)
    )
    out = row_dict(row)
    out["lines"] = [row_dict(r) for r in lines]
    return out


# ───────────────────────── BOM ─────────────────────────
async def get_bom_where_used(
    conn: AsyncConnection, *, component_code: str | None = None, supplier_code: str | None = None
) -> dict[str, Any]:
    if not component_code and not supplier_code:
        raise Problem(400, "invalid_parameter", "component_code or supplier_code is required")
    cq = select(t.component.c.code, t.component.c.name, t.component.c.kind, t.component.c.supplier_code)
    if component_code:
        cq = cq.where(t.component.c.code == component_code)
    if supplier_code:
        cq = cq.where(t.component.c.supplier_code == supplier_code)
    components = [row_dict(r) for r in await conn.execute(cq.order_by(t.component.c.code))]
    codes = [c["code"] for c in components]
    if not codes:
        return {"components": [], "styles": [], "factories": [], "open_po_lines": []}

    bl = t.bom_line
    bom_rows = list(
        await conn.execute(
            select(bl.c.style_code, bl.c.component_code, bl.c.factory_code, bl.c.qty_per_unit)
            .where(bl.c.component_code.in_(codes))
            .order_by(bl.c.style_code, bl.c.component_code, bl.c.factory_code)
        )
    )
    styles: dict[str, int] = {}
    style_codes: set[str] = set()
    explicit_factories: set[str] = set()
    any_factory = False
    for style_code, _comp, fac, qty in bom_rows:
        style_codes.add(style_code)
        styles[style_code] = max(styles.get(style_code, 0), qty)
        if fac is None:
            any_factory = True
        else:
            explicit_factories.add(fac)

    fq = select(t.factory.c.code, t.factory.c.name, t.factory.c.country, t.factory.c.supplier_code)
    if not any_factory:
        fq = fq.where(t.factory.c.code.in_(sorted(explicit_factories)))
    factories = [row_dict(r) for r in await conn.execute(fq.order_by(t.factory.c.code))]
    factory_codes = [f["code"] for f in factories]

    po, pl = t.purchase_order, t.po_line
    lines = await conn.execute(
        select(
            pl.c.po_code,
            pl.c.line_no,
            pl.c.sku_code.label("sku"),
            (pl.c.qty_ordered - pl.c.qty_received).label("qty"),
            pl.c.eta_dc,
            po.c.factory_code,
            po.c.dest_dc_code.label("dest_dc"),
            pl.c.status,
            pl.c.version,
        )
        .select_from(pl.join(po, po.c.code == pl.c.po_code).join(t.sku, t.sku.c.code == pl.c.sku_code))
        .where(
            po.c.factory_code.in_(factory_codes),
            t.sku.c.style_code.in_(sorted(style_codes)),
            pl.c.status.in_(("open", "in_production", "shipped")),
        )
        .order_by(pl.c.po_code, pl.c.line_no)
    )
    return {
        "components": components,
        "styles": [{"style_code": s, "qty_per_unit": q} for s, q in sorted(styles.items())],
        "factories": factories,
        "open_po_lines": [row_dict(r) for r in lines],
    }


# ───────────────────────── inventory & demand ─────────────────────────
INVENTORY_AGGREGATES = ("none", "cluster", "dc")
FORECAST_AGGREGATES = ("store", "cluster")


def _sku_filter(col: Any, sku_codes: Sequence[str] | None, style_code: str | None) -> list[Any]:
    conds = []
    if sku_codes:
        conds.append(col.in_(list(sku_codes)))
    if style_code:
        conds.append(col.in_(select(t.sku.c.code).where(t.sku.c.style_code == style_code)))
    return conds


async def list_inventory(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    location_codes: Sequence[str] | None = None,
    sku_codes: Sequence[str] | None = None,
    style_code: str | None = None,
    location_type: str | None = None,
    aggregate: str | None = None,
) -> Page:
    """Inventory positions with ATP. ``aggregate``: ``none`` (default) = raw DC and store
    rows; ``cluster`` = DC rows unchanged + store rows summed per store cluster
    (``location_type: "cluster"``, ``store_count``); ``dc`` = DC rows unchanged + store rows
    summed per home DC (``location_type: "dc_stores"``). ``location_type`` selects DC rows
    (``dc``) or store-level rows (``store``: raw or aggregated)."""
    aggregate = aggregate or "none"
    if aggregate not in INVENTORY_AGGREGATES:
        raise Problem(400, "invalid_parameter", f"aggregate must be one of {list(INVENTORY_AGGREGATES)}")
    if location_type and location_type not in ("dc", "store", "cluster", "dc_stores"):
        raise Problem(400, "invalid_parameter", "location_type must be 'dc' or 'store'")
    ip = t.inventory_position
    want_dc = location_type in (None, "dc")
    want_stores = location_type in (None, "store", "cluster", "dc_stores")
    parts = []
    if want_dc or aggregate == "none":
        raw = select(
            ip.c.location_type,
            ip.c.location_code,
            ip.c.sku_code.label("sku"),
            ip.c.on_hand,
            ip.c.allocated,
            ip.c.reserved,
            ip.c.in_transit,
            (ip.c.on_hand - ip.c.allocated - ip.c.reserved).label("atp"),
            literal(None, type_=Integer).label("store_count"),
            ip.c.version,
        ).where(*_sku_filter(ip.c.sku_code, sku_codes, style_code))
        if aggregate == "none":
            if location_type in ("dc", "store"):
                raw = raw.where(ip.c.location_type == location_type)
        else:
            raw = raw.where(ip.c.location_type == "dc")
        parts.append(raw)
    if aggregate != "none" and want_stores:
        st = t.store
        group = st.c.cluster_code if aggregate == "cluster" else st.c.home_dc_code
        agg = (
            select(
                literal("cluster" if aggregate == "cluster" else "dc_stores").label("location_type"),
                group.label("location_code"),
                ip.c.sku_code.label("sku"),
                func.sum(ip.c.on_hand).label("on_hand"),
                func.sum(ip.c.allocated).label("allocated"),
                func.sum(ip.c.reserved).label("reserved"),
                func.sum(ip.c.in_transit).label("in_transit"),
                func.sum(ip.c.on_hand - ip.c.allocated - ip.c.reserved).label("atp"),
                func.count().label("store_count"),
                literal(None, type_=Integer).label("version"),
            )
            .select_from(ip.join(st, st.c.code == ip.c.location_code))
            .where(ip.c.location_type == "store", *_sku_filter(ip.c.sku_code, sku_codes, style_code))
            .group_by(group, ip.c.sku_code)
        )
        parts.append(agg)
    if not parts:
        return _page([], None)
    sq = (parts[0] if len(parts) == 1 else union_all(*parts)).subquery()
    q = select(
        sq.c.location_type,
        sq.c.location_code,
        sq.c.sku,
        cast(sq.c.on_hand, Integer).label("on_hand"),
        cast(sq.c.allocated, Integer).label("allocated"),
        cast(sq.c.reserved, Integer).label("reserved"),
        cast(sq.c.in_transit, Integer).label("in_transit"),
        cast(sq.c.atp, Integer).label("atp"),
        sq.c.store_count,
        sq.c.version,
    )
    if location_codes:
        q = q.where(sq.c.location_code.in_(list(location_codes)))
    rows, nxt = await paginate(conn, q, [sq.c.location_type, sq.c.location_code, sq.c.sku], limit=limit, cursor=cursor)
    items = []
    for r in rows:
        d = row_dict(r)
        if d["store_count"] is None:
            d.pop("store_count")
        else:
            d.pop("version")
        items.append(d)
    return _page(items, nxt)


async def get_demand_forecast(
    conn: AsyncConnection,
    *,
    style_code: str | None = None,
    cluster_codes: Sequence[str] | None = None,
    weeks: Sequence[str] | None = None,
    store_codes: Sequence[str] | None = None,
    aggregate: str | None = None,
    limit: int = 10_000,
    cursor: str | None = None,
) -> dict[str, Any]:
    """Weekly forecast per style. Rows are stored per style × store × ISO week;
    ``aggregate="cluster"`` (default) sums them per store cluster, ``"store"`` returns the
    store rows. Size curves (basis points) per family × cluster come along."""
    aggregate = aggregate or "cluster"
    if aggregate not in FORECAST_AGGREGATES:
        raise Problem(400, "invalid_parameter", f"aggregate must be one of {list(FORECAST_AGGREGATES)}")
    df = t.demand_forecast
    if weeks:
        bad = [w for w in weeks if not is_iso_week(w)]
        if bad:
            raise Problem(400, "invalid_parameter", f"weeks must be ISO weeks (YYYY-Www): {bad}")
    conds = []
    if style_code:
        conds.append(df.c.style_code == style_code)
    if cluster_codes:
        conds.append(df.c.cluster_code.in_(list(cluster_codes)))
    if weeks:
        conds.append(df.c.iso_week.in_(list(weeks)))
    if store_codes:
        conds.append(df.c.store_code.in_(list(store_codes)))
    if aggregate == "cluster":
        sq = (
            select(df.c.style_code, df.c.cluster_code, df.c.iso_week, func.sum(df.c.units).label("units"))
            .where(*conds)
            .group_by(df.c.style_code, df.c.cluster_code, df.c.iso_week)
            .subquery()
        )
        q = select(sq.c.style_code, sq.c.cluster_code, sq.c.iso_week, cast(sq.c.units, Integer).label("units"))
        keys = [sq.c.style_code, sq.c.cluster_code, sq.c.iso_week]
    else:
        q = select(df.c.style_code, df.c.store_code, df.c.cluster_code, df.c.iso_week, df.c.units).where(
            *conds, df.c.store_code.is_not(None)
        )
        keys = [df.c.style_code, df.c.store_code, df.c.iso_week]
    rows, nxt = await paginate(conn, q, keys, limit=limit, cursor=cursor)

    sc = t.size_curve
    cq = select(sc.c.family, sc.c.cluster_code, sc.c.size, sc.c.share_bp, func.min(t.sku.c.size_seq).label("seq"))
    cq = cq.select_from(sc.join(t.sku, t.sku.c.size == sc.c.size)).group_by(
        sc.c.family, sc.c.cluster_code, sc.c.size, sc.c.share_bp
    )
    if style_code:
        cq = cq.where(sc.c.family == select(t.style.c.family).where(t.style.c.code == style_code).scalar_subquery())
    if cluster_codes:
        cq = cq.where(sc.c.cluster_code.in_(list(cluster_codes)))
    curves = await conn.execute(cq.order_by(sc.c.family, sc.c.cluster_code, "seq"))
    return {
        "aggregate": aggregate,
        "items": [row_dict(r) for r in rows],
        "next_cursor": nxt,
        "size_curves": [
            {"family": r.family, "cluster_code": r.cluster_code, "size": r.size, "share_bp": r.share_bp} for r in curves
        ],
    }


# ───────────────────────── orders ─────────────────────────
async def list_allocations(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    dc_code: str | None = None,
    store_code: str | None = None,
    cluster_code: str | None = None,
    sku_codes: Sequence[str] | None = None,
    status: str | None = None,
    ship_week: str | None = None,
) -> Page:
    a = t.allocation
    q = select(
        a.c.code,
        a.c.sku_code.label("sku"),
        a.c.dc_code,
        a.c.store_code,
        a.c.ship_week,
        a.c.qty,
        a.c.status,
        a.c.version,
    )
    if dc_code:
        q = q.where(a.c.dc_code == dc_code)
    if store_code:
        q = q.where(a.c.store_code == store_code)
    if cluster_code:
        q = q.where(a.c.store_code.in_(select(t.store.c.code).where(t.store.c.cluster_code == cluster_code)))
    if sku_codes:
        q = q.where(a.c.sku_code.in_(list(sku_codes)))
    if status:
        q = q.where(a.c.status == status)
    if ship_week:
        q = q.where(a.c.ship_week == ship_week)
    rows, nxt = await paginate(conn, q, [a.c.code], limit=limit, cursor=cursor)
    return _page([row_dict(r) for r in rows], nxt)


async def list_transfer_orders(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    status: str | None = None,
    from_dc: str | None = None,
    to_dc: str | None = None,
) -> Page:
    to = t.transfer_order
    q = select(*[c for c in to.c if c.key not in ("id", "updated_at")])
    if status:
        q = q.where(to.c.status == status)
    if from_dc:
        q = q.where(to.c.from_dc == from_dc)
    if to_dc:
        q = q.where(to.c.to_dc == to_dc)
    rows, nxt = await paginate(conn, q, [to.c.code], limit=limit, cursor=cursor)
    items = [row_dict(r) for r in rows]
    codes = [i["code"] for i in items]
    lines: dict[str, list[dict[str, Any]]] = {c: [] for c in codes}
    if codes:
        tl = t.transfer_order_line
        for r in await conn.execute(
            select(tl.c.to_code, tl.c.line_no, tl.c.sku_code.label("sku"), tl.c.qty)
            .where(tl.c.to_code.in_(codes))
            .order_by(tl.c.to_code, tl.c.line_no)
        ):
            lines[r.to_code].append({"line_no": r.line_no, "sku": r.sku, "qty": r.qty})
    for item in items:
        item["lines"] = lines[item["code"]]
        item["total_qty"] = sum(line["qty"] for line in item["lines"])
    return _page(items, nxt)


async def list_supplier_messages(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    supplier_code: str | None = None,
    since: str | None = None,
    status: str | None = None,
) -> Page:
    m = t.supplier_message
    q = _supplier_messages_query(supplier_code=supplier_code, since=since, status=status)
    rows, nxt = await paginate(
        conn, q, [m.c.received_at, m.c.id], limit=limit, cursor=cursor, key_types=(datetime, uuid.UUID)
    )
    return _page([row_dict(r) for r in rows], nxt)


async def latest_supplier_messages(
    conn: AsyncConnection, *, limit: int, supplier_code: str | None = None, since: str | None = None
) -> list[dict[str, Any]]:
    """The **newest** ``limit`` messages matching the filters, oldest first (MCP
    ``list_supplier_messages`` has no cursor: once an inbox outgrows the cap the newest
    messages — the ones an investigation needs — must be the ones returned, not dropped)."""
    m = t.supplier_message
    q = _supplier_messages_query(supplier_code=supplier_code, since=since, status=None)
    rows = list(await conn.execute(q.order_by(m.c.received_at.desc(), m.c.id.desc()).limit(limit)))
    return [row_dict(r) for r in reversed(rows)]


def _supplier_messages_query(*, supplier_code: str | None, since: str | None, status: str | None) -> Any:
    m = t.supplier_message
    q = select(m.c.id, m.c.supplier_code, m.c.received_at, m.c.subject, m.c.body, m.c.status, m.c.version)
    if supplier_code:
        q = q.where(m.c.supplier_code == supplier_code)
    if (ts := parse_timestamp(since, "since")) is not None:
        q = q.where(m.c.received_at >= ts)
    if status:
        q = q.where(m.c.status == status)
    return q


# ───────────────────────── search / entity ─────────────────────────
_SEARCH_SOURCES: dict[str, tuple[Any, Any, Any]] = {}


def _search_sources() -> dict[str, tuple[Any, Any, Any]]:
    if not _SEARCH_SOURCES:
        _SEARCH_SOURCES.update(
            {
                "sku": (t.sku, t.sku.c.code, t.sku.c.code),
                "style": (t.style, t.style.c.code, t.style.c.name),
                "po": (t.purchase_order, t.purchase_order.c.code, t.purchase_order.c.code),
                "store": (t.store, t.store.c.code, t.store.c.name),
                "dc": (t.dc, t.dc.c.code, t.dc.c.name),
                "supplier": (t.supplier, t.supplier.c.code, t.supplier.c.name),
                "factory": (t.factory, t.factory.c.code, t.factory.c.name),
            }
        )
    return _SEARCH_SOURCES


async def search_entities(
    conn: AsyncConnection, *, query: str, types: Sequence[str] | None = None, limit: int = 20
) -> dict[str, Any]:
    query = (query or "").strip()
    if not query:
        raise Problem(400, "invalid_parameter", "query must not be empty")
    wanted = list(types) if types else list(ENTITY_TYPES)
    unknown = [ty for ty in wanted if ty not in ENTITY_TYPES]
    if unknown:
        raise Problem(400, "invalid_parameter", f"unknown entity types: {unknown}")
    pattern = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    parts = []
    for rank, ty in enumerate(ENTITY_TYPES):
        if ty not in wanted:
            continue
        table, code_col, label_col = _search_sources()[ty]
        extra = [table.c.city] if "city" in table.c else []
        cond = or_(
            code_col.ilike(pattern, escape="\\"),
            label_col.ilike(pattern, escape="\\"),
            *[c.ilike(pattern, escape="\\") for c in extra],
        )
        exact = func.lower(code_col) == query.lower()
        parts.append(
            select(
                literal(ty).label("type"),
                code_col.label("code"),
                label_col.label("label"),
                literal(rank).label("type_rank"),
                exact.label("exact"),
            ).where(cond)
        )
    u = union_all(*parts).subquery()
    rows = await conn.execute(
        select(u.c.type, u.c.code, u.c.label)
        .order_by(u.c.exact.desc(), u.c.type_rank, u.c.code)
        .limit(min(max(limit, 1), 200))
    )
    return {"items": [{"type": r.type, "code": r.code, "label": r.label} for r in rows]}


async def get_entity(conn: AsyncConnection, *, type: str, code: str) -> dict[str, Any]:
    if type not in ENTITY_TYPES:
        raise Problem(400, "invalid_parameter", f"type must be one of {list(ENTITY_TYPES)}")
    if type == "po":
        data = await get_purchase_order(conn, code)
        return {"type": type, "code": code, "data": data, "version": data["version"]}
    table, code_col, _ = _search_sources()[type]
    row = (await conn.execute(select(table).where(code_col == code))).first()
    if row is None:
        raise not_found(type, code)
    data = row_dict(row, exclude=("id", "updated_at"))
    if type == "sku":
        inv = await conn.execute(
            select(
                t.inventory_position.c.location_code,
                (
                    t.inventory_position.c.on_hand - t.inventory_position.c.allocated - t.inventory_position.c.reserved
                ).label("atp"),
            )
            .where(t.inventory_position.c.sku_code == code, t.inventory_position.c.location_type == "dc")
            .order_by(t.inventory_position.c.location_code)
        )
        data["dc_atp"] = {r.location_code: r.atp for r in inv}
    if type == "style":
        data["skus"] = [
            r.code
            for r in await conn.execute(
                select(t.sku.c.code).where(t.sku.c.style_code == code).order_by(t.sku.c.size_seq)
            )
        ]
    return {"type": type, "code": code, "data": data, "version": data["version"]}


# ───────────────────────── audit ─────────────────────────
async def list_audit(
    conn: AsyncConnection,
    *,
    limit: int,
    cursor: str | None,
    change_set_code: str | None = None,
    entity_type: str | None = None,
    entity_ref: str | None = None,
) -> Page:
    al = t.audit_log
    q = select(al)
    if change_set_code:
        q = q.where(al.c.change_set_code == change_set_code)
    if entity_type:
        q = q.where(al.c.entity_type == entity_type)
    if entity_ref:
        q = q.where(al.c.entity_ref == entity_ref)
    rows, nxt = await paginate(conn, q, [al.c.id], limit=limit, cursor=cursor, key_types=(int,))
    return _page([row_dict(r) for r in rows], nxt)


async def sim_today(conn: AsyncConnection) -> date:
    row = (await conn.execute(select(t.sim_clock.c.today))).first()
    return row.today if row else DEMO_TODAY
