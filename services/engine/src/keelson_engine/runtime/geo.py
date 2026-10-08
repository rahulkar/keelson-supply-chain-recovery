"""Map geometry for the ``network_map@1`` block (``80-m3-deterministic-core.md`` §5).

* Sea lanes follow the hand-made waypoint polylines of ``data/geo/sea_routes.json``
  (Chittagong / Colombo / Ho Chi Minh City / Jebel Ali → Felixstowe / Rotterdam / Hamburg / Gdańsk
  via the Cape or via Suez, sea legs into the Jebel Ali sea-air hub, Tanger Med and Istanbul
  short-sea). A route is looked up by ``(origin, destination, chokepoint)`` and may be used
  reversed; the chokepoint comes from the lane's ``via`` (``CAPE`` / ``SUEZ``; Suez is preferred
  when the lane names neither).
* Road and air lanes are straight. A sea-air lane is the sea leg into its hub (the first ``via``
  entry with a catalogued route, e.g. Jebel Ali ``AEJEA``) followed by the air leg to the DC:
  :meth:`GeoCatalog.stops_path` keeps that air leg straight (one polyline), while
  :meth:`GeoCatalog.sea_air_legs` returns the sea leg and an **air arc** (:func:`air_arc`, a
  deterministic curved polyline; M4 ``90`` §10) separately, so the network map draws the sea leg as a
  path into the hub and the sea-air link itself as an arc from the hub to the DC.
* Short-sea: a sea lane whose destination is a DC (no destination port on the lane) follows the
  catalogued route from its origin port to the catalogued port nearest the DC, then a straight leg
  (near-shore Istanbul / Tanger Med lanes when they are modelled as sea lanes).
* ``data/geo/chokepoints.json`` (M4 ``90`` §1): chokepoint codes (the ``via`` codes of inbound
  lanes), names, aliases and an approximate polygon; :meth:`GeoCatalog.resolve_chokepoint` resolves a
  mention deterministically (code or alias, case-insensitive).
* ``data/geo/country_centroids.json`` gives fallback positions for locations exported without
  coordinates (``scn/1.0`` factories and suppliers), nudged deterministically per code so that
  several factories in one country do not overlap.

The files are looked up in ``$GEO_DIR``, ``/app/geo`` and the source tree's ``data/geo``; without
them every lane is a straight line (the map still renders). Positions are integer degrees × 1e6
(``[lon_e6, lat_e6]``), as in the block contract.
"""

from __future__ import annotations

import functools
import hashlib
import json
import math
import os
import re
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

__all__ = [
    "AIR_ARC_POINTS",
    "Chokepoint",
    "GeoCatalog",
    "Point",
    "air_arc",
    "bbox_of",
    "distance_km",
    "e6",
    "get_geo",
    "normalize_name",
    "parse_chokepoints",
    "set_geo",
]

log = structlog.get_logger(__name__)

Point = tuple[int, int]
"""``(lon_e6, lat_e6)``."""

CHOKEPOINT_PREFERENCE = ("SUEZ", "CAPE")
"""Route alternative used when a sea lane's ``via`` names neither chokepoint."""


def e6(degrees: float) -> int:
    """Degrees → integer degrees × 1e6 (half away from zero)."""
    scaled = float(degrees) * 1_000_000
    return int(scaled + 0.5) if scaled >= 0 else -int(-scaled + 0.5)


def _clamp(point: Point) -> Point:
    lon, lat = point
    return (max(-180_000_000, min(180_000_000, lon)), max(-90_000_000, min(90_000_000, lat)))


def bbox_of(points: Iterable[Point], *, pad_e6: int = 1_000_000) -> tuple[int, int, int, int]:
    """``[west, south, east, north]`` (e6) around ``points`` with a margin; world box when empty."""
    pts = list(points)
    if not pts:
        return (-180_000_000, -90_000_000, 180_000_000, 90_000_000)
    west = min(p[0] for p in pts) - pad_e6
    east = max(p[0] for p in pts) + pad_e6
    south = min(p[1] for p in pts) - pad_e6
    north = max(p[1] for p in pts) + pad_e6
    w, s = _clamp((west, south))
    e, n = _clamp((east, north))
    return (w, s, e, n)


