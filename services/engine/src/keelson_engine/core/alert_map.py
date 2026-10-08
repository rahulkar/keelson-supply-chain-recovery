"""The alert drawer's map (``docs/contracts/100-m6-signals.md`` §8): a ``network_map@1`` payload.

The hub's exposures name node codes only; the engine knows where the tenant's sites and lanes are
(the same network it pushes to the hub after every scn sync). ``GET /api/alerts/{id}`` adds
``map: {props, data}`` built here:

* the event footprint (Polygon / MultiPolygon parts of the event geometry) as the ``disrupted``
  footprint;
* every exposed site at its network position — ``disrupted`` inside the footprint
  (``intersects``) or named by a connector event (``entity_match``), ``at_risk`` when only within
  the kind radius (``dwithin_km``);
* every exposed lane as a link along its catalogued path (``at_risk``), with its endpoints as
  nodes (``ok`` unless exposed themselves).

Only codes, enums and coordinates are used: no event/alert text (labels are the tenant's own site
names). Everything is validated against the contract models; any problem yields ``None`` and the
web draws the footprint alone.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Final

from keelson_contracts.blocks import MapLegendItem, MapLink, MapNode, MapSummary, NetworkMapData, NetworkMapProps
from keelson_contracts.signals import AlertDetail, TenantNetwork
from keelson_engine.core.logs import get_logger

__all__ = ["MAP_TITLE", "alert_map", "build_alert_map"]

log = get_logger(__name__)

MAP_TITLE: Final = "Where it is"
MAX_PATH_POINTS: Final = 400
"""Each lane path is thinned to at most this many points (the drawer map is a thumbnail)."""
CACHE_TTL_S: Final = 60.0
MIN_PAD_E6: Final = 1_500_000
"""At least 1.5° of context around the framed area."""

_NODE_KIND: Final = {"supplier": "supplier", "factory": "factory", "port": "port", "dc": "dc", "store": "cluster"}
_NODE_STATUS: Final = {"intersects": "disrupted", "entity_match": "disrupted", "dwithin_km": "at_risk",
                       "lane_crosses": "at_risk"}
_STATUS_LABEL: Final = {"disrupted": "In the affected area", "at_risk": "Near the affected area", "ok": "Lane end"}
_RANK: Final = {"disrupted": 0, "at_risk": 1, "ok": 2}

_cache: dict[str, tuple[float, TenantNetwork, dict[str, tuple[str, str]]]] = {}


def _e6(lon: float, lat: float) -> tuple[int, int]:
    return round(max(-180.0, min(180.0, lon)) * 1e6), round(max(-90.0, min(90.0, lat)) * 1e6)


def _polygons(geometry: Any) -> list[list[list[tuple[int, int]]]]:
    """Polygon rings (``[lon_e6, lat_e6]``) of a GeoJSON geometry dict; other parts are skipped."""
    if not isinstance(geometry, Mapping):
        return []
    kind = geometry.get("type")
    if kind == "GeometryCollection":
        return [p for g in geometry.get("geometries") or [] for p in _polygons(g)]
    coords = geometry.get("coordinates")
    polys = [coords] if kind == "Polygon" else coords if kind == "MultiPolygon" else []
    out: list[list[list[tuple[int, int]]]] = []
    for poly in polys or []:
        rings = []
        for ring in poly or []:
            pts = [_e6(float(p[0]), float(p[1])) for p in ring if isinstance(p, Sequence) and len(p) >= 2]
            if len(pts) >= 3 and pts[0] != pts[-1]:
                pts.append(pts[0])
            if len(pts) >= 4:
                rings.append(pts)
        if rings:
            out.append(rings)
    return out


def _thin(path: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    if len(path) <= MAX_PATH_POINTS:
        return list(path)
    step = (len(path) - 1) / (MAX_PATH_POINTS - 1)
    return [path[round(i * step)] for i in range(MAX_PATH_POINTS)]


def _link_kind(mode: str, origin_type: str, dest_type: str) -> str:
    if "air" in mode:
        return "sea_air"
    if origin_type == "dc" and dest_type == "dc":
        return "transfer"
    return "inbound"


def build_alert_map(detail: AlertDetail, network: TenantNetwork,
                    lane_ends: Mapping[str, tuple[str, str]]) -> dict[str, Any] | None:
    """The pure builder (module docstring). ``None`` when there is nothing to draw."""
    nodes_by_code: dict[str, Any] = {}
    nodes_by_key = {(n.type, n.code): n for n in network.nodes}
    for n in network.nodes:
        nodes_by_code.setdefault(n.code, n)
    lanes = {ln.code: ln for ln in network.lanes}

    status: dict[tuple[str, str], str] = {}
    exposed_lanes: list[str] = []
    for e in detail.exposures:
        if e.node_type == "lane":
            if e.node_code in lanes and e.node_code not in exposed_lanes:
                exposed_lanes.append(e.node_code)
            continue
        key = (e.node_type, e.node_code)
        if key not in nodes_by_key:
            continue
        st = _NODE_STATUS.get(e.method, "at_risk")
        if key not in status or _RANK[st] < _RANK[status[key]]:
            status[key] = st

    links: list[MapLink] = []
    for code in sorted(exposed_lanes):
        ends = lane_ends.get(code)
        if ends is None:
            continue
        origin, dest = (nodes_by_code.get(ends[0]), nodes_by_code.get(ends[1]))
        if origin is None or dest is None:
            continue
        for end in (origin, dest):
            status.setdefault((end.type, end.code), "ok")
        lane = lanes[code]
        path = _thin([_e6(lon, lat) for lon, lat in lane.path])
        links.append(MapLink(id=f"lane:{code}", kind=_link_kind(lane.mode, origin.type, dest.type),  # type: ignore[arg-type]
                             from_=f"{origin.type}:{origin.code}", to=f"{dest.type}:{dest.code}", path=path,
                             status="at_risk"))

    nodes: list[MapNode] = []
    for (typ, code), st in sorted(status.items(), key=lambda kv: (_RANK[kv[1]], kv[0])):
        n = nodes_by_key[(typ, code)]
        lon_e6, lat_e6 = _e6(n.lon, n.lat)
        nodes.append(MapNode(id=f"{typ}:{code}", kind=_NODE_KIND[typ], label=n.name or code,  # type: ignore[arg-type]
                             lat_e6=lat_e6, lon_e6=lon_e6, status=st))  # type: ignore[arg-type]

    polygons = _polygons(detail.event.model_dump(mode="json").get("geometry"))
    footprint = None
    if polygons:
        geometry: dict[str, Any] = ({"type": "Polygon", "coordinates": polygons[0]} if len(polygons) == 1
                                    else {"type": "MultiPolygon", "coordinates": polygons})
        footprint = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "geometry": geometry, "properties": {"label": "Affected area", "status": "disrupted"}}]}
    if not nodes and footprint is None:
        return None

    # frame the event, not the world: the footprint + the exposed sites (lanes run off the edge;
    # far lane ends such as European ports would shrink the area to a dot)
    points: list[tuple[int, int]] = [(n.lon_e6, n.lat_e6) for n in nodes if n.status != "ok"]
    points += [p for poly in polygons for ring in poly for p in ring]
    if not points:
        points = [(n.lon_e6, n.lat_e6) for n in nodes] + [p for ln in links for p in ln.path]
    west, east = min(p[0] for p in points), max(p[0] for p in points)
    south, north = min(p[1] for p in points), max(p[1] for p in points)
    pad_x, pad_y = max(MIN_PAD_E6, (east - west) // 4), max(MIN_PAD_E6, (north - south) // 4)
    bbox = (max(-180_000_000, west - pad_x), max(-90_000_000, south - pad_y),
            min(180_000_000, east + pad_x), min(90_000_000, north + pad_y))

    present = sorted({n.status for n in nodes}, key=lambda s: _RANK[s])
    legend = [MapLegendItem(key=s, label=_STATUS_LABEL[s], kind="node") for s in present]
    if links:
        legend.append(MapLegendItem(key="at_risk", label="Lane crossing the affected area", kind="link"))
    if footprint is not None:
        legend.append(MapLegendItem(key="disrupted", label="Affected area", kind="footprint"))
    props = NetworkMapProps(
        title=MAP_TITLE,
        summary=MapSummary(nodes=len(nodes), at_risk_nodes=sum(n.status != "ok" for n in nodes), lanes=len(links)),
        bbox=bbox, legend=legend)
    data = NetworkMapData.model_validate({"nodes": [n.model_dump(mode="json") for n in nodes],
                                         "links": [ln.model_dump(mode="json", by_alias=True) for ln in links],
                                         "footprint": footprint})
    return {"props": props.model_dump(mode="json"), "data": data.model_dump(mode="json", by_alias=True)}


async def _network(tenant: str) -> tuple[TenantNetwork, dict[str, tuple[str, str]]] | None:
    from keelson_engine.core.network import build_tenant_network

    hit = _cache.get(tenant)
    now = time.monotonic()
    if hit is not None and now - hit[0] < CACHE_TTL_S:
        return hit[1], hit[2]
    ends: dict[str, tuple[str, str]] = {}
    network = await build_tenant_network(tenant, lane_ends=ends)
    if network is None:
        return None
    _cache[tenant] = (now, network, ends)
    return network, ends


async def alert_map(tenant: str, detail: AlertDetail) -> dict[str, Any] | None:
    """The drawer map of one alert (best effort: ``None`` on any problem; the web then draws the
    footprint alone)."""
    try:
        loaded = await _network(tenant)
        if loaded is None:
            return None
        return build_alert_map(detail, *loaded)
    except Exception as exc:  # noqa: BLE001 - a map is decoration: never fail the alert read
        log.warning("alert map not built", tenant=tenant, alert=detail.alert.id, error=type(exc).__name__)
        return None


def clear_cache(tenants: Iterable[str] | None = None) -> None:
    for t in list(tenants) if tenants is not None else list(_cache):
        _cache.pop(t, None)
