"""M3 canvas payload builders (``80-m3-deterministic-core.md`` §5) and the sea-route catalogue.

Network map: sea lanes follow the catalogued waypoints (Cape vs Suez from the lane's ``via``),
road lanes are straight, sea-air runs into the hub then straight to the DC, option links carry
their ``option_id``; statuses, summary, bbox, legend, payload size. Stockout timeline: top rows,
cell statuses and per-option first breach weeks. Scenario compare: Pareto flags, recommended and
guided-only markers, derived scorecards. Impact graph summary. Outcome KPI strip.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from keelson_contracts.blocks import (
    MAX_TIMELINE_ROWS,
    KpiItem,
    block_data_to_wire,
    make_block,
    parse_block_data,
)
from keelson_contracts.common import Provenance
from keelson_contracts.optimizer import ProblemData, Scorecard
from keelson_contracts.scenario import DisruptionScenario

from keelson_engine.runtime.geo import CHOKEPOINT_PREFERENCE, GeoCatalog, e6, get_geo
from keelson_engine.runtime.visuals import (
    affected_receipts,
    horizon_end,
    impact_graph,
    network_map,
    option_scorecard,
    outcome_kpi_items,
    pareto_flags,
    scenario_compare_props,
    stockout_timeline,
)

ORIGINS = ("BDCGP", "LKCMB", "VNSGN", "AEJEA")
DESTS = ("GBFXT", "NLRTM", "DEHAM", "PLGDN")


# ─────────────────────────────── geo catalogue ───────────────────────────────


def test_sea_route_catalogue_covers_the_required_lanes() -> None:
    geo = get_geo()
    assert geo.source is not None and len(geo.routes) >= 43
    for origin in ORIGINS:
        for dest in DESTS:
            cape = geo.sea_route(origin, dest, ["CAPE"])
            suez = geo.sea_route(origin, dest, ["SUEZ"])
            assert cape and suez and cape != suez, (origin, dest)
            assert min(lat for _, lat in cape) < e6(-34.0)  # rounds the Cape of Good Hope
            assert min(lat for _, lat in suez) > e6(0.5)  # stays north of the equator: Red Sea + Mediterranean
            suez_pt = geo.place("SUEZ")
            assert any(abs(lon - suez_pt[0]) < e6(0.5) and abs(lat - suez_pt[1]) < e6(1.0) for lon, lat in suez)
            assert cape[0] == geo.place(origin) and cape[-1] == geo.place(dest)
            # reversed lookup returns the reversed polyline; default prefers Suez
            assert geo.sea_route(dest, origin, ["CAPE"]) == tuple(reversed(cape))
            assert geo.sea_route(origin, dest) == suez
    for origin in ("MATNG", "TRIST"):
        for dest in DESTS:
            assert geo.sea_route(origin, dest), (origin, dest)
    for origin in ("BDCGP", "LKCMB", "VNSGN"):
        assert geo.sea_route(origin, "AEJEA")  # sea leg of sea-air
    for _, (_, kind, (lon, lat)) in geo.places.items():
        assert kind in ("port", "chokepoint") and -180e6 <= lon <= 180e6 and -90e6 <= lat <= 90e6
    assert set(CHOKEPOINT_PREFERENCE) <= set(geo.places)
    for route in geo.routes:
        assert all(-180e6 <= lon <= 180e6 and -90e6 <= lat <= 90e6 for lon, lat in route.path)
        # no absurd jumps between consecutive waypoints (hand-made polylines, < 25 degrees per hop)
        assert all(abs(a[0] - b[0]) + abs(a[1] - b[1]) < e6(25) for a, b in zip(route.path, route.path[1:]))


def test_lane_paths_by_mode() -> None:
    geo = get_geo()
    start, end = geo.place("BDCGP"), (e6(0.35), e6(51.49))
    sea = geo.stops_path([("BDCGP", start, True), ("LKCMB", geo.place("LKCMB"), True),
                          ("GBFXT", geo.place("GBFXT"), True), ("UK1", end, False)], mode="sea", via=["LKCMB", "CAPE"])
    assert sea[0] == start and sea[-1] == end and len(sea) > 20
    assert min(lat for _, lat in sea) < e6(-34.0)
    road = geo.stops_path([("TRIST", geo.place("TRIST"), True), ("DE1", end, False)], mode="road")
    assert road == [geo.place("TRIST"), end]
    sea_air = geo.stops_path([("BDCGP", start, True), ("AEJEA", geo.place("AEJEA"), True), ("UK1", end, False)],
                             mode="sea_air", via=["AEJEA"])
    assert geo.place("AEJEA") in sea_air and sea_air[-1] == end and sea_air[-2] == geo.place("AEJEA")
    empty = GeoCatalog()  # no catalogue: straight through the stops
    assert empty.stops_path([("A", (0, 0), True), ("B", (5, 5), True)], mode="sea") == [(0, 0), (5, 5)]
    assert empty.country_point("BD", key="x") is None


# ─────────────────────────────── fixtures ───────────────────────────────


def problem() -> ProblemData:
    skus = [{"code": f"PUF-01-{s}", "style_code": "PUF-01", "retail_price_cents": 4000, "unit_cost_cents": 1300}
            for s in ("S", "M")] + [{"code": "KNT-01-M", "style_code": "KNT-01", "retail_price_cents": 2000,
                                     "unit_cost_cents": 600}]
    geo = get_geo()

    def loc(kind: str, code: str, point: tuple[int, int], parent: str | None = None) -> dict[str, Any]:
        return {"code": code, "kind": kind, "name": code, "lon_e6": point[0], "lat_e6": point[1], "parent": parent}

    return ProblemData.model_validate({
        "tenant": "demo-retail", "as_of": "2026-10-05", "horizon": {"start_week": "2026-W41", "weeks": 9},
        "dcs": [{"code": "UK1"}, {"code": "DE1"}, {"code": "NL1"}],
        "clusters": [{"code": "UK1-COLD-A", "dc_code": "UK1"}, {"code": "DE1-COLD-A", "dc_code": "DE1"},
                     {"code": "NL1-MILD-A", "dc_code": "NL1"}],
        "skus": skus,
        "receipts": [{"ref": "PO-1#1", "dc_code": "UK1", "sku": "PUF-01-S", "qty": 600, "eta": "2026-11-09"},
                     {"ref": "PO-2#1", "dc_code": "DE1", "sku": "KNT-01-M", "qty": 400, "eta": "2026-11-12"}],
        "demand": [{"cluster_code": "UK1-COLD-A", "sku": "PUF-01-S", "iso_week": "2026-W45", "units": 500}],
        "lanes": [{"from_dc": "DE1", "to_dc": "UK1", "transit_days": 4, "cost_per_unit_cents": 60, "max_units_wk": 9000}],
        "locations": [
            loc("dc", "UK1", (e6(0.35), e6(51.49))), loc("dc", "DE1", (e6(11.03), e6(50.98))),
            loc("dc", "NL1", (e6(6.17), e6(51.37))),
            loc("cluster", "UK1-COLD-A", (e6(-1.0), e6(52.5)), "UK1"), loc("cluster", "DE1-COLD-A", (e6(12.0), e6(51.0)), "DE1"),
            loc("cluster", "NL1-MILD-A", (e6(5.0), e6(52.0)), "NL1"),
            loc("port", "BDCGP", geo.place("BDCGP")), loc("port", "GBFXT", geo.place("GBFXT")),
            loc("port", "LKCMB", geo.place("LKCMB")), loc("port", "TRIST", geo.place("TRIST")),
            loc("port", "VNSGN", geo.place("VNSGN")), loc("port", "DEHAM", geo.place("DEHAM")),
            loc("factory", "F-BD-01", (e6(90.4), e6(23.9)), "CMT-BD-01"), loc("factory", "F-TR-01", (e6(28.9), e6(41.0)), "CMT-TR-01"),
            loc("factory", "F-VN-01", (e6(106.7), e6(10.9)), "SUP-VN-01"), loc("supplier", "TRIM-BD-01", (e6(90.3), e6(23.7))),
        ],
        "inbound_lanes": [
            {"code": "CGP-FXT-SEA+FXT-UK1-RD", "origin": "BDCGP", "dest_dc": "UK1", "mode": "sea", "transit_days": 33,
             "cost_per_unit_cents": 113, "weekly_capacity_units": 60000, "via": ["LKCMB", "CAPE", "GBFXT"],
             "alt_lane_code": "CGP-UK1-SA"},
            {"code": "CGP-UK1-SA", "origin": "BDCGP", "dest_dc": "UK1", "mode": "sea_air", "transit_days": 16,
             "cost_per_unit_cents": 145, "weekly_capacity_units": 26000, "via": ["AEJEA"]},
            {"code": "SGN-HAM-SEA+HAM-DE1-RD", "origin": "VNSGN", "dest_dc": "DE1", "mode": "sea", "transit_days": 36,
             "cost_per_unit_cents": 108, "weekly_capacity_units": 50000, "via": ["SUEZ", "DEHAM"]},
            {"code": "IST-DE1-RD", "origin": "TRIST", "dest_dc": "DE1", "mode": "road", "transit_days": 5,
             "cost_per_unit_cents": 55, "weekly_capacity_units": 15000},
        ],
        "po_lines": [
            {"ref": "PO-1#1", "po_code": "PO-1", "supplier_code": "TRIM-BD-01", "factory_code": "F-BD-01",
             "dest_dc": "UK1", "sku": "PUF-01-S", "qty": 600, "eta": "2026-11-09", "ex_factory": "2026-10-07",
             "mode": "sea", "lane_code": "CGP-FXT-SEA+FXT-UK1-RD"},
            {"ref": "PO-2#1", "po_code": "PO-2", "supplier_code": "SUP-VN-01", "factory_code": "F-VN-01",
             "dest_dc": "DE1", "sku": "KNT-01-M", "qty": 400, "eta": "2026-11-12", "ex_factory": "2026-10-07",
             "mode": "sea", "lane_code": "SGN-HAM-SEA+HAM-DE1-RD"},
        ],
        "alt_sources": [{"supplier_code": "CMT-TR-01", "factory_code": "F-TR-01", "style_codes": ["PUF-01"],
                         "lead_time_days": 26, "unit_cost_cents": 1700, "moq_units": 300,
                         "weekly_capacity_units": 3000, "approved": True, "compliance_ok": True,
                         "lane_code": "IST-DE1-RD"}],
    })


def scenario(**extra: Any) -> DisruptionScenario:
    perturbations = [{"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": ["PO-1#1"]}, "days": 7,
                      "prov": "decision"}]
    perturbations += extra.get("more", [])
    return DisruptionScenario.model_validate({
        "id": "scn-1", "tenant": "demo-retail", "mode": "actual", "event_family": "SUP", "event_type": "trim_delay",
        "description": "Trim delay", "perturbations": perturbations, "matched": [], "assumptions": [],
        "config_version": "default/p-1",
    })


def impact() -> dict[str, Any]:
    weeks = [f"2026-W{w}" for w in range(41, 50)]
    timeline = []
    for i, cluster in enumerate(("UK1-COLD-A", "DE1-COLD-A", "NL1-MILD-A")):
        for w, week in enumerate(weeks):
            short = 30 * (3 - i) if (cluster != "NL1-MILD-A" and w >= 4) else 0
            pab = -short if short else (2 if cluster == "NL1-MILD-A" and w == 6 else 50)
            timeline.append({"cluster_code": cluster, "style_code": "PUF-01", "week": week,
                             "projected_available": pab, "demand": 10, "shortage": short})
    kpis = {"units_at_risk": 400, "revenue_at_risk_cents": 1_600_000, "margin_at_risk_cents": 1_080_000,
            "shortage_cluster_weeks": 10,
            "first_breach": [{"dc_code": "UK1", "cluster_code": "UK1-COLD-A", "style_code": "PUF-01",
                              "week": "2026-W45", "date": "2026-11-02"},
                             {"dc_code": "DE1", "cluster_code": "DE1-COLD-A", "style_code": "PUF-01",
                              "week": "2026-W45", "date": "2026-11-02"}]}
    return {"kpis": kpis, "baseline_kpis": {**kpis, "units_at_risk": 0, "first_breach": []},
            "by_dc": [{"dc_code": "UK1", "units_at_risk": 300, "revenue_at_risk_cents": 1_200_000}],
            "timeline": timeline, "provenance": {"inputs_hash": "a" * 64},
            "graph": {"nodes": [
                {"id": "supplier:TRIM-BD-01", "kind": "supplier", "label": "Meghna", "status": "disrupted", "units_at_risk": 0},
                {"id": "po:PO-1", "kind": "po", "label": "PO-1", "status": "impacted", "units_at_risk": 0},
                {"id": "dc:UK1", "kind": "dc", "label": "UK1", "status": "at_risk", "units_at_risk": 300,
                 "first_breach_week": "2026-W45"}],
                "edges": [{"source": "supplier:TRIM-BD-01", "target": "po:PO-1", "kind": "supplies"},
                          {"source": "po:PO-1", "target": "dc:UK1", "kind": "ships"}]}}


def options() -> list[dict[str, Any]]:
    breach = impact()["kpis"]["first_breach"]
    base = {"kpis_before": impact()["kpis"], "fallback": False}
    return [
        {**base, "id": "opt-A", "title": "Rebalance", "levers": ["reallocate_inventory"],
         "kpis_after": {**impact()["kpis"], "units_at_risk": 200, "first_breach": breach[1:]},
         "kpi_delta": {"units_recovered": 200, "net_benefit_cents": 300_000, "transfer_cost_cents": 12_000},
         "actions": [{"seq": 1}], "scorecard": card(cost=12_000, lost=200),
         "decisions": [{"lever": "reallocate_inventory", "from_dc": "DE1", "to_dc": "UK1", "sku": "PUF-01-S", "qty": 120,
                        "ship_week": "2026-W42", "arrive_week": "2026-W43"},
                       {"lever": "reallocate_inventory", "from_dc": "DE1", "to_dc": "UK1", "sku": "PUF-01-M", "qty": 80,
                        "ship_week": "2026-W42", "arrive_week": "2026-W43"}]},
        {**base, "id": "opt-B", "title": "Rebalance + sea-air", "levers": ["reallocate_inventory", "expedite_mode_shift"],
         "kpis_after": {**impact()["kpis"], "units_at_risk": 50, "first_breach": [{**breach[0], "week": "2026-W47"}]},
         "kpi_delta": {"units_recovered": 350, "net_benefit_cents": 400_000, "transfer_cost_cents": 12_000},
         "actions": [{"seq": 1}, {"seq": 2}], "scorecard": card(cost=30_000, lost=50), "guided_only": True,
         "guided_reason": "Merxa cannot apply po_line.split",
         "decisions": [{"lever": "expedite_mode_shift", "po_line_ref": "PO-1#1", "sku": "PUF-01-S", "dest_dc": "UK1",
                        "lane_code": "CGP-UK1-SA", "mode": "sea_air", "qty": 300, "book_week": "2026-W42",
                        "arrive_week": "2026-W44", "premium_cents": 9000}]},
        {**base, "id": "opt-C", "title": "Near-shore", "levers": ["resource_alt_supplier"],
         "kpis_after": {**impact()["kpis"], "units_at_risk": 250, "first_breach": breach},
         "kpi_delta": {"units_recovered": 150, "net_benefit_cents": 100_000, "transfer_cost_cents": 0},
         "actions": [{"seq": 1}], "scorecard": card(cost=40_000, lost=250),
         "decisions": [{"lever": "resource_alt_supplier", "supplier_code": "CMT-TR-01", "factory_code": "F-TR-01",
                        "dest_dc": "DE1", "sku": "PUF-01-S", "qty": 300, "eta": "2026-10-31",
                        "replaces_po_line_ref": "PO-1#1", "premium_cents": 12_000}]},
        {**base, "id": "do-nothing", "title": "Do nothing", "levers": [], "kpis_after": impact()["kpis"],
         "kpi_delta": {"units_recovered": 0, "net_benefit_cents": 0, "transfer_cost_cents": 0}, "actions": [],
         "scorecard": card(cost=0, lost=400), "decisions": []},
    ]


def card(*, cost: int, lost: int) -> dict[str, Any]:
    return Scorecard(fill_rate_bp=9000, lost_units=lost, lost_margin_cents=lost * 2700, incremental_cost_cents=cost,
                     cost_breakdown={"transfer": cost, "expedite_premium": 0, "nearshore_premium": 0},
                     net_benefit_cents=0, shortage_days=7, first_breach_shift_days=0, tts_gain_days=0,
                     changes=1).model_dump(mode="json")


LAND_BOXES = {
    # (west, east, south, north) in degrees: interiors well inside land (coarse, conservative)
    "Marmara Island": (27.47, 27.73, 40.57, 40.68), "Sri Lanka": (80.0, 81.7, 6.2, 9.6),
    "Madagascar": (44.5, 49.0, -24.5, -13.5), "Iberia": (-8.5, -1.0, 37.5, 43.0), "Jutland": (8.3, 10.0, 55.0, 57.0),
    "Sinai": (33.2, 34.3, 28.5, 30.8), "Arabia": (45.0, 55.0, 18.0, 25.0), "southern Africa": (15.0, 35.0, -30.0, 10.0),
    "Great Britain": (-3.5, -0.5, 51.5, 54.5), "India": (74.0, 86.0, 12.0, 25.0), "Indochina": (100.0, 106.0, 12.0, 20.0),
    "Sumatra": (100.0, 103.0, -3.0, 0.5), "northern Sumatra": (96.0, 98.5, 2.5, 4.5),
}


def test_sea_route_waypoints_stay_off_land() -> None:
    """Review regression: catalogued waypoints never sit inside a land mass (one Istanbul route
    point was on Marmara Island, one Elbe point on the river bank)."""
    on_land = [
        (r.origin, r.dest, r.via, name, (lon / 1e6, lat / 1e6))
        for r in get_geo().routes
        for lon, lat in r.path
        for name, (w, e, s_, n) in LAND_BOXES.items()
        if e6(w) <= lon <= e6(e) and e6(s_) <= lat <= e6(n)
    ]
    assert on_land == []


# ─────────────────────────────── network map ───────────────────────────────


def test_network_map_nodes_links_and_paths() -> None:
    props, data = network_map(problem=problem(), scenario=scenario(), impact=impact(), options=options(),
                              geo=get_geo(), title="Merxa supply network")
    nodes = {n.id: n for n in data.nodes}
    links = {ln.id: ln for ln in data.links}
    assert nodes["dc:UK1"].status == "at_risk" and nodes["dc:UK1"].units_at_risk == 300
    assert nodes["dc:DE1"].status == "ok"
    assert nodes["cluster:UK1-COLD-A"].status == "at_risk" and nodes["cluster:NL1-MILD-A"].status == "ok"
    assert nodes["factory:F-BD-01"].status == "impacted" and nodes["factory:F-VN-01"].status == "ok"
    assert nodes["chokepoint:CAPE"].kind == "chokepoint" and "port:LKCMB" in nodes
    sea = links["lane:CGP-FXT-SEA+FXT-UK1-RD"]
    assert (sea.kind, sea.status, sea.units, sea.option_id) == ("inbound", "impacted", 600, None)
    assert (sea.from_, sea.to) == ("port:BDCGP", "dc:UK1")
    assert len(sea.path) > 20 and min(lat for _, lat in sea.path) < e6(-34.0)  # around the Cape
    vn = links["lane:SGN-HAM-SEA+HAM-DE1-RD"]
    assert min(lat for _, lat in vn.path) > e6(0.5) and vn.status == "ok"  # via Suez
    assert links["feed:F-BD-01>BDCGP"].kind == "inbound" and len(links["feed:F-BD-01>BDCGP"].path) == 2
    assert links["transfer:DE1>UK1"].path == [(nodes["dc:DE1"].lon_e6, nodes["dc:DE1"].lat_e6),
                                               (nodes["dc:UK1"].lon_e6, nodes["dc:UK1"].lat_e6)]
    # option links
    tr = links["opt-A:transfer:DE1>UK1"]
    assert (tr.kind, tr.units, tr.option_id, len(tr.path)) == ("transfer", 200, "opt-A", 2)
    sa = links["opt-B:sea_air:CGP-UK1-SA"]
    assert sa.kind == "sea_air" and sa.option_id == "opt-B" and sa.units == 300
    assert get_geo().place("AEJEA") in sa.path and sa.path[-1] == (nodes["dc:UK1"].lon_e6, nodes["dc:UK1"].lat_e6)
    ns = links["opt-C:nearshore:F-TR-01>DE1"]
    assert ns.kind == "nearshore" and ns.from_ == "factory:F-TR-01" and ns.to == "dc:DE1"
    assert ns.path[0] == (nodes["factory:F-TR-01"].lon_e6, nodes["factory:F-TR-01"].lat_e6)
    assert ns.path[1:] == [get_geo().place("TRIST"), (nodes["dc:DE1"].lon_e6, nodes["dc:DE1"].lat_e6)]  # road: straight
    assert not any(ln.option_id == "do-nothing" for ln in data.links)
    # props summary / bbox / legend
    assert props.summary.nodes == len(data.nodes) and props.summary.lanes == len(data.links)
    assert props.summary.at_risk_nodes == sum(1 for n in data.nodes if n.status != "ok")
    west, south, east, north = props.bbox
    for ln in data.links:
        assert all(west <= lon <= east and south <= lat <= north for lon, lat in ln.path)
    assert {i.key for i in props.legend if i.kind == "link"} == {"inbound", "transfer", "sea_air", "nearshore"}
    assert {i.key for i in props.legend if i.kind == "node"} >= {"impacted", "at_risk", "ok"}
    # the payload is a valid data_ref body and stays small
    wire = block_data_to_wire(data)
    assert parse_block_data("network_map@1", wire) == data
    assert len(json.dumps(wire)) < 100_000
    assert all("from" in ln for ln in wire["links"])


def test_network_map_without_coordinates_is_empty() -> None:
    bare = problem().model_copy(update={"locations": []})
    props, data = network_map(problem=bare, scenario=scenario(), impact=impact(), options=options(), geo=get_geo(),
                              title="m")
    assert data.nodes == [] and data.links == [] and props.summary.nodes == 0  # compose skips the block


def test_network_map_marks_closed_ports_and_chokepoints() -> None:
    closure = {"op": "capacity_scale", "target": {"kinds": ["chokepoint"], "ids": ["SUEZ"]}, "factor_bp": 0,
               "prov": "fact"}
    _, data = network_map(problem=problem(), scenario=scenario(more=[closure]), impact=impact(), options=[],
                          geo=get_geo(), title="map")
    nodes = {n.id: n for n in data.nodes}
    links = {ln.id: ln for ln in data.links}
    assert nodes["chokepoint:SUEZ"].status == "disrupted"
    assert links["lane:SGN-HAM-SEA+HAM-DE1-RD"].status == "disrupted"
    assert links["lane:CGP-FXT-SEA+FXT-UK1-RD"].status == "impacted"  # Cape lane: delayed receipts only


def test_network_map_closed_vendor_lane_disrupts_the_chained_inbound_lane() -> None:
    """Review regression: a ``lane`` target names a vendor lane (``CGP-FXT-SEA``); inbound lanes
    chain legs (``CGP-FXT-SEA+FXT-UK1-RD``), so the chain containing the leg is disrupted too."""
    closure = {"op": "capacity_scale", "target": {"kinds": ["lane"], "ids": ["CGP-FXT-SEA"]}, "factor_bp": 0,
               "prov": "fact"}
    _, data = network_map(problem=problem(), scenario=scenario(more=[closure]), impact=impact(), options=[],
                          geo=get_geo(), title="map")
    links = {ln.id: ln for ln in data.links}
    assert links["lane:CGP-FXT-SEA+FXT-UK1-RD"].status == "disrupted"
    assert links["lane:SGN-HAM-SEA+HAM-DE1-RD"].status == "ok"
    whole = {"op": "capacity_scale", "target": {"kinds": ["lane"], "ids": ["SGN-HAM-SEA+HAM-DE1-RD"]},
             "factor_bp": 0, "prov": "fact"}
    _, data = network_map(problem=problem(), scenario=scenario(more=[whole]), impact=impact(), options=[],
                          geo=get_geo(), title="map")
    assert {ln.id: ln for ln in data.links}["lane:SGN-HAM-SEA+HAM-DE1-RD"].status == "disrupted"


def test_derived_scorecard_follows_the_optimizer_rules() -> None:
    """Review regression: without an optimizer scorecard the engine recomputes it with the
    optimizer's own rules — a missing breach counts as the day after the horizon (so a breach a
    plan *introduces* is a negative shift), fill rate is floored."""
    p = problem()
    assert horizon_end(p) == dt.date(2026, 12, 7)  # W49 Monday + 7: the day after the horizon
    opt = {k: v for k, v in options()[0].items() if k != "scorecard"}
    gone = {**opt, "kpis_after": {**opt["kpis_after"], "first_breach": []}}
    assert option_scorecard(gone, total_demand=1, horizon_end=horizon_end(p)).first_breach_shift_days == 35
    new_breach = {**opt, "kpis_before": {**opt["kpis_before"], "first_breach": []},
                  "kpis_after": {**opt["kpis_after"], "first_breach": impact()["kpis"]["first_breach"][:1]}}
    assert option_scorecard(new_breach, total_demand=1, horizon_end=horizon_end(p)).first_breach_shift_days == -35
    third = {**opt, "kpis_after": {**opt["kpis_after"], "units_at_risk": 1}}
    assert option_scorecard(third, total_demand=3, horizon_end=horizon_end(p)).fill_rate_bp == 6666  # floor, not 6667


def test_affected_receipts_resolves_po_and_entity_targets() -> None:
    p = problem()
    by_po = scenario(more=[{"op": "lead_time_shift", "target": {"kinds": ["po"], "ids": ["PO-2"]}, "days": 3,
                            "prov": "decision"}])
    assert affected_receipts(p, by_po) == {"PO-1#1", "PO-2#1"}
    by_factory = DisruptionScenario.model_validate({**scenario().model_dump(mode="json"), "perturbations": [
        {"op": "lead_time_shift", "target": {"kinds": ["factory"], "ids": ["F-VN-01"]}, "days": 3, "prov": "decision"}]})
    assert affected_receipts(p, by_factory) == {"PO-2#1"}


# ─────────────────────────────── timeline / compare / graph ───────────────────────────────


def test_stockout_timeline_rows_and_breach_by_option() -> None:
    props, data = stockout_timeline(problem=problem(), impact=impact(), options=options())
    assert props.weeks[0] == "2026-W41" and len(props.weeks) == 9
    assert [o.id for o in props.options] == ["opt-A", "opt-B", "opt-C", "do-nothing"]
    rows = {r.id: r for r in data.rows}
    assert list(rows) == ["UK1-COLD-A|PUF-01", "DE1-COLD-A|PUF-01", "NL1-MILD-A|PUF-01"]  # by total shortage
    uk = rows["UK1-COLD-A|PUF-01"]
    assert uk.dc_code == "UK1" and uk.first_breach_week == "2026-W45" and uk.label == "PUF-01 · UK1-COLD-A"
    assert [c.status for c in uk.cells[:5]] == ["ok", "ok", "ok", "ok", "short"] and uk.cells[4].shortage_units == 90
    assert uk.by_option == {"opt-A": None, "opt-B": "2026-W47", "opt-C": "2026-W45", "do-nothing": "2026-W45"}
    nl = rows["NL1-MILD-A|PUF-01"]  # never short, but tight in one week
    assert nl.first_breach_week is None and [c.status for c in nl.cells].count("tight") == 1
    wire = block_data_to_wire(data)
    assert parse_block_data("stockout_timeline@1", wire) == data


def test_stockout_timeline_keeps_the_top_rows_and_skips_quiet_cases() -> None:
    p = problem()
    many = {"timeline": [{"cluster_code": "UK1-COLD-A", "style_code": f"S{i:02d}", "week": "2026-W45",
                          "projected_available": -i, "demand": 5, "shortage": i} for i in range(1, 60)]}
    _, data = stockout_timeline(problem=p, impact=many, options=[])
    assert len(data.rows) == MAX_TIMELINE_ROWS and data.rows[0].style_code == "S59" and data.rows[-1].style_code == "S20"
    quiet = {"timeline": [{"cluster_code": "UK1-COLD-A", "style_code": "S", "week": "2026-W45",
                           "projected_available": 99, "demand": 5, "shortage": 0}]}
    assert stockout_timeline(problem=p, impact=quiet, options=[]) is None


def test_scenario_compare_pareto_recommended_and_guided() -> None:
    opts = options()
    assert pareto_flags(opts) == {"opt-A": True, "opt-B": True, "opt-C": False, "do-nothing": True}
    props = scenario_compare_props(opts, recommended_id="opt-A")
    by = {o.id: o for o in props.options}
    assert by["opt-A"].recommended and not by["opt-B"].recommended
    assert by["opt-B"].guided_only and by["opt-B"].guided_reason == "Merxa cannot apply po_line.split"
    assert by["opt-C"].guided_reason is None and by["do-nothing"].levers == []
    assert (props.axes.x, props.axes.y) == ("incremental_cost_cents", "lost_units")
    block = make_block("scenario_compare", props, title="Options", provenance={"kind": "simulated", "engine": "e",
                                                                               "as_of": "2026-10-05"}, block_id="blk_cmp1")
    assert block.data_ref is None and block.placement == "canvas"


def test_scorecard_derived_when_the_optimizer_has_none() -> None:
    opt = {k: v for k, v in options()[0].items() if k != "scorecard"}
    card_ = option_scorecard(opt, total_demand=1000, horizon_end=dt.date(2026, 12, 6))
    assert card_.lost_units == 200 and card_.fill_rate_bp == 8000
    assert card_.incremental_cost_cents == card_.cost_breakdown.transfer == 12_000
    assert card_.net_benefit_cents == 300_000 and card_.changes == 1
    assert card_.first_breach_shift_days == 0  # DE1 breach unchanged at W45
    fixed = {**opt, "kpis_after": {**opt["kpis_after"], "first_breach": []}}
    assert option_scorecard(fixed, total_demand=0, horizon_end=dt.date(2026, 12, 6)).first_breach_shift_days == 34
    assert option_scorecard(fixed, total_demand=0, horizon_end=dt.date(2026, 12, 6)).fill_rate_bp == 10_000
    given = options()[1]
    assert option_scorecard(given, total_demand=1, horizon_end=dt.date(2026, 12, 6)).incremental_cost_cents == 30_000


def test_impact_graph_summary() -> None:
    props, graph = impact_graph(impact())
    assert (props.summary.nodes, props.summary.disrupted, props.summary.impacted) == (3, 1, 1)
    assert parse_block_data("impact_graph@1", block_data_to_wire(graph)) == graph
    assert impact_graph({"graph": None}) is None


def test_outcome_kpi_items_extend_the_exposure_strip() -> None:
    exposure = [KpiItem(key="units_at_risk", label="Units at risk", value=400, unit="units",
                        prov=Provenance.SIMULATED, preliminary=False)]
    items = outcome_kpi_items(exposure, options()[0], Scorecard.model_validate(options()[0]["scorecard"]))
    assert [i.key for i in items] == ["units_at_risk", "units_recovered", "incremental_cost_cents", "net_benefit_cents",
                                      "fill_rate_bp"]
    assert items[1].value == 200 and items[4].unit == "pct_bp"


@pytest.mark.parametrize("kind", ["network_map", "impact_graph", "stockout_timeline"])
def test_data_backed_blocks_point_at_their_own_data(kind: str) -> None:
    props = {"network_map": lambda: network_map(problem=problem(), scenario=scenario(), impact=impact(), options=[],
                                                geo=get_geo(), title="m")[0],
             "impact_graph": lambda: impact_graph(impact())[0],
             "stockout_timeline": lambda: stockout_timeline(problem=problem(), impact=impact(), options=options())[0]}[kind]()
    block = make_block(kind, props, title=kind, provenance={"kind": "simulated", "engine": "e", "as_of": "2026-10-05"},
                       block_id="blk_abc123")
    assert block.data_ref == "/api/blocks/blk_abc123/data" and block.placement == "canvas"
