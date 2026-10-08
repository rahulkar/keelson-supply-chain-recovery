"""Weekly projection used to prove (and demo) the seed story.

``project(data, shift_bd_days=7)`` answers: if every open Bangladesh PO line arrives N
days later, which DCs go short in which ISO weeks, and how much stock can DE1/PL1 give
away without going short themselves? ``shifts={"VN": 10}`` delays the Vietnamese lines
instead (the ``full`` profile's port-closure story). ``ref_shifts`` delays single PO lines;
:func:`closure_shifts` derives them from the **lane data** for a chokepoint closure (the
``full`` profile's Act 2 story: Suez closed 21 days → the VN sailings go round the Cape).

The projection mirrors the Keelson optimizer's impact model (``docs/contracts/
40-optimizer.md``), so the units this module reports are the units Keelson will report:

* **Supply.** Per DC x SKU the DC pool starts at ``on_hand - reserved + in_transit``
  (released allocations still sit in the DC and serve its clusters). Per cluster x SKU
  the cluster starts with the **store stock** of its stores, ``Σ max(0, on_hand -
  allocated - reserved)``. Receipts (open PO lines) join the pool in the ISO week of
  their (shifted) ETA; an ETA before the first week lands in the first week, after
  the last week it is not counted.
* **Weekly fair share.** Each cluster requests ``max(0, demand - cluster stock)``; when
  the pool cannot fill every request it is rationed pro-rata to the requests (largest
  remainder, ties by cluster code). Unmet demand is lost (no backorders); leftover
  cluster stock carries over.

Reported per DC:

* ``shortage`` — lost units per week (summed over clusters and SKUs).
* ``first_breach`` — earliest short week per (cluster, style).
* ``min_pab`` — backorder view at network level (DC + store stock + cumulative
  receipts − cumulative demand), Σ_sku min(0, PAB) per week.
* ``transferable`` — Σ_sku max(0, min(ATP, min_w network PAB)): what the DC can ship to
  another DC (Merxa checks ATP) without its own network going short.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection

from merxa_api import tables as t
from merxa_api.calendar import iso_week
from merxa_api.seed import reference as ref
from merxa_api.seed import reference_full as reff
from merxa_api.seed.split import sku_split

OPEN_LINE_STATUSES = ("open", "in_production", "shipped")


MODE_SPEED: dict[str, int] = {"sea": 0, "road": 0, "sea_air": 1, "air": 2}
"""Mode ranking (as in the Keelson optimizer): a closure never re-routes onto a faster,
premium mode automatically — that is an expedite decision."""


@dataclass(frozen=True)
class LaneInfo:
    mode: str
    transit_days: int
    via: tuple[str, ...]
    alt_lane_code: str | None
    origin: str | None = None
    dest: str | None = None


@dataclass
class StoryInputs:
    weeks: list[str]
    atp: dict[tuple[str, str], int]  # (dc, sku) → on_hand - allocated - reserved
    dc_supply: dict[tuple[str, str], int]  # (dc, sku) → on_hand - reserved + in_transit
    cluster_dc: dict[str, str]  # cluster → dc
    cluster_stock: dict[tuple[str, str], int]  # (cluster, sku) → Σ store max(0, on_hand - allocated - reserved)
    # (dc, sku, eta, open qty, factory country, po line ref "<po>#<line>")
    receipts: list[tuple[str, str, date, int, str, str]]
    demand: dict[tuple[str, str], dict[str, int]]  # (cluster, sku) → week → units
    sku_style: dict[str, str]
    as_of: date | None = None  # sim clock "today"
    routes: dict[str, tuple[str, date]] = field(default_factory=dict)  # po line ref → (main lane, departure)
    lanes: dict[str, LaneInfo] = field(default_factory=dict)


@dataclass
class DcProjection:
    shortage: dict[str, int] = field(default_factory=dict)  # week → lost units (summed over SKUs)
    first_breach: dict[tuple[str, str], str] = field(default_factory=dict)  # (cluster, style) → week
    min_pab: dict[str, int] = field(default_factory=dict)  # week → Σ_sku min(0, network PAB) (backorder)
    transferable: int = 0  # Σ_sku max(0, min(ATP, min_w network PAB))
    lost_by_sku: dict[str, int] = field(default_factory=dict)  # sku → lost units (only SKUs with losses)
    transferable_by_sku: dict[str, int] = field(default_factory=dict)  # sku → transferable (only > 0)

    @property
    def total_shortage(self) -> int:
        return sum(self.shortage.values())


def ration(pool: int, requests: Sequence[int]) -> list[int]:
    """Pro-rata integer allocation of ``pool`` to ``requests`` (largest remainder, ties by index)."""
    total = sum(requests)
    if total <= pool:
        return list(requests)
    if pool <= 0:
        return [0] * len(requests)
    alloc = [pool * r // total for r in requests]
    leftover = pool - sum(alloc)
    order = sorted(range(len(requests)), key=lambda i: (-((pool * requests[i]) % total), i))
    for i in order[:leftover]:
        alloc[i] += 1
    return alloc


def closure_shifts(data: StoryInputs, chokepoint: str, *, days: int, start: date | None = None) -> dict[str, int]:
    """Extra days per open PO line when ``chokepoint`` closes for ``days`` from ``start``
    (default: the sim clock), resolved from the lane data the way the Keelson optimizer
    resolves a ``capacity_scale factor_bp 0`` closure (docs/contracts/90 §1, 80):

    a line whose main lane has the chokepoint in ``via`` and whose sailing
    departs inside ``[start, start + days)`` either waits for the reopening (``days``) or
    switches to the lane's ``alt_lane_code`` (``alt transit − transit`` extra days) when that
    lane avoids the chokepoint and is not a faster (premium) mode — whichever is earlier."""
    if days <= 0:
        return {}
    start = start or data.as_of
    if start is None:
        raise ValueError("closure_shifts needs a start date (StoryInputs.as_of is unset)")
    end = start + timedelta(days=days)
    out: dict[str, int] = {}
    for *_, line_ref in data.receipts:
        route = data.routes.get(line_ref)
        if route is None:
            continue
        lane_code, departure = route
        lane = data.lanes.get(lane_code)
        if lane is None or chokepoint not in lane.via or not start <= departure < end:
            continue
        options = [days]
        alt = data.lanes.get(lane.alt_lane_code) if lane.alt_lane_code else None
        if (
            alt is not None
            and chokepoint not in alt.via
            and MODE_SPEED.get(alt.mode, 0) <= MODE_SPEED.get(lane.mode, 0)
        ):
            options.append(max(0, alt.transit_days - lane.transit_days))
        out[line_ref] = min(options)
    return out


def project(
    data: StoryInputs,
    *,
    shift_bd_days: int = 0,
    shifts: dict[str, int] | None = None,
    ref_shifts: dict[str, int] | None = None,
) -> dict[str, DcProjection]:
    """Project weekly shortages; ``shifts`` maps a factory country to a delay in days,
    ``ref_shifts`` a PO line ref (``<po>#<line>``) to extra days (both add up)."""
    weeks = data.weeks
    n = len(weeks)
    pos = {w: i for i, w in enumerate(weeks)}
    delay = dict(shifts or {})
    if shift_bd_days:
        delay["BD"] = delay.get("BD", 0) + shift_bd_days
    per_ref = ref_shifts or {}
    rec: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * n)
    for dc, sku, eta, qty, country, line_ref in data.receipts:
        wk = iso_week(eta + timedelta(days=delay.get(country, 0) + per_ref.get(line_ref, 0)))
        if wk < weeks[0]:
            wk = weeks[0]
        if wk in pos:
            rec[(dc, sku)][pos[wk]] += qty

    clusters_of: dict[str, list[str]] = defaultdict(list)
    for cl in sorted(data.cluster_dc):
        clusters_of[data.cluster_dc[cl]].append(cl)
    dcs = sorted({dc for dc, _ in data.dc_supply} | set(clusters_of))
    skus = sorted({s for _, s in data.dc_supply} | {s for _, s in data.demand} | {s for _, s in data.cluster_stock})

    out: dict[str, DcProjection] = {}
    for dc in dcs:
        proj = out[dc] = DcProjection(shortage=dict.fromkeys(weeks, 0), min_pab=dict.fromkeys(weeks, 0))
        for sku in skus:
            key = (dc, sku)
            active = [
                c for c in clusters_of.get(dc, []) if (c, sku) in data.demand or data.cluster_stock.get((c, sku), 0)
            ]
            dem = [[data.demand.get((c, sku), {}).get(w, 0) for w in weeks] for c in active]
            stock = [data.cluster_stock.get((c, sku), 0) for c in active]
            r = rec.get(key, [0] * n)
            pool = data.dc_supply.get(key, 0)
            pab = pool + sum(stock)
            lowest: int | None = None
            style = data.sku_style.get(sku, sku)
            for t_ in range(n):
                pool += r[t_]
                alloc = ration(pool, [max(0, dem[i][t_] - stock[i]) for i in range(len(active))])
                pool -= sum(alloc)
                for i, cl in enumerate(active):
                    available = stock[i] + alloc[i]
                    lost = max(0, dem[i][t_] - available)
                    if lost:
                        proj.shortage[weeks[t_]] += lost
                        proj.lost_by_sku[sku] = proj.lost_by_sku.get(sku, 0) + lost
                        proj.first_breach.setdefault((cl, style), weeks[t_])
                    stock[i] = max(0, available - dem[i][t_])
                pab += r[t_] - sum(dem[i][t_] for i in range(len(active)))
                proj.min_pab[weeks[t_]] += min(0, pab)
                lowest = pab if lowest is None else min(lowest, pab)
            give = max(0, min(data.atp.get(key, 0), lowest if lowest is not None else 0))
            if give:
                proj.transferable += give
                proj.transferable_by_sku[sku] = give
    return out


