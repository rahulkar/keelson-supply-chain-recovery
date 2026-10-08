"""Tenant network for the Signal Hub (``docs/contracts/100-m6-signals.md`` §5).

After every successful scn sync (:func:`on_sync_ready`, called by ``core.scn_sync``) the engine
pushes the tenant's network — ``PUT /v1/tenants/{tenant}/network`` with a
:class:`~keelson_contracts.signals.TenantNetwork` — built deterministically from the tenant's
**current** canonical snapshots (every connector with a ``scn_current`` pointer; on a duplicate
``(type, code)`` the connector first by slug wins):

* nodes ``{type, code, name, lat, lon}``: ``supplier``, ``factory``, ``port`` (+ ``airport`` as
  ``port``, like the network map), ``dc``, and stores **aggregated to clusters** (type ``store``,
  code = the cluster code, positioned at the cluster's own coordinates, else its stores' centroid,
  else its parent DC);
* lanes ``{code, mode, path}``: the lane's stops (origin, catalogued ``via`` ports, destination)
  drawn with ``data/geo/sea_routes.json`` (``runtime.geo.GeoCatalog.stops_path``: sea legs follow
  the CAPE/SUEZ polylines, road/air legs are straight), as float ``[lon, lat]``.

Positions: the snapshot's own ``lat``/``lon`` when present. scn/1.0 lets suppliers/factories be
exported without coordinates (Merxa does); they are then placed — deterministically, documented,
never invented by an LLM — at their own factory (a supplier), else at the tenant's port in the same
country (the one most lanes start from), else at the country centroid
(``data/geo/country_centroids.json``). Ports without coordinates use the catalogue's place. A node
without any position is skipped.

The push is best effort: a hub outage never fails a sync (the next sync pushes again).
"""

from __future__ import annotations

import asyncio
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import text

from keelson_contracts.signals import NetworkLane, NetworkNode, TenantNetwork
from keelson_engine.core.db import tenant_session
from keelson_engine.core.logs import get_logger

__all__ = ["MAX_LANES", "MAX_NODES", "build_tenant_network", "on_sync_ready", "push_alert_rules",
           "push_tenant_network"]

log = get_logger(__name__)

MAX_NODES = 20_000
MAX_LANES = 5_000
PUSH_TIMEOUT_S = 60.0
MAX_CODE_CHARS = 120
"""``keelson_contracts.signals.NodeCode`` max length; longer codes are skipped, never truncated."""

Point = tuple[int, int]
"""``(lon_e6, lat_e6)`` (the geo catalogue's unit)."""

_NODE_TYPE = {"supplier": "supplier", "factory": "factory", "port": "port", "airport": "port", "dc": "dc"}
_TYPE_ORDER = {"supplier": 0, "factory": 1, "port": 2, "dc": 3, "store": 4}

Q_SNAPSHOTS = "SELECT connector, snapshot_id FROM scn_current ORDER BY connector"
Q_LOCATIONS = """
SELECT kind, code, name, country, lat, lon, parent_code
  FROM scn_location
 WHERE tenant = :t AND snapshot_id = :s AND kind <> 'store'
 ORDER BY kind, code
"""
Q_STORE_CENTROIDS = """
SELECT parent_code AS cluster_code, avg(lat) AS lat, avg(lon) AS lon
  FROM scn_location
 WHERE tenant = :t AND snapshot_id = :s AND kind = 'store' AND parent_code IS NOT NULL
   AND lat IS NOT NULL AND lon IS NOT NULL
 GROUP BY parent_code
"""
Q_LANES = """
SELECT code, origin_code, dest_code, mode, via
  FROM scn_lane
 WHERE tenant = :t AND snapshot_id = :s
 ORDER BY code
"""


def _e6(value: float) -> int:
    scaled = float(value) * 1_000_000
    return int(scaled + 0.5) if scaled >= 0 else -int(-scaled + 0.5)


def _point(lat: Any, lon: Any) -> Point | None:
    try:
        la, lo = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(la) and math.isfinite(lo)) or not (-90 <= la <= 90 and -180 <= lo <= 180):
        return None
    return (_e6(lo), _e6(la))


def _deg(point: Point) -> tuple[float, float]:
    """``(lon, lat)`` floats from e6."""
    return (point[0] / 1_000_000, point[1] / 1_000_000)


Rows = Sequence[Mapping[str, Any]]


