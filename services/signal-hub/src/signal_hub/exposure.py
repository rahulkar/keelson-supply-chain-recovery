"""Exposure of a tenant network to an event (``100`` §5) — PostGIS geo-match + entity match.

Per (event, tenant), in the tenant's RLS context:

* nodes inside the event geometry → ``intersects`` (distance 0, weight 10000);
* nodes within the event's kind radius (``event.radius_km``: TC 300, EQ 250/100, FL 50, port 10 …)
  → ``dwithin_km`` (whole km, half-up; weight 10000 → 2000 linearly with distance/radius);
* lanes whose path crosses the geometry → ``lane_crosses`` (distance 0, weight 9000), or passes within
  :data:`LANE_BUFFER_KM` of it (e.g. a lane starting at a closed port point; weight 7000);
* ``location_refs`` of the event's current member signals naming a tenant node or lane
  (``UNLOCODE:`` port, ``SUPPLIER:``, ``FACTORY:``, ``DC:``, ``STORE:``, ``LANE:``) → ``entity_match``
  (weight 10000);
* the public organisation-name keys of a current member (``signal.match_keys``, OFAC SDN names)
  equal to a tenant supplier/factory ``name_key`` → ``entity_match`` (weight 10000; ``124`` §1);
* the tenant's enabled **watch regions** (``110`` §5, :mod:`signal_hub.admin`): a ``radius`` region
  exposes its named node within ``radius_km`` of the footprint (``dwithin_km``, weight 10000 → 2000
  over the region radius; a lane → ``lane_crosses`` 7000); a ``polygon`` region touched by the
  footprint exposes the tenant nodes inside it (``dwithin_km`` with the real distance) and the lanes
  crossing it (``lane_crosses``), weight :data:`~signal_hub.admin.WATCH_POLYGON_WEIGHT_BP`.

One exposure per (node_type, node_code); precedence intersects > entity_match > lane_crosses >
dwithin_km. All numbers come from the tenant network snapshot; nothing comes from an LLM.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

import asyncpg

from keelson_contracts.signals import Exposure

__all__ = [
    "LANE_BUFFER_KM",
    "REF_NODE_TYPES",
    "WATCH_POLYGON_WEIGHT_BP",
    "compute_exposures",
    "merge_matches",
    "persist_exposures",
]

LANE_BUFFER_KM: Final = 25
WATCH_POLYGON_WEIGHT_BP: Final = 5000
REF_NODE_TYPES: Final[dict[str, str]] = {
    "UNLOCODE": "port",
    "SUPPLIER": "supplier",
    "FACTORY": "factory",
    "DC": "dc",
    "STORE": "store",
    "LANE": "lane",
}
_PRECEDENCE: Final[dict[str, int]] = {"intersects": 0, "entity_match": 1, "lane_crosses": 2, "dwithin_km": 3}


def _km(meters: float) -> int:
    return int(Decimal(str(meters / 1000.0)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _dwithin_weight(distance_m: float, radius_km: int) -> int:
    if radius_km <= 0:
        return 10_000
    ratio = min(1.0, max(0.0, distance_m / (radius_km * 1000.0)))
    return int(Decimal(str(10_000 - 8000 * ratio)).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def merge_matches(event_id: str, tenant: str, matches: Iterable[dict[str, Any]]) -> list[Exposure]:
    """Keep the strongest match per (node_type, node_code); deterministic order."""
    best: dict[tuple[str, str], dict[str, Any]] = {}
    for m in matches:
        key = (m["node_type"], m["node_code"])
        cur = best.get(key)
        if cur is None or (_PRECEDENCE[m["method"]], -m["weight_bp"]) < (_PRECEDENCE[cur["method"]], -cur["weight_bp"]):
            best[key] = m
    return [
        Exposure(event_id=event_id, tenant=tenant, **best[key])
        for key in sorted(best)
    ]


async def compute_exposures(conn: asyncpg.Connection, event_id: str, tenant: str) -> list[Exposure]:
    """Exposures of ``tenant`` to ``event_id`` (``conn`` must be in that tenant's context)."""
    ev = await conn.fetchrow("SELECT id, geog IS NOT NULL AS has_geog, radius_km FROM event WHERE id = $1", event_id)
    if ev is None:
        return []
    matches: list[dict[str, Any]] = []
    if ev["has_geog"]:
        for r in await conn.fetch(
            """
            SELECT n.type, n.code, ST_Intersects(n.geog, e.geog) AS inside, ST_Distance(n.geog, e.geog) AS dist_m
            FROM event e JOIN network_node n ON n.tenant = $2
            WHERE e.id = $1 AND ST_DWithin(n.geog, e.geog, e.radius_km * 1000.0)
            """,
            event_id, tenant,
        ):
            if r["inside"]:
                matches.append({"node_type": r["type"], "node_code": r["code"], "method": "intersects",
                                "distance_km": 0, "weight_bp": 10_000})
            else:
                matches.append({"node_type": r["type"], "node_code": r["code"], "method": "dwithin_km",
                                "distance_km": _km(r["dist_m"]), "weight_bp": _dwithin_weight(r["dist_m"], ev["radius_km"])})
        for r in await conn.fetch(
            """
            SELECT l.code, ST_Intersects(l.path, e.geog) AS crosses, ST_Distance(l.path, e.geog) AS dist_m
            FROM event e JOIN network_lane l ON l.tenant = $2
            WHERE e.id = $1 AND ST_DWithin(l.path, e.geog, $3 * 1000.0)
            """,
            event_id, tenant, LANE_BUFFER_KM,
        ):
            matches.append({"node_type": "lane", "node_code": r["code"], "method": "lane_crosses",
                            "distance_km": 0 if r["crosses"] else _km(r["dist_m"]),
                            "weight_bp": 9000 if r["crosses"] else 7000})
        matches += await _watch_region_matches(conn, event_id, tenant)
    refs = [r["ref"] for r in await conn.fetch(
        "SELECT DISTINCT unnest(location_refs) AS ref FROM signal WHERE event_id = $1 AND is_current", event_id
    )]
    wanted: dict[str, set[str]] = {}
    for ref in refs:
        scheme, _, code = ref.partition(":")
        node_type = REF_NODE_TYPES.get(scheme)
        if node_type:
            wanted.setdefault(node_type, set()).add(code)
    lane_codes = sorted(wanted.pop("lane", set()))
    if lane_codes:
        for r in await conn.fetch("SELECT code FROM network_lane WHERE tenant = $1 AND code = ANY($2::text[])", tenant, lane_codes):
            matches.append({"node_type": "lane", "node_code": r["code"], "method": "entity_match",
                            "distance_km": None, "weight_bp": 10_000})
    codes = sorted({c for cs in wanted.values() for c in cs})
    if codes:
        for r in await conn.fetch(
            "SELECT type, code FROM network_node WHERE tenant = $1 AND code = ANY($2::text[])", tenant, codes
        ):
            if r["code"] in wanted.get(r["type"], set()):
                matches.append({"node_type": r["type"], "node_code": r["code"], "method": "entity_match",
                                "distance_km": None, "weight_bp": 10_000})
    matches += await _name_matches(conn, event_id, tenant)
    return merge_matches(event_id, tenant, matches)


async def _name_matches(conn: asyncpg.Connection, event_id: str, tenant: str) -> list[dict[str, Any]]:
    """``entity_match`` by organisation name (``124`` §1): a current member's public ``match_keys``
    (OFAC SDN names) equal to the ``name_key`` of one of the tenant's suppliers or factories. Runs
    in the tenant's context, so only its own nodes are compared and no tenant code ever enters a
    global row."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT n.type, n.code
        FROM signal s
        JOIN network_node n ON n.tenant = $2 AND n.name_key = ANY(s.match_keys)
        WHERE s.event_id = $1 AND s.is_current AND cardinality(s.match_keys) > 0
          AND n.type IN ('supplier', 'factory')
        """,
        event_id, tenant,
    )
    return [{"node_type": r["type"], "node_code": r["code"], "method": "entity_match", "distance_km": None,
             "weight_bp": 10_000} for r in rows]


