"""USGS adapter (``100`` §3): summary GeoJSON feed → ``fact`` earthquake signals.

Feed shape (``/earthquakes/feed/v1.0/summary/4.5_day.geojson``): FeatureCollection with
``metadata {generated, title, count}``; each feature has ``id`` and ``properties`` ``mag``, ``place``,
``time``/``updated`` (epoch ms), ``url``, ``alert`` (PAGER green/yellow/orange/red or null),
``tsunami`` (0/1), ``sig``, ``status``, ``type``, ``title`` and a ``Point`` ``[lon, lat, depth_km]``.
Public domain. An updated quake (new ``updated``/``mag``/``alert``) is a new version.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

from signal_hub import geo, scoring
from signal_hub.gazetteer import Gazetteer
from signal_hub.models import SignalDraft, SourceContext, clip, safe_source_url
from signal_hub.timeutil import epoch_ms

__all__ = ["CONNECTOR_ID", "parse_usgs"]

CONNECTOR_ID: Final = "usgs"
SOURCE_NAME: Final = "USGS Earthquake Hazards Program"
LICENSE: Final = "Public domain (USGS)"
EXPIRY: Final = dt.timedelta(days=7)


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    f = float(value)
    return f if f == f and abs(f) != float("inf") else None


def _country_ref(place: str, gaz: Gazetteer) -> str | None:
    """``"120 km SSW of Kokopo, Papua New Guinea"`` → ``ISO3166:PG`` (names only; 2-3 letter tails such
    as US state codes are never read as countries)."""
    tail = place.rsplit(",", 1)[-1].strip()
    if len(tail) <= 3:
        return None
    iso = gaz.country(tail)
    return f"ISO3166:{iso}" if iso else None


def parse_usgs(doc: Any, ctx: SourceContext, gaz: Gazetteer) -> list[SignalDraft]:
    if not isinstance(doc, dict):
        return []
    drafts: list[SignalDraft] = []
    features = doc.get("features")
    for feature in features if isinstance(features, list) else []:
        if not isinstance(feature, dict):
            continue
        props = feature.get("properties")
        if not isinstance(props, dict):
            continue
        qid = feature.get("id")
        mag = _num(props.get("mag"))
        if not isinstance(qid, str) or not qid or mag is None:
            continue
        if str(props.get("type") or "earthquake") != "earthquake":
            continue
        geometry = geo.clean_geometry(feature.get("geometry"))
        if geometry is None or geometry.get("type") != "Point":
            continue
        coords = (feature.get("geometry") or {}).get("coordinates") or []
        depth = _num(coords[2]) if len(coords) > 2 else None
        occurred = epoch_ms(props.get("time")) or ctx.retrieved_at
        updated = epoch_ms(props.get("updated")) or occurred
        alert = props.get("alert") if isinstance(props.get("alert"), str) else None
        tsunami = _num(props.get("tsunami"))
        tsunami_flag = bool(tsunami and tsunami >= 1)
        place_raw = props.get("place")
        place = place_raw if isinstance(place_raw, str) else ""
        title = clip(props.get("title") if isinstance(props.get("title"), str) else None, 300) or clip(
            f"M {mag:.1f} - {place or 'unknown location'}", 300
        )
        refs = [r for r in [_country_ref(place, gaz)] if r]
        native: dict[str, Any] = {"scheme": "usgs", "mag": mag, "tsunami": tsunami_flag}
        if alert:
            native["alert"] = alert.lower()[:20]
        sig = _num(props.get("sig"))
        if sig is not None:
            native["sig"] = int(sig)
        if depth is not None:
            native["depth_km"] = round(depth, 1)
        if isinstance(props.get("magType"), str):
            native["mag_type"] = props["magType"][:10]
        if isinstance(props.get("status"), str):
            native["status"] = props["status"][:20]
        summary = f"Magnitude {mag:.1f}" + (f", PAGER {alert.lower()}" if alert else "") + (
            ", tsunami flag set" if tsunami_flag else "") + (f" · {place}" if place else "")
        drafts.append(SignalDraft(
            tenant=ctx.tenant,
            connector_id=ctx.connector(CONNECTOR_ID),
            native_key=ctx.key(qid),
            origin=ctx.origin("feed"),
            kind="natural_hazard",
            subtype="earthquake",
            title=title or f"M {mag:.1f} earthquake",
            summary=clip(summary, 2000),
            raw_text=None,
            geometry=geometry,
            location_refs=refs,
            starts_at=occurred,
            ends_at=occurred,
            observed_at=updated,
            expires_at=occurred + EXPIRY,
            severity_bp=scoring.usgs_severity_bp(mag, alert=alert, tsunami=tsunami_flag),
            severity_native=native,
            provenance=ctx.provenance("fact"),
            extracted_by=None,
            source_name=ctx.source_name(SOURCE_NAME),
            source_url=safe_source_url(props.get("url") if isinstance(props.get("url"), str) else None),
            license=LICENSE,
            authority_bp=scoring.AUTHORITY["usgs"],
            retrieved_at=ctx.retrieved_at,
            native_id=qid[:500],
            authoritative=True,
            pack_id=ctx.pack_id,
        ))
    return drafts
