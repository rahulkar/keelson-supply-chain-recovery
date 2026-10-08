#!/usr/bin/env python3
"""Offline validator for the Signal Hub demo packs (``docs/contracts/100-m6-signals.md`` §7).

A pack is ``data/scenarios/<id>/pack.yaml`` (the ``ScenarioPack`` contract) plus its fixtures (served by
the hub at ``/demo-feeds/<id>/<file>`` so the real adapters parse them), a ``README.md`` and an
``expected.yaml`` (the expected events, alerts and exposures against the reference tenant network
``network-value-fashion-eu.yaml``). No network, no database, no LLM.

Three levels, each used when its imports are available (``--require`` makes a missing one an error):

1. **shape** (stdlib + PyYAML): pack keys, step order, per-adapter payload keys, fixture files and
   their real-feed shapes (GDACS event list + embedded footprints, USGS summary GeoJSON, RSS 2.0 / Atom,
   Merxa ``supplier_message.received`` webhook body), GeoJSON validity (closed, simple, counter-clockwise
   rings, 2D WGS84), only reserved ``.example`` hosts (clearly fictional), every news / free-text item
   covered by a pre-baked extraction whose mentions appear verbatim in the item text, nothing in the
   future of the step's sim time, and the "simulated" labelling. Real-feed details the hub's parsers
   rely on or silently drop are errors: GDACS integer ``alertscore``, the RSS adapter's 40-item and
   300-character limits, matching RFC 822 weekdays, uuidv7 ids whose time matches the event, and a
   repeated news item (same connector + guid) that changes text or extraction (the hub ignores it).
2. **contract** (``keelson_contracts``): ``ScenarioPack`` and ``NewsExtraction`` (+
   ``news_extraction_violations``) validation.
3. **replay** (``signal_hub``): every step is parsed by the hub's real adapters (``parse_gdacs``,
   ``parse_usgs``, ``parse_feed``, ``parse_connector_event``) with the real gazetteer, pre-baked
   extractions go through the real quarantine screen (``run_quarantine``) and ``draft_from_extraction``,
   every draft becomes a contract ``DisruptionSignal``; then, at each checkpoint of ``expected.yaml``, the
   events (members, attach rules), severities, kind radii, alert actions (``DEFAULT_ALERT_RULES``) and
   exposures (intersects / dwithin_km / lane_crosses / entity_match, same precedence as the hub) are
   recomputed in pure Python and compared **exactly** with the expectations. Geometric decisions closer
   than ``MARGIN_KM`` to a threshold are reported as fragile (PostGIS uses geodesics; this uses a
   densified local projection).

Usage (repo root)::

    python data/scenarios/validate.py                 # all packs
    python data/scenarios/validate.py typhoon-hcmc    # one pack
    python data/scenarios/validate.py --require replay

Exit status 0 when there is no error (warnings are printed). In Docker (all three levels)::

    uv run --project services/signal-hub python data/scenarios/validate.py --require replay
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import datetime as dt
import email.utils
import html
import itertools
import json
import math
import re
import sys
import uuid
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlsplit

import yaml

SCENARIOS_DIR: Final = Path(__file__).resolve().parent
REPO: Final = SCENARIOS_DIR.parents[1]
GEO_DIR: Final = REPO / "data" / "geo"
NETWORK_DEFAULT: Final = "network-value-fashion-eu.yaml"

PACK_KEYS: Final = {"id", "title", "description", "start_at", "steps"}
STEP_KEYS: Final = {"t_offset_s", "adapter", "payload", "note"}
PACK_ID_RE: Final = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
ADAPTERS: Final = ("gdacs", "usgs", "rss", "merxa_inject", "chat_hint")
PAYLOAD_KEYS: Final[dict[str, set[str]]] = {
    "gdacs": {"fixture"},
    "usgs": {"fixture"},
    "rss": {"fixture", "connector_id", "source_name", "extraction", "extractions"},
    "merxa_inject": {"event", "fixture", "extraction"},
    "chat_hint": {"text"},
}
FIXTURE_SUFFIXES: Final[dict[str, tuple[str, ...]]] = {
    "gdacs": (".geojson", ".json"),
    "usgs": (".geojson", ".json"),
    "rss": (".xml", ".rss", ".atom"),
    "merxa_inject": (".json",),
}
PACK_SIDE_FILES: Final = {"pack.yaml", "README.md", "expected.yaml"}
EXTRACTION_KEYS: Final = ("kind", "subtype", "places", "companies", "ports", "chokepoints", "dates_text",
                          "injection_suspected")
MENTION_LISTS: Final = ("places", "companies", "ports", "chokepoints", "dates_text")
SIGNAL_KINDS: Final = ("natural_hazard", "weather", "geopolitical", "labor", "logistics", "regulatory", "cyber",
                       "supplier", "demand", "hypothetical")
SUBTYPE_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
CONNECTOR_RE: Final = re.compile(r"^[a-z][a-z0-9_.-]{0,99}$")
GDACS_TS_RE: Final = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$")
GDACS_LEVELS: Final = ("Green", "Orange", "Red")
GDACS_LEVEL_SCORE: Final = {"Green": 1, "Orange": 2, "Red": 3}  # live GDACS API v2: alertscore is this integer
GDACS_TYPES: Final = ("TC", "EQ", "FL", "VO", "DR", "WF")
FOOTPRINT_TYPES: Final = ("Polygon", "MultiPolygon", "LineString", "MultiLineString")
CONNECTOR_EVENT_TYPES: Final = ("sailing.updated", "lane.capacity_changed", "po_line.updated",
                                "supplier_message.received")
MAX_SNIPPET: Final = 300
MAX_TITLE: Final = 300  # the hub clips headlines / supplier-message subjects to 300 characters
HUB_MAX_FEED_ITEMS: Final = 40  # signal_hub.adapters.rss.parse_feed reads the first 40 items only
UUID7_SKEW: Final = dt.timedelta(days=1)
MAX_MENTION: Final = 80
MAX_MENTIONS: Final = 20
MAX_NOTE: Final = 500
MAX_HUB_TEXT: Final = 3500
MERXA_MAX_SUBJECT: Final = 500
MERXA_MAX_BODY: Final = 20_000
NODE_TYPES: Final = ("supplier", "factory", "port", "dc", "store", "lane")
METHODS: Final = ("intersects", "dwithin_km", "lane_crosses", "entity_match")
PRECEDENCE: Final = {"intersects": 0, "entity_match": 1, "lane_crosses": 2, "dwithin_km": 3}
REF_NODE_TYPES: Final = {"UNLOCODE": "port", "SUPPLIER": "supplier", "FACTORY": "factory", "DC": "dc",
                         "STORE": "store", "LANE": "lane"}
STRONG_SCHEMES: Final = frozenset({"UNLOCODE", "CHOKEPOINT", "SUPPLIER", "FACTORY", "DC", "STORE", "LANE"})
LANE_BUFFER_KM: Final = 25.0
ATTACH_RADIUS_KM: Final = 300.0
ATTACH_WINDOW: Final = dt.timedelta(hours=72)
MARGIN_KM: Final = 5.0
DENSIFY_KM: Final = 25.0
EARTH_KM: Final = 6371.0088
LEVELS: Final = ("shape", "contract", "replay")
TENANT: Final = "demo-retail"
_TAG_RE: Final = re.compile(r"<[^>]*>")
ATOM: Final = "{http://www.w3.org/2005/Atom}"
DC_NS: Final = "{http://purl.org/dc/elements/1.1/}"


# ─────────────────────────────── imports (optional levels) ───────────────────


def _try_imports() -> dict[str, Any]:
    """Import ``keelson_contracts`` / ``signal_hub`` (from the repo when not installed)."""
    mods: dict[str, Any] = {}
    for src in (REPO / "packages" / "contracts-py" / "src", REPO / "services" / "signal-hub" / "src"):
        if src.is_dir() and str(src) not in sys.path:
            sys.path.append(str(src))
    try:
        from keelson_contracts import llm as c_llm
        from keelson_contracts import signals as c_signals

        mods["contracts"] = (c_signals, c_llm)
    except Exception as exc:  # optional level
        mods["contracts_error"] = f"{type(exc).__name__}: {exc}"
    if "contracts" in mods:
        try:
            from signal_hub import cluster as h_cluster
            from signal_hub import pipeline as h_pipeline
            from signal_hub import quarantine as h_quarantine
            from signal_hub import replay as h_replay
            from signal_hub import scoring as h_scoring
            from signal_hub.adapters import connector as h_connector
            from signal_hub.adapters import gdacs as h_gdacs
            from signal_hub.adapters import rss as h_rss
            from signal_hub.adapters import usgs as h_usgs
            from signal_hub.gazetteer import Gazetteer
            from signal_hub.models import SourceContext, clip

            mods["hub"] = {
                "cluster": h_cluster, "pipeline": h_pipeline, "quarantine": h_quarantine, "replay": h_replay,
                "scoring": h_scoring, "connector": h_connector, "gdacs": h_gdacs, "rss": h_rss, "usgs": h_usgs,
                "Gazetteer": Gazetteer, "SourceContext": SourceContext, "clip": clip,
            }
        except Exception as exc:  # optional level
            mods["hub_error"] = f"{type(exc).__name__}: {exc}"
    else:
        mods["hub_error"] = "needs keelson_contracts"
    return mods


# ─────────────────────────────────── report ─────────────────────────────────


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)

    def error(self, where: str, msg: str) -> None:
        self.errors.append(f"{where}: {msg}")

    def warn(self, where: str, msg: str) -> None:
        self.warnings.append(f"{where}: {msg}")

    def info(self, where: str, msg: str) -> None:
        self.infos.append(f"{where}: {msg}")


# ──────────────────────────────── small helpers ─────────────────────────────


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    return re.sub(r"\s+", " ", html.unescape(_TAG_RE.sub(" ", text))).strip()


def parse_iso(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo is not None else None


def parse_gdacs_ts(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not GDACS_TS_RE.match(value):
        return None
    return dt.datetime.fromisoformat(value).replace(tzinfo=dt.UTC)


def parse_rfc822(value: Any) -> dt.datetime | None:
    if not isinstance(value, str):
        return None
    try:
        t = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    return t if t.tzinfo is not None else None


def is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def fictional_url(url: Any) -> bool:
    """Absolute http(s) URL on a reserved RFC 2606 ``.example`` host (never a real site)."""
    if not isinstance(url, str):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    return parts.scheme in ("http", "https") and (host == "example" or host.endswith(".example"))


def tag_uri_fictional(value: str) -> bool:
    m = re.match(r"^tag:([^,]+),", value)
    return bool(m) and (m.group(1).lower().endswith(".example"))


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(el: Any) -> str | None:
    if el is None:
        return None
    return "".join(el.itertext()).strip() or None


def _xml_root(body: bytes) -> Any:
    try:
        from defusedxml import ElementTree as SafeET  # the hub's parser

        return SafeET.fromstring(body)
    except ImportError:
        import xml.etree.ElementTree as ET  # local, committed fixtures only

        return ET.fromstring(body)  # stdlib fallback, used on the committed fixtures only (the hub parses with defusedxml)


# ─────────────────────────────────── geometry ───────────────────────────────


Pos = tuple[float, float]


def _to_xyz(p: Pos) -> tuple[float, float, float]:
    lon, lat = math.radians(p[0]), math.radians(p[1])
    return (math.cos(lat) * math.cos(lon), math.cos(lat) * math.sin(lon), math.sin(lat))


def _from_xyz(v: tuple[float, float, float]) -> Pos:
    x, y, z = v
    return (math.degrees(math.atan2(y, x)), math.degrees(math.atan2(z, math.hypot(x, y))))


def haversine_km(a: Pos, b: Pos) -> float:
    la1, la2 = math.radians(a[1]), math.radians(b[1])
    dla, dlo = la2 - la1, math.radians(b[0] - a[0])
    h = math.sin(dla / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin(dlo / 2) ** 2
    return 2 * EARTH_KM * math.asin(min(1.0, math.sqrt(h)))


def densify(line: Sequence[Pos], step_km: float = DENSIFY_KM) -> list[Pos]:
    """Great-circle densification (PostGIS geography edges are geodesics)."""
    out: list[Pos] = []
    for a, b in itertools.pairwise(line):
        d = haversine_km(a, b)
        n = max(1, math.ceil(d / step_km))
        va, vb = _to_xyz(a), _to_xyz(b)
        omega = d / EARTH_KM
        out.append(a)
        for i in range(1, n):
            f = i / n
            if omega < 1e-12:
                continue
            s1, s2 = math.sin((1 - f) * omega) / math.sin(omega), math.sin(f * omega) / math.sin(omega)
            out.append(_from_xyz(tuple(s1 * x + s2 * y for x, y in zip(va, vb, strict=True))))  # type: ignore[arg-type]
    if line:
        out.append(line[-1])
    return out


def _seg_dist_km(p: Pos, a: Pos, b: Pos) -> float:
    """Point-segment distance in a local equirectangular projection centred on ``p``."""
    kx = 111.32 * math.cos(math.radians(p[1]))
    ky = 110.574
    ax, ay = (a[0] - p[0]) * kx, (a[1] - p[1]) * ky
    bx, by = (b[0] - p[0]) * kx, (b[1] - p[1]) * ky
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = 0.0 if length2 == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length2))
    return math.hypot(ax + t * dx, ay + t * dy)


def point_in_ring(p: Pos, ring: Sequence[Pos]) -> bool:
    x, y = p
    inside = False
    for (x1, y1), (x2, y2) in itertools.pairwise(ring):
        if (y1 > y) != (y2 > y) and x < (x2 - x1) * (y - y1) / (y2 - y1) + x1:
            inside = not inside
    return inside


def _orient(a: Pos, b: Pos, c: Pos) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def segments_cross(a: Pos, b: Pos, c: Pos, d: Pos) -> bool:
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    if ((o1 > 0) != (o2 > 0)) and ((o3 > 0) != (o4 > 0)) and o1 * o2 != 0 and o3 * o4 != 0:
        return True

    def on(p: Pos, q: Pos, r: Pos) -> bool:
        return min(p[0], r[0]) <= q[0] <= max(p[0], r[0]) and min(p[1], r[1]) <= q[1] <= max(p[1], r[1])

    return ((o1 == 0 and on(a, c, b)) or (o2 == 0 and on(a, d, b)) or (o3 == 0 and on(c, a, d))
            or (o4 == 0 and on(c, b, d)))


def ring_area2(ring: Sequence[Pos]) -> float:
    """Twice the signed planar area (> 0 = counter-clockwise)."""
    return sum(a[0] * b[1] - b[0] * a[1] for a, b in itertools.pairwise(ring))


def ring_self_intersections(ring: Sequence[Pos]) -> list[tuple[int, int]]:
    n = len(ring) - 1  # closed
    hits: list[tuple[int, int]] = []
    for i in range(n):
        for j in range(i + 1, n):
            if j == i + 1 or (i == 0 and j == n - 1):
                continue  # adjacent edges share a vertex
            if segments_cross(ring[i], ring[i + 1], ring[j], ring[j + 1]):
                hits.append((i, j))
    return hits


@dataclass
class Shape:
    """A simple geometry prepared for checks: ``polys`` (outer, holes...), ``lines``, ``points``."""

    polys: list[list[list[Pos]]] = field(default_factory=list)
    lines: list[list[Pos]] = field(default_factory=list)
    points: list[Pos] = field(default_factory=list)

    @classmethod
    def of(cls, geometry: dict[str, Any] | None) -> Shape:
        s = cls()
        for g in _flatten(geometry):
            t, c = g.get("type"), g.get("coordinates")
            if t == "Point":
                s.points.append((float(c[0]), float(c[1])))
            elif t == "MultiPoint":
                s.points.extend((float(p[0]), float(p[1])) for p in c)
            elif t == "LineString":
                s.lines.append(densify([(float(p[0]), float(p[1])) for p in c]))
            elif t == "MultiLineString":
                s.lines.extend(densify([(float(p[0]), float(p[1])) for p in ln]) for ln in c)
            elif t == "Polygon":
                s.polys.append([densify([(float(p[0]), float(p[1])) for p in r]) for r in c])
            elif t == "MultiPolygon":
                s.polys.extend([densify([(float(p[0]), float(p[1])) for p in r]) for r in poly] for poly in c)
        return s

    @property
    def empty(self) -> bool:
        return not (self.polys or self.lines or self.points)

    def contains(self, p: Pos) -> bool:
        return any(point_in_ring(p, poly[0]) and not any(point_in_ring(p, h) for h in poly[1:]) for poly in self.polys)

    def segments(self) -> Iterable[tuple[Pos, Pos]]:
        for poly in self.polys:
            for ring in poly:
                yield from itertools.pairwise(ring)
        for line in self.lines:
            yield from itertools.pairwise(line)

    def vertices(self) -> Iterable[Pos]:
        for poly in self.polys:
            for ring in poly:
                yield from ring
        for line in self.lines:
            yield from line
        yield from self.points

    def distance_km(self, p: Pos) -> float:
        """0 inside a polygon, else the distance to the nearest edge / point."""
        if self.contains(p):
            return 0.0
        return self.boundary_distance_km(p)

    def boundary_edges(self) -> Iterable[tuple[Pos, Pos]]:
        """Polygon ring edges on the boundary of the union: an edge inside another polygon (e.g. a GDACS
        track-point circle inside the cone) cannot decide intersects vs dwithin, so it is skipped."""
        for i, poly in enumerate(self.polys):
            others = [q for j, q in enumerate(self.polys) if j != i]
            for ring in poly:
                for a, b in itertools.pairwise(ring):
                    mid = ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)
                    if any(point_in_ring(mid, q[0]) and not any(point_in_ring(mid, h) for h in q[1:]) for q in others):
                        continue
                    yield a, b

    def ring_distance_km(self, p: Pos) -> float:
        """Distance to the nearest edge of the polygons' union boundary (inf without polygons)."""
        best = math.inf
        for a, b in self.boundary_edges():
            best = min(best, _seg_dist_km(p, a, b))
        return best

    def boundary_distance_km(self, p: Pos) -> float:
        best = math.inf
        for a, b in self.segments():
            best = min(best, _seg_dist_km(p, a, b))
        for q in self.points:
            best = min(best, haversine_km(p, q))
        return best

    def line_relation(self, line: Sequence[Pos]) -> tuple[bool, float]:
        """(crosses, distance_km) of a densified polyline to this shape."""
        if any(self.contains(v) for v in line):
            return True, 0.0
        segs = list(self.segments())
        for a, b in itertools.pairwise(line):
            for c, d in segs:
                if segments_cross(a, b, c, d):
                    return True, 0.0
        best = math.inf
        for v in line:
            best = min(best, self.boundary_distance_km(v))
        for q in self.vertices():
            for a, b in itertools.pairwise(line):
                best = min(best, _seg_dist_km(q, a, b))
        return False, best


