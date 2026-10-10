"""MeteoAlarm adapter (``124`` §2): EUMETNET legacy Atom feed per country (+ CAP detail polygons)
→ ``fact`` weather signals, orange and red warnings only by default.

Feed shape (checked against live feeds, 2026-10-08): ``/feeds/meteoalarm-legacy-atom-<country>`` is
Atom with ``cap:`` elements per entry: ``cap:identifier``, ``cap:geocode`` (``EMMA_ID``),
``cap:areaDesc``, ``cap:event``, ``cap:sent``/``onset``/``expires``, ``cap:severity`` (national
services disagree on it: DWD sends Minor for yellow, KNMI Moderate), ``cap:message_type``
(Alert/Update/Cancel), a ``link type="application/cap+xml"`` to the CAP 1.2 document, and a title
``"<Level> <Type> Warning issued for <Country> - <Area>"`` that MeteoAlarm builds from the awareness
level and type (the Atom entries carry no awareness parameters). The CAP document has
``parameter`` ``awareness_level`` (``"3; orange; Severe"``) and ``awareness_type`` (``"1; Wind"``)
and per ``area`` a ``polygon`` (``lat,lon`` pairs) or ``circle`` (``lat,lon radius_km``) plus the
``EMMA_ID`` geocode; it wins over the title when it was fetched.

One signal per (CAP identifier, EMMA area): ``native_key`` ``<identifier>#<EMMA_ID>``. Level >=
``METEOALARM_MIN_LEVEL`` (3 = orange) only; fog and avalanches are skipped; a ``Cancel`` entry and
an entry that disappears from the feed are withdrawn by the poller. Severity yellow 3000 / orange
6000 / red 8500. ``starts_at`` = onset (else sent), ``ends_at``/``expires_at`` = expires. Refs:
the country (``ISO3166``). Geometry: the CAP polygon or circle (32-gon) when the detail document
was fetched (at most ``METEOALARM_MAX_DETAIL_FETCHES`` per poll, only from ``feeds.meteoalarm.org``
over https), else none (refs only: no geo exposure until an EMMA area gazetteer exists). Not an
anchor: neighbouring warnings cluster into one candidate event, and a later GDACS event absorbs them.

Untrusted text: ``areaDesc`` (a district name from the national service) is clipped and stripped of
control characters and only used in the title; everything else is built from fixed labels.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Final
from urllib.parse import urlsplit
from xml.etree.ElementTree import Element

from defusedxml import ElementTree as SafeET

from signal_hub import geo, scoring
from signal_hub.models import SignalDraft, SourceContext, clip, safe_source_url
from signal_hub.timeutil import not_after, parse_ts

__all__ = [
    "ATTRIBUTION",
    "DETAIL_HOST",
    "LICENSE",
    "SOURCE_NAME",
    "TYPES",
    "CapDetail",
    "MeteoAlarmError",
    "WarningEntry",
    "connector_id",
    "detail_urls",
    "feed_url",
    "parse_atom",
    "parse_cap_detail",
    "parse_meteoalarm",
]

CONNECTOR_PREFIX: Final = "meteoalarm"
SOURCE_NAME: Final = "MeteoAlarm (EUMETNET)"
LICENSE: Final = "MeteoAlarm / EUMETNET: CC BY 4.0-equivalent; redistribution terms apply"
ATTRIBUTION: Final = "Source: MeteoAlarm.org (EUMETNET) and the national meteorological services"
DETAIL_HOST: Final = "feeds.meteoalarm.org"
MAX_ENTRIES: Final = 2000
MAX_FEED_BYTES: Final = 12 * 1024 * 1024
DEFAULT_TTL: Final = dt.timedelta(hours=24)
"""``expires_at`` of a warning without ``cap:expires``."""
LEVELS: Final[dict[str, int]] = {"green": 1, "yellow": 2, "orange": 3, "red": 4}
_LEVEL_NAMES: Final = {v: k for k, v in LEVELS.items()}
TYPES: Final[dict[int, tuple[str, str, str] | None]] = {
    1: ("weather", "wind_storm", "wind"),
    2: ("weather", "snow_ice", "snow / ice"),
    3: ("weather", "thunderstorm", "thunderstorm"),
    4: None,  # fog
    5: ("weather", "heat", "high temperature"),
    6: ("weather", "cold", "low temperature"),
    7: ("weather", "coastal_flood", "coastal event"),
    8: ("natural_hazard", "wildfire", "forest fire"),
    9: None,  # avalanches
    10: ("weather", "heavy_rain", "rain"),
    11: None,  # unknown
    12: ("natural_hazard", "flood", "flooding"),
    13: ("natural_hazard", "flood", "rain / flood"),
}
"""MeteoAlarm awareness type → (kind, subtype, label); ``None`` = skipped."""
_TYPE_LABELS: Final[dict[str, int]] = {
    "wind": 1, "snowice": 2, "thunderstorm": 3, "thunderstorms": 3, "fog": 4, "hightemperature": 5,
    "extremehightemperature": 5, "lowtemperature": 6, "extremelowtemperature": 6, "coastalevent": 7,
    "forestfire": 8, "avalanche": 9, "avalanches": 9, "rain": 10, "flooding": 12, "flood": 12, "rainflood": 13,
}
_TITLE_RE: Final = re.compile(r"^\s*(Green|Yellow|Orange|Red)\s+(.{1,40}?)\s+Warning\b", re.IGNORECASE)
_PARAM_RE: Final = re.compile(r"^\s*(\d{1,2})\s*;")
_EMMA_RE: Final = re.compile(r"^[A-Z]{2}[A-Z0-9]{1,10}$")
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")
_ATOM: Final = "{http://www.w3.org/2005/Atom}"


class MeteoAlarmError(ValueError):
    """Not a MeteoAlarm Atom feed / CAP document."""


def connector_id(slug: str) -> str:
    return f"{CONNECTOR_PREFIX}.{slug}"


def feed_url(template: str, slug: str) -> str:
    """``METEOALARM_URL_TEMPLATE`` with ``{country}`` replaced (no ``str.format``: a template is config)."""
    return template.replace("{country}", slug)


def _local(tag: Any) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _child_text(el: Element, name: str) -> str | None:
    node = next((c for c in el if _local(c.tag) == name), None)
    text = "".join(node.itertext()).strip() if node is not None else ""
    return text or None


def _norm_type(label: str) -> int | None:
    return _TYPE_LABELS.get(re.sub(r"[^a-z]", "", label.lower()))


def _param_number(value: str | None) -> int | None:
    m = _PARAM_RE.match(value or "")
    return int(m.group(1)) if m else None


@dataclass(slots=True)
class WarningEntry:
    """One Atom entry (an area of a CAP alert)."""

    identifier: str
    emma_id: str
    area: str | None
    level: int | None
    type_no: int | None
    sent: dt.datetime | None
    onset: dt.datetime | None
    expires: dt.datetime | None
    message_type: str
    cap_severity: str | None
    detail_url: str | None
    area_url: str | None

    @property
    def native(self) -> str:
        return f"{self.identifier}#{self.emma_id}"


def _parse_xml(body: bytes, what: str) -> Element:
    if len(body) > MAX_FEED_BYTES:
        raise MeteoAlarmError(f"{what} larger than {MAX_FEED_BYTES} bytes")
    try:
        return SafeET.fromstring(body)
    except Exception as exc:  # defusedxml raises its own DTD/entity errors besides ParseError
        raise MeteoAlarmError(f"{what}: not well-formed XML ({type(exc).__name__})") from None


def parse_atom(body: bytes) -> list[WarningEntry]:
    """Every usable entry of a legacy Atom feed (any level; filtering happens in the parser). A feed
    with entries of which none is usable (no CAP identifier / ``EMMA_ID`` geocode: a format change)
    is an error, never an empty list — the poller withdraws whatever an answer no longer lists."""
    root = _parse_xml(body, "MeteoAlarm feed")
    if root.tag != f"{_ATOM}feed":
        raise MeteoAlarmError("MeteoAlarm feed: root is not an Atom <feed>")
    entries = root.findall(f"{_ATOM}entry")[:MAX_ENTRIES]
    out: list[WarningEntry] = []
    for entry in entries:
        identifier = _child_text(entry, "identifier")
        geocode = next((c for c in entry if _local(c.tag) == "geocode"), None)
        emma = None
        if geocode is not None and (_child_text(geocode, "valueName") or "").upper() == "EMMA_ID":
            emma = (_child_text(geocode, "value") or "").strip().upper()
        if not identifier or len(identifier) > 300 or not emma or not _EMMA_RE.match(emma):
            continue
        title = _child_text(entry, "title") or ""
        m = _TITLE_RE.match(title)
        detail_url = area_url = None
        for link in entry.findall(f"{_ATOM}link"):
            href = (link.get("href") or "").strip()
            if link.get("type") == "application/cap+xml":
                detail_url = href or None
            elif link.get("rel") in (None, "alternate") and "geocode=" in href:
                area_url = href or None
        out.append(WarningEntry(
            identifier=identifier.strip(),
            emma_id=emma,
            area=clip(_CONTROL_RE.sub(" ", _child_text(entry, "areaDesc") or ""), 120),
            level=LEVELS.get(m.group(1).lower()) if m else None,
            type_no=_norm_type(m.group(2)) if m else None,
            sent=parse_ts(_child_text(entry, "sent")),
            onset=parse_ts(_child_text(entry, "onset")),
            expires=parse_ts(_child_text(entry, "expires")),
            message_type=(_child_text(entry, "message_type") or "Alert").strip().capitalize()[:20],
            cap_severity=(_child_text(entry, "severity") or "")[:20] or None,
            detail_url=detail_url,
            area_url=area_url,
        ))
    if entries and not out:
        raise MeteoAlarmError(f"MeteoAlarm feed: none of its {len(entries)} entries is usable "
                              "(CAP identifier + EMMA_ID geocode)")
    return out


def detail_urls(warnings: list[WarningEntry], *, allowed_host: str = DETAIL_HOST) -> dict[str, str]:
    """CAP document URL per identifier: ``https`` on ``allowed_host`` only (no SSRF via feed data)."""
    out: dict[str, str] = {}
    for w in warnings:
        if not w.detail_url or w.identifier in out:
            continue
        try:
            parts = urlsplit(w.detail_url)
        except ValueError:
            continue
        if parts.scheme == "https" and (parts.hostname or "").lower() == allowed_host.lower() \
                and not parts.username and parts.port in (None, 443) and parts.path.startswith("/api/"):
            out[w.identifier] = w.detail_url
    return out


@dataclass(slots=True)
class CapDetail:
    level: int | None = None
    type_no: int | None = None
    areas: dict[str, dict[str, Any]] = field(default_factory=dict)
    """EMMA_ID → geometry (polygon / circle of that area)."""


def _cap_shapes(area: Element) -> list[dict[str, Any]]:
    shapes: list[dict[str, Any]] = []
    for node in area:
        name, text = _local(node.tag), "".join(node.itertext()).strip()
        if name == "polygon":
            ring: list[list[float]] = []
            for pair in text.split()[:5000]:
                try:
                    lat_s, lon_s = pair.split(",")[:2]
                    ring.append([float(lon_s), float(lat_s)])
                except ValueError:
                    continue
            cleaned = geo.clean_geometry({"type": "Polygon", "coordinates": [ring]})
            if cleaned is not None:
                shapes.append(cleaned)
        elif name == "circle":
            try:
                centre, radius = text.split()[:2]
                lat_s, lon_s = centre.split(",")[:2]
                circle = geo.circle_polygon(float(lon_s), float(lat_s), float(radius))
            except ValueError:
                continue
            if circle is not None:
                shapes.append(circle)
    return shapes


def parse_cap_detail(body: bytes) -> CapDetail:
    """Awareness parameters and per-EMMA-area geometry of a CAP 1.2 ``alert`` document."""
    root = _parse_xml(body, "CAP document")
    if _local(root.tag) != "alert":
        raise MeteoAlarmError("CAP document: root is not <alert>")
    infos = [i for i in root if _local(i.tag) == "info"]
    infos.sort(key=lambda i: 0 if (_child_text(i, "language") or "").lower().startswith("en") else 1)
    out = CapDetail()
    for info in infos:
        for param in (p for p in info if _local(p.tag) == "parameter"):
            name, value = _child_text(param, "valueName"), _child_text(param, "value")
            if name == "awareness_level" and out.level is None:
                out.level = _param_number(value)
            elif name == "awareness_type" and out.type_no is None:
                out.type_no = _param_number(value)
        for area in (a for a in info if _local(a.tag) == "area"):
            emma = None
            for gc in (g for g in area if _local(g.tag) == "geocode"):
                if (_child_text(gc, "valueName") or "").upper() == "EMMA_ID":
                    emma = (_child_text(gc, "value") or "").strip().upper()
            if not emma or emma in out.areas:
                continue
            shapes = _cap_shapes(area)
            merged = geo.merge(shapes) if shapes else None
            if merged is not None:
                out.areas[emma] = merged
    return out


def parse_meteoalarm(
    warnings: list[WarningEntry],
    ctx: SourceContext,
    *,
    connector: str,
    country_iso: str | None,
    min_level: int = 3,
    details: Mapping[str, CapDetail] | None = None,
    stored: Mapping[str, dict[str, Any]] | None = None,
) -> tuple[list[SignalDraft], int]:
    """Warnings of one country feed → (drafts at or above ``min_level``, entries skipped).

    ``details`` holds the CAP documents fetched this poll (by identifier); ``stored`` the geometry
    already stored per native key (an unchanged warning keeps its polygon without a re-fetch)."""
    drafts: list[SignalDraft] = []
    skipped = 0
    seen: set[str] = set()
    floor = max(2, min(4, min_level))
    for w in warnings:
        detail = (details or {}).get(w.identifier)
        level = detail.level if detail is not None and detail.level in _LEVEL_NAMES else w.level
        type_no = detail.type_no if detail is not None and detail.type_no in TYPES else w.type_no
        mapped = TYPES.get(type_no) if type_no is not None else None
        if (w.native in seen or w.message_type == "Cancel" or level is None or level < floor or mapped is None
                or w.sent is None):
            skipped += 1
            continue
        seen.add(w.native)
        kind, subtype, label = mapped
        level_name = _LEVEL_NAMES[level]
        geometry = detail.areas.get(w.emma_id) if detail is not None else None
        if geometry is None:
            geometry = (stored or {}).get(ctx.key(w.native))
        starts = w.onset or w.sent
        expires = w.expires or (w.sent + DEFAULT_TTL)
        iso = country_iso or (w.emma_id[:2] if re.match(r"^[A-Z]{2}$", w.emma_id[:2]) else None)
        area = w.area or w.emma_id
        native: dict[str, Any] = {
            "scheme": "meteoalarm", "awareness_level": level, "level": level_name, "awareness_type": type_no,
            "emma_id": w.emma_id, "message_type": w.message_type, "cap_severity": w.cap_severity,
        }
        summary = (f"MeteoAlarm {level_name} warning ({label}) for {area}"
                   f"{f' ({iso})' if iso else ''}, valid {starts:%Y-%m-%d %H:%M}–{expires:%Y-%m-%d %H:%M} UTC; "
                   "issued by the national meteorological service"
                   + ("" if geometry is not None else "; area polygon not loaded yet (country reference only)"))
        drafts.append(SignalDraft(
            tenant=ctx.tenant,
            connector_id=ctx.connector(connector),
            native_key=ctx.key(w.native),
            origin=ctx.origin("feed"),
            kind=kind,
            subtype=subtype,
            title=clip(f"{level_name.capitalize()} {label} warning: {area} (MeteoAlarm)", 300) or "MeteoAlarm warning",
            summary=clip(summary, 2000),
            raw_text=None,
            geometry=geometry,
            location_refs=[f"ISO3166:{iso}"] if iso else [],
            starts_at=starts,
            ends_at=expires if expires >= starts else None,
            observed_at=not_after(w.sent, ctx.retrieved_at) or ctx.retrieved_at,
            expires_at=expires,
            severity_bp=scoring.METEOALARM_SEVERITY[level] if level in scoring.METEOALARM_SEVERITY else 3000,
            severity_native=native,
            provenance=ctx.provenance("fact"),
            extracted_by=None,
            source_name=ctx.source_name(SOURCE_NAME),
            source_url=safe_source_url(w.area_url),
            license=LICENSE,
            authority_bp=scoring.AUTHORITY["meteoalarm"],
            retrieved_at=ctx.retrieved_at,
            native_id=w.native[:500],
            authoritative=False,
            pack_id=ctx.pack_id,
        ))
    return drafts, skipped