AIR_ARC_POINTS = 17
"""Points of an air arc polyline (both ends included)."""
AIR_ARC_BULGE_BP = 1800
"""Arc height as a share of the chord length (basis points): a visible but modest curve."""

_NAME_JUNK = re.compile(r"[^0-9a-z]+")


def normalize_name(text: str) -> str:
    """Lower-case, accents folded, punctuation as spaces, a leading ``the`` dropped."""
    folded = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode("ascii").lower()
    words = _NAME_JUNK.sub(" ", folded).split()
    if words and words[0] == "the":
        words = words[1:]
    return " ".join(words)


def distance_km(a: Point, b: Point) -> float:
    """Great-circle distance between two e6 points (km)."""
    lon1, lat1, lon2, lat2 = (math.radians(v / 1_000_000) for v in (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0 * math.asin(min(1.0, math.sqrt(h)))


def air_arc(start: Point, end: Point, *, points: int = AIR_ARC_POINTS) -> list[Point]:
    """A curved air leg ``start`` → ``end``: a quadratic Bézier whose control point sits on the
    chord's perpendicular bisector, to the right of the direction of travel (poleward for the usual
    north-west-bound legs from the Gulf into Europe). Integer e6 points, exact ends, deterministic."""
    if start == end:
        return [start, end]
    (x0, y0), (x2, y2) = start, end
    dx, dy = x2 - x0, y2 - y0
    # right-hand normal (dy, -dx) scaled to AIR_ARC_BULGE_BP of the chord (×2: a quadratic Bézier
    # reaches half its control point's offset)
    k = 2 * AIR_ARC_BULGE_BP / 10000
    cx = (x0 + x2) / 2 + dy * k
    cy = (y0 + y2) / 2 - dx * k
    out: list[Point] = []
    n = max(2, points) - 1
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        x = u * u * x0 + 2 * u * t * cx + t * t * x2
        y = u * u * y0 + 2 * u * t * cy + t * t * y2
        out.append(_clamp((int(round(x)), int(round(y)))))
    out[0], out[-1] = start, end
    return _dedupe(out)


@dataclass(frozen=True)
class Chokepoint:
    code: str
    name: str
    aliases: tuple[str, ...]
    polygon: tuple[Point, ...]
    """Closed ring of e6 points (first == last); empty when the file gives none."""


@dataclass(frozen=True)
class _Route:
    origin: str
    dest: str
    via: str | None
    path: tuple[Point, ...]


@dataclass
class GeoCatalog:
    """Sea routes, named places (ports / chokepoints) and country centroids."""

    places: dict[str, tuple[str, str, Point]] = field(default_factory=dict)
    """code → (name, kind, position)."""
    routes: list[_Route] = field(default_factory=list)
    centroids: dict[str, Point] = field(default_factory=dict)
    source: str | None = None
    chokepoints: dict[str, Chokepoint] = field(default_factory=dict)
    """M4: chokepoint code → names, aliases, polygon (``data/geo/chokepoints.json``)."""

    @classmethod
    def from_documents(cls, routes_doc: dict[str, Any] | None, centroids_doc: dict[str, Any] | None,
                       *, source: str | None = None,
                       chokepoints_doc: dict[str, Any] | None = None) -> GeoCatalog:
        cat = cls(source=source)
        if chokepoints_doc:
            cat.chokepoints = parse_chokepoints(chokepoints_doc)
        if routes_doc:
            for code, place in (routes_doc.get("places") or {}).items():
                cat.places[str(code)] = (
                    str(place.get("name") or code),
                    str(place.get("kind") or "port"),
                    (e6(place["lon"]), e6(place["lat"])),
                )
            for r in routes_doc.get("routes") or []:
                path = tuple((e6(lon), e6(lat)) for lon, lat in r["path"])
                if len(path) >= 2:
                    cat.routes.append(_Route(str(r["from"]), str(r["to"]), r.get("via"), path))
        if centroids_doc:
            for code, (lon, lat) in (centroids_doc.get("centroids") or {}).items():
                cat.centroids[str(code).upper()] = (e6(lon), e6(lat))
        return cat

    # ───────────────────────────── places ─────────────────────────────

    def place(self, code: str) -> Point | None:
        hit = self.places.get(code)
        return hit[2] if hit else None

    def place_name(self, code: str) -> str | None:
        hit = self.places.get(code)
        return hit[0] if hit else None

    def place_kind(self, code: str) -> str | None:
        hit = self.places.get(code)
        return hit[1] if hit else None

    def country_point(self, country: str | None, *, key: str) -> Point | None:
        """Country centroid nudged by up to ±0.6° per ``key`` (deterministic, stable per code)."""
        if not country:
            return None
        base = self.centroids.get(country.upper())
        if base is None:
            return None
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        dx = (int.from_bytes(digest[:2], "big") % 1_200_001) - 600_000
        dy = (int.from_bytes(digest[2:4], "big") % 1_200_001) - 600_000
        return _clamp((base[0] + dx, base[1] + dy))

    # ─────────────────────────── chokepoints ───────────────────────────

    def resolve_chokepoint(self, text: str, *, exact: bool = False) -> Chokepoint | None:
        """The chokepoint a mention names (``None`` = none): the code, name or an alias matched on
        the normalised text (:func:`normalize_name`) exactly, or (unless ``exact``) the name or an
        alias as a whole-word phrase inside the mention ("Suez Canal closure"); the longest
        matching alias wins, ties by code.

        The bare *code* only matches exactly: as a phrase it would capture unrelated places
        ("Cape Town" is not the Cape of Good Hope ``CAPE``)."""
        wanted = normalize_name(text)
        if not wanted:
            return None
        padded = f" {wanted} "
        best: tuple[int, str] | None = None
        for code, cp in sorted(self.chokepoints.items()):
            if normalize_name(code) == wanted:
                return cp
            for alias in (normalize_name(cp.name), *(normalize_name(a) for a in cp.aliases)):
                if not alias:
                    continue
                if alias == wanted:
                    return cp
                if not exact and f" {alias} " in padded and (best is None or len(alias) > best[0]):
                    best = (len(alias), code)
        return self.chokepoints[best[1]] if best else None

    def chokepoint_point(self, code: str) -> Point | None:
        """Map position of a chokepoint: the catalogued place, else its polygon's centre."""
        hit = self.place(code)
        if hit is not None:
            return hit
        cp = self.chokepoints.get(code)
        if cp is None or not cp.polygon:
            return None
        ring = cp.polygon[:-1] if len(cp.polygon) > 1 and cp.polygon[0] == cp.polygon[-1] else cp.polygon
        return (sum(p[0] for p in ring) // len(ring), sum(p[1] for p in ring) // len(ring))

    # ───────────────────────────── routes ─────────────────────────────

    def sea_route(self, origin: str, dest: str, via: Sequence[str] = ()) -> tuple[Point, ...] | None:
        """Catalogued polyline origin → dest (possibly reversed), honouring CAPE / SUEZ in ``via``."""
        wanted = [c for c in via if c in CHOKEPOINT_PREFERENCE]
        order = [*wanted, *(c for c in CHOKEPOINT_PREFERENCE if c not in wanted), None]
        for choke in order:
            for r in self.routes:
                if r.via != choke:
                    continue
                if r.origin == origin and r.dest == dest:
                    return r.path
                if r.origin == dest and r.dest == origin:
                    return tuple(reversed(r.path))
        return None

    def lane_path(self, *, mode: str, origin: str, dest: str, start: Point, end: Point,
                  via: Sequence[str] = (), dest_port: str | None = None) -> list[Point]:
        """Polyline of one lane leg from ``start`` (``origin``) to ``end`` (``dest``).

        ``dest_port`` is the port code at ``end`` when the leg ends at a port (sea lanes); for a
        sea-air leg ending at a DC the sea part runs into the hub (first ``via`` with a route)."""
        if mode == "sea":
            route = self.sea_route(origin, dest_port or dest, via)
            if route:
                return _anchor(list(route), start, end)
            return _through(start, [self.place(v) for v in via], end)
        if mode == "sea_air":
            for hub in via:
                sea = self.sea_route(origin, hub, ())
                if sea:
                    return [*_anchor(list(sea), start, sea[-1]), end]
            return _through(start, [self.place(v) for v in via], end)
        return [start, end]

    def short_sea_route(self, origin: str, end: Point) -> tuple[str, tuple[Point, ...]] | None:
        """Catalogued chokepoint-free route from port ``origin`` to the port nearest ``end`` (a DC),
        as ``(port code, polyline)``; ``None`` when no such port is nearer to ``end`` than the
        origin itself (a straight leg is then the better picture)."""
        start = self.place(origin)
        best: tuple[float, str, tuple[Point, ...]] | None = None
        for r in self.routes:
            if r.via is not None:
                continue
            if r.origin == origin:
                port, path = r.dest, r.path
            elif r.dest == origin:
                port, path = r.origin, tuple(reversed(r.path))
            else:
                continue
            km = distance_km(path[-1], end)
            if best is None or (km, port) < (best[0], best[1]):
                best = (km, port, path)
        if best is None:
            return None
        if start is not None and distance_km(start, end) <= best[0]:
            return None
        return best[1], best[2]

    def sea_air_legs(self, stops: Sequence[tuple[str, Point, bool]],
                     via: Sequence[str] = ()) -> tuple[list[Point], str, list[Point]] | None:
        """``(sea leg, hub code, air arc)`` of a sea-air lane from its stops (origin port … DC).

        The sea leg follows the catalogued route into the first ``via`` hub that has one (pinned to
        the stops' own coordinates when the hub is a stop); the air leg is :func:`air_arc` from the
        hub to the destination. ``None`` when the origin is not a port or no hub route is catalogued
        (callers then draw :meth:`stops_path`)."""
        if len(stops) < 2 or not stops[0][2]:
            return None
        origin_code, origin_pt, _ = stops[0]
        dest_pt = stops[-1][1]
        positions = {code: pt for code, pt, is_port in stops if is_port}
        for hub in via:
            route = self.sea_route(origin_code, hub, ())
            if not route:
                continue
            hub_pt = positions.get(hub, route[-1])
            sea = _anchor(list(route), origin_pt, hub_pt)
            return sea, hub, air_arc(hub_pt, dest_pt)
        return None


    def stops_path(self, stops: Sequence[tuple[str, Point, bool]], *, mode: str, via: Sequence[str] = ()) -> list[Point]:
        """Polyline through ``stops`` (``(code, position, is_port)`` from origin to destination).

        Road and air lanes are straight between stops. For sea and sea-air lanes the catalogue is
        consulted greedily: from each port stop, the farthest later port stop with a catalogued
        route is reached along that route (CAPE / SUEZ from ``via``; intermediate transshipment
        ports without a route of their own are skipped); remaining hops are straight (e.g. the
        road leg port → DC or the air leg hub → DC)."""
        if len(stops) < 2:
            return _dedupe([s[1] for s in stops] * 2)
        if mode not in ("sea", "sea_air"):
            return _dedupe([s[1] for s in stops])
        if mode == "sea" and len(stops) == 2 and stops[0][2] and not stops[1][2]:
            # a sea lane straight into a DC (no destination port): short-sea to the nearest port
            chokes_ = [v for v in via if v in CHOKEPOINT_PREFERENCE]
            if not self.sea_route(stops[0][0], stops[1][0], chokes_):
                short = self.short_sea_route(stops[0][0], stops[1][1])
                if short is not None:
                    return _dedupe([*_anchor(list(short[1]), stops[0][1], short[1][-1]), stops[1][1]])
        chokes = [v for v in via if v in CHOKEPOINT_PREFERENCE]
        out: list[Point] = [stops[0][1]]
        i = 0
        while i < len(stops) - 1:
            code_i, _, port_i = stops[i]
            jump: tuple[int, tuple[Point, ...]] | None = None
            if port_i:
                for j in range(len(stops) - 1, i, -1):
                    code_j, _, port_j = stops[j]
                    if not port_j:
                        continue
                    route = self.sea_route(code_i, code_j, chokes)
                    if route:
                        jump = (j, route)
                        break
            if jump is None:
                out.append(stops[i + 1][1])
                i += 1
                continue
            j, route = jump
            out.extend(_anchor(list(route), stops[i][1], stops[j][1])[1:])
            i = j
        return _dedupe(out)


def _anchor(path: list[Point], start: Point, end: Point) -> list[Point]:
    """Pin a catalogued polyline to the actual endpoints (the snapshot's own coordinates)."""
    out = list(path)
    out[0] = start
    out[-1] = end
    return _dedupe(out)


def _through(start: Point, middle: Sequence[Point | None], end: Point) -> list[Point]:
    return _dedupe([start, *(p for p in middle if p is not None), end])


def _dedupe(points: Sequence[Point]) -> list[Point]:
    out: list[Point] = []
    for p in points:
        if not out or out[-1] != p:
            out.append(p)
    if len(out) == 1:
        out.append(out[0])
    return out


_CHOKEPOINT_KEYS = frozenset({"name", "aliases", "polygon"})


def parse_chokepoints(doc: dict[str, Any]) -> dict[str, Chokepoint]:
    """``chokepoints.json`` → code → :class:`Chokepoint`. Raises ``ValueError`` on a malformed
    entry (platform data: a typo must not silently drop a chokepoint)."""
    raw = doc.get("chokepoints")
    if not isinstance(raw, dict):
        raise ValueError("chokepoints.json: 'chokepoints' must be an object")
    out: dict[str, Chokepoint] = {}
    for code, entry in raw.items():
        if not isinstance(entry, dict):
            raise ValueError(f"chokepoints.json: {code}: entry must be an object")
        if unknown := sorted(set(entry) - _CHOKEPOINT_KEYS):
            raise ValueError(f"chokepoints.json: {code}: unknown keys {unknown}")
        ring: list[Point] = []
        for pt in entry.get("polygon") or []:
            lon, lat = int(pt[0]), int(pt[1])
            if not (-180_000_000 <= lon <= 180_000_000 and -90_000_000 <= lat <= 90_000_000):
                raise ValueError(f"chokepoints.json: {code}: polygon point out of range")
            ring.append((lon, lat))
        if ring and (len(ring) < 4 or ring[0] != ring[-1]):
            raise ValueError(f"chokepoints.json: {code}: polygon must be a closed ring of at least 4 points")
        out[str(code)] = Chokepoint(code=str(code), name=str(entry.get("name") or code),
                                    aliases=tuple(str(a) for a in entry.get("aliases") or []), polygon=tuple(ring))
    return out


def _candidate_dirs() -> list[Path]:
    dirs: list[Path] = []
    env = os.environ.get("GEO_DIR")
    if env:
        dirs.append(Path(env))
    dirs.append(Path("/app/geo"))
    here = Path(__file__).resolve()
    for parent in here.parents:
        dirs.append(parent / "data" / "geo")
    return dirs


def _read(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


@functools.cache
def _load_default() -> GeoCatalog:
    for d in _candidate_dirs():
        routes = d / "sea_routes.json"
        if routes.is_file():
            cat = GeoCatalog.from_documents(_read(routes), _read(d / "country_centroids.json"), source=str(d),
                                            chokepoints_doc=_read(d / "chokepoints.json"))
            log.info("geo catalogue loaded", source=str(d), routes=len(cat.routes), places=len(cat.places),
                     chokepoints=len(cat.chokepoints))
            return cat
    log.warning("geo catalogue not found; network map lanes are straight lines", searched=[str(d) for d in _candidate_dirs()[:3]])
    return GeoCatalog()


_override: GeoCatalog | None = None


def get_geo() -> GeoCatalog:
    return _override if _override is not None else _load_default()


def set_geo(catalog: GeoCatalog | None) -> None:
    """Install a catalogue (tests); ``None`` restores the file-backed default."""
    global _override
    _override = catalog
