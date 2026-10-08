"""ProblemData v2 from the current ``scn/1.0`` snapshot (``80-m3-deterministic-core.md`` §2, §4).

One tenant-scoped read session (RLS), a fixed set of SQL aggregates over the ``scn_*`` tables of
one ``snapshot_id`` (stores are aggregated to clusters in SQL), then deterministic Python
assembly into :class:`~keelson_contracts.optimizer.ProblemData`:

* ``dcs`` / ``clusters`` / ``locations`` — ``scn_location`` (clusters are placed at their stores'
  centroid; locations exported without coordinates use the country centroid of
  ``data/geo/country_centroids.json``; ``airport`` maps to ``port``);
* ``skus`` — SKU items of the styles in scope (styles with forecast in the horizon or open PO lines);
* ``demand`` — store (or cluster) forecast summed per style × cluster × week in SQL, split to SKUs
  by the cluster's size curve with largest-remainder rounding (same rule as the M2 gatherer);
* ``inventory`` (DC rows) and ``cluster_inventory`` (store ATP summed per cluster, floored at 0 —
  Merxa's ``aggregate="cluster"`` semantics);
* ``receipts`` + ``po_lines`` — open PO lines (``qty_ordered > qty_received``, not
  received/cancelled/closed) with their inbound lane (from the sailing, else inferred from the
  transit time ``eta − ex_factory`` among the lanes from the factory's country);
* ``lanes`` — DC-to-DC transfer lanes from the tenant connector config; ``inbound_lanes`` —
  ``scn_lane`` legs chained from an origin port/factory to a DC (codes ``LEG+LEG``), cost and
  weekly capacity from the rate cards valid in the horizon — the binding (lowest) capacity and
  highest cost, since a lane carries one value for every week (``per_teu`` / ``per_kg``
  converted with the tenant's ``units_per_teu`` / ``grams_per_unit``);
* ``bom``, ``production`` (orders with the PO line's SKU and quantity; movable = planned/held; a
  line's daily capacity is the fastest rate its orders demonstrate), ``alt_sources`` (connector
  config ``alt_sources`` when given, else derived: a factory with a factory-specific BOM line for
  a style it does not currently supply is an approved source of that style);
* ``allocations`` — store allocations (planned/released, ship week ≥ the horizon start) summed per
  SKU × DC × cluster × ship week × status. A group of one keeps the Merxa code and version; larger
  groups get a synthetic code (:data:`GROUP_ALLOC_PREFIX`) that ``compile_changeset`` expands back
  to per-store ``allocation.set_qty`` items from the pinned snapshot (:func:`expand_allocation_group`).
  Released allocations are ``protected`` (already on their way to stores).
"""

from __future__ import annotations

import datetime as dt
import math
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import structlog
from keelson_contracts.changeset import PO_LINE_REF_PATTERN
from keelson_contracts.common import iso_week_of, monday_of, week_range
from keelson_contracts.optimizer import (
    DC,
    Allocation,
    AltSource,
    BomLine,
    Cluster,
    ClusterInventory,
    DemandRow,
    Horizon,
    InboundLane,
    InventoryPosition,
    Lane,
    Location,
    PoLine,
    ProblemData,
    ProductionLine,
    ProductionOrder,
    Receipt,
    Sku,
)

from .geo import GeoCatalog, e6, get_geo
from .problem import CLOSED_LINE_STATUSES, ProblemBundle, _split_units, receipt_ref
from .scn_store import SnapshotRef, SqlReader
from .spec import PlanningDefaults

__all__ = [
    "GROUP_ALLOC_PREFIX",
    "AllocationMember",
    "build_problem_from_snapshot",
    "compose_inbound_lanes",
    "expand_allocation_group",
    "parse_allocation_group",
    "receipts_for_entity",
]

log = structlog.get_logger(__name__)

GROUP_ALLOC_PREFIX = "grp:alloc:"
"""Synthetic code of a cluster allocation that aggregates several store allocations."""
OPEN_ALLOCATION_STATUSES = ("planned", "released")
"""Allocation statuses the target system lets ``allocation.set_qty`` change (Merxa)."""
MOVABLE_ORDER_STATUSES = frozenset({"planned", "held"})
UNBOUNDED_CAPACITY = 1_000_000_000
"""Weekly capacity of a lane leg without a rate card (no published limit)."""
MAX_LEGS = 4
_PO_LINE_REF = re.compile(PO_LINE_REF_PATTERN)

# ─────────────────────────────────── SQL ───────────────────────────────────
# Every query filters ``tenant`` and ``snapshot_id`` explicitly (RLS applies as well).

