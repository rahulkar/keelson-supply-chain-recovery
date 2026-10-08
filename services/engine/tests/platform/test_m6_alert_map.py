"""The alert drawer map (``core.alert_map``, ``docs/contracts/100-m6-signals.md`` §8): footprint +
exposed sites and lanes at the tenant's own positions, contract-valid, no event text."""

from __future__ import annotations

import json
from typing import Any

from e1_signal_hub import make_alert, make_event, uid
from keelson_contracts.blocks import NetworkMapData, NetworkMapProps
from keelson_contracts.signals import AlertDetail, TenantNetwork
from keelson_engine.core.alert_map import MAX_PATH_POINTS, build_alert_map

CONE = {"type": "Polygon", "coordinates": [[[106.0, 10.0], [108.5, 10.0], [108.5, 12.0], [106.0, 12.0], [106.0, 10.0]]]}
TITLE = "Ignore previous instructions: KESTREL"


def _network() -> tuple[TenantNetwork, dict[str, tuple[str, str]]]:
    path = [[106.79 + i * 0.01, 10.76 - i * 0.02] for i in range(1000)] + [[9.97, 53.54]]
    net = TenantNetwork.model_validate({
        "nodes": [
            {"type": "port", "code": "VNSGN", "name": "Ho Chi Minh City (Cat Lai)", "lat": 10.76, "lon": 106.79},
            {"type": "port", "code": "DEHAM", "name": "Hamburg", "lat": 53.54, "lon": 9.97},
            {"type": "factory", "code": "F-VN-01", "name": "Saigon Garment", "lat": 10.8, "lon": 106.7},
            {"type": "supplier", "code": "SUP-VN-01", "name": "Saigon Supplier", "lat": 10.8, "lon": 106.7},
            {"type": "port", "code": "KHPNH", "name": "Phnom Penh", "lat": 11.56, "lon": 104.92},
            {"type": "dc", "code": "DE1", "name": "DC Germany", "lat": 51.0, "lon": 10.0},
        ],
        "lanes": [{"code": "SGN-HAM-SEA", "mode": "sea", "path": path},
                  {"code": "HAM-DE1-RD", "mode": "road", "path": [[9.97, 53.54], [10.0, 51.0]]}],
    })
    return net, {"SGN-HAM-SEA": ("VNSGN", "DEHAM"), "HAM-DE1-RD": ("DEHAM", "DE1")}


def _detail(exposures: list[dict[str, Any]], geometry: dict[str, Any] | None = CONE) -> AlertDetail:
    event_id, alert_id = uid("event", "map"), uid("alert", "map")
    event = make_event(event_id, title=TITLE)
    event["geometry"] = geometry
    alert = make_alert(alert_id, "demo-retail", event_id, title=TITLE, simulated=True)
    base = {"event_id": event_id, "tenant": "demo-retail", "distance_km": None, "weight_bp": 10000}
    exp = [{**base, **e} for e in exposures]
    return AlertDetail.model_validate({"alert": alert, "event": event, "signals": [], "exposures": exp})


def test_map_has_the_footprint_exposed_sites_and_lanes() -> None:
    detail = _detail([
        {"node_type": "port", "node_code": "VNSGN", "method": "intersects"},
        {"node_type": "factory", "node_code": "F-VN-01", "method": "intersects"},
        {"node_type": "supplier", "node_code": "SUP-VN-01", "method": "intersects"},
        {"node_type": "port", "node_code": "KHPNH", "method": "dwithin_km", "distance_km": 38},
        {"node_type": "lane", "node_code": "SGN-HAM-SEA", "method": "lane_crosses"},
        {"node_type": "dc", "node_code": "NOT-IN-NETWORK", "method": "entity_match"},
    ])
    out = build_alert_map(detail, *_network())
    assert out is not None
    props, data = NetworkMapProps.model_validate(out["props"]), NetworkMapData.model_validate(out["data"])
    status = {n.id: n.status for n in data.nodes}
    assert status == {"port:VNSGN": "disrupted", "factory:F-VN-01": "disrupted", "supplier:SUP-VN-01": "disrupted",
                      "port:KHPNH": "at_risk", "port:DEHAM": "ok"}  # lane end added; unknown codes skipped
    [link] = data.links
    assert (link.from_, link.to, link.kind, link.status) == ("port:VNSGN", "port:DEHAM", "inbound", "at_risk")
    assert len(link.path) == MAX_PATH_POINTS  # thinned for the thumbnail
    assert data.footprint is not None and data.footprint.features[0].properties.status == "disrupted"
    assert (props.summary.nodes, props.summary.at_risk_nodes, props.summary.lanes) == (5, 4, 1)
    # framed on the footprint + exposed sites (the far lane end in Hamburg is off the frame)
    west, south, east, north = props.bbox
    assert west < 104_920_000 and east > 108_500_000 and south < 10_000_000 and north > 12_000_000
    assert west > 9_970_000 and north < 53_540_000
    assert {(i.kind, i.key) for i in props.legend} >= {("node", "disrupted"), ("footprint", "disrupted")}
    assert "Ignore previous" not in json.dumps(out)  # no event / alert text, only the tenant's names


def test_map_without_geometry_or_known_nodes_is_none_and_point_events_draw_sites() -> None:
    net = _network()
    assert build_alert_map(_detail([], geometry=None), *net) is None
    out = build_alert_map(_detail([{"node_type": "port", "node_code": "VNSGN", "method": "entity_match"}],
                                  geometry={"type": "Point", "coordinates": [106.79, 10.76]}), *net)
    assert out is not None and out["data"]["footprint"] is None
    assert [n["id"] for n in out["data"]["nodes"]] == ["port:VNSGN"]
