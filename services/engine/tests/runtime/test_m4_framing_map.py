"""Map path geometry (``90`` §10 carry-over): sea-air lanes are the sea leg into the Jebel Ali hub
plus an air arc to the DC; sea lanes ending at a DC follow the catalogued short-sea routes from
Istanbul / Tanger Med; closed chokepoints get a footprint polygon."""

from __future__ import annotations

from typing import Any

from keelson_contracts.blocks import block_data_to_wire, parse_block_data
from keelson_contracts.scenario import DisruptionScenario
from test_m4_framing_support import full_problem

from keelson_engine.runtime.geo import AIR_ARC_POINTS, GeoCatalog, air_arc, distance_km, e6, get_geo
from keelson_engine.runtime.visuals import network_map

PROBLEM = full_problem(weeks=3)


def _where(code: str) -> tuple[int, int]:
    loc = next(loc for loc in PROBLEM.locations if loc.code == code)
    return (loc.lon_e6, loc.lat_e6)


def test_air_arc_is_a_deterministic_curve_with_exact_ends() -> None:
    hub, dc = get_geo().place("AEJEA"), _where("DE1")
    arc = air_arc(hub, dc)
    assert arc[0] == hub and arc[-1] == dc and len(arc) == AIR_ARC_POINTS
    assert arc == air_arc(hub, dc)
    assert all(isinstance(v, int) for p in arc for v in p)
    # it bulges off the chord (an arc, not a straight line) by a modest amount
    mid = arc[len(arc) // 2]
    chord_mid = ((hub[0] + dc[0]) // 2, (hub[1] + dc[1]) // 2)
    off = distance_km(mid, chord_mid)
    assert 0.1 * distance_km(hub, dc) < off < 0.3 * distance_km(hub, dc)
    assert mid[1] > chord_mid[1]  # poleward for the Gulf → Europe leg
    assert air_arc(hub, hub) == [hub, hub]


def test_sea_air_legs_split_at_the_hub() -> None:
    geo = get_geo()
    stops = [("VNSGN", _where("VNSGN"), True), ("AEJEA", _where("AEJEA"), True), ("DE1", _where("DE1"), False)]
    legs = geo.sea_air_legs(stops, ["AEJEA"])
    assert legs is not None
    sea, hub, air = legs
    assert hub == "AEJEA"
    assert sea[0] == _where("VNSGN") and sea[-1] == _where("AEJEA") and len(sea) > 10  # the catalogued route
    assert air == air_arc(_where("AEJEA"), _where("DE1"))
    whole = geo.stops_path(stops, mode="sea_air", via=["AEJEA"])
    assert whole == [*sea, _where("DE1")]  # the single-polyline form keeps a straight air leg
    # no hub route / origin not a port → no split, the caller draws the plain path
    assert geo.sea_air_legs([("F-VN-01", (0, 0), False), ("DE1", _where("DE1"), False)], ["AEJEA"]) is None
    assert GeoCatalog().sea_air_legs(stops, ["AEJEA"]) is None


def test_short_sea_into_a_dc_follows_the_catalogued_route() -> None:
    geo = get_geo()
    for origin, dc, port in (("TRIST", "DE1", "DEHAM"), ("MATNG", "NL1", "NLRTM")):
        path = geo.stops_path([(origin, _where(origin), True), (dc, _where(dc), False)], mode="sea")
        assert path[0] == _where(origin) and path[-1] == _where(dc)
        assert len(path) > 10  # along the short-sea route, not a straight line
        assert geo.short_sea_route(origin, _where(dc))[0] == port
    # road lanes stay straight
    road = geo.stops_path([("TRIST", _where("TRIST"), True), ("DE1", _where("DE1"), False)], mode="road")
    assert road == [_where("TRIST"), _where("DE1")]
    # no port nearer to the DC than the origin itself → straight
    assert geo.short_sea_route("NLRTM", _where("NL1")) is None


def _scenario(perturbations: list[dict[str, Any]]) -> DisruptionScenario:
    return DisruptionScenario.model_validate({
        "id": "s", "tenant": "demo-retail", "mode": "what_if", "event_family": "LOG",
        "event_type": "LOG.chokepoint_closure", "description": "Suez", "perturbations": perturbations,
        "matched": [], "assumptions": [], "config_version": "default/p-1",
    })


def _impact() -> dict[str, Any]:
    return {"by_dc": [{"dc_code": "DE1", "units_at_risk": 500}], "timeline": []}


def test_network_map_draws_sea_air_as_sea_leg_plus_air_arc_and_a_closed_suez() -> None:
    closure = [{"op": "capacity_scale", "target": {"kinds": ["chokepoint"], "ids": ["SUEZ"]}, "factor_bp": 0,
                "prov": "decision", "window": {"start": "2026-10-05", "end": "2026-10-25"}}]
    options = [{"id": "opt-B", "title": "Sea-air", "decisions": [
        {"lever": "expedite_mode_shift", "lane_code": "SGN-DE1-SA", "qty": 400}]}]
    props, data = network_map(problem=PROBLEM, scenario=_scenario(closure), impact=_impact(), options=options,
                              geo=get_geo(), title="Network")
    links = {ln.id: ln for ln in data.links}
    nodes = {n.id: n for n in data.nodes}
    hub = (nodes["port:AEJEA"].lon_e6, nodes["port:AEJEA"].lat_e6)
    dc = (nodes["dc:DE1"].lon_e6, nodes["dc:DE1"].lat_e6)
    air = links["opt-B:sea_air:SGN-DE1-SA"]
    sea = links["opt-B:sea_air:SGN-DE1-SA:sea"]
    assert (air.kind, air.from_, air.to, air.option_id, air.units) == ("sea_air", "port:AEJEA", "dc:DE1", "opt-B", 400)
    assert air.path == air_arc(hub, dc)
    assert (sea.kind, sea.from_, sea.to, sea.option_id, sea.units) == ("inbound", "port:VNSGN", "port:AEJEA", "opt-B", None)
    assert sea.path[0] == (nodes["port:VNSGN"].lon_e6, nodes["port:VNSGN"].lat_e6) and sea.path[-1] == hub
    # the base sea lanes via Suez are disrupted, the Cape alternatives are not on the map (no PO lines)
    assert links["lane:SGN-HAM-SEA+HAM-DE1-RD"].status == "disrupted"
    assert min(lat for _, lat in links["lane:SGN-HAM-SEA+HAM-DE1-RD"].path) > e6(0.5)  # drawn via Suez
    assert nodes["chokepoint:SUEZ"].status == "disrupted"
    # the closed chokepoint's footprint, inside the bbox, with a legend entry
    assert data.footprint is not None
    [feature] = data.footprint.features
    assert feature.properties.label == "Suez Canal" and feature.properties.status == "disrupted"
    west, south, east, north = props.bbox
    assert all(west <= lon <= east and south <= lat <= north for ring in feature.geometry.coordinates for lon, lat in ring)
    assert any(i.kind == "footprint" and i.key == "closed_chokepoint" for i in props.legend)
    wire = block_data_to_wire(data)
    assert parse_block_data("network_map@1", wire) == data


def test_base_sea_air_lanes_split_too_and_maps_without_closure_have_no_footprint() -> None:
    lines = [pl.model_copy(update={"lane_code": "CGP-UK1-SA", "mode": "sea_air"}) if pl.po_code == "PO-BD-0001" else pl
             for pl in PROBLEM.po_lines]
    problem = PROBLEM.model_copy(update={"po_lines": lines})
    delay = [{"op": "lead_time_shift", "target": {"kinds": ["po"], "ids": ["PO-BD-0001"]}, "days": 7, "prov": "decision"}]
    _props, data = network_map(problem=problem, scenario=_scenario(delay), impact=_impact(), options=[],
                               geo=get_geo(), title="Network")
    links = {ln.id: ln for ln in data.links}
    assert links["lane:CGP-UK1-SA"].kind == "sea_air" and links["lane:CGP-UK1-SA"].from_ == "port:AEJEA"
    assert links["lane:CGP-UK1-SA:sea"].kind == "inbound" and links["lane:CGP-UK1-SA:sea"].to == "port:AEJEA"
    assert links["lane:CGP-UK1-SA"].status == links["lane:CGP-UK1-SA:sea"].status == "impacted"
    assert links["lane:CGP-UK1-SA"].units == 3000 and links["lane:CGP-UK1-SA:sea"].units is None
    assert data.footprint is None