Q_LOCATIONS = """
SELECT kind, code, name, country, lat, lon, parent_code
  FROM scn_location
 WHERE tenant = :t AND snapshot_id = :s AND kind <> 'store'
 ORDER BY kind, code
"""
Q_CLUSTER_CENTROIDS = """
SELECT parent_code AS cluster_code, avg(lat) AS lat, avg(lon) AS lon, count(*) AS stores
  FROM scn_location
 WHERE tenant = :t AND snapshot_id = :s AND kind = 'store' AND parent_code IS NOT NULL
 GROUP BY parent_code
"""
Q_ITEMS = """
SELECT kind, code, style_code, family, size, size_seq, retail_price_cents, unit_cost_cents
  FROM scn_item
 WHERE tenant = :t AND snapshot_id = :s AND kind IN ('sku', 'style')
"""
Q_DEMAND = """
SELECT f.style_code, COALESCE(st.parent_code, f.location_code) AS cluster_code, f.iso_week,
       SUM(f.units)::bigint AS units
  FROM scn_demand_forecast f
  LEFT JOIN scn_location st
         ON f.location_kind = 'store' AND st.tenant = f.tenant AND st.snapshot_id = f.snapshot_id
        AND st.kind = 'store' AND st.code = f.location_code
 WHERE f.tenant = :t AND f.snapshot_id = :s AND f.iso_week = ANY(:weeks)
   AND (f.location_kind = 'cluster' OR st.parent_code IS NOT NULL)
 GROUP BY 1, 2, 3
"""
Q_SIZE_CURVES = """
SELECT family, cluster_code, size, share_bp
  FROM scn_size_curve
 WHERE tenant = :t AND snapshot_id = :s
"""
Q_INVENTORY_DC = """
SELECT location_code, sku_code, on_hand, allocated, reserved, in_transit
  FROM scn_inventory_position
 WHERE tenant = :t AND snapshot_id = :s AND location_kind = 'dc'
"""
Q_INVENTORY_CLUSTER = """
SELECT st.parent_code AS cluster_code, i.sku_code, SUM(i.on_hand - i.allocated - i.reserved)::bigint AS atp,
       count(*) AS positions
  FROM scn_inventory_position i
  JOIN scn_location st
    ON st.tenant = i.tenant AND st.snapshot_id = i.snapshot_id AND st.kind = 'store' AND st.code = i.location_code
 WHERE i.tenant = :t AND i.snapshot_id = :s AND i.location_kind = 'store' AND st.parent_code IS NOT NULL
 GROUP BY 1, 2
"""
Q_PO_LINES = """
SELECT p.source_id, p.po_code, p.line_no, p.supplier_code, p.factory_code, p.dest_dc_code, p.sku_code,
       p.qty_ordered, p.qty_received, p.ex_factory_date, p.eta_dc, p.status, p.src_version,
       s.lane_code AS sailing_lane
  FROM scn_po_line p
  LEFT JOIN scn_sailing s ON s.tenant = p.tenant AND s.snapshot_id = p.snapshot_id AND s.code = p.sailing_code
 WHERE p.tenant = :t AND p.snapshot_id = :s AND p.qty_ordered > p.qty_received
   AND p.status <> ALL(:closed)
 ORDER BY p.po_code, p.line_no
"""
Q_LANES = """
SELECT code, origin_code, dest_code, mode, transit_days, via, alt_lane_code
  FROM scn_lane
 WHERE tenant = :t AND snapshot_id = :s
 ORDER BY code
"""
Q_RATES = """
SELECT lane_code, valid_from, valid_to, basis, rate_cents, weekly_capacity
  FROM scn_lane_rate
 WHERE tenant = :t AND snapshot_id = :s
 ORDER BY lane_code, valid_from
"""
Q_BOM = """
SELECT style_code, component_code, supplier_code, factory_code
  FROM scn_bom_line
 WHERE tenant = :t AND snapshot_id = :s
 ORDER BY style_code, component_code, factory_code NULLS FIRST
"""
Q_PRODUCTION = """
SELECT o.code, o.po_line_ref, o.line_code, o.seq, o.planned_start, o.planned_end, o.status,
       p.sku_code, p.qty_ordered, p.factory_code, p.supplier_code
  FROM scn_production_order o
  LEFT JOIN scn_po_line p ON p.tenant = o.tenant AND p.snapshot_id = o.snapshot_id AND p.source_id = o.po_line_ref
 WHERE o.tenant = :t AND o.snapshot_id = :s
 ORDER BY o.line_code, o.seq, o.code
"""
Q_ALLOCATIONS = """
SELECT a.sku_code, a.dc_code, st.parent_code AS cluster_code, a.ship_week, a.status,
       count(*) AS n, SUM(a.qty)::bigint AS qty, MAX(a.src_version) AS version, MIN(a.code) AS code
  FROM scn_allocation a
  JOIN scn_location st
    ON st.tenant = a.tenant AND st.snapshot_id = a.snapshot_id AND st.kind = 'store' AND st.code = a.store_code
 WHERE a.tenant = :t AND a.snapshot_id = :s AND a.status = ANY(:statuses) AND a.ship_week >= :w0
   AND st.parent_code IS NOT NULL
 GROUP BY 1, 2, 3, 4, 5
"""
Q_ALLOCATION_MEMBERS = """
SELECT a.code, a.store_code, a.qty, a.src_version
  FROM scn_allocation a
  JOIN scn_location st
    ON st.tenant = a.tenant AND st.snapshot_id = a.snapshot_id AND st.kind = 'store' AND st.code = a.store_code
 WHERE a.tenant = :t AND a.snapshot_id = :s AND a.sku_code = :sku AND a.dc_code = :dc
   AND st.parent_code = :cluster AND a.ship_week = :week AND a.status = :status
 ORDER BY a.code
"""


# ────────────────────────────────── helpers ──────────────────────────────────


def _int(value: Any, default: int = 0) -> int:
    if value is None:
        return default
    return int(value)