async def _watch_region_matches(conn: asyncpg.Connection, event_id: str, tenant: str) -> list[dict[str, Any]]:
    """Exposure through the tenant's enabled watch regions (the event has a footprint)."""
    matches: list[dict[str, Any]] = []
    for r in await conn.fetch(
        """
        SELECT w.radius_km, n.type, n.code, ST_Distance(n.geog, e.geog) AS dist_m
        FROM watch_region w
        JOIN network_node n ON n.tenant = w.tenant AND n.type = w.node_type AND n.code = w.node_code
        JOIN event e ON e.id = $1
        WHERE w.tenant = $2 AND w.enabled AND w.kind = 'radius' AND w.node_type <> 'lane'
          AND ST_DWithin(n.geog, e.geog, w.radius_km * 1000.0)
        """,
        event_id, tenant,
    ):
        dist = float(r["dist_m"])
        if dist <= 0:
            matches.append({"node_type": r["type"], "node_code": r["code"], "method": "intersects",
                            "distance_km": 0, "weight_bp": 10_000})
        else:
            matches.append({"node_type": r["type"], "node_code": r["code"], "method": "dwithin_km",
                            "distance_km": _km(dist), "weight_bp": _dwithin_weight(dist, int(r["radius_km"]))})
    for r in await conn.fetch(
        """
        SELECT l.code, ST_Intersects(l.path, e.geog) AS crosses, ST_Distance(l.path, e.geog) AS dist_m
        FROM watch_region w
        JOIN network_lane l ON l.tenant = w.tenant AND l.code = w.node_code
        JOIN event e ON e.id = $1
        WHERE w.tenant = $2 AND w.enabled AND w.kind = 'radius' AND w.node_type = 'lane'
          AND ST_DWithin(l.path, e.geog, w.radius_km * 1000.0)
        """,
        event_id, tenant,
    ):
        matches.append({"node_type": "lane", "node_code": r["code"], "method": "lane_crosses",
                        "distance_km": 0 if r["crosses"] else _km(r["dist_m"]),
                        "weight_bp": 9000 if r["crosses"] else 7000})
    for r in await conn.fetch(
        """
        SELECT DISTINCT n.type, n.code, ST_Distance(n.geog, e.geog) AS dist_m
        FROM watch_region w
        JOIN event e ON e.id = $1
        JOIN network_node n ON n.tenant = w.tenant AND ST_Covers(w.geog, n.geog)
        WHERE w.tenant = $2 AND w.enabled AND w.kind = 'polygon' AND ST_Intersects(w.geog, e.geog)
          AND (cardinality(w.node_types) = 0 OR n.type = ANY(w.node_types))
        """,
        event_id, tenant,
    ):
        dist = float(r["dist_m"])
        if dist <= 0:
            matches.append({"node_type": r["type"], "node_code": r["code"], "method": "intersects",
                            "distance_km": 0, "weight_bp": 10_000})
        else:
            matches.append({"node_type": r["type"], "node_code": r["code"], "method": "dwithin_km",
                            "distance_km": _km(dist), "weight_bp": WATCH_POLYGON_WEIGHT_BP})
    for r in await conn.fetch(
        """
        SELECT DISTINCT l.code, ST_Intersects(l.path, e.geog) AS crosses, ST_Distance(l.path, e.geog) AS dist_m
        FROM watch_region w
        JOIN event e ON e.id = $1
        JOIN network_lane l ON l.tenant = w.tenant AND ST_Intersects(w.geog, l.path)
        WHERE w.tenant = $2 AND w.enabled AND w.kind = 'polygon' AND ST_Intersects(w.geog, e.geog)
          AND (cardinality(w.node_types) = 0 OR 'lane' = ANY(w.node_types))
        """,
        event_id, tenant,
    ):
        matches.append({"node_type": "lane", "node_code": r["code"], "method": "lane_crosses",
                        "distance_km": 0 if r["crosses"] else _km(r["dist_m"]),
                        "weight_bp": 9000 if r["crosses"] else WATCH_POLYGON_WEIGHT_BP})
    return matches


async def persist_exposures(
    conn: asyncpg.Connection, event_id: str, tenant: str, exposures: list[Exposure], now: dt.datetime
) -> None:
    await conn.execute("DELETE FROM exposure WHERE tenant = $1 AND event_id = $2", tenant, event_id)
    if exposures:
        await conn.executemany(
            """
            INSERT INTO exposure (tenant, event_id, node_type, node_code, method, distance_km, weight_bp, computed_at)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
            """,
            [(tenant, event_id, e.node_type, e.node_code, e.method, e.distance_km, e.weight_bp, now) for e in exposures],
        )