def recoverable(
    projection: dict[str, DcProjection],
    short_dcs: Sequence[str] = ref.SHORT_DCS,
    surplus_dcs: Sequence[str] = tuple(ref.SURPLUS_RATIO),
) -> int:
    """Rough estimate of the units a DC-to-DC rebalance from ``surplus_dcs`` recovers:
    Σ_sku min(units lost at ``short_dcs``, transferable at ``surplus_dcs``). It ignores lanes
    and timing; the optimizer's LP may do somewhat better (it also re-times stock)."""
    lost: dict[str, int] = defaultdict(int)
    give: dict[str, int] = defaultdict(int)
    for dc in short_dcs:
        for sku, qty in projection[dc].lost_by_sku.items():
            lost[sku] += qty
    for dc in surplus_dcs:
        for sku, qty in projection[dc].transferable_by_sku.items():
            give[sku] += qty
    return sum(min(qty, give[sku]) for sku, qty in lost.items())


def transfer_recoverable(inputs: StoryInputs, projection: dict[str, DcProjection]) -> int:
    """Rough estimate of the units a DC-to-DC transfer can recover, **along the lanes that
    exist**: for every short DC and SKU, ``min(units lost, transferable stock left)`` at the
    DCs with a transfer lane (origin and destination both DCs) **into** that DC. Transfer
    lanes are directional (the optimizer's ``reallocate_inventory`` uses them as given), so
    surplus at a DC without a lane into the short DC recovers nothing — unlike
    :func:`recoverable`, which ignores lanes. It ignores timing (an upper bound)."""
    dcs = set(projection)
    sources: dict[str, list[str]] = defaultdict(list)
    for code in sorted(inputs.lanes):
        ln = inputs.lanes[code]
        if ln.origin in dcs and ln.dest in dcs and ln.origin != ln.dest and ln.origin not in sources[ln.dest]:
            sources[ln.dest].append(ln.origin)
    left = {(dc, sku): qty for dc, p in projection.items() for sku, qty in p.transferable_by_sku.items()}
    total = 0
    for dc in sorted(projection):
        for sku, lost in sorted(projection[dc].lost_by_sku.items()):
            for src in sorted(sources.get(dc, ())):
                take = min(lost, left.get((src, sku), 0))
                if take:
                    left[(src, sku)] -= take
                    lost -= take
                    total += take
    return total


