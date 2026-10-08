"""Deterministic gazetteer over the repo's geo data (``100`` §4).

Sources (``GEO_DIR``, default ``/app/geo`` = ``data/geo``):

* ``ports.json`` — UN/LOCODE subset with float WGS84 coordinates and aliases;
* ``chokepoints.json`` — chokepoint polygons (integer degrees x 1e6) and aliases;
* ``regions.json`` — ISO 3166-1 countries (alpha-2/alpha-3, names, aliases) and multi-country regions;
* ``country_centroids.json`` — country centroids (kept for callers that need a position).

Matching is on the *whole* mention after normalisation (case, accents and punctuation ignored, a
leading ``the``/``port of`` and a trailing ``port``/``terminal`` dropped). Mentions come from the
quarantined extractor and are untrusted: they can only ever select an entry of this curated table,
never introduce a new code or coordinate.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

__all__ = ["Chokepoint", "Gazetteer", "GeoResult", "Port", "load_gazetteer", "normalise"]

_PUNCT_RE = re.compile(r"[^a-z0-9]+")
_LEADING = ("the ", "port of ")
_TRAILING = (" container terminal", " terminal", " seaport", " port", " harbour", " harbor")
MAX_MENTION_CHARS = 80


def normalise(text: str) -> str:
    """Lowercase ASCII words separated by single spaces (accents and punctuation dropped)."""
    decomposed = unicodedata.normalize("NFKD", text)
    ascii_text = decomposed.encode("ascii", "ignore").decode("ascii").casefold()
    return _PUNCT_RE.sub(" ", ascii_text).strip()


def _variants(text: str) -> list[str]:
    """``normalise(text)`` plus the forms with the common leading/trailing words removed."""
    base = normalise(text)
    out = [base] if base else []
    stripped = base
    changed = True
    while changed and stripped:
        changed = False
        for prefix in _LEADING:
            if stripped.startswith(prefix) and len(stripped) > len(prefix):
                stripped = stripped[len(prefix):]
                changed = True
        for suffix in _TRAILING:
            if stripped.endswith(suffix) and len(stripped) > len(suffix):
                stripped = stripped[: -len(suffix)]
                changed = True
        if changed and stripped not in out:
            out.append(stripped)
    return out


@dataclass(frozen=True, slots=True)
class Port:
    code: str
    name: str
    country: str
    lat: float
    lon: float

    @property
    def geometry(self) -> dict[str, Any]:
        return {"type": "Point", "coordinates": [self.lon, self.lat]}


@dataclass(frozen=True, slots=True)
class Chokepoint:
    code: str
    name: str
    ring: tuple[tuple[float, float], ...]

    @property
    def geometry(self) -> dict[str, Any]:
        return {"type": "Polygon", "coordinates": [[list(p) for p in self.ring]]}


@dataclass(slots=True)
class GeoResult:
    """``location_refs`` (unique, ordered) and geometries for a set of mentions."""

    location_refs: list[str] = field(default_factory=list)
    geometries: list[dict[str, Any]] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def add_ref(self, ref: str) -> None:
        if ref not in self.location_refs:
            self.location_refs.append(ref)

    def add_geometry(self, geometry: dict[str, Any]) -> None:
        if geometry not in self.geometries:
            self.geometries.append(geometry)

    def merged_geometry(self) -> dict[str, Any] | None:
        if not self.geometries:
            return None
        if len(self.geometries) == 1:
            return self.geometries[0]
        return {"type": "GeometryCollection", "geometries": list(self.geometries)}


class Gazetteer:
    def __init__(self, geo_dir: Path) -> None:
        self.geo_dir = geo_dir
        self.ports: dict[str, Port] = {}
        self.chokepoints: dict[str, Chokepoint] = {}
        self.countries: dict[str, dict[str, Any]] = {}
        self.regions: dict[str, dict[str, Any]] = {}
        self.centroids: dict[str, tuple[float, float]] = {}
        self._port_index: dict[str, str] = {}
        self._choke_index: dict[str, str] = {}
        self._country_index: dict[str, str] = {}
        self._region_index: dict[str, str] = {}
        self._iso3: dict[str, str] = {}
        self._load()

    # ───────────── loading ─────────────
    def _read(self, name: str) -> dict[str, Any]:
        path = self.geo_dir / name
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{path}: expected a JSON object")
        return data

    @staticmethod
    def _index(index: dict[str, str], key: str, *texts: str) -> None:
        for text in texts:
            for variant in _variants(text):
                index.setdefault(variant, key)

    def _load(self) -> None:
        for code, p in self._read("ports.json")["ports"].items():
            port = Port(code, str(p["name"]), str(p["country"]), float(p["lat"]), float(p["lon"]))
            self.ports[code] = port
            self._index(self._port_index, code, code, port.name, *p.get("aliases", []))
        for code, c in self._read("chokepoints.json")["chokepoints"].items():
            ring = tuple((x / 1e6, y / 1e6) for x, y in c["polygon"])
            if ring[0] != ring[-1]:
                ring = (*ring, ring[0])
            self.chokepoints[code] = Chokepoint(code, str(c["name"]), ring)
            self._index(self._choke_index, code, code, code.replace("_", " "), c["name"], *c.get("aliases", []))
        regions = self._read("regions.json")
        for iso2, c in regions["countries"].items():
            self.countries[iso2] = c
            self._iso3[str(c.get("iso3", "")).upper()] = iso2
            # Short codes/aliases (<= 3 chars) only match exactly (see regions.json description);
            # whole-mention matching makes that the only mode here anyway.
            self._index(self._country_index, iso2, iso2, c.get("iso3", ""), c["name"], *c.get("aliases", []))
        for code, r in regions["regions"].items():
            self.regions[code] = r
            self._index(self._region_index, code, code, r["name"], *r.get("aliases", []))
        for iso2, (lon, lat) in self._read("country_centroids.json")["centroids"].items():
            self.centroids[iso2] = (float(lon), float(lat))

    # ───────────── lookups ─────────────
    @staticmethod
    def _lookup(index: dict[str, str], mention: str) -> str | None:
        if not mention or len(mention) > MAX_MENTION_CHARS:
            return None
        for variant in _variants(mention):
            if variant in index:
                return index[variant]
        return None

    def port(self, mention: str) -> Port | None:
        code = self._lookup(self._port_index, mention)
        return self.ports[code] if code else None

    def chokepoint(self, mention: str) -> Chokepoint | None:
        code = self._lookup(self._choke_index, mention)
        return self.chokepoints[code] if code else None

    def country(self, mention: str) -> str | None:
        return self._lookup(self._country_index, mention)

    def region(self, mention: str) -> str | None:
        return self._lookup(self._region_index, mention)

    def display_name(self, ref: str) -> str | None:
        """The curated name of a location ref (``UNLOCODE:VNSGN`` → ``Ho Chi Minh City (Cat Lai)``,
        ``CHOKEPOINT:SUEZ``, ``ISO3166:VN``, ``REGION:…``) from the repo data, else ``None`` (tenant
        codes such as ``SUPPLIER:…`` and unknown codes). Trusted text: safe for explanations."""
        scheme, _, code = (ref or "").partition(":")
        if scheme == "UNLOCODE" and (port := self.ports.get(code)):
            return port.name
        if scheme == "CHOKEPOINT" and (choke := self.chokepoints.get(code)):
            return choke.name
        if scheme == "ISO3166" and (country := self.countries.get(code)):
            return str(country.get("name") or code)
        if scheme == "REGION" and (region := self.regions.get(code)):
            return str(region.get("name") or code)
        return None

    def iso2(self, code: str) -> str | None:
        """ISO alpha-2 for an alpha-2 or alpha-3 code (``VNM`` → ``VN``), else None."""
        code = (code or "").strip().upper()
        if len(code) == 2 and code in self.countries:
            return code
        return self._iso3.get(code)

    # ───────────── geocoding ─────────────
    def geocode(
        self,
        *,
        places: Sequence[str] = (),
        ports: Sequence[str] = (),
        chokepoints: Sequence[str] = (),
    ) -> GeoResult:
        """Resolve extracted mentions to ``location_refs`` + geometries.

        Ports → ``UNLOCODE:`` + point; chokepoints → ``CHOKEPOINT:`` + polygon; countries →
        ``ISO3166:`` and regions → ``REGION:`` (refs only: a country or region is too coarse a
        footprint for geo exposure). ``places`` try port, chokepoint, country, region in that order.
        """
        out = GeoResult()

        def add_port(p: Port) -> None:
            out.add_ref(f"UNLOCODE:{p.code}")
            out.add_ref(f"ISO3166:{p.country}")
            out.add_geometry(p.geometry)

        def add_choke(c: Chokepoint) -> None:
            out.add_ref(f"CHOKEPOINT:{c.code}")
            out.add_geometry(c.geometry)

        for mention in ports:
            if p := self.port(mention):
                add_port(p)
            elif c := self.chokepoint(mention):
                add_choke(c)
            else:
                out.unresolved.append(mention)
        for mention in chokepoints:
            if c := self.chokepoint(mention):
                add_choke(c)
            elif p := self.port(mention):
                add_port(p)
            else:
                out.unresolved.append(mention)
        for mention in places:
            if p := self.port(mention):
                add_port(p)
            elif c := self.chokepoint(mention):
                add_choke(c)
            elif iso := self.country(mention):
                out.add_ref(f"ISO3166:{iso}")
            elif region := self.region(mention):
                out.add_ref(f"REGION:{region}")
            else:
                out.unresolved.append(mention)
        return out


@lru_cache(maxsize=4)
def load_gazetteer(geo_dir: Path) -> Gazetteer:
    return Gazetteer(geo_dir)