def _date(value: Any) -> dt.date | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def _point(lat: Any, lon: Any) -> tuple[int, int] | None:
    if lat is None or lon is None:
        return None
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat_f) and math.isfinite(lon_f)):
        return None
    return (e6(lon_f), e6(lat_f))


@dataclass(frozen=True)
class _Leg:
    code: str
    origin: str
    dest: str
    mode: str
    transit_days: int
    via: tuple[str, ...]
    alt_lane_code: str | None
    cost_per_unit_cents: int
    weekly_capacity_units: int


def _valid_from(rate: Mapping[str, Any]) -> dt.date:
    """Open start (``NULL``) = valid since ever."""
    return _date(rate.get("valid_from")) or dt.date.min


def _valid_to(rate: Mapping[str, Any]) -> dt.date:
    """Open end (``NULL``) = valid for ever; inclusive (scn/1.0)."""
    return _date(rate.get("valid_to")) or dt.date.max


def _rate_for(rates: Sequence[Mapping[str, Any]], as_of: dt.date) -> Mapping[str, Any] | None:
    """The rate card valid on ``as_of``; else the next one to start; else the last one."""
    valid = [r for r in rates if _valid_from(r) <= as_of <= _valid_to(r)]
    if valid:
        return max(valid, key=_valid_from)
    future = [r for r in rates if _valid_from(r) > as_of]
    if future:
        return min(future, key=_valid_from)
    return rates[-1] if rates else None


def _rates_in_window(
    rates: Sequence[Mapping[str, Any]], start: dt.date, end: dt.date
) -> list[Mapping[str, Any]]:
    """Rate cards valid on any day of ``[start, end]`` (the planning horizon); without one, the
    card :func:`_rate_for` picks for ``start``."""
    hits = [r for r in rates if _valid_from(r) <= end and _valid_to(r) >= start]
    if hits:
        return hits
    one = _rate_for(rates, start)
    return [one] if one is not None else []


def _window_terms(
    rates: Sequence[Mapping[str, Any]], start: dt.date, end: dt.date, planning: PlanningDefaults
) -> tuple[int, int]:
    """``(cost_per_unit_cents, weekly_capacity_units)`` over the horizon: ``InboundLane`` carries
    one value for every week, so the binding terms are taken — the **lowest** weekly capacity and
    the **highest** unit cost of the cards valid in the window (e.g. a peak-season card starting
    mid-horizon). A plan then never books more than the target system accepts in any week."""
    terms = [_unit_terms(r, planning) for r in _rates_in_window(rates, start, end)]
    if not terms:
        return _unit_terms(None, planning)
    return max(t[0] for t in terms), min(t[1] for t in terms)


def _unit_terms(rate: Mapping[str, Any] | None, planning: PlanningDefaults) -> tuple[int, int]:
    """``(cost_per_unit_cents, weekly_capacity_units)`` of a rate card row."""
    if rate is None:
        return 0, UNBOUNDED_CAPACITY
    basis = str(rate.get("basis") or "per_unit")
    cents = _int(rate.get("rate_cents"))
    cap = rate.get("weekly_capacity")
    if basis == "per_teu":
        cost = math.ceil(cents / planning.units_per_teu)
        units = None if cap is None else int(cap) * planning.units_per_teu
    elif basis == "per_kg":
        cost = math.ceil(cents * planning.grams_per_unit / 1000)
        units = None if cap is None else int(cap) * 1000 // planning.grams_per_unit
    else:
        cost = cents
        units = None if cap is None else int(cap)
    return cost, UNBOUNDED_CAPACITY if units is None else units