def inputs_from_dataset(ds: Any, weeks: list[str] | None = None) -> StoryInputs:
    """Build :class:`StoryInputs` straight from a generated :class:`~merxa_api.seed.dataset.Dataset`."""
    weeks = list(weeks or ref.FORECAST_WEEKS)
    cluster_dc = {c["code"]: c["dc_code"] for c in ds.iter_dicts("store_cluster")}
    store_cluster = {s["code"]: s["cluster_code"] for s in ds.iter_dicts("store")}
    atp: dict[tuple[str, str], int] = {}
    dc_supply: dict[tuple[str, str], int] = {}
    cluster_stock: dict[tuple[str, str], int] = defaultdict(int)
    inv = ds.tables["inventory_position"]
    for row in inv.rows:
        _add_position(*row, store_cluster, atp, dc_supply, cluster_stock)
    pos = {p["code"]: p for p in ds.iter_dicts("purchase_order")}
    country = {f["code"]: f["country"] for f in ds.iter_dicts("factory")}
    receipts = sorted(
        (
            pos[ln["po_code"]]["dest_dc_code"],
            ln["sku_code"],
            ln["eta_dc"],
            ln["qty_ordered"] - ln["qty_received"],
            country[pos[ln["po_code"]]["factory_code"]],
            f"{ln['po_code']}#{ln['line_no']}",
        )
        for ln in ds.iter_dicts("po_line")
        if ln["status"] in OPEN_LINE_STATUSES and ln["qty_ordered"] - ln["qty_received"] > 0
    )
    sailings = {s["code"]: (s["lane_code"], s["etd"]) for s in ds.iter_dicts("sailing")}
    shipments = {
        s["code"]: (s["lane_code"], s["sailing_code"], s["etd"], s["status"]) for s in ds.iter_dicts("shipment")
    }
    shipment_refs = [(sl["po_line_ref"], sl["shipment_code"]) for sl in ds.iter_dicts("shipment_line")]
    lanes = {
        ln["code"]: LaneInfo(
            ln["mode"], ln["transit_days"], tuple(ln["via"] or ()), ln["alt_lane_code"], ln["origin"], ln["dest"]
        )
        for ln in ds.iter_dicts("lane")
    }
    clock = [c["today"] for c in ds.iter_dicts("sim_clock")]
    sku_rows = [(s["code"], s["style_code"], s["size"], s["size_seq"]) for s in ds.iter_dicts("sku")]
    curves = [(c["family"], c["cluster_code"], c["size"], c["share_bp"]) for c in ds.iter_dicts("size_curve")]
    family_of = {s["code"]: s["family"] for s in ds.iter_dicts("style")}
    week_set = set(weeks)
    agg: dict[tuple[str, str, str], int] = defaultdict(int)
    fc = ds.tables["demand_forecast"]
    ci = {c: i for i, c in enumerate(fc.columns)}
    i_style, i_cluster, i_week, i_units = ci["style_code"], ci["cluster_code"], ci["iso_week"], ci["units"]
    for r in fc.rows:
        if r[i_week] in week_set:
            agg[(r[i_style], r[i_cluster], r[i_week])] += r[i_units]
    forecast = [(st, cc, wk, units) for (st, cc, wk), units in sorted(agg.items())]
    demand, sku_style = _cluster_demand(weeks, sku_rows, curves, family_of, forecast)
    return StoryInputs(
        weeks=weeks,
        atp=atp,
        dc_supply=dc_supply,
        cluster_dc=cluster_dc,
        cluster_stock=dict(cluster_stock),
        receipts=receipts,
        demand=demand,
        sku_style=sku_style,
        as_of=clock[0] if clock else None,
        routes=_routes(receipts, shipment_refs, shipments, sailings),
        lanes=lanes,
    )


