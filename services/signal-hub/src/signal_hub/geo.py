"""GeoJSON helpers: position counting, size-capping simplification, flattening and merging.

Geometry arriving from feeds is reduced to at most :data:`MAX_POSITIONS_PER_GEOMETRY` positions
*before* contract-model validation (the contract caps a geometry at 50 000 positions and checks
only after parsing the whole tree). Positions are rounded to 5 decimals (~1 m) and altitude is
dropped.
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any, Final

__all__ = [
    "EARTH_RADIUS_KM",
    "MAX_POSITIONS_PER_GEOMETRY",
    "circle_polygon",
    "clean_geometry",
    "count_positions",
    "distance_to_ring_km",
    "flatten",
    "haversine_km",
    "merge",
    "point_in_ring",
]

MAX_POSITIONS_PER_GEOMETRY: Final = 4000
MAX_MERGED_POSITIONS: Final = 20_000
_SIMPLE = {"Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"}


def count_positions(geometry: Any) -> int:
    if not isinstance(geometry, dict):
        return 0
    if geometry.get("type") == "GeometryCollection":
        return sum(count_positions(g) for g in geometry.get("geometries") or [])
    total = 0

    def walk(node: Any) -> None:
        nonlocal total
        if isinstance(node, list) and node and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in node):
            total += 1
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(geometry.get("coordinates"))
    return total


def _pos(node: Any) -> list[float] | None:
    if not isinstance(node, (list, tuple)) or len(node) < 2:
        return None
    lon, lat = node[0], node[1]
    if isinstance(lon, bool) or isinstance(lat, bool) or not isinstance(lon, (int, float)) or not isinstance(lat, (int, float)):
        return None
    lon, lat = float(lon), float(lat)
    if not (math.isfinite(lon) and math.isfinite(lat)) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
        return None
    return [round(lon, 5), round(lat, 5)]


def _line(coords: Any, *, step: int) -> list[list[float]] | None:
    if not isinstance(coords, list):
        return None
    pts = [p for p in (_pos(c) for c in coords) if p is not None]
    if step > 1 and len(pts) > 2:
        pts = pts[::step] + ([pts[-1]] if (len(pts) - 1) % step else [])
    # drop consecutive duplicates
    out: list[list[float]] = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    return out if len(out) >= 2 else None


def _ring(coords: Any, *, step: int) -> list[list[float]] | None:
    pts = _line(coords, step=step)
    if pts is None:
        return None
    if pts[0] != pts[-1]:
        pts.append(pts[0])
    return pts if len(pts) >= 4 else None


def _clean_simple(geometry: dict[str, Any], step: int) -> dict[str, Any] | None:
    gtype, coords = geometry.get("type"), geometry.get("coordinates")
    if gtype == "Point":
        p = _pos(coords)
        return {"type": "Point", "coordinates": p} if p else None
    if gtype == "MultiPoint":
        pts = [p for p in (_pos(c) for c in coords or []) if p]
        return {"type": "MultiPoint", "coordinates": pts} if pts else None
    if gtype == "LineString":
        line = _line(coords, step=step)
        return {"type": "LineString", "coordinates": line} if line else None
    if gtype == "MultiLineString":
        lines = [ln for ln in (_line(c, step=step) for c in coords or []) if ln]
        return {"type": "MultiLineString", "coordinates": lines} if lines else None
    if gtype == "Polygon":
        rings = [r for r in (_ring(c, step=step) for c in coords or []) if r]
        return {"type": "Polygon", "coordinates": rings} if rings else None
    if gtype == "MultiPolygon":
        polys = []
        for poly in coords or []:
            rings = [r for r in (_ring(c, step=step) for c in poly or []) if r]
            if rings:
                polys.append(rings)
        return {"type": "MultiPolygon", "coordinates": polys} if polys else None
    return None


def flatten(geometry: Any) -> list[dict[str, Any]]:
    """Simple geometries of ``geometry`` (collections flattened, one level deep or more)."""
    if not isinstance(geometry, dict):
        return []
    if geometry.get("type") == "GeometryCollection":
        return [g for sub in geometry.get("geometries") or [] for g in flatten(sub)]
    return [geometry] if geometry.get("type") in _SIMPLE else []


def clean_geometry(geometry: Any, *, max_positions: int = MAX_POSITIONS_PER_GEOMETRY) -> dict[str, Any] | None:
    """A valid, size-capped copy of a GeoJSON geometry (or None when nothing usable remains).

    Invalid positions are dropped, rings are closed, altitude is removed, nested collections are
    flattened, and lines/rings are decimated (every n-th vertex, endpoints kept) until the whole
    geometry has at most ``max_positions`` positions.
    """
    parts = flatten(geometry)
    if not parts:
        return None
    total = sum(count_positions(p) for p in parts) or 1
    step = max(1, math.ceil(total / max_positions))
    for _ in range(12):
        cleaned = [c for c in (_clean_simple(p, step) for p in parts) if c is not None]
        if not cleaned:
            return None
        result = cleaned[0] if len(cleaned) == 1 else {"type": "GeometryCollection", "geometries": cleaned}
        if count_positions(result) <= max_positions:
            return result
        step *= 2
    return None


def merge(geometries: list[dict[str, Any]], *, max_positions: int = MAX_MERGED_POSITIONS) -> dict[str, Any] | None:
    """One geometry for several (an event footprint): a single one as is, else a flat collection."""
    parts: list[dict[str, Any]] = []
    for g in geometries:
        for p in flatten(g):
            if p not in parts:
                parts.append(p)
    if not parts:
        return None
    merged = parts[0] if len(parts) == 1 else {"type": "GeometryCollection", "geometries": parts}
    return clean_geometry(merged, max_positions=max_positions)


# ───────────── point / ring helpers (M8 feeds, ``124`` §1; pure Python, no PostGIS) ─────────────

EARTH_RADIUS_KM: Final = 6371.0088


def haversine_km(lon1: float, lat1: float, lon2: float, lat2: float) -> float:
    """Great-circle distance between two WGS84 positions."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def point_in_ring(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> bool:
    """Even-odd test of a position against a closed ``[[lon, lat], …]`` ring (planar, small rings)."""
    inside = False
    for (x1, y1), (x2, y2) in itertools.pairwise((p[0], p[1]) for p in ring):
        if (y1 > lat) != (y2 > lat) and lon < (x2 - x1) * (lat - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def distance_to_ring_km(lon: float, lat: float, ring: Sequence[Sequence[float]]) -> float:
    """0 inside the ring, else the distance to its nearest edge (local equirectangular projection
    centred on the position: accurate to well under 1 % at the tens-of-km buffers it is used for)."""
    if point_in_ring(lon, lat, ring):
        return 0.0
    kx, ky = 111.320 * math.cos(math.radians(lat)), 110.574
    best = math.inf
    for (x1, y1), (x2, y2) in itertools.pairwise((p[0], p[1]) for p in ring):
        ax, ay, bx, by = (x1 - lon) * kx, (y1 - lat) * ky, (x2 - lon) * kx, (y2 - lat) * ky
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def circle_polygon(lon: float, lat: float, radius_km: float, *, vertices: int = 32) -> dict[str, Any] | None:
    """A counter-clockwise closed ``vertices``-gon approximating a circle (CAP ``<circle>``)."""
    if not (math.isfinite(radius_km) and 0 < radius_km <= 2000) or not (-90 < lat < 90):
        return None
    kx, ky = 111.320 * math.cos(math.radians(lat)), 110.574
    ring = []
    for i in range(vertices):
        a = 2 * math.pi * i / vertices
        ring.append([round(lon + radius_km * math.cos(a) / kx, 5), round(lat + radius_km * math.sin(a) / ky, 5)])
    ring.append(ring[0])
    return clean_geometry({"type": "Polygon", "coordinates": [ring]})