def compose_inbound_lanes(
    lanes: Sequence[Mapping[str, Any]],
    rates: Sequence[Mapping[str, Any]],
    *,
    dc_codes: set[str],
    as_of: dt.date,
    planning: PlanningDefaults,
    until: dt.date | None = None,
) -> tuple[list[InboundLane], dict[str, tuple[str, ...]]]:
    """Chain ``scn_lane`` legs from an origin (port/factory) to a DC.

    Returns the inbound lanes and, per inbound lane code, its leg codes. Legs that start at a DC
    are transfer lanes (tenant config) and are skipped. Sources are the origins of main-haul legs
    (sea / sea_air / air) and of road legs whose origin no main-haul leg reaches (near-shore
    trucking, river feeders). Single-leg lanes keep the vendor code; chains are ``A+B[+C]``.

    A leg's cost and weekly capacity come from the rate cards valid in ``[as_of, until]`` (the
    planning horizon; ``until`` defaults to ``as_of``): highest cost, lowest capacity
    (:func:`_window_terms`)."""
    rates_by_lane: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rates:
        rates_by_lane[str(r["lane_code"])].append(r)
    end = max(as_of, until or as_of)
    legs: list[_Leg] = []
    for ln in lanes:
        if ln["origin_code"] in dc_codes:
            continue
        cost, cap = _window_terms(rates_by_lane.get(ln["code"], []), as_of, end, planning)
        legs.append(
            _Leg(
                code=str(ln["code"]), origin=str(ln["origin_code"]), dest=str(ln["dest_code"]), mode=str(ln["mode"]),
                transit_days=_int(ln["transit_days"]), via=tuple(str(v) for v in ln.get("via") or ()),
                alt_lane_code=ln.get("alt_lane_code"), cost_per_unit_cents=cost, weekly_capacity_units=cap,
            )
        )
    legs.sort(key=lambda leg: leg.code)
    by_origin: dict[str, list[_Leg]] = defaultdict(list)
    for leg in legs:
        by_origin[leg.origin].append(leg)
    main_dests = {leg.dest for leg in legs if leg.mode != "road"}
    sources = sorted(
        {leg.origin for leg in legs if leg.mode != "road"}
        | {leg.origin for leg in legs if leg.mode == "road" and leg.origin not in main_dests}
    )
    paths: list[tuple[_Leg, ...]] = []

    def walk(node: str, chain: tuple[_Leg, ...], seen: frozenset[str]) -> None:
        for leg in by_origin.get(node, []):
            if leg.dest in seen:
                continue
            nxt = (*chain, leg)
            if leg.dest in dc_codes:
                paths.append(nxt)
            elif len(nxt) < MAX_LEGS:
                walk(leg.dest, nxt, seen | {leg.dest})

    for src in sources:
        walk(src, (), frozenset({src}))

    def mode_of(chain: Sequence[_Leg]) -> str:
        modes = {leg.mode for leg in chain}
        for m in ("sea_air", "air", "sea"):
            if m in modes:
                return m
        return "road"

    built: dict[str, tuple[tuple[_Leg, ...], InboundLane]] = {}
    for chain in paths:
        code = "+".join(leg.code for leg in chain)
        if code in built:
            continue
        via: list[str] = []
        for i, leg in enumerate(chain):
            via.extend(leg.via)
            if i < len(chain) - 1:
                via.append(leg.dest)
        built[code] = (
            chain,
            InboundLane(
                code=code, origin=chain[0].origin, dest_dc=chain[-1].dest, mode=mode_of(chain),  # type: ignore[arg-type]
                transit_days=sum(leg.transit_days for leg in chain),
                cost_per_unit_cents=sum(leg.cost_per_unit_cents for leg in chain),
                weekly_capacity_units=min(leg.weekly_capacity_units for leg in chain),
                via=list(dict.fromkeys(via)),
            ),
        )

    def alt_for(chain: tuple[_Leg, ...], dest_dc: str, own: str) -> str | None:
        alts = [leg.alt_lane_code for leg in chain if leg.alt_lane_code]
        for alt in alts:
            options = sorted(
                (c for c, (ch, il) in built.items() if il.dest_dc == dest_dc and ch[0].code == alt and c != own),
                key=lambda c: (len(built[c][0]), c),
            )
            if options:
                return options[0]
        return None

    out: list[InboundLane] = []
    legs_of: dict[str, tuple[str, ...]] = {}
    for code in sorted(built):
        chain, lane = built[code]
        alt = alt_for(chain, lane.dest_dc, code)
        out.append(lane.model_copy(update={"alt_lane_code": alt}) if alt else lane)
        legs_of[code] = tuple(leg.code for leg in chain)
    return out, legs_of


def _lane_for_line(
    sailing_lane: str | None,
    *,
    dest_dc: str,
    factory_country: str | None,
    transit: int | None,
    lanes: Sequence[InboundLane],
    legs_of: Mapping[str, tuple[str, ...]],
    port_country: Mapping[str, str | None],
) -> InboundLane | None:
    """The PO line's inbound lane: the chain containing its sailing's lane (prefer chains that
    start with it), else the lane from the factory's country whose transit best matches
    ``eta − ex_factory``."""
    to_dc = [ln for ln in lanes if ln.dest_dc == dest_dc]
    if sailing_lane:
        hits = [ln for ln in to_dc if sailing_lane in legs_of.get(ln.code, ())]
        if hits:
            hits.sort(key=lambda ln: (legs_of[ln.code][0] != sailing_lane, len(legs_of[ln.code]), ln.code))
            return hits[0]
    if factory_country and transit is not None:
        cands = [ln for ln in to_dc if port_country.get(ln.origin) == factory_country]
        if cands:
            cands.sort(key=lambda ln: (abs(ln.transit_days - transit), ln.code))
            return cands[0]
    return None


def receipts_for_entity(data: ProblemData, kind: str, code: str) -> set[str]:
    """Open PO-line refs affected by a supplier / component / factory (deterministic BOM walk):
    a supplier's or component's BOM lines name ``(style, factory | every factory)``; a PO line is
    affected when its SKU's style and its factory match. A supplier also affects the lines it
    supplies directly (CMT supplier on the PO)."""
    style_of = {s.code: s.style_code for s in data.skus}
    if kind == "factory":
        return {pl.ref for pl in data.po_lines if pl.factory_code == code}
    uses: set[tuple[str, str | None]] = set()
    for b in data.bom:
        if (kind == "supplier" and b.supplier_code == code) or (kind == "component" and b.component_code == code):
            uses.add((b.style_code, b.factory_code))
    refs: set[str] = set()
    for pl in data.po_lines:
        style = style_of.get(pl.sku)
        if kind == "supplier" and pl.supplier_code == code:
            refs.add(pl.ref)
        elif (style, None) in uses or (style, pl.factory_code) in uses:
            refs.add(pl.ref)
    return refs


# ───────────────────────────── allocation groups ─────────────────────────────


