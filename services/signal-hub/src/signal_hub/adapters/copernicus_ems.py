"""Copernicus Emergency Management Service rapid-mapping adapter (``124`` §2) → ``fact`` signals.

Feed: the public dashboard API ``/backend/dashboard-api/public-activations-info/?limit=50`` (checked
against the live service, 2026-10-08; the legacy ``activations-rapid/feed`` RSS answers 404, so
there is no fallback): ``{count, next, previous, results: [activation…]}``, newest first. An
activation has ``code`` (``EMSR933``), ``name``, ``category`` (``Flood``, ``Storm``, ``Wildfire``,
``Earthquake``, ``Mass movement``, ``Volcanic activity``, ``Industrial accident``, ``Transport
accident``, ``Other``), ``countries`` (country names), naive-UTC ``eventTime`` / ``activationTime`` /
``lastUpdate``, ``closed``, ``centroid`` (WKT ``POINT (lon lat)``), ``gdacsId``, ``n_aois``,
``n_products``. The parser also accepts country objects (``{name}``/``{iso3}``) and a GeoJSON or
``{lat, lon}`` centroid.

One signal per activation (``native_key`` = code); mapped categories only (:data:`CATEGORIES`, the
rest is counted as skipped). Authoritative (an anchor: an EMS flood joins a GDACS flood event within
100 km under the 72 h same-hazard window). Severity by subtype
(:data:`signal_hub.scoring.COPERNICUS_EMS_SEVERITY`). ``starts_at`` = event time (else activation);
``expires_at`` = activation + ``COPERNICUS_EMS_TTL_DAYS``, or, once closed, the last update (both
from feed fields, so an unchanged activation never makes a new version). Geometry: the centroid
point (AOI footprints are deferred); ``ISO3166`` refs from the country names.

Untrusted text: ``name`` is clipped and stripped of control characters (title only).
"""

from __future__ import annotations

import datetime as dt
import re
from typing import Any, Final

from signal_hub import geo, scoring
from signal_hub.gazetteer import Gazetteer
from signal_hub.models import SignalDraft, SourceContext, clip
from signal_hub.timeutil import not_after, parse_ts

__all__ = [
    "ATTRIBUTION",
    "CATEGORIES",
    "CONNECTOR_ID",
    "LICENSE",
    "SOURCE_NAME",
    "EmsError",
    "activation_url",
    "parse_centroid",
    "parse_ems",
]

CONNECTOR_ID: Final = "copernicus_ems"
SOURCE_NAME: Final = "Copernicus EMS Rapid Mapping"
LICENSE: Final = "© European Union, Copernicus EMS (free, full and open; attribution required)"
ATTRIBUTION: Final = "© European Union, Copernicus Emergency Management Service"
MAX_ACTIVATIONS: Final = 200
CODE_RE: Final = re.compile(r"^EMSR\d{3,4}$")
CATEGORIES: Final[dict[str, tuple[str, str]]] = {
    "flood": ("natural_hazard", "flood"),
    "storm": ("weather", "wind_storm"),
    "wind": ("weather", "wind_storm"),
    "earthquake": ("natural_hazard", "earthquake"),
    "wildfire": ("natural_hazard", "wildfire"),
    "volcanic activity": ("natural_hazard", "volcano"),
    "mass movement": ("natural_hazard", "landslide"),
}
"""``category`` (case-insensitive) → (kind, subtype); anything else is skipped."""
_WKT_POINT_RE: Final = re.compile(r"^\s*POINT\s*\(\s*(-?\d+(?:\.\d+)?)\s+(-?\d+(?:\.\d+)?)\s*\)\s*$", re.IGNORECASE)
_CONTROL_RE: Final = re.compile(r"[\x00-\x1f\x7f]")


class EmsError(ValueError):
    """A dashboard-API answer without a ``results`` list."""


def activation_url(code: str) -> str:
    """The public activation page (the dashboard redirects ``/<code>`` here)."""
    return f"https://mapping.emergency.copernicus.eu/activations/{code}/"


def parse_centroid(value: Any) -> dict[str, Any] | None:
    """WKT ``POINT (lon lat)``, a GeoJSON Point or ``{lat, lon}`` → a clean Point (else None)."""
    if isinstance(value, str):
        m = _WKT_POINT_RE.match(value)
        if not m:
            return None
        return geo.clean_geometry({"type": "Point", "coordinates": [float(m.group(1)), float(m.group(2))]})
    if isinstance(value, dict):
        if value.get("type") == "Point":
            return geo.clean_geometry(value)
        lat, lon = value.get("lat", value.get("latitude")), value.get("lon", value.get("lng", value.get("longitude")))
        if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            return geo.clean_geometry({"type": "Point", "coordinates": [lon, lat]})
    return None