def _routes(
    receipts: Sequence[tuple[Any, ...]],
    shipment_refs: Sequence[tuple[str, str]],
    shipments: dict[str, tuple[str | None, str | None, date, str]],
    sailings: dict[str, tuple[str, date]],
) -> dict[str, tuple[str, date]]:
    """Main lane + departure per open PO line: the sailing's lane/ETD when the shipment rides
    a sailing, else the shipment's own lane/ETD. A line on several live shipments takes the
    earliest departure (ties by shipment code)."""
    open_refs = {r[-1] for r in receipts}
    best: dict[str, tuple[date, str, str]] = {}
    for line_ref, ship_code in shipment_refs:
        ship = shipments.get(ship_code)
        if line_ref not in open_refs or ship is None:
            continue
        lane, sailing, etd, status = ship
        if status == "cancelled":
            continue
        if sailing and sailing in sailings:
            lane, etd = sailings[sailing]
        if not lane:
            continue
        cand = (etd, ship_code, lane)
        if line_ref not in best or cand < best[line_ref]:
            best[line_ref] = cand
    return {r: (lane, etd) for r, (etd, _, lane) in sorted(best.items())}


async def load_inputs(conn: AsyncConnection, weeks: list[str] | None = None) -> StoryInputs:
    """Read what the projection needs from the live Merxa database."""
    weeks = list(weeks or ref.FORECAST_WEEKS)
    cluster_dc = {
        code: dc for code, dc in await conn.execute(select(t.store_cluster.c.code, t.store_cluster.c.dc_code))
    }
    store_cluster = {code: cc for code, cc in await conn.execute(select(t.store.c.code, t.store.c.cluster_code))}
    atp: dict[tuple[str, str], int] = {}
    dc_supply: dict[tuple[str, str], int] = {}
    cluster_stock: dict[tuple[str, str], int] = defaultdict(int)
    ip = t.inventory_position
    for row in await conn.execute(
        select(
            ip.c.location_type,
            ip.c.location_code,
            ip.c.sku_code,
            ip.c.on_hand,
            ip.c.allocated,
            ip.c.reserved,
            ip.c.in_transit,
        )
    ):
        _add_position(*row, store_cluster, atp, dc_supply, cluster_stock)

    q = (
        select(
            t.purchase_order.c.dest_dc_code,
            t.po_line.c.sku_code,
            t.po_line.c.eta_dc,
            t.po_line.c.qty_ordered,
            t.po_line.c.qty_received,
            t.factory.c.country,
            t.po_line.c.po_code,
            t.po_line.c.line_no,
        )
        .select_from(
            t.po_line.join(t.purchase_order, t.po_line.c.po_code == t.purchase_order.c.code).join(
                t.factory, t.purchase_order.c.factory_code == t.factory.c.code
            )
        )
        .where(t.po_line.c.status.in_(OPEN_LINE_STATUSES))
    )
    receipts = sorted(
        (dc, sku, eta, qty_o - qty_r, country, f"{po_code}#{line_no}")
        for dc, sku, eta, qty_o, qty_r, country, po_code, line_no in await conn.execute(q)
        if qty_o - qty_r > 0
    )
    sa, sh, sl, ln = t.sailing, t.shipment, t.shipment_line, t.lane
    sailings = {
        code: (lane, etd) for code, lane, etd in await conn.execute(select(sa.c.code, sa.c.lane_code, sa.c.etd))
    }
    shipments = {
        r.code: (r.lane_code, r.sailing_code, r.etd, r.status)
        for r in await conn.execute(select(sh.c.code, sh.c.lane_code, sh.c.sailing_code, sh.c.etd, sh.c.status))
    }
    shipment_refs = [
        (r.po_line_ref, r.shipment_code) for r in await conn.execute(select(sl.c.po_line_ref, sl.c.shipment_code))
    ]
    lanes = {
        r.code: LaneInfo(r.mode, r.transit_days, tuple(r.via or ()), r.alt_lane_code, r.origin, r.dest)
        for r in await conn.execute(
            select(ln.c.code, ln.c.mode, ln.c.transit_days, ln.c.via, ln.c.alt_lane_code, ln.c.origin, ln.c.dest)
        )
    }
    as_of = (await conn.execute(select(t.sim_clock.c.today))).scalar_one_or_none()

    curves = list(
        await conn.execute(
            select(t.size_curve.c.family, t.size_curve.c.cluster_code, t.size_curve.c.size, t.size_curve.c.share_bp)
        )
    )
    sku_rows = list(await conn.execute(select(t.sku.c.code, t.sku.c.style_code, t.sku.c.size, t.sku.c.size_seq)))
    family_of = {code: fam for code, fam in await conn.execute(select(t.style.c.code, t.style.c.family))}
    df = t.demand_forecast
    fq = (
        select(df.c.style_code, df.c.cluster_code, df.c.iso_week, func.sum(df.c.units).label("units"))
        .where(df.c.iso_week.in_(weeks))
        .group_by(df.c.style_code, df.c.cluster_code, df.c.iso_week)
        .order_by(df.c.style_code, df.c.cluster_code, df.c.iso_week)
    )
    forecast = [(r.style_code, r.cluster_code, r.iso_week, int(r.units)) for r in await conn.execute(fq)]
    demand, sku_style = _cluster_demand(weeks, sku_rows, curves, family_of, forecast)
    return StoryInputs(
        weeks=weeks,
        atp=atp,
        dc_supply=dc_supply,
        cluster_dc=cluster_dc,
        cluster_stock=dict(cluster_stock),
        receipts=receipts,
        demand=demand,
        sku_style=sku_style,
        as_of=as_of,
        routes=_routes(receipts, shipment_refs, shipments, sailings),
        lanes=lanes,
    )