def _group_code(dc: str, cluster: str, sku: str, week: str, status: str) -> str:
    return f"{GROUP_ALLOC_PREFIX}{dc}|{cluster}|{sku}|{week}|{status}"


def parse_allocation_group(code: str) -> dict[str, str] | None:
    """``{dc, cluster, sku, week, status}`` of a synthetic group code (``None`` for a real code)."""
    if not code.startswith(GROUP_ALLOC_PREFIX):
        return None
    parts = code[len(GROUP_ALLOC_PREFIX) :].split("|")
    if len(parts) != 5 or not all(parts):
        raise ValueError(f"malformed allocation group code {code!r}")
    return dict(zip(("dc", "cluster", "sku", "week", "status"), parts, strict=True))


@dataclass(frozen=True)
class AllocationMember:
    code: str
    store_code: str
    qty: int
    version: int


async def expand_allocation_group(
    reader: SqlReader, *, tenant: str, snapshot_id: str, group_code: str
) -> list[AllocationMember]:
    """The store allocations behind a synthetic cluster allocation, from the pinned snapshot."""
    key = parse_allocation_group(group_code)
    if key is None:
        raise ValueError(f"{group_code!r} is not an allocation group")
    rows = await reader.fetch(
        Q_ALLOCATION_MEMBERS,
        {"t": tenant, "s": snapshot_id, "sku": key["sku"], "dc": key["dc"], "cluster": key["cluster"],
         "week": key["week"], "status": key["status"]},
    )
    return [
        AllocationMember(code=str(r["code"]), store_code=str(r["store_code"]), qty=_int(r["qty"]),
                         version=max(1, _int(r["src_version"], 1)))
        for r in rows
    ]


# ───────────────────────────────── builder ─────────────────────────────────


def _transfer_lanes(lanes: Iterable[Mapping[str, Any]], dc_codes: set[str]) -> list[Lane]:
    out: list[Lane] = []
    seen: set[str] = set()
    for lane in lanes:
        if lane.get("from_dc") not in dc_codes or lane.get("to_dc") not in dc_codes or lane["from_dc"] == lane["to_dc"]:
            continue
        model = Lane(
            from_dc=str(lane["from_dc"]), to_dc=str(lane["to_dc"]), transit_days=int(lane["transit_days"]),
            cost_per_unit_cents=int(lane["cost_per_unit_cents"]), max_units_wk=int(lane["max_units_wk"]),
        )
        if model.code not in seen:
            seen.add(model.code)
            out.append(model)
    return out