def _assemble(snapshots: Sequence[tuple[Rows, Rows, Rows]], geo: Any,
              lane_ends: dict[str, tuple[str, str]] | None = None) -> TenantNetwork:
    """Pure assembly from ``[(locations, store_centroids, lanes)]`` per snapshot (see module docstring).
    ``lane_ends`` (optional) is filled with ``{lane code: (origin code, dest code)}`` of the lanes kept."""
    locs: dict[tuple[str, str], Mapping[str, Any]] = {}
    centroids: dict[str, Point] = {}
    lanes: dict[str, Mapping[str, Any]] = {}
    for loc_rows, centroid_rows, lane_rows in snapshots:
        for r in loc_rows:
            locs.setdefault((str(r["kind"]), str(r["code"])), r)
        for r in centroid_rows:
            pt = _point(r["lat"], r["lon"])
            if pt is not None:
                centroids.setdefault(str(r["cluster_code"]), pt)
        for r in lane_rows:
            lanes.setdefault(str(r["code"]), r)

    def own(kind: str, code: str) -> Point | None:
        r = locs.get((kind, code))
        return _point(r.get("lat"), r.get("lon")) if r is not None else None

    # ports: own coordinates, else the catalogue place
    port_pt: dict[str, Point] = {}
    for wanted in ("port", "airport"):  # a port wins over an airport with the same code
        for (kind, code), _r in sorted(locs.items()):
            if kind == wanted and code not in port_pt:
                pt = own(kind, code) or geo.place(code)
                if pt is not None:
                    port_pt[code] = pt
    # the tenant's export port per country: the one most lanes start from (ties: code)
    starts = Counter(str(r["origin_code"]) for r in lanes.values())
    country_port: dict[str, str] = {}
    for (kind, code), r in sorted(locs.items()):
        if kind != "port" or code not in port_pt or not r.get("country"):
            continue
        country = str(r["country"]).upper()
        best = country_port.get(country)
        if best is None or (-starts[code], code) < (-starts[best], best):
            country_port[country] = code

    def country_fallback(r: Mapping[str, Any]) -> Point | None:
        country = str(r.get("country") or "").upper()
        if not country:
            return None
        if country in country_port:
            return port_pt[country_port[country]]
        return geo.centroids.get(country)

    positions: dict[tuple[str, str], Point] = {}
    for (kind, code), r in sorted(locs.items()):
        if kind in ("port", "airport"):
            pt = own(kind, code) or port_pt.get(code)
        elif kind == "dc":
            pt = own(kind, code) or country_fallback(r)
        elif kind == "factory":
            pt = own(kind, code) or country_fallback(r)
        else:
            continue
        if pt is not None:
            positions[(kind, code)] = pt
    factories_of: dict[str, list[str]] = {}
    for (kind, code), r in sorted(locs.items()):
        if kind == "factory" and r.get("parent_code"):
            factories_of.setdefault(str(r["parent_code"]), []).append(code)
    for (kind, code), r in sorted(locs.items()):
        if kind != "supplier":
            continue
        pt = own(kind, code)
        if pt is None:
            pt = next((positions[("factory", f)] for f in factories_of.get(code, []) if ("factory", f) in positions),
                      None)
        if pt is None:
            pt = country_fallback(r)
        if pt is not None:
            positions[(kind, code)] = pt
    for (kind, code), r in sorted(locs.items()):
        if kind != "cluster":
            continue
        pt = own(kind, code) or centroids.get(code)
        if pt is None and r.get("parent_code"):
            pt = positions.get(("dc", str(r["parent_code"])))
        if pt is not None:
            positions[(kind, code)] = pt

    nodes: dict[tuple[str, str], NetworkNode] = {}
    kind_rank = {"supplier": 0, "factory": 1, "port": 2, "airport": 3, "dc": 4, "cluster": 5}
    for (kind, code), pt in sorted(positions.items(), key=lambda kv: (kind_rank.get(kv[0][0], 9), kv[0][1])):
        node_type = "store" if kind == "cluster" else _NODE_TYPE.get(kind)
        if node_type is None or (node_type, code) in nodes:
            continue  # an airport and a port sharing a code: the port wins
        if len(code) > MAX_CODE_CHARS:
            continue  # never truncated: two cut codes could collide and fail the whole push
        lon, lat = _deg(pt)
        r = locs[(kind, code)]
        nodes[(node_type, code)] = NetworkNode(type=node_type, code=code,  # type: ignore[arg-type]
                                               name=str(r.get("name") or code)[:200], lat=lat, lon=lon)

    # any location by code (lane endpoints may be ports, factories or DCs)
    by_code: dict[str, tuple[Point, bool]] = {}
    for kind in ("port", "airport", "dc", "factory", "supplier", "cluster"):
        for (k, code), pt in sorted(positions.items()):
            if k == kind:
                by_code.setdefault(code, (pt, kind in ("port", "airport")))
    out_lanes: list[NetworkLane] = []
    for code, r in sorted(lanes.items()):
        origin, dest = str(r["origin_code"]), str(r["dest_code"])
        start, end = by_code.get(origin), by_code.get(dest)
        if start is None or end is None:
            continue
        via = [str(v) for v in (r.get("via") or [])]
        stops: list[tuple[str, Point, bool]] = [(origin, start[0], start[1])]
        for v in via:
            hit = port_pt.get(v)
            if hit is None and geo.place_kind(v) == "port":
                hit = geo.place(v)
            if hit is not None:
                stops.append((v, hit, True))
        stops.append((dest, end[0], end[1]))
        mode = str(r.get("mode") or "sea")[:32] or "sea"
        path = geo.stops_path(stops, mode=mode, via=via)
        positions_deg = [_deg(p) for p in path][:5000]
        if len(positions_deg) < 2:
            continue
        if len(code) > MAX_CODE_CHARS:
            continue
        out_lanes.append(NetworkLane(code=code, mode=mode, path=positions_deg))
        if lane_ends is not None:
            lane_ends[code] = (origin, dest)

    node_list = sorted(nodes.values(), key=lambda n: (_TYPE_ORDER[n.type], n.code))
    if len(node_list) > MAX_NODES or len(out_lanes) > MAX_LANES:
        log.warning("tenant network truncated", nodes=len(node_list), lanes=len(out_lanes))
    return TenantNetwork(nodes=node_list[:MAX_NODES], lanes=out_lanes[:MAX_LANES])