def _flatten(geometry: Any) -> list[dict[str, Any]]:
    if not isinstance(geometry, dict):
        return []
    if geometry.get("type") == "GeometryCollection":
        return [g for sub in geometry.get("geometries") or [] for g in _flatten(sub)]
    return [geometry]


def check_geometry(where: str, geom: Any, rep: Report) -> None:
    """GeoJSON (RFC 7946) sanity for fixture geometry: 2D float WGS84, closed simple CCW rings."""
    if not isinstance(geom, dict) or geom.get("type") not in (
        "Point", "MultiPoint", "LineString", "MultiLineString", "Polygon", "MultiPolygon"
    ):
        rep.error(where, f"geometry must be a simple GeoJSON geometry, got {geom!r:.80}")
        return

    def pos_ok(p: Any) -> bool:
        return (isinstance(p, list) and len(p) == 2 and all(is_number(v) for v in p)
                and -180 <= p[0] <= 180 and -90 <= p[1] <= 90)

    def ring_ok(ring: Any, label: str, outer: bool) -> None:
        if not isinstance(ring, list) or len(ring) < 4 or not all(pos_ok(p) for p in ring):
            rep.error(where, f"{label}: a ring needs >= 4 [lon, lat] positions (2D, WGS84 range)")
            return
        if ring[0] != ring[-1]:
            rep.error(where, f"{label}: ring is not closed")
            return
        pts = [(float(p[0]), float(p[1])) for p in ring]
        area = ring_area2(pts)
        if area == 0:
            rep.error(where, f"{label}: degenerate ring (zero area)")
        elif (area > 0) != outer:
            rep.error(where, f"{label}: {'exterior' if outer else 'hole'} ring must be "
                             f"{'counter-clockwise' if outer else 'clockwise'} (RFC 7946 §3.1.6)")
        if hits := ring_self_intersections(pts):
            rep.error(where, f"{label}: ring self-intersects at edges {hits[:3]}")

    t, c = geom["type"], geom.get("coordinates")
    if t == "Point":
        if not pos_ok(c):
            rep.error(where, "Point needs one 2D [lon, lat] position")
    elif t in ("MultiPoint", "LineString"):
        if not isinstance(c, list) or len(c) < (1 if t == "MultiPoint" else 2) or not all(pos_ok(p) for p in c):
            rep.error(where, f"{t} needs 2D [lon, lat] positions")
    elif t == "MultiLineString":
        if not isinstance(c, list) or not c or not all(isinstance(ln, list) and len(ln) >= 2
                                                        and all(pos_ok(p) for p in ln) for ln in c):
            rep.error(where, "MultiLineString needs lines of >= 2 2D positions")
    elif t == "Polygon":
        if not isinstance(c, list) or not c:
            rep.error(where, "Polygon needs >= 1 ring")
        else:
            for i, ring in enumerate(c):
                ring_ok(ring, f"ring {i}", i == 0)
    elif t == "MultiPolygon":
        if not isinstance(c, list) or not c:
            rep.error(where, "MultiPolygon needs >= 1 polygon")
        else:
            for j, poly in enumerate(c):
                for i, ring in enumerate(poly if isinstance(poly, list) else []):
                    ring_ok(ring, f"polygon {j} ring {i}", i == 0)