async def build_problem_from_snapshot(
    tenant: str,
    reader: SqlReader,
    *,
    snapshot: SnapshotRef,
    lanes: Sequence[Mapping[str, Any]],
    as_of: dt.date,
    horizon_weeks: int,
    planning: PlanningDefaults | None = None,
    connector_config: Mapping[str, Any] | None = None,
    geo: GeoCatalog | None = None,
) -> ProblemBundle:
    planning = planning or PlanningDefaults()
    geo = geo or get_geo()
    p = {"t": tenant, "s": snapshot.id}
    weeks = week_range(iso_week_of(as_of), horizon_weeks)
    week_set = set(weeks)
    counts: Counter[str] = Counter()

    # ── locations / network ──
    loc_rows = await reader.fetch(Q_LOCATIONS, p)
    centroid_rows = await reader.fetch(Q_CLUSTER_CENTROIDS, p)
    dc_rows = sorted((r for r in loc_rows if r["kind"] == "dc"), key=lambda r: r["code"])
    dc_codes = {str(r["code"]) for r in dc_rows}
    cluster_rows = sorted(
        (r for r in loc_rows if r["kind"] == "cluster" and r.get("parent_code") in dc_codes), key=lambda r: r["code"]
    )
    dcs = [DC(code=str(r["code"])) for r in dc_rows]
    clusters = [Cluster(code=str(r["code"]), dc_code=str(r["parent_code"])) for r in cluster_rows]
    cluster_codes = {c.code for c in clusters}
    cluster_dc = {c.code: c.dc_code for c in clusters}
    counts["stores"] = sum(_int(r["stores"]) for r in centroid_rows if r["cluster_code"] in cluster_codes)

    # ── items / scope ──
    items = await reader.fetch(Q_ITEMS, p)
    styles = {str(r["code"]): r for r in items if r["kind"] == "style"}
    sku_rows = [r for r in items if r["kind"] == "sku" and r.get("style_code")]
    demand_rows = await reader.fetch(Q_DEMAND, {**p, "weeks": weeks})
    po_rows = await reader.fetch(Q_PO_LINES, {**p, "closed": sorted(CLOSED_LINE_STATUSES)})
    sku_style = {str(r["code"]): str(r["style_code"]) for r in sku_rows}
    styles_in_scope = {str(r["style_code"]) for r in demand_rows if r["cluster_code"] in cluster_codes}
    styles_in_scope |= {sku_style[r["sku_code"]] for r in po_rows if r["sku_code"] in sku_style and r["dest_dc_code"] in dc_codes}

    def price(r: Mapping[str, Any], key: str) -> int:
        value = r.get(key)
        if value is None:
            value = (styles.get(str(r["style_code"])) or {}).get(key)
        return _int(value)

    skus = sorted(
        (
            Sku(code=str(r["code"]), style_code=str(r["style_code"]), retail_price_cents=price(r, "retail_price_cents"),
                unit_cost_cents=price(r, "unit_cost_cents"))
            for r in sku_rows
            if r["style_code"] in styles_in_scope
        ),
        key=lambda s: s.code,
    )
    sku_codes = {s.code for s in skus}
    unit_cost = {s.code: s.unit_cost_cents for s in skus}
    family_of = {code: str((r or {}).get("family") or "") for code, r in styles.items()}
    sizes_by_style: dict[str, list[tuple[str, str]]] = defaultdict(list)
    for r in sorted(sku_rows, key=lambda r: (_int(r.get("size_seq")), str(r["code"]))):
        if r["code"] in sku_codes:
            size = str(r.get("size") or str(r["code"]).rsplit("-", 1)[-1])
            sizes_by_style[str(r["style_code"])].append((str(r["code"]), size))
            family_of.setdefault(str(r["style_code"]), str(r.get("family") or ""))

    # ── demand: style × cluster × week (SQL) → SKU by size curve ──
    curves: dict[tuple[str, str], dict[str, int]] = defaultdict(dict)
    for r in await reader.fetch(Q_SIZE_CURVES, p):
        curves[(str(r["family"]), str(r["cluster_code"]))][str(r["size"])] = _int(r["share_bp"])
    demand: list[DemandRow] = []
    for r in sorted(demand_rows, key=lambda r: (r["cluster_code"], r["style_code"], r["iso_week"])):
        if r["cluster_code"] not in cluster_codes or r["iso_week"] not in week_set:
            continue
        style = str(r["style_code"])
        curve = curves.get((family_of.get(style, ""), str(r["cluster_code"]))) or curves.get(("", str(r["cluster_code"])), {})
        shares = [(code, curve.get(size, 0)) for code, size in sizes_by_style.get(style, [])]
        for sku, units in sorted(_split_units(_int(r["units"]), shares).items()):
            if units > 0:
                demand.append(DemandRow(cluster_code=str(r["cluster_code"]), sku=sku, iso_week=str(r["iso_week"]), units=units))
    counts["forecast_rows"] = len(demand_rows)

    # ── inventory ──
    inv_dc = await reader.fetch(Q_INVENTORY_DC, p)
    inventory = [
        InventoryPosition(
            location_type="dc", location_code=str(r["location_code"]), sku=str(r["sku_code"]), on_hand=_int(r["on_hand"]),
            allocated=_int(r["allocated"]), reserved=_int(r["reserved"]), in_transit=_int(r["in_transit"]),
        )
        for r in sorted(inv_dc, key=lambda r: (r["location_code"], r["sku_code"]))
        if r["location_code"] in dc_codes and r["sku_code"] in sku_codes
    ]
    inv_cl = await reader.fetch(Q_INVENTORY_CLUSTER, p)
    cluster_inventory = [
        ClusterInventory(cluster_code=str(r["cluster_code"]), sku=str(r["sku_code"]), on_hand=_int(r["atp"]))
        for r in sorted(inv_cl, key=lambda r: (r["cluster_code"], r["sku_code"]))
        if r["cluster_code"] in cluster_codes and r["sku_code"] in sku_codes and _int(r["atp"]) > 0
    ]
    counts["inventory_positions"] = len(inv_dc) + sum(_int(r["positions"]) for r in inv_cl)

    # ── inbound lanes ──
    lane_rows = await reader.fetch(Q_LANES, p)
    rate_rows = await reader.fetch(Q_RATES, p)
    inbound, legs_of = compose_inbound_lanes(
        lane_rows, rate_rows, dc_codes=dc_codes, as_of=monday_of(weeks[0]), planning=planning,
        until=monday_of(weeks[-1]) + dt.timedelta(days=6),
    )
    loc_country = {(str(r["kind"]), str(r["code"])): r.get("country") for r in loc_rows}
    port_country = {code: country for (kind, code), country in loc_country.items() if kind in ("port", "airport")}
    factory_country = {code: country for (kind, code), country in loc_country.items() if kind == "factory"}
    for (kind, code), country in loc_country.items():  # a lane may start at a factory itself
        if kind == "factory":
            port_country.setdefault(code, country)

    # ── PO lines / receipts ──
    receipts: list[Receipt] = []
    po_lines: list[PoLine] = []
    po_dicts: list[dict[str, Any]] = []
    skipped = 0
    for r in po_rows:
        dest = str(r["dest_dc_code"])
        sku = str(r["sku_code"])
        eta = _date(r.get("eta_dc"))
        if dest not in dc_codes or sku not in sku_codes or eta is None:
            skipped += 1
            continue
        po_code, line_no = str(r["po_code"]), _int(r["line_no"])
        ref = str(r["source_id"]) if str(r["source_id"]) == receipt_ref(po_code, line_no) else receipt_ref(po_code, line_no)
        qty = _int(r["qty_ordered"]) - _int(r["qty_received"])
        factory = str(r.get("factory_code") or r["supplier_code"])
        ex_factory = _date(r.get("ex_factory_date"))
        lane = _lane_for_line(
            r.get("sailing_lane"), dest_dc=dest, factory_country=factory_country.get(factory),
            transit=(eta - ex_factory).days if ex_factory else None, lanes=inbound, legs_of=legs_of,
            port_country=port_country,
        )
        if ex_factory is None:
            ex_factory = eta - dt.timedelta(days=lane.transit_days if lane else 0)
        receipts.append(Receipt(ref=ref, dc_code=dest, sku=sku, qty=qty, eta=eta))
        po_lines.append(
            PoLine(
                ref=ref, po_code=po_code, supplier_code=str(r["supplier_code"]), factory_code=factory, dest_dc=dest,
                sku=sku, qty=qty, eta=eta, ex_factory=ex_factory, mode=(lane.mode if lane else "sea"),  # type: ignore[arg-type]
                lane_code=lane.code if lane else None,
            )
        )
        po_dicts.append(
            {"po_code": po_code, "line_no": line_no, "sku": sku, "qty": qty, "eta_dc": eta.isoformat(), "dest_dc": dest,
             "supplier_code": str(r["supplier_code"]), "factory_code": factory, "version": _int(r.get("src_version"), 1)}
        )
    counts["po_lines_skipped"] = skipped

    # ── BOM ──
    bom_rows = await reader.fetch(Q_BOM, p)
    bom = [
        BomLine(style_code=str(r["style_code"]), component_code=str(r["component_code"]),
                supplier_code=str(r["supplier_code"]), factory_code=r.get("factory_code"))
        for r in bom_rows
        if r["style_code"] in styles_in_scope
    ]

    # ── production ──
    prod_rows = await reader.fetch(Q_PRODUCTION, p)
    by_line: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in prod_rows:
        by_line[str(r["line_code"])].append(r)
    production: list[ProductionLine] = []
    factory_daily: Counter[str] = Counter()
    def describable(r: Mapping[str, Any]) -> bool:
        ref = r.get("po_line_ref")
        return r.get("sku_code") in sku_codes and isinstance(ref, str) and _PO_LINE_REF.match(ref) is not None

    for line_code in sorted(by_line):
        rows = by_line[line_code]
        if any(not describable(r) and r["status"] in MOVABLE_ORDER_STATUSES for r in rows):
            continue  # a movable order we cannot describe: the line cannot be resequenced safely
        orders: list[ProductionOrder] = []
        rate = 0
        for r in rows:
            if not describable(r):
                continue
            start, end = _date(r["planned_start"]), _date(r["planned_end"])
            if start is None or end is None or end < start:
                continue
            qty = _int(r.get("qty_ordered"))
            rate = max(rate, math.ceil(qty / ((end - start).days + 1)))
            orders.append(
                ProductionOrder(code=str(r["code"]), po_line_ref=str(r["po_line_ref"]), sku=str(r["sku_code"]), qty=qty,
                                planned_start=start, planned_end=end, seq=_int(r["seq"]),
                                movable=r["status"] in MOVABLE_ORDER_STATUSES)
            )
        if not any(o.movable for o in orders):
            continue
        factories = Counter(str(r.get("factory_code") or r.get("supplier_code") or "") for r in rows)
        factory = sorted(factories.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
        factory_daily[factory] += rate
        production.append(ProductionLine(line_code=line_code, factory_code=factory, daily_capacity_units=rate, orders=orders))

    # ── locations (map) ──
    locations: list[Location] = []
    parent_supplier: dict[str, str] = {}
    centroids = {str(r["cluster_code"]): _point(r["lat"], r["lon"]) for r in centroid_rows}
    dc_point: dict[str, tuple[int, int]] = {}
    for r in loc_rows:
        kind, code = str(r["kind"]), str(r["code"])
        if kind == "factory" and r.get("parent_code"):
            parent_supplier[code] = str(r["parent_code"])
        pt = _point(r.get("lat"), r.get("lon"))
        if kind == "cluster":
            pt = centroids.get(code)
        if pt is None and kind in ("port", "airport"):
            pt = geo.place(code)
        if pt is None:
            pt = geo.country_point(r.get("country"), key=f"{kind}:{code}")
        if kind == "dc" and pt is not None:
            dc_point[code] = pt
        if kind == "cluster" and pt is None:
            pt = dc_point.get(str(r.get("parent_code")))
        if pt is None or (kind == "cluster" and code not in cluster_codes):
            continue
        locations.append(
            Location(code=code, kind="port" if kind == "airport" else kind,  # type: ignore[arg-type]
                     name=str(r.get("name") or code), lon_e6=pt[0], lat_e6=pt[1],
                     parent=str(r["parent_code"]) if r.get("parent_code") and kind in ("cluster", "factory") else None)
        )
    unique_locations: dict[tuple[str, str], Location] = {}
    for loc in locations:  # an airport and a port may share a code: the first one wins
        unique_locations.setdefault((loc.kind, loc.code), loc)
    locations = list(unique_locations.values())

    # ── alternate sources ──
    alt_sources = _alt_sources(
        connector_config or {}, planning=planning, bom=bom, po_lines=po_lines, styles_in_scope=styles_in_scope,
        sku_codes=sku_codes, unit_cost=unit_cost, sku_style=sku_style, inbound=inbound, port_country=port_country,
        factory_country=factory_country, parent_supplier=parent_supplier, factory_daily=factory_daily,
    )

    # ── allocations ──
    alloc_rows = await reader.fetch(
        Q_ALLOCATIONS, {**p, "statuses": list(OPEN_ALLOCATION_STATUSES), "w0": weeks[0]}
    )
    allocations: list[Allocation] = []
    for r in sorted(alloc_rows, key=lambda r: (r["dc_code"], r["cluster_code"], r["sku_code"], r["ship_week"], r["status"])):
        dc, cluster, sku = str(r["dc_code"]), str(r["cluster_code"]), str(r["sku_code"])
        if dc not in dc_codes or cluster not in cluster_codes or sku not in sku_codes:
            continue
        n = _int(r["n"])
        code = str(r["code"]) if n == 1 else _group_code(dc, cluster, sku, str(r["ship_week"]), str(r["status"]))
        allocations.append(
            Allocation(code=code, sku=sku, dc_code=dc, cluster_code=cluster, ship_week=str(r["ship_week"]),
                       qty=_int(r["qty"]), protected=r["status"] == "released", version=max(1, _int(r["version"], 1)))
        )
    counts["allocation_rows"] = sum(_int(r["n"]) for r in alloc_rows)

    data = ProblemData(
        tenant=tenant,
        as_of=as_of,
        horizon=Horizon(start_week=weeks[0], weeks=horizon_weeks),
        dcs=dcs,
        clusters=clusters,
        skus=skus,
        inventory=inventory,
        cluster_inventory=cluster_inventory,
        receipts=receipts,
        demand=demand,
        lanes=_transfer_lanes(lanes, dc_codes),
        protected_min=[],
        locations=sorted(locations, key=lambda loc: (loc.kind, loc.code)),
        po_lines=po_lines,
        bom=bom,
        inbound_lanes=inbound,
        alt_sources=alt_sources,
        production=production,
        allocations=allocations,
    )
    return ProblemBundle(
        data=data,
        evidence_ids=[],
        counts={
            "dcs": len(dcs),
            "clusters": len(clusters),
            "skus": len(skus),
            "purchase_orders": len({pl.po_code for pl in po_lines}),
            "po_lines": len(po_lines),
            "inventory_positions": counts["inventory_positions"],
            "forecast_rows": counts["forecast_rows"],
            "stores": counts["stores"],
            "allocations": len(allocations),
            "allocation_rows": counts["allocation_rows"],
            "inbound_lanes": len(inbound),
            "alt_sources": len(alt_sources),
            "production_lines": len(production),
            "po_lines_skipped": counts["po_lines_skipped"],
            "connector_calls": 0,
        },
        po_lines=po_dicts,
        source="scn",
        snapshot_id=snapshot.id,
        snapshot_as_of=snapshot.as_of,
        snapshot_label=snapshot.label,
    )


def _alt_sources(
    config: Mapping[str, Any],
    *,
    planning: PlanningDefaults,
    bom: Sequence[BomLine],
    po_lines: Sequence[PoLine],
    styles_in_scope: set[str],
    sku_codes: set[str],
    unit_cost: Mapping[str, int],
    sku_style: Mapping[str, str],
    inbound: Sequence[InboundLane],
    port_country: Mapping[str, str | None],
    factory_country: Mapping[str, str | None],
    parent_supplier: Mapping[str, str],
    factory_daily: Mapping[str, int],
) -> list[AltSource]:
    lane_codes = {ln.code for ln in inbound}
    explicit = config.get("alt_sources")
    if explicit:
        out: list[AltSource] = []
        for raw in explicit:
            src = AltSource.model_validate(raw)
            styles = [s for s in src.style_codes if s in styles_in_scope]
            if src.lane_code in lane_codes and styles:
                out.append(src.model_copy(update={"style_codes": styles}))
        return out
    cfg = planning.alt_sourcing
    incumbents: dict[str, set[str]] = defaultdict(set)
    for pl in po_lines:
        incumbents[sku_style.get(pl.sku, "")].add(pl.factory_code)
    makes: dict[str, set[str]] = defaultdict(set)
    for b in bom:
        if b.factory_code is not None and b.style_code in styles_in_scope:
            makes[b.factory_code].add(b.style_code)
    style_cost: dict[str, int] = defaultdict(int)
    for sku, style in sku_style.items():
        if sku in sku_codes:
            style_cost[style] = max(style_cost[style], unit_cost.get(sku, 0))
    out = []
    for factory in sorted(makes):
        styles = sorted(s for s in makes[factory] if factory not in incumbents.get(s, set()))
        supplier = parent_supplier.get(factory)
        if not styles or not supplier:
            continue
        country = factory_country.get(factory)
        lanes = [ln for ln in inbound if ln.origin == factory or (country and port_country.get(ln.origin) == country)]
        weekly = factory_daily.get(factory, 0) * 6 or cfg.weekly_capacity_units
        cost = math.ceil(max(style_cost[s] for s in styles) * (10_000 + cfg.unit_cost_premium_bp) / 10_000)
        for ln in sorted(lanes, key=lambda ln: ln.code):
            out.append(
                AltSource(
                    supplier_code=supplier, factory_code=factory, style_codes=styles,
                    lead_time_days=cfg.production_lead_time_days + ln.transit_days, unit_cost_cents=cost,
                    moq_units=cfg.moq_units, weekly_capacity_units=weekly, approved=True,
                    compliance_ok=factory not in cfg.blocked_factories, lane_code=ln.code,
                )
            )
    return out