def _add_position(
    location_type: str,
    location_code: str,
    sku: str,
    on_hand: int,
    allocated: int,
    reserved: int,
    in_transit: int,
    store_cluster: dict[str, str],
    atp: dict[tuple[str, str], int],
    dc_supply: dict[tuple[str, str], int],
    cluster_stock: dict[tuple[str, str], int],
) -> None:
    if location_type == "dc":
        atp[(location_code, sku)] = on_hand - allocated - reserved
        dc_supply[(location_code, sku)] = max(0, on_hand - reserved) + in_transit
    elif location_code in store_cluster:
        cluster_stock[(store_cluster[location_code], sku)] += max(0, on_hand - allocated - reserved)


def _cluster_demand(
    weeks: list[str],
    sku_rows: Sequence[Sequence[Any]],
    curves: Sequence[Sequence[Any]],
    family_of: dict[str, str],
    forecast: Sequence[Sequence[Any]],
) -> tuple[dict[tuple[str, str], dict[str, int]], dict[str, str]]:
    """Split style-level cluster forecasts into SKUs with the cluster's size curve."""
    curve_bp: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for family, cc, size, bp in curves:
        curve_bp[(family, cc)][size] = bp
    sizes_by_style: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    sku_style: dict[str, str] = {}
    for code, style_code, size, seq in sku_rows:
        sizes_by_style[style_code].append((seq, size, code))
        sku_style[code] = style_code
    demand: dict[tuple[str, str], dict[str, int]] = defaultdict(lambda: dict.fromkeys(weeks, 0))
    for style_code, cc, wk, units in forecast:
        ordered = sorted(sizes_by_style[style_code])
        curve = curve_bp[(family_of[style_code], cc)]
        split = sku_split(units, [curve.get(size, 0) for _, size, _ in ordered])
        for (_, _, sku_code), qty in zip(ordered, split, strict=True):
            demand[(cc, sku_code)][wk] += qty
    return dict(demand), sku_style