# ───────────────────────────── level 1: pack shape ──────────────────────────


@dataclass
class NewsItem:
    """One news / free-text item as the hub would see it (for coverage + faithfulness checks)."""

    key_candidates: tuple[str, ...]
    text: str
    published_at: dt.datetime | None
    dedupe: str = ""  # "<connector_id>|<native id>": the hub stores a news item once and never re-reads it


def load_yaml(path: Path) -> Any:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def check_pack(pack_dir: Path, rep: Report, mods: dict[str, Any]) -> dict[str, Any] | None:
    where = pack_dir.name
    path = pack_dir / "pack.yaml"
    try:
        data = load_yaml(path)
    except yaml.YAMLError as exc:
        rep.error(where, f"pack.yaml is not valid YAML: {exc}")
        return None
    if not isinstance(data, dict):
        rep.error(where, "pack.yaml must be a mapping")
        return None
    if set(data) != PACK_KEYS:
        rep.error(where, f"pack keys must be exactly {sorted(PACK_KEYS)}, got {sorted(data)}")
    pid = data.get("id")
    if not isinstance(pid, str) or not PACK_ID_RE.match(pid) or pid != pack_dir.name:
        rep.error(where, f"id {pid!r} must match the directory name and {PACK_ID_RE.pattern}")
    title, desc = data.get("title"), data.get("description")
    if not isinstance(title, str) or not 1 <= len(title) <= 300:
        rep.error(where, "title must be 1..300 characters")
    elif "simulated" not in title.lower():
        rep.error(where, "title must say it is simulated (e.g. '… (simulated)')")
    if not isinstance(desc, str) or len(desc) > 2000:
        rep.error(where, "description must be a string of at most 2000 characters")
    elif not desc.startswith("SIMULATED"):
        rep.error(where, "description must start with 'SIMULATED'")
    start = data.get("start_at")
    start_at = parse_iso(start) if isinstance(start, str) else None
    if start_at is None:
        rep.error(where, "start_at must be a timezone-aware ISO 8601 string (quoted in YAML)")
    readme = pack_dir / "README.md"
    if not readme.is_file():
        rep.error(where, "README.md is missing")
    else:
        text = readme.read_text(encoding="utf-8").lower()
        for word in ("simulated", "fictional", "expected exposure"):
            if word not in text:
                rep.error(where, f"README.md must mention '{word}'")
    if not (pack_dir / "expected.yaml").is_file():
        rep.error(where, "expected.yaml is missing")

    steps = data.get("steps")
    if not isinstance(steps, list) or not steps:
        rep.error(where, "steps must be a non-empty list")
        return None
    offsets: list[int] = []
    referenced: set[str] = set()
    seen_news: dict[str, tuple[int, str, Any]] = {}
    for i, step in enumerate(steps):
        sw = f"{where} step {i}"
        if not isinstance(step, dict) or set(step) != STEP_KEYS:
            rep.error(sw, f"keys must be exactly {sorted(STEP_KEYS)}")
            continue
        off, adapter, payload, note = step["t_offset_s"], step["adapter"], step["payload"], step["note"]
        if isinstance(off, bool) or not isinstance(off, int) or off < 0:
            rep.error(sw, "t_offset_s must be an int >= 0")
            continue
        offsets.append(off)
        if not isinstance(note, str) or len(note) > MAX_NOTE:
            rep.error(sw, f"note must be a string of at most {MAX_NOTE} characters")
        if adapter not in ADAPTERS:
            rep.error(sw, f"adapter must be one of {ADAPTERS}")
            continue
        if not isinstance(payload, dict):
            rep.error(sw, "payload must be a mapping")
            continue
        extra = set(payload) - PAYLOAD_KEYS[adapter]
        if extra:
            rep.error(sw, f"unknown payload keys for {adapter}: {sorted(extra)}")
        at = start_at + dt.timedelta(seconds=off) if start_at else None
        fixture = payload.get("fixture")
        if fixture is not None or adapter in ("gdacs", "usgs", "rss"):
            fpath = _check_fixture_name(sw, pack_dir, adapter, fixture, rep)
            if fpath is not None:
                referenced.add(fpath.name)
        else:
            fpath = None
        if adapter == "gdacs" and fpath:
            _check_gdacs(sw, fpath, at, rep)
        elif adapter == "usgs" and fpath:
            _check_usgs(sw, fpath, at, rep)
        elif adapter == "rss":
            _check_rss_payload(sw, payload, rep)
            connector = str(payload.get("connector_id") or "rss.demo")  # the hub's default
            items = _check_feed(sw, fpath, at, rep, connector) if fpath else []
            _check_extractions(sw, payload, items, rep, mods, seen_news, i)
        elif adapter == "merxa_inject":
            if ("event" in payload) == ("fixture" in payload):
                rep.error(sw, "merxa_inject needs exactly one of 'event' or 'fixture'")
                continue
            raw: Any = payload.get("event")
            if fpath is not None:
                try:
                    raw = json.loads(fpath.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    rep.error(sw, f"{fpath.name} is not valid JSON: {exc}")
                    continue
            items = _check_connector_event(sw, raw, at, rep)
            _check_extractions(sw, payload, items, rep, mods, seen_news, i)
        elif adapter == "chat_hint":
            text = payload.get("text")
            if not isinstance(text, str) or not 1 <= len(text) <= MAX_NOTE:
                rep.error(sw, f"chat_hint needs text (1..{MAX_NOTE} characters)")
    if offsets != sorted(offsets):
        rep.error(where, "steps must be ordered by t_offset_s")
    for f in sorted(pack_dir.iterdir()):
        if f.is_file() and f.name not in PACK_SIDE_FILES and f.name not in referenced:
            rep.warn(where, f"{f.name} is not referenced by any step (it is still served at /demo-feeds)")
        if f.is_dir():
            rep.error(where, f"subdirectory {f.name}/ is not allowed in a pack (fixtures are flat)")

    if "contracts" in mods:
        c_signals = mods["contracts"][0]
        try:
            c_signals.ScenarioPack.model_validate(data)
        except Exception as exc:  # pydantic ValidationError
            rep.error(where, f"ScenarioPack contract: {exc}")
    return data


def _check_fixture_name(where: str, pack_dir: Path, adapter: str, name: Any, rep: Report) -> Path | None:
    if not isinstance(name, str) or not name:
        rep.error(where, "payload.fixture (file name) is required")
        return None
    if "/" in name or "\\" in name or name.startswith(".") or ".." in name:
        rep.error(where, f"fixture {name!r} must be a plain file name in the pack directory")
        return None
    if not name.lower().endswith(FIXTURE_SUFFIXES[adapter]):
        rep.error(where, f"fixture {name!r} must end with one of {FIXTURE_SUFFIXES[adapter]} for {adapter}")
        return None
    path = pack_dir / name
    if not path.is_file():
        rep.error(where, f"fixture {name!r} does not exist")
        return None
    return path


def _check_gdacs(where: str, path: Path, at: dt.datetime | None, rep: Report) -> None:
    w = f"{where} {path.name}"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        rep.error(w, f"not valid JSON: {exc}")
        return
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection" or not isinstance(doc.get("features"), list):
        rep.error(w, "must be a GeoJSON FeatureCollection (GDACS geteventlist shape)")
        return
    if "simulated" not in str(doc.get("_fixture", "")).lower():
        rep.error(w, "the '_fixture' note must say the data is SIMULATED")
    events: dict[tuple[str, int, int], dict[str, Any]] = {}
    footprints: list[tuple[int, dict[str, Any]]] = []
    for i, feat in enumerate(doc["features"]):
        fw = f"{w} feature {i}"
        if not isinstance(feat, dict) or feat.get("type") != "Feature":
            rep.error(fw, "must be a GeoJSON Feature")
            continue
        props, geom = feat.get("properties"), feat.get("geometry")
        if not isinstance(props, dict) or not isinstance(geom, dict):
            rep.error(fw, "needs properties and geometry")
            continue
        etype, eid, ep = props.get("eventtype"), props.get("eventid"), props.get("episodeid")
        if etype not in GDACS_TYPES or isinstance(eid, bool) or not isinstance(eid, int) \
                or isinstance(ep, bool) or not isinstance(ep, int):
            rep.error(fw, f"eventtype must be one of {GDACS_TYPES}; eventid / episodeid integers")
            continue
        check_geometry(fw, geom, rep)
        if geom.get("type") == "Point" and props.get("Class") == "Point_Centroid":
            events[(etype, eid, ep)] = feat
            _check_gdacs_event(fw, props, at, rep)
        elif geom.get("type") in FOOTPRINT_TYPES:
            footprints.append((i, feat))
        elif geom.get("type") == "Point":
            if not str(props.get("Class") or "").startswith("Point_"):
                rep.error(fw, "non-centroid points need a getgeometry Class (e.g. Point_Polygon_Point_1)")
        else:
            rep.error(fw, f"unexpected geometry type {geom.get('type')}")
    if not events:
        rep.error(w, "no event feature (Point with Class 'Point_Centroid')")
    for i, feat in footprints:
        p = feat["properties"]
        if (p.get("eventtype"), p.get("eventid"), p.get("episodeid")) not in events:
            rep.error(f"{w} feature {i}", "footprint does not match an event (eventtype, eventid, episodeid) in this file")
        if not p.get("Class"):
            rep.error(f"{w} feature {i}", "footprint needs a getgeometry Class (e.g. Poly_Cones, Line_Line_0)")


def _check_gdacs_event(where: str, props: dict[str, Any], at: dt.datetime | None, rep: Report) -> None:
    for key in ("eventname", "name", "description", "alertlevel", "alertscore", "episodealertlevel",
                "episodealertscore", "iscurrent",
                "country", "fromdate", "todate", "datemodified", "iso3", "affectedcountries", "severitydata", "url"):
        if key not in props:
            rep.error(where, f"GDACS event property '{key}' is missing")
    if props.get("alertlevel") not in GDACS_LEVELS or props.get("episodealertlevel") not in GDACS_LEVELS:
        rep.error(where, f"alertlevel / episodealertlevel must be one of {GDACS_LEVELS} (GDACS capitalisation)")
    level, score = props.get("alertlevel"), props.get("alertscore")
    if level in GDACS_LEVEL_SCORE and (isinstance(score, bool) or not isinstance(score, int)
                                       or score != GDACS_LEVEL_SCORE[level]):
        rep.error(where, f"alertscore must be the integer level score {GDACS_LEVEL_SCORE[level]} for {level} "
                         f"(GDACS sends 1 / 2 / 3), got {score!r}")
    if "episodealertscore" in props and not is_number(props.get("episodealertscore")):
        rep.error(where, "episodealertscore must be a number")
    if props.get("iscurrent") not in ("true", "false"):
        rep.error(where, "iscurrent must be the string 'true' or 'false' (as GDACS sends it)")
    times = {k: parse_gdacs_ts(props.get(k)) for k in ("fromdate", "todate", "datemodified")}
    for k, v in times.items():
        if v is None:
            rep.error(where, f"{k} must be naive UTC 'YYYY-MM-DDTHH:MM:SS' (GDACS format)")
    if times["fromdate"] and times["todate"] and times["fromdate"] > times["todate"]:
        rep.error(where, "fromdate is after todate")
    if at and times["datemodified"] and times["datemodified"] > at:
        rep.error(where, f"datemodified {times['datemodified']:%Y-%m-%dT%H:%M}Z is after the step's sim time {at:%Y-%m-%dT%H:%M}Z")
    countries = props.get("affectedcountries")
    if not isinstance(countries, list) or not countries or not all(
        isinstance(c, dict) and {"iso2", "iso3", "countryname"} <= set(c) for c in countries
    ):
        rep.error(where, "affectedcountries must be a non-empty list of {iso2, iso3, countryname}")
    sev = props.get("severitydata")
    if not isinstance(sev, dict) or not is_number(sev.get("severity")) or not isinstance(sev.get("severitytext"), str) \
            or not isinstance(sev.get("severityunit"), str):
        rep.error(where, "severitydata must be {severity: number, severitytext: str, severityunit: str}")
    urls = props.get("url")
    if not isinstance(urls, dict) or not urls:
        rep.error(where, "url must be an object {geometry, report, details}")
    else:
        for k, v in urls.items():
            if not fictional_url(v):
                rep.error(where, f"url.{k} must be on a reserved .example host (simulated data never links a real report)")
    for k in ("icon", "iconoverall"):
        if k in props and not fictional_url(props[k]):
            rep.error(where, f"{k} must be on a reserved .example host")
    if "simulated" not in str(props.get("source", "")).lower() and "simulated" not in str(props.get("htmldescription", "")).lower():
        rep.error(where, "source or htmldescription must say SIMULATED")


def _check_usgs(where: str, path: Path, at: dt.datetime | None, rep: Report) -> None:
    w = f"{where} {path.name}"
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        rep.error(w, f"not valid JSON: {exc}")
        return
    if not isinstance(doc, dict) or doc.get("type") != "FeatureCollection" or not isinstance(doc.get("features"), list):
        rep.error(w, "must be a GeoJSON FeatureCollection (USGS summary feed shape)")
        return
    meta = doc.get("metadata")
    if not isinstance(meta, dict) or not {"generated", "url", "title", "count"} <= set(meta):
        rep.error(w, "metadata {generated, url, title, count} is required (USGS summary feed)")
    elif not fictional_url(meta.get("url")):
        rep.error(w, "metadata.url must be on a reserved .example host")
    for i, feat in enumerate(doc["features"]):
        fw = f"{w} feature {i}"
        props, geom = (feat or {}).get("properties"), (feat or {}).get("geometry")
        if not isinstance(props, dict) or not isinstance(feat.get("id"), str):
            rep.error(fw, "needs an id and properties")
            continue
        for key in ("mag", "place", "time", "updated", "url", "tsunami", "sig", "type", "title"):
            if key not in props:
                rep.error(fw, f"USGS property '{key}' is missing")
        if not isinstance(props.get("time"), int) or isinstance(props.get("time"), bool):
            rep.error(fw, "time must be epoch milliseconds (int)")
        elif at and props["time"] > at.timestamp() * 1000:
            rep.error(fw, "time is after the step's sim time")
        if not fictional_url(props.get("url")):
            rep.error(fw, "url must be on a reserved .example host")
        c = (geom or {}).get("coordinates")
        if (geom or {}).get("type") != "Point" or not isinstance(c, list) or len(c) != 3 or not all(is_number(v) for v in c):
            rep.error(fw, "geometry must be a Point [lon, lat, depth_km] (USGS shape)")


def _check_rss_payload(where: str, payload: dict[str, Any], rep: Report) -> None:
    cid = payload.get("connector_id")
    if cid is not None and (not isinstance(cid, str) or not CONNECTOR_RE.match(cid) or not cid.startswith("rss.")):
        rep.error(where, "connector_id must look like 'rss.<slug>'")
    name = payload.get("source_name")
    if name is not None and (not isinstance(name, str) or not 1 <= len(name) <= 200):
        rep.error(where, "source_name must be 1..200 characters")


def _check_feed(where: str, path: Path, at: dt.datetime | None, rep: Report,
                connector: str = "rss.demo") -> list[NewsItem]:
    w = f"{where} {path.name}"
    body = path.read_bytes()
    if b"SIMULATED" not in body:
        rep.error(w, "the feed must carry a 'SIMULATED' comment")
    try:
        root = _xml_root(body)
    except Exception as exc:  # parser errors vary
        rep.error(w, f"not well-formed XML: {type(exc).__name__}: {exc}")
        return []
    items: list[NewsItem] = []
    raw_count = 0
    if _local(root.tag) == "rss":
        if root.get("version") != "2.0":
            rep.error(w, "RSS root needs version='2.0'")
        channel = root.find("channel")
        if channel is None:
            rep.error(w, "RSS needs a <channel>")
            return []
        for tag in ("title", "link", "description"):
            if _text(channel.find(tag)) is None:
                rep.error(w, f"<channel> needs <{tag}>")
        if not fictional_url(_text(channel.find("link"))):
            rep.error(w, "channel <link> must be on a reserved .example host")
        for tag in ("lastBuildDate", "pubDate"):
            value = _text(channel.find(tag))
            if value is None:
                continue
            when = parse_rfc822(value)
            if when is None:
                rep.error(w, f"channel <{tag}> {value!r} is not an RFC 822 date with a zone")
                continue
            _check_weekday(w, f"channel <{tag}>", value, when, rep)
            if at and when > at:
                rep.error(w, f"channel <{tag}> {when.isoformat()} is after the step's sim time {at.isoformat()}")
        raw_count = len(channel.findall("item"))
        for j, item in enumerate(channel.findall("item")):
            iw = f"{w} item {j}"
            title, link, guid = _text(item.find("title")), _text(item.find("link")), _text(item.find("guid"))
            pub = _text(item.find("pubDate"))
            desc = _text(item.find("description"))
            if not title or not link or not guid or not pub or not desc:
                rep.error(iw, "needs <title>, <link>, <guid>, <pubDate> and <description>")
                continue
            if not fictional_url(link):
                rep.error(iw, "<link> must be on a reserved .example host")
            if guid.startswith("http") and not fictional_url(guid):
                rep.error(iw, "a URL <guid> must be on a reserved .example host")
            published = parse_rfc822(pub)
            if published is None:
                rep.error(iw, f"<pubDate> {pub!r} is not an RFC 822 date with a zone")
            else:
                _check_weekday(iw, "<pubDate>", pub, published, rep)
            items.append(_news_item(iw, title, desc, (guid, link, strip_html(title)), published, at, rep,
                                    f"{connector}|{guid}"))
    elif _local(root.tag) == "feed" and root.tag.startswith(ATOM):
        raw_count = len(root.findall(f"{ATOM}entry"))
        feed_updated = _text(root.find(f"{ATOM}updated"))
        if feed_updated is not None:
            when = parse_iso(feed_updated)
            if when is None:
                rep.error(w, f"feed <updated> {feed_updated!r} is not RFC 3339 with a zone")
            elif at and when > at:
                rep.error(w, f"feed <updated> {when.isoformat()} is after the step's sim time {at.isoformat()}")
        for j, entry in enumerate(root.findall(f"{ATOM}entry")):
            iw = f"{w} entry {j}"
            title = _text(entry.find(f"{ATOM}title"))
            link_el = next((ln for ln in entry.findall(f"{ATOM}link") if ln.get("rel") in (None, "alternate")), None)
            link = link_el.get("href") if link_el is not None else None
            eid = _text(entry.find(f"{ATOM}id"))
            when = _text(entry.find(f"{ATOM}published")) or _text(entry.find(f"{ATOM}updated"))
            summary = _text(entry.find(f"{ATOM}summary"))
            if not title or not link or not eid or not when or not summary:
                rep.error(iw, "needs <title>, <link rel=alternate href>, <id>, <published|updated> and <summary>")
                continue
            if not fictional_url(link):
                rep.error(iw, "<link href> must be on a reserved .example host")
            if not (fictional_url(eid) or tag_uri_fictional(eid) or eid.startswith("urn:uuid:")):
                rep.error(iw, "<id> must be a tag: URI on a .example authority, a .example URL or a urn:uuid")
            published = parse_iso(when)
            if published is None:
                rep.error(iw, f"<published> {when!r} is not RFC 3339 with a zone")
            items.append(_news_item(iw, title, summary, (eid, link, strip_html(title)), published, at, rep,
                                    f"{connector}|{eid}"))
    else:
        rep.error(w, "must be RSS 2.0 (<rss version='2.0'>) or Atom 1.0 (<feed xmlns=Atom>)")
    if not items:
        rep.error(w, "the feed has no items")
    if raw_count > HUB_MAX_FEED_ITEMS:
        rep.error(w, f"the feed has {raw_count} items; the hub's RSS adapter reads only the first "
                     f"{HUB_MAX_FEED_ITEMS} (the rest would be silently dropped)")
    guids = [it.key_candidates[0] for it in items]
    if len(set(guids)) != len(guids):
        rep.error(w, "item guids / ids must be unique")
    return items


WEEKDAYS: Final = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _check_weekday(where: str, label: str, raw: str, when: dt.datetime, rep: Report) -> None:
    """RFC 822 dates repeat the weekday; parsers ignore it, so a wrong one slips through unnoticed."""
    m = re.match(r"^\s*([A-Za-z]{3}),", raw)
    if m and m.group(1).title() != WEEKDAYS[when.weekday()]:
        rep.error(where, f"{label} {raw!r}: {when:%Y-%m-%d} is a {WEEKDAYS[when.weekday()]}, not a {m.group(1)}")


def _news_item(where: str, title: str, raw_desc: str, keys: tuple[str, ...], published: dt.datetime | None,
               at: dt.datetime | None, rep: Report, dedupe: str = "") -> NewsItem:
    snippet = strip_html(raw_desc)
    if len(snippet) > MAX_SNIPPET:
        rep.error(where, f"snippet is {len(snippet)} characters after HTML stripping; the hub keeps {MAX_SNIPPET} "
                         "(keep it shorter so the pre-baked extraction matches what is stored)")
    if len(strip_html(title)) > MAX_TITLE:
        rep.error(where, f"title is longer than {MAX_TITLE} characters")
    if at and published and published > at:
        rep.error(where, f"published {published.isoformat()} is after the step's sim time {at.isoformat()}")
    return NewsItem(keys, f"{strip_html(title)}\n{snippet}", published, dedupe)


def uuid7_time(value: Any) -> dt.datetime | None:
    """The creation time of a UUIDv7 (its 48-bit millisecond prefix), None for other UUIDs."""
    try:
        u = uuid.UUID(str(value))
    except ValueError:
        return None
    if u.version != 7:
        return None
    return dt.datetime.fromtimestamp(int(u.hex[:12], 16) / 1000, dt.UTC)


def _check_uuid7(where: str, label: str, value: Any, ref: dt.datetime | None, at: dt.datetime | None,
                 rep: Report) -> None:
    """A uuidv7 id embeds its creation time: it must agree with the event's own time (Merxa ids are uuidv7)."""
    t = uuid7_time(value)
    if t is None:
        return
    if ref is not None and abs(t - ref) > UUID7_SKEW:
        rep.error(where, f"{label} is a uuidv7 created {t:%Y-%m-%dT%H:%MZ}, not at {ref:%Y-%m-%dT%H:%MZ} "
                         "(regenerate it from the event time)")
    if at is not None and t > at:
        rep.error(where, f"{label} is a uuidv7 created after the step's sim time")


def _collapse(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _check_connector_event(where: str, raw: Any, at: dt.datetime | None, rep: Report) -> list[NewsItem]:
    if not isinstance(raw, dict):
        rep.error(where, "the Merxa event must be an object {type, id, timestamp, data}")
        return []
    for key in ("type", "id", "timestamp", "data"):
        if key not in raw:
            rep.error(where, f"Merxa webhook body needs '{key}' (docs/contracts/22 §5)")
    etype = raw.get("type")
    if etype not in CONNECTOR_EVENT_TYPES:
        rep.error(where, f"type must be one of {CONNECTOR_EVENT_TYPES} (the hub ignores others)")
    if not isinstance(raw.get("id"), str) or not raw.get("id"):
        rep.error(where, "id must be a non-empty string")
    ts = parse_iso(raw.get("timestamp"))
    if ts is None:
        rep.error(where, "timestamp must be ISO 8601 with a zone")
    elif at and ts > at:
        rep.error(where, "timestamp is after the step's sim time")
    _check_uuid7(where, "id", raw.get("id"), ts, at, rep)
    data = raw.get("data")
    if not isinstance(data, dict):
        rep.error(where, "data must be an object")
        return []
    if etype != "supplier_message.received":
        return []
    try:
        uuid.UUID(str(data.get("id")))
    except ValueError:
        rep.error(where, "data.id must be a UUID (Merxa ids are uuidv7)")
    code = data.get("supplier_code")
    if not isinstance(code, str) or not re.match(r"^[A-Za-z0-9._>-]{1,64}$", code):
        rep.error(where, "data.supplier_code must be a supplier code")
    received = parse_iso(data.get("received_at"))
    if received is None:
        rep.error(where, "data.received_at must be ISO 8601 with a zone")
    elif at and received > at:
        rep.error(where, "data.received_at is after the step's sim time")
    _check_uuid7(where, "data.id", data.get("id"), received, at, rep)
    subject, body = data.get("subject"), data.get("body")
    if not isinstance(subject, str) or not subject.strip() or len(subject) > MERXA_MAX_SUBJECT:
        rep.error(where, f"data.subject must be 1..{MERXA_MAX_SUBJECT} characters (Merxa limit)")
        subject = ""
    elif len(_collapse(subject)) > MAX_TITLE:
        rep.error(where, f"data.subject is longer than the hub keeps ({MAX_TITLE}, the signal title); "
                         "the extraction would not match")
    if not isinstance(body, str) or not body.strip() or len(body) > MERXA_MAX_BODY:
        rep.error(where, f"data.body must be 1..{MERXA_MAX_BODY} characters (Merxa limit)")
        body = ""
    elif len(_collapse(body)) > MAX_HUB_TEXT:
        rep.error(where, f"data.body is longer than the hub keeps ({MAX_HUB_TEXT}); the extraction would not match")
    connector = str(raw.get("connector_id") or "merxa")
    native = f"supplier_message/{data.get('id') or raw.get('id')}"
    # the hub collapses whitespace (``clip``) before the quarantine sees the text
    text = _collapse(subject) + "\n" + _collapse(body)
    return [NewsItem((f"supplier_message.received:{raw.get('id')}",), text,
                     received, f"{connector}|{native}")]


def _check_extractions(where: str, payload: dict[str, Any], items: list[NewsItem], rep: Report,
                       mods: dict[str, Any], seen: dict[str, tuple[int, str, Any]] | None = None,
                       step: int = -1) -> None:
    by_key = payload.get("extractions")
    default = payload.get("extraction")
    if by_key is not None and (not isinstance(by_key, dict) or not by_key):
        rep.error(where, "extractions must be a non-empty mapping {guid|link|title: extraction}")
        by_key = {}
    by_key = by_key or {}
    used: set[str] = set()
    for item in items:
        key = next((k for k in item.key_candidates if k in by_key), None)
        extraction = by_key.get(key) if key else default
        if key:
            used.add(key)
        if seen is not None and item.dedupe:
            first = seen.get(item.dedupe)
            if first is None:
                seen[item.dedupe] = (step, item.text, extraction)
            elif (first[1], first[2]) != (item.text, extraction):
                what = "text" if first[1] != item.text else "pre-baked extraction"
                rep.error(where, f"item {item.key_candidates[0]!r} repeats the item of step {first[0]} with a different "
                                 f"{what}; the hub stores a news item once per guid and never re-reads it, so the "
                                 "change would be ignored (publish the update under a new guid)")
        if extraction is None:
            rep.error(where, f"item {item.key_candidates[0]!r} has no pre-baked extraction (it would wait for the "
                             "LLM as pending_extraction; demo packs must not need an LLM)")
            continue
        _check_one_extraction(f"{where} extraction[{item.key_candidates[0]}]", extraction, item.text, rep, mods)
    for key in sorted(set(by_key) - used):
        rep.error(where, f"extractions key {key!r} matches no item (keys are the item guid / link / title)")
    if default is not None and not items:
        rep.error(where, "extraction matches no free-text item (this step produces none)")


def _check_one_extraction(where: str, ex: Any, text: str, rep: Report, mods: dict[str, Any]) -> None:
    if not isinstance(ex, dict):
        rep.error(where, "must be a mapping")
        return
    if set(ex) != set(EXTRACTION_KEYS):
        rep.error(where, f"keys must be exactly {list(EXTRACTION_KEYS)} (strict NewsExtraction schema)")
        return
    if ex["kind"] not in SIGNAL_KINDS:
        rep.error(where, f"kind must be one of {SIGNAL_KINDS}")
    if not isinstance(ex["subtype"], str) or not SUBTYPE_RE.match(ex["subtype"]):
        rep.error(where, "subtype must be snake_case (1..64)")
    if ex["injection_suspected"] is not False:
        rep.error(where, "injection_suspected must be false (a flagged extraction is dropped by the hub)")
    folded = text.casefold()
    for name in MENTION_LISTS:
        values = ex[name]
        if not isinstance(values, list) or len(values) > MAX_MENTIONS:
            rep.error(where, f"{name} must be a list of at most {MAX_MENTIONS} strings")
            continue
        for v in values:
            if not isinstance(v, str) or not 1 <= len(v.strip()) <= MAX_MENTION or len(v) > MAX_MENTION:
                rep.error(where, f"{name}: {v!r} must be 1..{MAX_MENTION} characters")
            elif v.casefold() not in folded:
                rep.error(where, f"{name}: {v!r} does not appear in the item text (extractions copy names as written)")
    if "contracts" in mods:
        c_llm = mods["contracts"][1]
        try:
            model = c_llm.NewsExtraction.model_validate(ex)
        except Exception as exc:  # pydantic ValidationError
            rep.error(where, f"NewsExtraction contract: {exc}")
            return
        for problem in c_llm.news_extraction_violations(model):
            rep.error(where, f"news_extraction_violations: {problem}")


# ───────────────────────────── level 3: replay check ────────────────────────


@dataclass
class SimSignal:
    key: str
    step: int
    draft: Any  # signal_hub.models.SignalDraft


def _strip_key(native_key: str, pack_id: str) -> str:
    prefix = f"{TENANT}/{pack_id}/"
    return native_key[len(prefix):] if native_key.startswith(prefix) else native_key


def simulate(pack_dir: Path, data: dict[str, Any], hub: dict[str, Any], mods: dict[str, Any],
             rep: Report) -> list[dict[str, SimSignal]]:
    """Run every step through the real adapters; returns the current signals after each step."""
    c_signals, _c_llm = mods["contracts"]
    pack = c_signals.ScenarioPack.model_validate(data)
    gaz = hub["Gazetteer"](GEO_DIR)
    replay = hub["replay"]
    current: dict[str, SimSignal] = {}
    out: list[dict[str, SimSignal]] = []
    seen_news: set[tuple[str, str]] = set()  # the hub stores a news item once (ON CONFLICT DO NOTHING)
    for index, step in enumerate(pack.steps):
        where = f"{pack.id} step {index} (replay)"
        at = pack.start_at + dt.timedelta(seconds=step.t_offset_s)
        ctx = hub["SourceContext"](retrieved_at=at, tenant=TENANT, pack_id=pack.id)
        payload = step.payload
        drafts: list[Any] = []
        news: list[Any] = []

        def fixture_bytes(name: Any) -> bytes:
            path = replay.fixture_path(SCENARIOS_DIR, pack.id, name)  # the hub's own traversal / type check
            return path.read_bytes()

        try:
            if step.adapter == "gdacs":
                drafts = hub["gdacs"].parse_gdacs(json.loads(fixture_bytes(payload.get("fixture"))), ctx, gaz)
            elif step.adapter == "usgs":
                drafts = hub["usgs"].parse_usgs(json.loads(fixture_bytes(payload.get("fixture"))), ctx, gaz)
            elif step.adapter == "rss":
                news = hub["rss"].parse_feed(
                    fixture_bytes(payload.get("fixture")), ctx,
                    connector_id=str(payload.get("connector_id") or "rss.demo"),
                    source_name=str(payload.get("source_name") or "Demo Freight Daily"),
                )
            elif step.adapter == "merxa_inject":
                raw = payload.get("event")
                if raw is None:
                    raw = json.loads(fixture_bytes(payload.get("fixture")))
                event = hub["connector"].ConnectorEvent.model_validate(raw)
                drafts, news = hub["connector"].parse_connector_event(event, ctx)
        except Exception as exc:  # report any adapter failure
            rep.error(where, f"the real {step.adapter} adapter failed: {type(exc).__name__}: {exc}")
            out.append(dict(current))
            continue
        if step.adapter in ("gdacs", "usgs", "rss") and not (drafts or news):
            rep.error(where, f"the real {step.adapter} adapter produced nothing from the fixture")
        if step.adapter == "merxa_inject" and not (drafts or news):
            rep.error(where, "the connector adapter produced neither a fact signal nor a free-text item")
        drafts = list(drafts)
        by_key = payload.get("extractions") if isinstance(payload.get("extractions"), dict) else {}
        default = payload.get("extraction") if isinstance(payload.get("extraction"), dict) else None
        for item in news:
            if (item.connector_id, item.native_key) in seen_news:
                continue  # Pipeline.queue_news: a known item is neither re-stored nor re-extracted
            seen_news.add((item.connector_id, item.native_key))
            native = str(item.context.get("native_id") or "")
            prebaked = by_key.get(native) or by_key.get(item.url or "") or by_key.get(item.title) or default
            # exactly the text Pipeline.process_news screens (clip collapses whitespace, newline included)
            text = hub["clip"](f"{item.title}\n{item.snippet or ''}", 4000) or item.title
            outcome = asyncio.run(hub["quarantine"].run_quarantine(text, None, prebaked=prebaked))
            if outcome.status != "extracted":
                rep.error(where, f"item {native!r}: quarantine outcome {outcome.status!r} "
                                 f"({outcome.error or outcome.guard}); the demo needs 'extracted'")
                continue
            row = dataclasses.asdict(item)
            drafts.append(hub["pipeline"].draft_from_extraction(row, outcome.extraction, outcome.extracted_by, gaz))
        for d in drafts:
            if d.origin != "demo" or d.provenance != "simulated" or d.pack_id != pack.id or d.tenant != TENANT:
                rep.error(where, f"{d.native_key}: replay output must be origin demo / provenance simulated / tenant-scoped")
            if d.observed_at > at:
                rep.error(where, f"{d.native_key}: observed_at {d.observed_at.isoformat()} is after the sim time")
            try:
                d.to_signal(signal_id=c_signals.signal_id(d.connector_id, d.native_key, 1), version=1,
                            supersedes=None, confidence_bp=5000, event_id=None)
            except Exception as exc:  # pydantic ValidationError
                rep.error(where, f"{d.native_key}: DisruptionSignal contract: {exc}")
            key = _strip_key(d.native_key, pack.id)
            prev = current.get(key)
            if prev is None or prev.draft.content_hash() != d.content_hash():
                current[key] = SimSignal(key, index, d)
        out.append(dict(current))
    return out


@dataclass
class NetNode:
    type: str
    code: str
    pos: Pos


def load_network(path: Path, rep: Report, hub: dict[str, Any]) -> tuple[list[NetNode], dict[str, list[Pos]]]:
    data = load_yaml(path)
    ports = json.loads((GEO_DIR / "ports.json").read_text(encoding="utf-8"))["ports"]
    centroids = json.loads((GEO_DIR / "country_centroids.json").read_text(encoding="utf-8"))["centroids"]
    routes = json.loads((GEO_DIR / "sea_routes.json").read_text(encoding="utf-8"))
    nodes: list[NetNode] = []
    for n in data["nodes"]:
        if "at" in n:
            kind, _, ref = str(n["at"]).partition(":")
            if kind == "port" and ref in ports:
                pos = (float(ports[ref]["lon"]), float(ports[ref]["lat"]))
            elif kind == "country" and ref in centroids:
                pos = (float(centroids[ref][0]), float(centroids[ref][1]))
            else:
                rep.error(path.name, f"node {n['code']}: cannot resolve at={n['at']!r}")
                continue
        else:
            pos = (float(n["lon"]), float(n["lat"]))
        nodes.append(NetNode(str(n["type"]), str(n["code"]), pos))
    where: dict[str, tuple[Pos, bool]] = {}
    for n in nodes:
        if n.type in ("port", "dc", "factory", "supplier"):
            where.setdefault(n.code, (n.pos, n.type == "port"))
    route_list = [(r["from"], r["to"], r["via"], [(float(p[0]), float(p[1])) for p in r["path"]])
                  for r in routes["routes"]]
    place_kind = {k: v.get("kind") for k, v in routes["places"].items()}

    def sea_route(a: str, b: str, via: Sequence[str]) -> list[Pos] | None:
        wanted = [c for c in via if c in ("SUEZ", "CAPE")]
        order = [*wanted, *(c for c in ("SUEZ", "CAPE") if c not in wanted), None]
        for choke in order:
            for o, d, v, p in route_list:
                if v != choke:
                    continue
                if (o, d) == (a, b):
                    return list(p)
                if (o, d) == (b, a):
                    return list(reversed(p))
        return None

    lanes: dict[str, list[Pos]] = {}
    for ln in data["lanes"]:
        origin, dest = ln["origin"], ln["dest"]
        if origin not in where or dest not in where:
            rep.error(path.name, f"lane {ln['code']}: unknown endpoint")
            continue
        via = [str(v) for v in ln.get("via") or []]
        stops: list[tuple[str, Pos, bool]] = [(origin, *where[origin])]
        for v in via:
            if v in where and where[v][1]:
                stops.append((v, where[v][0], True))
            elif place_kind.get(v) == "port":
                stops.append((v, (float(routes["places"][v]["lon"]), float(routes["places"][v]["lat"])), True))
        stops.append((dest, *where[dest]))
        mode = ln.get("mode", "sea")
        path_pts: list[Pos] = [stops[0][1]]
        i = 0
        while i < len(stops) - 1:
            jump = None
            if mode in ("sea", "sea_air") and stops[i][2]:
                for j in range(len(stops) - 1, i, -1):
                    if stops[j][2] and (r := sea_route(stops[i][0], stops[j][0], via)):
                        jump = (j, r)
                        break
            if jump is None:
                path_pts.append(stops[i + 1][1])
                i += 1
                continue
            j, r = jump
            r[0], r[-1] = stops[i][1], stops[j][1]
            path_pts.extend(r[1:])
            i = j
        lanes[str(ln["code"])] = densify(path_pts)
    return nodes, lanes


def _bbox(points: Iterable[Pos]) -> tuple[float, float, float, float]:
    pts = list(points)
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


def _near_bbox(a: tuple[float, float, float, float], b: tuple[float, float, float, float], pad: float) -> bool:
    return not (a[2] + pad < b[0] or b[2] + pad < a[0] or a[3] + pad < b[1] or b[3] + pad < a[1])


def compute_exposures(shape: Shape, radius_km: float, refs: Sequence[str], nodes: list[NetNode],
                      lanes: dict[str, list[Pos]], where: str, rep: Report) -> dict[tuple[str, str], str]:
    matches: dict[tuple[str, str], str] = {}

    def put(key: tuple[str, str], method: str) -> None:
        cur = matches.get(key)
        if cur is None or PRECEDENCE[method] < PRECEDENCE[cur]:
            matches[key] = method

    if not shape.empty:
        for n in nodes:
            inside = shape.contains(n.pos)
            dist = 0.0 if inside else shape.boundary_distance_km(n.pos)
            edge = shape.ring_distance_km(n.pos)
            if edge < MARGIN_KM:
                rep.warn(where, f"{n.type} {n.code} is {edge:.1f} km from the footprint boundary "
                                f"(intersects vs dwithin may differ under PostGIS)")
            if inside:
                put((n.type, n.code), "intersects")
            elif dist <= radius_km:
                if radius_km - dist < MARGIN_KM:
                    rep.error(where, f"{n.type} {n.code} is {dist:.1f} km away, within {MARGIN_KM} km of the "
                                     f"{radius_km:.0f} km radius (fragile)")
                put((n.type, n.code), "dwithin_km")
            elif dist - radius_km < MARGIN_KM:
                rep.error(where, f"{n.type} {n.code} is {dist:.1f} km away, just outside the {radius_km:.0f} km radius (fragile)")
        gbox = _bbox(shape.vertices())
        for code, path in lanes.items():
            if not _near_bbox(gbox, _bbox(path), 1.0):
                continue
            crosses, dist = shape.line_relation(path)
            if crosses or dist <= LANE_BUFFER_KM:
                if not crosses and LANE_BUFFER_KM - dist < MARGIN_KM:
                    rep.error(where, f"lane {code} passes {dist:.1f} km from the footprint (fragile buffer)")
                put(("lane", code), "lane_crosses")
            elif dist - LANE_BUFFER_KM < MARGIN_KM:
                rep.error(where, f"lane {code} passes {dist:.1f} km from the footprint, just outside the buffer (fragile)")
    wanted: dict[str, set[str]] = {}
    for ref in refs:
        scheme, _, code = ref.partition(":")
        if scheme in REF_NODE_TYPES:
            wanted.setdefault(REF_NODE_TYPES[scheme], set()).add(code)
    for code in sorted(wanted.pop("lane", set())):
        if code in lanes:
            put(("lane", code), "entity_match")
    for n in nodes:
        if n.code in wanted.get(n.type, set()):
            put((n.type, n.code), "entity_match")
    return matches


def _strong(refs: Iterable[str]) -> set[str]:
    return {r for r in refs if r.partition(":")[0] in STRONG_SCHEMES}


def check_expected(pack_dir: Path, data: dict[str, Any], hub: dict[str, Any], mods: dict[str, Any],
                   rep: Report) -> None:
    where = f"{pack_dir.name} expected.yaml"
    path = pack_dir / "expected.yaml"
    if not path.is_file():
        return
    exp = load_yaml(path)
    if not isinstance(exp, dict) or exp.get("pack") != pack_dir.name or not isinstance(exp.get("checkpoints"), list):
        rep.error(where, "must be {pack: <id>, network: <file>, checkpoints: [...]}")
        return
    c_signals = mods["contracts"][0]
    snapshots = simulate(pack_dir, data, hub, mods, rep)
    nodes, lanes = load_network(SCENARIOS_DIR / str(exp.get("network") or NETWORK_DEFAULT), rep, hub)
    cluster, scoring = hub["cluster"], hub["scoring"]
    seen_steps: set[int] = set()
    for cp in exp["checkpoints"]:
        step = cp.get("after_step")
        if not isinstance(step, int) or not 0 <= step < len(snapshots):
            rep.error(where, f"after_step {step!r} is not a step index")
            continue
        seen_steps.add(step)
        cw = f"{pack_dir.name} after step {step}"
        signals = snapshots[step]
        claimed: set[str] = set()
        events = cp.get("events") or []
        if "signals" in cp and cp["signals"] != len(signals):
            rep.error(cw, f"expected {cp['signals']} current signals, the replay has {len(signals)}: {sorted(signals)}")
        event_geoms: list[tuple[str, Shape, set[str], Any]] = []
        for ev in events:
            label = ev.get("label", "?")
            ew = f"{cw} event {label!r}"
            members = ev.get("members") or []
            missing = [m for m in members if m not in signals]
            if missing:
                rep.error(ew, f"members not produced by the replay: {missing} (have {sorted(signals)})")
                continue
            claimed.update(members)
            sims = [signals[m] for m in members]
            anchors = [s for s in sims if s.draft.authoritative]
            lead = anchors[0] if anchors else min(sims, key=lambda s: (s.draft.observed_at, s.key))
            if members and members[0] != lead.key:
                rep.error(ew, f"the first member must be the event lead ({lead.key!r}: the anchor, else the earliest)")
            geoms = [s.draft.geometry for s in [*anchors, *[s for s in sims if not s.draft.authoritative]] if s.draft.geometry]
            merged: dict[str, Any] = {"type": "GeometryCollection", "geometries": [g for geo_ in geoms for g in _flatten(geo_)]}
            shape = Shape.of(merged)
            refs = sorted({r for s in sims for r in s.draft.location_refs})
            # attach rules (cluster v1): every non-lead member must be attachable to the lead's event
            for s in sims:
                if s is lead:
                    continue
                ok_kind = cluster.kinds_compatible(s.draft.kind, lead.draft.kind)
                ok_time = abs(s.draft.observed_at - lead.draft.observed_at) <= ATTACH_WINDOW
                near = False
                if s.draft.geometry and not shape.empty:
                    sshape = Shape.of(s.draft.geometry)
                    near = any(shape.distance_km(v) <= ATTACH_RADIUS_KM for v in sshape.vertices())
                shared = bool(_strong(s.draft.location_refs) & _strong(lead.draft.location_refs))
                if not (ok_kind and ok_time and (near or shared)):
                    rep.error(ew, f"member {s.key!r} would not attach (kind ok={ok_kind}, time ok={ok_time}, "
                                  f"within {ATTACH_RADIUS_KM:.0f} km={near}, shared ref={shared})")
            kind, subtype = lead.draft.kind, lead.draft.subtype
            mag = lead.draft.severity_native.get("mag") if isinstance(lead.draft.severity_native, dict) else None
            radius = cluster.radius_km(subtype, magnitude=mag)
            severity = scoring.event_severity_bp([s.draft.severity_bp for s in anchors], [s.draft.severity_bp for s in sims])
            for k, actual in (("kind", kind), ("subtype", subtype), ("severity_bp", severity), ("radius_km", radius)):
                if k in ev and ev[k] != actual:
                    rep.error(ew, f"{k}: expected {ev[k]!r}, replay gives {actual!r}")
            matches = compute_exposures(shape, float(radius), refs, nodes, lanes, ew, rep)
            actual_exp = {t: {} for t in NODE_TYPES}
            for (t, code), method in sorted(matches.items()):
                actual_exp[t][code] = method
            want = ev.get("exposures") or {}
            for t in NODE_TYPES:
                w_t = {str(k): str(v) for k, v in (want.get(t) or {}).items()}
                a_t = actual_exp[t]
                if w_t != a_t:
                    rep.error(ew, f"{t} exposures differ: expected {w_t}, replay gives {a_t}")
            for t in want:
                if t not in NODE_TYPES:
                    rep.error(ew, f"unknown node type {t!r} in exposures")
            exposures = [c_signals.Exposure(event_id="00000000-0000-5000-8000-000000000000", tenant=TENANT,
                                            node_type=t, node_code=code, method=m,
                                            distance_km=0 if m in ("intersects", "lane_crosses") else (None if m == "entity_match" else 1),
                                            weight_bp=10_000)
                         for (t, code), m in matches.items()]
            counts = c_signals.NodeCounts.of(exposures)
            decision = c_signals.decide_alert_action(c_signals.DEFAULT_ALERT_RULES, severity_bp=severity,
                                                     confidence_bp=10_000, kind=kind, counts=counts)
            got_alert = None
            if decision is not None and decision[0] != "ignore":
                got_alert = {"action": decision[0], "severity": c_signals.alert_severity_for(severity)}
            if "alert" in ev and ev["alert"] != got_alert:
                rep.error(ew, f"alert: expected {ev['alert']!r}, replay gives {got_alert!r}")
            event_geoms.append((label, shape, _strong(refs), lead))
        unclaimed = set(signals) - claimed
        if unclaimed:
            rep.error(cw, f"current signals not assigned to an expected event: {sorted(unclaimed)}")
        # separate events must not attach to each other
        for i, (la, sa, ra, lead_a) in enumerate(event_geoms):
            for lb, sb, rb, lead_b in event_geoms[i + 1:]:
                if not cluster.kinds_compatible(lead_a.draft.kind, lead_b.draft.kind):
                    continue
                if abs(lead_a.draft.observed_at - lead_b.draft.observed_at) > ATTACH_WINDOW:
                    continue
                near = (not sa.empty and not sb.empty
                        and any(sa.distance_km(v) <= ATTACH_RADIUS_KM for v in sb.vertices()))
                if near or (ra & rb):
                    rep.error(cw, f"events {la!r} and {lb!r} would cluster into one event")
    data_steps = len(data.get("steps") or [])
    hint_steps = {i for i, s in enumerate(data.get("steps") or []) if s.get("adapter") == "chat_hint"}
    for i in range(data_steps):
        if i not in seen_steps and i not in hint_steps:
            rep.error(where, f"no checkpoint after step {i} (every ingesting step needs one)")


# ─────────────────────────────────── main ───────────────────────────────────


def discover(names: Sequence[str]) -> list[Path]:
    if names:
        return [SCENARIOS_DIR / n for n in names]
    return sorted(p for p in SCENARIOS_DIR.iterdir() if p.is_dir() and (p / "pack.yaml").is_file())


def validate(names: Sequence[str] = (), *, require: str | None = None, mods: dict[str, Any] | None = None) -> Report:
    mods = _try_imports() if mods is None else mods
    rep = Report()
    level = "replay" if "hub" in mods else ("contract" if "contracts" in mods else "shape")
    if require and LEVELS.index(level) < LEVELS.index(require):
        reason = mods.get("hub_error") if require == "replay" else mods.get("contracts_error")
        rep.error("validate", f"level {require!r} required but only {level!r} is available ({reason})")
    rep.info("validate", f"level: {level}")
    packs = discover(names)
    if not packs:
        rep.error("validate", f"no packs under {SCENARIOS_DIR}")
    for pack_dir in packs:
        if not (pack_dir / "pack.yaml").is_file():
            rep.error(pack_dir.name, "pack.yaml not found")
            continue
        before = len(rep.errors)
        data = check_pack(pack_dir, rep, mods)
        if data is None or len(rep.errors) > before:
            continue  # replay only on a structurally valid pack
        if "hub" in mods:
            check_expected(pack_dir, data, mods["hub"], mods, rep)
        rep.info(pack_dir.name, "checked")
    return rep


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Validate the Signal Hub demo packs offline.")
    ap.add_argument("packs", nargs="*", help="pack ids (default: every directory with a pack.yaml)")
    ap.add_argument("--require", choices=LEVELS, default=None, help="fail unless this level can run")
    args = ap.parse_args(argv)
    rep = validate(args.packs, require=args.require)
    for line in rep.infos:
        print(f"info  {line}")
    for line in rep.warnings:
        print(f"WARN  {line}")
    for line in rep.errors:
        print(f"ERROR {line}")
    print(f"{len(rep.errors)} error(s), {len(rep.warnings)} warning(s)")
    return 1 if rep.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
