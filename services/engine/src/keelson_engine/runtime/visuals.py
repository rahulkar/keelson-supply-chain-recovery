"""Payload builders for the M3 canvas visuals and the outcome KPIs (``80-m3-deterministic-core.md`` §4–§5).

Pure functions over the plan options (as saved by ``simulate``), the confirmed scenario, the
ProblemData and the impact response — no I/O, deterministic, so they are unit-tested directly:

* :func:`option_scorecard` — the optimizer's ``scorecard`` or, for an optimizer without one, a
  scorecard recomputed from the option's KPIs (never from an objective value);
* :func:`scenario_compare_props` — every option with levers, scorecard, recommended / guided-only
  markers and Pareto flags on (incremental cost, lost units);
* :func:`network_map` — nodes (DCs, clusters at their stores' centroid, ports, chokepoints,
  factories, affected suppliers) and links (inbound lanes along the catalogued sea routes, factory
  feeders, DC transfer lanes, plus each option's transfers / sea-air / near-shore moves);
* :func:`impact_graph` — the optimizer's grouped graph (≤ 300 nodes) with its summary;
* :func:`stockout_timeline` — top 40 (cluster, style) rows by shortage with per-option first
  breach weeks (``by_option``);
* :func:`outcome_kpi_items` — the exposure strip extended with the recommended option's effect.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from keelson_contracts.blocks import (
    MAX_TIMELINE_ROWS,
    CompareOption,
    Footprint,
    ImpactGraphProps,
    ImpactGraphSummary,
    KpiItem,
    MapLegendItem,
    MapLink,
    MapNode,
    MapSummary,
    NetworkMapData,
    NetworkMapProps,
    ScenarioCompareProps,
    StockoutRow,
    StockoutTimelineData,
    StockoutTimelineProps,
    TimelineCell,
    TimelineOption,
)
from keelson_contracts.common import Provenance, monday_of, week_range
from keelson_contracts.optimizer import (
    CostBreakdown,
    ImpactGraph,
    ImpactKpis,
    InboundLane,
    ProblemData,
    Scorecard,
)

from .geo import GeoCatalog, Point, bbox_of
from .problem_scn import receipts_for_entity

__all__ = [
    "LINK_LABELS",
    "STATUS_LABELS",
    "affected_receipts",
    "impact_graph",
    "network_map",
    "option_scorecard",
    "outcome_kpi_items",
    "pareto_flags",
    "scenario_compare_props",
    "stockout_timeline",
]

STATUS_LABELS = {"disrupted": "Disrupted", "impacted": "Impacted", "at_risk": "At risk", "ok": "Operating normally"}
LINK_LABELS = {"inbound": "Inbound lane", "transfer": "DC transfer", "sea_air": "Sea-air", "nearshore": "Near-shore"}
_STATUS_RANK = {"ok": 0, "at_risk": 1, "impacted": 2, "disrupted": 3}


def _worse(a: str, b: str) -> str:
    return a if _STATUS_RANK[a] >= _STATUS_RANK[b] else b


# ─────────────────────────────── scorecards ───────────────────────────────


def _earliest(kpis: Mapping[str, Any]) -> dt.date | None:
    dates = [dt.date.fromisoformat(str(b["date"])) for b in kpis.get("first_breach") or []]
    return min(dates) if dates else None


def option_scorecard(option: Mapping[str, Any], *, total_demand: int, horizon_end: dt.date) -> Scorecard:
    """The optimizer's scorecard, or one recomputed from the option's KPIs (do-nothing: no cost)."""
    if option.get("scorecard"):
        return Scorecard.model_validate(option["scorecard"])
    before = option.get("kpis_before") or {}
    after = option.get("kpis_after") or {}
    delta = option.get("kpi_delta") or {}
    lost = int(after.get("units_at_risk") or 0)
    # served / demand, floored like the optimizer's ``fill_rate_bp``
    fill = 10_000 if total_demand <= 0 else max(0, min(10_000, (total_demand - lost) * 10_000 // total_demand))
    transfer = int(delta.get("transfer_cost_cents") or 0)
    # the optimizer's rule: a missing breach counts as the day after the horizon (``horizon_end``),
    # so a breach the plan removes moves "to the end" and one it introduces moves earlier
    first_before, first_after = _earliest(before), _earliest(after)
    shift = ((first_after or horizon_end) - (first_before or horizon_end)).days
    return Scorecard(
        fill_rate_bp=fill,
        lost_units=lost,
        lost_margin_cents=int(after.get("margin_at_risk_cents") or 0),
        incremental_cost_cents=transfer,
        cost_breakdown=CostBreakdown(transfer=transfer, expedite_premium=0, nearshore_premium=0),
        net_benefit_cents=int(delta.get("net_benefit_cents") or 0),
        shortage_days=7 * int(after.get("shortage_cluster_weeks") or 0),
        first_breach_shift_days=shift,
        tts_gain_days=0,
        changes=len(option.get("actions") or []),
    )


def pareto_flags(options: Sequence[Mapping[str, Any]], *, x: str = "incremental_cost_cents",
                 y: str = "lost_units") -> dict[str, bool]:
    """Non-dominated options when minimising both ``x`` and ``y`` of their scorecards."""
    points = {str(o["id"]): (int(o["scorecard"][x]), int(o["scorecard"][y])) for o in options}
    flags: dict[str, bool] = {}
    for oid, (px, py) in points.items():
        dominated = any(
            qx <= px and qy <= py and (qx < px or qy < py) for qid, (qx, qy) in points.items() if qid != oid
        )
        flags[oid] = not dominated
    return flags


def scenario_compare_props(options: Sequence[Mapping[str, Any]], *, recommended_id: str | None) -> ScenarioCompareProps:
    pareto = pareto_flags(options)
    return ScenarioCompareProps(
        options=[
            CompareOption(
                id=str(o["id"]),
                title=str(o["title"]),
                levers=list(o.get("levers") or []),
                scorecard=Scorecard.model_validate(o["scorecard"]),
                recommended=str(o["id"]) == recommended_id,
                guided_only=bool(o.get("guided_only")),
                guided_reason=o.get("guided_reason") if o.get("guided_only") else None,
                pareto=pareto[str(o["id"])],
            )
            for o in options
        ]
    )


# ─────────────────────────────── scenario targets ───────────────────────────────


def _targets(scenario: Any) -> dict[str, set[str]]:
    """Target ids by kind over every perturbation of the scenario."""
    out: dict[str, set[str]] = defaultdict(set)
    for p in getattr(scenario, "perturbations", None) or []:
        target = getattr(p, "target", None)
        if target is None:
            continue
        for kind in target.kinds:
            out[str(kind)].update(str(i) for i in target.ids)
    return out


def affected_receipts(problem: ProblemData, scenario: Any) -> set[str]:
    """PO-line refs shifted by the scenario's lead-time perturbations (receipt / po_line / po and
    the BOM-resolved supplier / component / factory targets)."""
    refs: set[str] = set()
    for p in getattr(scenario, "perturbations", None) or []:
        if getattr(p, "op", None) != "lead_time_shift":
            continue
        kinds, ids = set(p.target.kinds), set(p.target.ids)
        if kinds & {"receipt", "po_line"}:
            refs |= ids
        if "po" in kinds:
            refs |= {pl.ref for pl in problem.po_lines if pl.po_code in ids} | {
                r.ref for r in problem.receipts if r.ref.split("#", 1)[0] in ids
            }
        for kind in ("supplier", "component", "factory"):
            if kind in kinds:
                for code in ids:
                    refs |= receipts_for_entity(problem, kind, code)
    return refs


# ─────────────────────────────── network map ───────────────────────────────


def _locate(problem: ProblemData, geo: GeoCatalog) -> dict[tuple[str, str], tuple[str, Point]]:
    """``(kind, code) → (name, position)`` from the ProblemData locations (+ catalogue places)."""
    out: dict[tuple[str, str], tuple[str, Point]] = {}
    for loc in problem.locations:
        out[(loc.kind, loc.code)] = (loc.name, (loc.lon_e6, loc.lat_e6))
    for code, (name, kind, point) in geo.places.items():
        out.setdefault((kind, code), (name, point))
    return out


def _lane_stops(lane: InboundLane, where: Mapping[tuple[str, str], tuple[str, Point]],
                geo: GeoCatalog) -> list[tuple[str, Point, bool]] | None:
    """``(code, position, is_port)`` from the lane origin through its via ports to the DC."""
    stops: list[tuple[str, Point, bool]] = []
    origin = where.get(("port", lane.origin)) or where.get(("factory", lane.origin))
    if origin is None:
        return None
    stops.append((lane.origin, origin[1], ("port", lane.origin) in where))
    for v in lane.via:
        hit = where.get(("port", v))
        if hit is not None:
            stops.append((v, hit[1], True))
    dest = where.get(("dc", lane.dest_dc))
    if dest is None:
        return None
    stops.append((lane.dest_dc, dest[1], False))
    return stops


def network_map(
    *,
    problem: ProblemData,
    scenario: Any,
    impact: Mapping[str, Any],
    options: Sequence[Mapping[str, Any]],
    geo: GeoCatalog,
    title: str,
) -> tuple[NetworkMapProps, NetworkMapData]:
    where = _locate(problem, geo)
    targets = _targets(scenario)
    shifted = affected_receipts(problem, scenario)
    closed = set(targets.get("port", set())) | set(targets.get("chokepoint", set()))
    closed_lanes = set(targets.get("lane", set()))
    nodes: dict[str, MapNode] = {}

    def add_node(kind: str, code: str, status: str = "ok", units: int | None = None) -> str | None:
        hit = where.get((kind, code))
        if hit is None:
            return None
        node_id = f"{kind}:{code}"
        prev = nodes.get(node_id)
        if prev is not None:
            status = _worse(prev.status, status)
            units = units if units is not None else prev.units_at_risk
        nodes[node_id] = MapNode(id=node_id, kind=kind, label=hit[0], lat_e6=hit[1][1], lon_e6=hit[1][0],  # type: ignore[arg-type]
                                 status=status, units_at_risk=units)  # type: ignore[arg-type]
        return node_id

    dc_risk = {str(r["dc_code"]): int(r["units_at_risk"]) for r in impact.get("by_dc") or []}
    for dc in problem.dcs:
        risk = dc_risk.get(dc.code, 0)
        add_node("dc", dc.code, "at_risk" if risk > 0 else "ok", risk)
    cluster_short: dict[str, int] = defaultdict(int)
    for pt in impact.get("timeline") or []:
        cluster_short[str(pt["cluster_code"])] += int(pt.get("shortage") or 0)
    for cl in problem.clusters:
        short = cluster_short.get(cl.code, 0)
        add_node("cluster", cl.code, "at_risk" if short > 0 else "ok", short)

    links: dict[str, MapLink] = {}
    lanes = {ln.code: ln for ln in problem.inbound_lanes}
    lane_units: dict[str, int] = defaultdict(int)
    lane_hit: dict[str, bool] = defaultdict(bool)
    feeders: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for pl in problem.po_lines:
        if pl.lane_code in lanes:
            lane_units[pl.lane_code] += pl.qty
            lane_hit[pl.lane_code] |= pl.ref in shifted
            origin = lanes[pl.lane_code].origin
            if origin != pl.factory_code:
                acc = feeders[(pl.factory_code, origin)]
                acc[0] += pl.qty
                acc[1] += int(pl.ref in shifted)
        add_node("factory", pl.factory_code, "impacted" if pl.ref in shifted else "ok")
    for kind in ("supplier", "factory"):
        for code in sorted(targets.get(kind, set())):
            add_node(kind, code, "disrupted")
    for code in sorted(closed):
        add_node("port", code, "disrupted") or add_node("chokepoint", code, "disrupted")

    def sea_air_split(lane: InboundLane) -> tuple[list[Point], str, list[Point]] | None:
        """M4 (90 §10): a sea-air lane as its sea leg into the hub + the air arc to the DC."""
        if lane.mode != "sea_air":
            return None
        stops = _lane_stops(lane, where, geo)
        if stops is None:
            return None
        legs = geo.sea_air_legs(stops, lane.via)
        if legs is None:
            return None
        sea, hub, air = legs
        hub_id = add_node("port", hub)
        if hub_id is None:
            return None
        return sea, hub_id, air

    def add_lane_links(link_id: str, lane: InboundLane, src: str, dst: str, path: list[Point], *, kind: str,
                       status: str, units: int | None, option_id: str | None) -> None:
        """One link per lane; a sea-air lane becomes a sea leg (``<id>:sea``, an inbound path into the
        hub) plus the sea-air link itself from the hub to the DC along the air arc."""
        split = sea_air_split(lane) if kind == "sea_air" else None
        if split is None:
            links[link_id] = MapLink(id=link_id, kind=kind, from_=src, to=dst, path=path, status=status,  # type: ignore[arg-type]
                                     units=units, option_id=option_id)
            return
        sea, hub_id, air = split
        links[f"{link_id}:sea"] = MapLink(id=f"{link_id}:sea", kind="inbound", from_=src, to=hub_id, path=sea,
                                          status=status, units=None, option_id=option_id)  # type: ignore[arg-type]
        links[link_id] = MapLink(id=link_id, kind="sea_air", from_=hub_id, to=dst, path=air, status=status,  # type: ignore[arg-type]
                                 units=units, option_id=option_id)

    def lane_path(lane: InboundLane) -> tuple[list[Point], str, str] | None:
        stops = _lane_stops(lane, where, geo)
        if stops is None:
            return None
        origin_kind = "port" if stops[0][2] else "factory"
        src = add_node(origin_kind, lane.origin)
        for code, _, is_port in stops[1:-1]:
            if is_port:
                add_node("port", code)
        for v in lane.via:
            if geo.place_kind(v) == "chokepoint":
                add_node("chokepoint", v, "disrupted" if v in closed else "ok")
        dst = f"dc:{lane.dest_dc}"
        if src is None or dst not in nodes:
            return None
        return geo.stops_path(stops, mode=lane.mode, via=lane.via), src, dst

    for code in sorted(lane_units):
        lane = lanes[code]
        built = lane_path(lane)
        if built is None:
            continue
        path, src, dst = built
        # a lane target names a vendor lane: the inbound lane itself or one of its legs (``A+B``)
        disrupted = bool(closed_lanes & {code, *code.split("+")}) or bool(closed & {lane.origin, *lane.via})
        status = "disrupted" if disrupted else ("impacted" if lane_hit[code] else "ok")
        add_lane_links(f"lane:{code}", lane, src, dst, path,
                       kind="sea_air" if lane.mode in ("sea_air", "air") else "inbound",
                       status=status, units=lane_units[code], option_id=None)
        if disrupted:
            for v in (lane.origin, *lane.via):
                if v in closed:
                    add_node("port", v, "disrupted") or add_node("chokepoint", v, "disrupted")
    for (factory, origin), (units, hits) in sorted(feeders.items()):
        src, dst = f"factory:{factory}", f"port:{origin}"
        if src in nodes and dst in nodes:
            links[f"feed:{factory}>{origin}"] = MapLink(
                id=f"feed:{factory}>{origin}", kind="inbound", from_=src, to=dst,
                path=[(nodes[src].lon_e6, nodes[src].lat_e6), (nodes[dst].lon_e6, nodes[dst].lat_e6)],
                status="impacted" if hits else "ok", units=units,
            )
    for ln in problem.lanes:
        a, b = f"dc:{ln.from_dc}", f"dc:{ln.to_dc}"
        if a in nodes and b in nodes:
            links[f"transfer:{ln.code}"] = MapLink(
                id=f"transfer:{ln.code}", kind="transfer", from_=a, to=b,
                path=[(nodes[a].lon_e6, nodes[a].lat_e6), (nodes[b].lon_e6, nodes[b].lat_e6)], status="ok",
            )

    for opt in options:
        oid = str(opt["id"])
        transfers: dict[tuple[str, str], int] = defaultdict(int)
        expedites: dict[str, int] = defaultdict(int)
        nearshore: dict[tuple[str, str], int] = defaultdict(int)
        for d in opt.get("decisions") or []:
            lever = d.get("lever")
            if lever == "reallocate_inventory":
                transfers[(str(d["from_dc"]), str(d["to_dc"]))] += int(d["qty"])
            elif lever == "expedite_mode_shift":
                expedites[str(d["lane_code"])] += int(d["qty"])
            elif lever == "resource_alt_supplier":
                nearshore[(str(d["factory_code"]), str(d["dest_dc"]))] += int(d["qty"])
        for (a_dc, b_dc), units in sorted(transfers.items()):
            a, b = f"dc:{a_dc}", f"dc:{b_dc}"
            if a in nodes and b in nodes:
                link_id = f"{oid}:transfer:{a_dc}>{b_dc}"
                links[link_id] = MapLink(
                    id=link_id, kind="transfer", from_=a, to=b, status="ok", units=units, option_id=oid,
                    path=[(nodes[a].lon_e6, nodes[a].lat_e6), (nodes[b].lon_e6, nodes[b].lat_e6)],
                )
        for code, units in sorted(expedites.items()):
            lane = lanes.get(code)
            built = lane_path(lane) if lane is not None else None
            if built is None:
                continue
            path, src, dst = built
            add_lane_links(f"{oid}:sea_air:{code}", lane, src, dst, path, kind="sea_air", status="ok",  # type: ignore[arg-type]
                           units=units, option_id=oid)
        for (factory, dest), units in sorted(nearshore.items()):
            factory_id = add_node("factory", factory)
            dst = f"dc:{dest}"
            if factory_id is None or dst not in nodes:
                continue
            lane = next(
                (lanes[s.lane_code] for s in problem.alt_sources
                 if s.factory_code == factory and s.lane_code in lanes and lanes[s.lane_code].dest_dc == dest),
                None,
            )
            path = [(nodes[factory_id].lon_e6, nodes[factory_id].lat_e6)]
            if lane is not None and (lane_stops := _lane_stops(lane, where, geo)) is not None:
                path += geo.stops_path(lane_stops, mode=lane.mode, via=lane.via)
            else:
                path.append((nodes[dst].lon_e6, nodes[dst].lat_e6))
            link_id = f"{oid}:nearshore:{factory}>{dest}"
            links[link_id] = MapLink(id=link_id, kind="nearshore", from_=factory_id, to=dst, path=_squash(path),
                                     status="ok", units=units, option_id=oid)

    footprint = _chokepoint_footprint(sorted(targets.get("chokepoint", set())), geo)
    node_list = sorted(nodes.values(), key=lambda n: n.id)
    link_list = sorted(links.values(), key=lambda ln: ln.id)
    points: list[Point] = [(n.lon_e6, n.lat_e6) for n in node_list]
    for ln in link_list:
        points.extend((p[0], p[1]) for p in ln.path)
    statuses = sorted({n.status for n in node_list} | {ln.status for ln in link_list}, key=lambda s: -_STATUS_RANK[s])
    legend = [MapLegendItem(key=s, label=STATUS_LABELS[s], kind="node") for s in statuses]
    legend += [MapLegendItem(key=k, label=LINK_LABELS[k], kind="link") for k in LINK_LABELS if any(ln.kind == k for ln in link_list)]
    if footprint is not None:
        legend.append(MapLegendItem(key="closed_chokepoint", label="Closed chokepoint", kind="footprint"))
        for feature in footprint.features:
            points.extend((p[0], p[1]) for ring in feature.geometry.coordinates for p in ring)  # type: ignore[union-attr]
    props = NetworkMapProps(
        title=title,
        summary=MapSummary(nodes=len(node_list), at_risk_nodes=sum(1 for n in node_list if n.status != "ok"),
                           lanes=len(link_list)),
        bbox=bbox_of(points),
        legend=legend,
    )
    return props, NetworkMapData(nodes=node_list, links=link_list, footprint=footprint)


def _chokepoint_footprint(codes: Sequence[str], geo: GeoCatalog) -> Footprint | None:
    """M4: the polygons of the chokepoints a capacity perturbation targets (``chokepoints.json``)."""
    features = []
    for code in codes:
        cp = geo.chokepoints.get(code)
        if cp is None or len(cp.polygon) < 4:
            continue
        features.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [[list(p) for p in cp.polygon]]},
                         "properties": {"label": cp.name, "status": "disrupted"}})
    return Footprint.model_validate({"type": "FeatureCollection", "features": features}) if features else None


def _squash(points: Iterable[Point]) -> list[Point]:
    out: list[Point] = []
    for p in points:
        if not out or out[-1] != p:
            out.append(p)
    if len(out) == 1:
        out.append(out[0])
    return out


# ─────────────────────────────── impact graph ───────────────────────────────


def impact_graph(impact: Mapping[str, Any]) -> tuple[ImpactGraphProps, ImpactGraph] | None:
    raw = impact.get("graph")
    if not raw:
        return None
    graph = ImpactGraph.model_validate(raw)
    summary = ImpactGraphSummary(
        nodes=len(graph.nodes),
        disrupted=sum(1 for n in graph.nodes if n.status == "disrupted"),
        impacted=sum(1 for n in graph.nodes if n.status == "impacted"),
    )
    return ImpactGraphProps(summary=summary), graph


# ─────────────────────────────── stockout timeline ───────────────────────────────


def stockout_timeline(
    *, problem: ProblemData, impact: Mapping[str, Any], options: Sequence[Mapping[str, Any]],
) -> tuple[StockoutTimelineProps, StockoutTimelineData] | None:
    """Top rows by shortage; ``None`` when nothing runs short or tight."""
    weeks = week_range(problem.horizon.start_week, problem.horizon.weeks)
    cluster_dc = {c.code: c.dc_code for c in problem.clusters}
    points: dict[tuple[str, str], dict[str, Mapping[str, Any]]] = defaultdict(dict)
    for pt in impact.get("timeline") or []:
        points[(str(pt["cluster_code"]), str(pt["style_code"]))][str(pt["week"])] = pt
    breaches: dict[str, dict[tuple[str, str, str], str]] = {}
    for opt in options:
        table: dict[tuple[str, str, str], str] = {}
        for b in (opt.get("kpis_after") or {}).get("first_breach") or []:
            key = (str(b["dc_code"]), str(b["cluster_code"]), str(b["style_code"]))
            table[key] = min(table.get(key, str(b["week"])), str(b["week"]))
        breaches[str(opt["id"])] = table
    ranked: list[tuple[int, str, StockoutRow]] = []
    for (cluster, style), by_week in points.items():
        dc = cluster_dc.get(cluster)
        if dc is None:
            continue
        cells: list[TimelineCell] = []
        total = 0
        tight = False
        first: str | None = None
        for week in weeks:
            pt = by_week.get(week)
            short = int(pt.get("shortage") or 0) if pt else 0
            pab = int(pt.get("projected_available") or 0) if pt else 0
            demand = int(pt.get("demand") or 0) if pt else 0
            if short > 0:
                status = "short"
                first = first or week
            elif demand > 0 and pab < demand:
                status, tight = "tight", True
            else:
                status = "ok"
            total += short
            cells.append(TimelineCell(week=week, status=status, shortage_units=short))  # type: ignore[arg-type]
        if total <= 0 and not tight:
            continue
        row_id = f"{cluster}|{style}"
        ranked.append(
            (
                total,
                first or "9999-W99",
                StockoutRow(
                    id=row_id, dc_code=dc, cluster_code=cluster, style_code=style, label=f"{style} · {cluster}",
                    cells=cells, first_breach_week=first,
                    by_option={oid: table.get((dc, cluster, style)) for oid, table in breaches.items()},
                ),
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda r: (-r[0], r[1], r[2].id))
    rows = [r[2] for r in ranked[:MAX_TIMELINE_ROWS]]
    props = StockoutTimelineProps(
        weeks=weeks, options=[TimelineOption(id=str(o["id"]), title=str(o["title"])) for o in options]
    )
    return props, StockoutTimelineData(rows=rows)


# ─────────────────────────────── KPI strip ───────────────────────────────


def outcome_kpi_items(
    exposure: Sequence[KpiItem], option: Mapping[str, Any], scorecard: Scorecard
) -> list[KpiItem]:
    """Exposure KPIs followed by what the recommended option recovers and costs."""
    delta = option.get("kpi_delta") or {}
    extra = [
        KpiItem(key="units_recovered", label="Units recovered by the plan", value=int(delta.get("units_recovered") or 0),
                unit="units", prov=Provenance.SIMULATED, preliminary=False),
        KpiItem(key="incremental_cost_cents", label="Plan cost", value=scorecard.incremental_cost_cents,
                unit="eur_cents", prov=Provenance.SIMULATED, preliminary=False),
        KpiItem(key="net_benefit_cents", label="Net benefit", value=scorecard.net_benefit_cents, unit="eur_cents",
                prov=Provenance.SIMULATED, preliminary=False),
        KpiItem(key="fill_rate_bp", label="Fill rate with the plan", value=scorecard.fill_rate_bp, unit="pct_bp",
                prov=Provenance.SIMULATED, preliminary=False),
    ]
    keys = {i.key for i in exposure}
    return [*exposure, *(i for i in extra if i.key not in keys)]


def horizon_end(problem: ProblemData) -> dt.date:
    """The day after the horizon — where a breach that disappears is counted as moved to (the
    optimizer's ``first_breach_shift_days`` rule, so recomputed scorecards match its own)."""
    weeks = week_range(problem.horizon.start_week, problem.horizon.weeks)
    return monday_of(weeks[-1]) + dt.timedelta(days=7)


def total_demand(problem: ProblemData) -> int:
    return sum(d.units for d in problem.demand)


def impact_kpis(impact: Mapping[str, Any]) -> ImpactKpis:
    return ImpactKpis.model_validate(impact["kpis"])