def summarize(projection: dict[str, DcProjection]) -> dict[str, Any]:
    return {
        "units_at_risk": sum(p.total_shortage for p in projection.values()),
        "recoverable_by_rebalance": recoverable(projection),
        "dcs": {
            dc: {
                "shortage_by_week": {w: v for w, v in p.shortage.items() if v},
                "total_shortage": p.total_shortage,
                "first_breach_weeks": sorted(set(p.first_breach.values())),
                "first_breaches": len(p.first_breach),
                "transferable": p.transferable,
            }
            for dc, p in sorted(projection.items())
        },
    }


def knit_shortage(inputs: StoryInputs, projection: dict[str, DcProjection]) -> dict[str, int]:
    return {
        dc: sum(q for sku, q in p.lost_by_sku.items() if inputs.sku_style.get(sku, "")[:3] in KNIT_PREFIXES)
        for dc, p in sorted(projection.items())
    }


def story_report(inputs: StoryInputs) -> dict[str, Any]:
    """The documented scenarios: baseline, Bangladesh +7 days and (when Vietnamese supply
    exists) a 10-day closure of Ho Chi Minh City port and (when open PO lines sail via Suez)
    the Act 2 what-if: Suez closed for 21 days from the sim clock."""
    report: dict[str, Any] = {
        "baseline": summarize(project(inputs)),
        "bd_plus_7_days": summarize(project(inputs, shift_bd_days=7)),
    }
    if any(r[4] == "VN" for r in inputs.receipts):
        vn = project(inputs, shifts={"VN": 10})
        report["vn_port_closure_10_days"] = {**summarize(vn), "knit_shortage": knit_shortage(inputs, vn)}
    if inputs.as_of is not None:
        shifts = closure_shifts(inputs, reff.SUEZ_CHOKEPOINT, days=reff.SUEZ_CLOSURE_DAYS)
        if shifts:
            suez = project(inputs, ref_shifts=shifts)
            report[f"suez_closure_{reff.SUEZ_CLOSURE_DAYS}_days"] = {
                "chokepoint": reff.SUEZ_CHOKEPOINT,
                "days": reff.SUEZ_CLOSURE_DAYS,
                "from": inputs.as_of.isoformat(),
                "delayed_po_lines": len(shifts),
                "extra_days": sorted(set(shifts.values())),
                **summarize(suez),
                "knit_shortage": knit_shortage(inputs, suez),
                # Along the directional DC-to-DC lanes only: the knit surplus sits at UK1/NL1
                # but no lane runs from there to DE1/PL1, so a rebalance recovers (almost)
                # nothing here — the remedies are expedite / re-routing (sea-air lanes).
                "recoverable_by_dc_transfer": transfer_recoverable(inputs, suez),
            }
    return report


KNIT_PREFIXES = frozenset({"FLC", "KNT", "CRD", "HDY"})
