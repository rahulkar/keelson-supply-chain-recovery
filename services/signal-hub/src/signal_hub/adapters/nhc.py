"""NOAA National Hurricane Center adapter (``124`` §2): ``CurrentStorms.json`` → ``fact`` signals.

Feed shape (checked against the live feed, 2026-10-08): ``{activeStorms: [storm…]}``; a storm has
``id`` (``al092026``: basin + number + year), ``name``, ``classification`` (``TD``/``STD``/``TS``/
``STS``/``HU``/``PTC``/``PC``…), ``intensity`` (max sustained wind, knots, a string), ``pressure``
(mb, a string), ``latitudeNumeric``/``longitudeNumeric``, ``movementDir``/``movementSpeed``,
``lastUpdate`` and per-product objects ``{advNum, issuance, url | kmzFile, zipFile}``
(``publicAdvisory``, ``trackCone``, ``forecastTrack`` … some ``null``).

One signal per storm: ``native_key`` = storm id, ``native_id`` = ``<id>:<advNum>`` (each advisory
is a new version). ``natural_hazard`` / ``tropical_cyclone`` (``NAT.storm``, 300 km radius),
authoritative (an anchor: it joins a GDACS tropical-cyclone event under the 72 h same-hazard rule,
:func:`signal_hub.cluster.same_hazard_window`). Severity from the intensity
(:func:`signal_hub.scoring.nhc_severity_bp`). ``starts_at`` estimates genesis deterministically as
``issuance − (advisory number − 1) × 6 h``; ``expires_at`` = issuance + 12 h (a storm that leaves the
feed is withdrawn by the poller). Geometry: the forecast cone + track (KMZ, :mod:`.kml`) merged with
the current position, else the position alone. No ``location_refs``.

Untrusted text: the storm name only ever appears sanitised (``[A-Za-z' -]{1,40}``); everything else
in the title/summary is built from numbers and fixed labels.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Mapping, Sequence
from typing import Any, Final
from urllib.parse import urlsplit

from signal_hub import geo, scoring
from signal_hub.models import SignalDraft, SourceContext, clip, safe_source_url
from signal_hub.timeutil import not_after, parse_ts

__all__ = [
    "ATTRIBUTION",
    "CONNECTOR_ID",
    "GIS_HOST",
    "LICENSE",
    "SOURCE_NAME",
    "NhcError",
    "advisory_tag",
    "gis_urls",
    "parse_nhc",
    "storms",
]

CONNECTOR_ID: Final = "nhc"
SOURCE_NAME: Final = "NOAA National Hurricane Center"
LICENSE: Final = "Public domain (NOAA/NWS)"
ATTRIBUTION: Final = "Source: NOAA National Hurricane Center"
GIS_HOST: Final = "www.nhc.noaa.gov"
EXPIRY: Final = dt.timedelta(hours=12)
ADVISORY_INTERVAL: Final = dt.timedelta(hours=6)
MAX_STORMS: Final = 50
STORM_ID_RE: Final = re.compile(r"^(al|ep|cp)\d{6}$")
_NAME_RE: Final = re.compile(r"^[A-Za-z' -]{1,40}$")
_ADV_RE: Final = re.compile(r"^0*(\d{1,3})[A-Za-z]?$")
_GIS_PATH_RE: Final = re.compile(r"^/[A-Za-z0-9_./-]{1,200}\.(kmz|kml)$")
_BASINS: Final = {"al": "Atlantic", "ep": "Eastern Pacific", "cp": "Central Pacific"}
_LABELS: Final[dict[str, str]] = {
    "TD": "Tropical Depression",
    "STD": "Subtropical Depression",
    "TS": "Tropical Storm",
    "STS": "Subtropical Storm",
    "HU": "Hurricane",
    "MH": "Major Hurricane",
    "PTC": "Potential Tropical Cyclone",
    "PC": "Post-Tropical Cyclone",
    "EX": "Extratropical Cyclone",
    "RL": "Remnant Low",
}


class NhcError(ValueError):
    """``CurrentStorms.json`` without an ``activeStorms`` list (never read as "no storms")."""


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(str(value).strip())
    except ValueError:
        return None
    return int(f) if f == f and abs(f) < 1e6 else None


def _coord(numeric: Any, text: Any, neg: str) -> float | None:
    if isinstance(numeric, (int, float)) and not isinstance(numeric, bool):
        return float(numeric)
    m = re.match(r"^\s*(\d{1,3}(?:\.\d+)?)\s*([NSEW])\s*$", str(text or ""))
    if not m:
        return None
    v = float(m.group(1))
    return -v if m.group(2) == neg else v


def storms(doc: Any) -> list[dict[str, Any]]:
    """The valid storm objects of a ``CurrentStorms.json`` document (``NhcError`` when it has no
    ``activeStorms`` list: a malformed feed must never look like "every storm is gone")."""
    if not isinstance(doc, dict) or not isinstance(doc.get("activeStorms"), list):
        raise NhcError("CurrentStorms.json has no activeStorms list")
    out: list[dict[str, Any]] = []
    for s in doc["activeStorms"][:MAX_STORMS]:
        if isinstance(s, dict) and isinstance(s.get("id"), str) and STORM_ID_RE.match(s["id"].lower()):
            out.append(s)
    return out


def advisory_tag(storm: Mapping[str, Any]) -> str | None:
    """The public advisory number (``"007"``, ``"12A"``) — what changes with each advisory."""
    adv = _dict(storm.get("publicAdvisory")).get("advNum")
    text = str(adv).strip() if adv is not None else ""
    return text if _ADV_RE.match(text) else None


def gis_urls(storm: Mapping[str, Any], *, allowed_host: str = GIS_HOST) -> list[str]:
    """Cone + track KMZ links of a storm, only ``https`` on ``allowed_host`` (no SSRF via feed data)."""
    out: list[str] = []
    for product in ("trackCone", "forecastTrack"):
        url = _dict(storm.get(product)).get("kmzFile")
        if not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            continue
        if parts.scheme == "https" and (parts.hostname or "").lower() == allowed_host.lower() \
                and not parts.username and parts.port in (None, 443) and _GIS_PATH_RE.match(parts.path):
            out.append(url.strip())
    return out


def _name(raw: Any) -> str:
    text = str(raw or "").strip()
    return text if _NAME_RE.match(text) else "Unnamed"


def _category(kt: int | None) -> int:
    if kt is None or kt < 64:
        return 0
    return next(c for floor, c in ((137, 5), (113, 4), (96, 3), (83, 2), (64, 1)) if kt >= floor)


def parse_nhc(
    doc: Any,
    ctx: SourceContext,
    *,
    gis: Mapping[str, Sequence[dict[str, Any]]] | None = None,
) -> list[SignalDraft]:
    """``CurrentStorms.json`` (+ cone/track geometries per storm id) → signal drafts."""
    drafts: list[SignalDraft] = []
    for storm in storms(doc):
        draft = _draft(storm, ctx, (gis or {}).get(storm["id"].lower(), ()))
        if draft is not None:
            drafts.append(draft)
    return drafts


def _draft(storm: dict[str, Any], ctx: SourceContext, shapes: Sequence[dict[str, Any]]) -> SignalDraft | None:
    sid = storm["id"].lower()
    adv_text = advisory_tag(storm)
    advisory = _dict(storm.get("publicAdvisory"))
    issued = parse_ts(advisory.get("issuance")) or parse_ts(storm.get("lastUpdate"))
    if adv_text is None or issued is None:
        return None
    observed = not_after(issued, ctx.retrieved_at) or ctx.retrieved_at
    adv_num = int(_ADV_RE.match(adv_text).group(1))  # type: ignore[union-attr]
    starts = issued - ADVISORY_INTERVAL * max(0, adv_num - 1)
    lat = _coord(storm.get("latitudeNumeric"), storm.get("latitude"), "S")
    lon = _coord(storm.get("longitudeNumeric"), storm.get("longitude"), "W")
    position = geo.clean_geometry({"type": "Point", "coordinates": [lon, lat]}) if lat is not None and lon is not None else None
    # cone + track lines only: the track KML's forecast points add nothing, and the position is our own
    parts = [g for g in shapes if isinstance(g, dict) and g.get("type") != "Point"]
    geometry = geo.merge([*parts, *([position] if position else [])]) if parts else position
    klass = str(storm.get("classification") or "").strip().upper()[:4]
    label = _LABELS.get(klass, "Tropical Cyclone")
    kt = _int(storm.get("intensity"))
    mb = _int(storm.get("pressure"))
    name = _name(storm.get("name"))
    category = _category(kt)
    basin = sid[:2]
    native: dict[str, Any] = {
        "scheme": "nhc", "storm_id": sid, "basin": basin.upper(), "classification": klass or None,
        "category": category, "intensity_kt": kt, "pressure_mb": mb, "adv_num": adv_text,
    }
    summary = [f"NHC {label} ({_BASINS[basin]})"]
    if kt is not None:
        summary.append(f"maximum sustained winds {kt} kt" + (f" (category {category})" if category else ""))
    if mb is not None:
        summary.append(f"minimum pressure {mb} mb")
    direction, speed = _int(storm.get("movementDir")), _int(storm.get("movementSpeed"))
    if direction is not None and speed is not None:
        summary.append(f"moving {direction % 360}° at {speed} kt")
    if lat is not None and lon is not None:
        summary.append(f"centre {abs(lat):.1f}°{'N' if lat >= 0 else 'S'} {abs(lon):.1f}°{'W' if lon < 0 else 'E'}")
    url = advisory.get("url")
    return SignalDraft(
        tenant=ctx.tenant,
        connector_id=ctx.connector(CONNECTOR_ID),
        native_key=ctx.key(sid),
        origin=ctx.origin("feed"),
        kind="natural_hazard",
        subtype="tropical_cyclone",
        title=clip(f"{label} {name} (NHC advisory {adv_text.lstrip('0') or '0'})", 300) or f"{label} {sid}",
        summary=clip(" · ".join(summary), 2000),
        raw_text=None,
        geometry=geometry,
        location_refs=[],
        starts_at=starts,
        ends_at=None,
        observed_at=observed,
        expires_at=issued + EXPIRY,
        severity_bp=scoring.nhc_severity_bp(kt),
        severity_native=native,
        provenance=ctx.provenance("fact"),
        extracted_by=None,
        source_name=ctx.source_name(SOURCE_NAME),
        source_url=safe_source_url(url if isinstance(url, str) else None),
        license=LICENSE,
        authority_bp=scoring.AUTHORITY["nhc"],
        retrieved_at=ctx.retrieved_at,
        native_id=f"{sid}:{adv_text}",
        authoritative=True,
        pack_id=ctx.pack_id,
    )