async def build_tenant_network(tenant: str,
                               lane_ends: dict[str, tuple[str, str]] | None = None) -> TenantNetwork | None:
    """The tenant's network from its current snapshots; ``None`` when it has no snapshot yet
    (``lane_ends``: see :func:`_assemble`)."""
    from keelson_engine.runtime.geo import get_geo

    snaps: list[tuple[Rows, Rows, Rows]] = []
    async with tenant_session(tenant, snapshot=True) as s:
        current = (await s.execute(text(Q_SNAPSHOTS))).all()
        for _connector, sid in current:
            p = {"t": tenant, "s": sid}
            locs = [dict(m) for m in (await s.execute(text(Q_LOCATIONS), p)).mappings().all()]
            cents = [dict(m) for m in (await s.execute(text(Q_STORE_CENTROIDS), p)).mappings().all()]
            lanes = [dict(m) for m in (await s.execute(text(Q_LANES), p)).mappings().all()]
            snaps.append((locs, cents, lanes))
    if not snaps:
        return None
    return _assemble(snaps, get_geo(), lane_ends)


async def push_tenant_network(tenant: str) -> dict[str, int] | None:
    """Build + ``PUT`` the network; ``None`` when there is nothing to push or the hub is off.
    Raises :class:`~keelson_engine.core.signal_hub.HubError` on a hub failure."""
    from keelson_engine.core import signal_hub

    if not signal_hub.is_configured():
        return None
    network = await build_tenant_network(tenant)
    if network is None:
        return None
    await signal_hub.put_network(tenant, network)
    counts = {"nodes": len(network.nodes), "lanes": len(network.lanes)}
    await push_alert_rules(tenant)
    from keelson_engine.core.signals_admin import push_settings

    try:  # M7: source subscriptions + watch regions (best effort)
        await push_settings(tenant)
    except Exception as exc:  # noqa: BLE001 - never fails the network push
        log.warning("signals settings push failed", tenant=tenant, error=type(exc).__name__)
    log.info("tenant network pushed to the signal hub", tenant=tenant, **counts)
    return counts


async def push_alert_rules(tenant: str) -> bool:
    """Push the tenant's own alert rules (``tenant.config.alert_rules``; ``[]`` = the defaults) so the
    hub raises alerts with the same rules the engine's auto-open evaluates. Best effort: returns
    False (logged) when the hub refused or is unavailable; the next sync pushes again."""
    from keelson_engine.core import alerts, signal_hub

    try:
        await signal_hub.put_alert_rules(tenant, list(await alerts.custom_rules(tenant)))
    except signal_hub.HubError as exc:
        log.warning("alert rules not pushed to the signal hub", tenant=tenant, error=exc.code, status=exc.status)
        return False
    return True


async def on_sync_ready(tenant: str, connector: str) -> None:
    """``core.scn_sync`` hook after a successful sync: push, never raise."""
    from keelson_engine.core import signal_hub

    if not signal_hub.is_configured():
        return
    try:
        async with asyncio.timeout(PUSH_TIMEOUT_S):
            await push_tenant_network(tenant)
    except Exception as exc:  # noqa: BLE001 - a hub outage never fails a sync; the next sync pushes again
        log.warning("tenant network push failed", tenant=tenant, connector=connector, error=type(exc).__name__)