def _refs(countries: Any, gaz: Gazetteer) -> list[str]:
    refs: list[str] = []
    for c in countries if isinstance(countries, list) else []:
        iso: str | None = None
        if isinstance(c, str):
            iso = gaz.country(c)
        elif isinstance(c, dict):
            iso = gaz.iso2(str(c.get("iso3") or c.get("iso2") or "")) or gaz.country(str(c.get("name") or c.get("short_name") or ""))
        if iso and f"ISO3166:{iso}" not in refs:
            refs.append(f"ISO3166:{iso}")
    return refs[:50]


def results(doc: Any) -> list[dict[str, Any]]:
    if not isinstance(doc, dict) or not isinstance(doc.get("results"), list):
        raise EmsError("Copernicus EMS answer has no results list")
    return [r for r in doc["results"][:MAX_ACTIVATIONS] if isinstance(r, dict)]


def parse_ems(doc: Any, ctx: SourceContext, gaz: Gazetteer, *, ttl_days: int = 14) -> tuple[list[SignalDraft], int]:
    """Activation list → (drafts, number of activations skipped: unmapped category or no code/time)."""
    drafts: list[SignalDraft] = []
    skipped = 0
    for act in results(doc):
        draft = _draft(act, ctx, gaz, ttl_days)
        if draft is None:
            skipped += 1
        else:
            drafts.append(draft)
    return drafts, skipped


def _draft(act: dict[str, Any], ctx: SourceContext, gaz: Gazetteer, ttl_days: int) -> SignalDraft | None:
    code = str(act.get("code") or "").strip().upper()
    mapped = CATEGORIES.get(str(act.get("category") or "").strip().lower())
    activated = parse_ts(act.get("activationTime"))
    if not CODE_RE.match(code) or mapped is None or activated is None:
        return None
    kind, subtype = mapped
    event_time = parse_ts(act.get("eventTime"))
    starts = min(event_time, activated) if event_time else activated
    updated = parse_ts(act.get("lastUpdate"))
    closed = act.get("closed") is True
    if closed:
        end = max(updated or activated, starts)
        ends: dt.datetime | None = end
        expires = end
    else:
        ends = None
        expires = activated + dt.timedelta(days=max(1, ttl_days))
    observed = not_after(updated or activated, ctx.retrieved_at) or ctx.retrieved_at
    name = clip(_CONTROL_RE.sub(" ", str(act.get("name") or "")), 200)
    refs = _refs(act.get("countries"), gaz)
    category = str(act.get("category")).strip()
    native: dict[str, Any] = {"scheme": "copernicus_ems", "code": code, "category": category[:40], "closed": closed}
    for key, field in (("n_aois", "aois"), ("n_products", "products")):
        n = act.get(key)
        if isinstance(n, int) and not isinstance(n, bool):
            native[field] = n
    gdacs_id = act.get("gdacsId")
    if isinstance(gdacs_id, str) and re.match(r"^[A-Z]{2}\d{1,10}$", gdacs_id):
        native["gdacs_id"] = gdacs_id
    status = "closed" if closed else "open"
    summary = f"Copernicus EMS rapid-mapping activation {code} ({category.lower()}, {status})"
    if native.get("products") is not None:
        summary += f": {native['products']} map product(s)"
    return SignalDraft(
        tenant=ctx.tenant,
        connector_id=ctx.connector(CONNECTOR_ID),
        native_key=ctx.key(code),
        origin=ctx.origin("feed"),
        kind=kind,
        subtype=subtype,
        title=clip(f"{code}: {name}" if name else f"Copernicus EMS activation {code}", 300) or code,
        summary=clip(summary, 2000),
        raw_text=None,
        geometry=parse_centroid(act.get("centroid")),
        location_refs=refs,
        starts_at=starts,
        ends_at=ends,
        observed_at=observed,
        expires_at=expires,
        severity_bp=scoring.COPERNICUS_EMS_SEVERITY[subtype],
        severity_native=native,
        provenance=ctx.provenance("fact"),
        extracted_by=None,
        source_name=ctx.source_name(SOURCE_NAME),
        source_url=None if ctx.demo else activation_url(code),
        license=LICENSE,
        authority_bp=scoring.AUTHORITY["copernicus_ems"],
        retrieved_at=ctx.retrieved_at,
        native_id=code,
        authoritative=True,
        pack_id=ctx.pack_id,
    )
