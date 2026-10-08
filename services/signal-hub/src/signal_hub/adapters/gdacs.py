"""GDACS adapter (``100`` §3): ``geteventlist`` GeoJSON → ``fact`` signals (TC/EQ/FL…).

Feed shape (GDACS API v2, ``/gdacsapi/api/events/geteventlist/SEARCH``): a FeatureCollection whose
event features have a centroid ``Point`` and ``properties`` ``eventtype`` (TC, EQ, FL, VO, DR, WF),
``eventid``, ``episodeid``, ``name``/``eventname``, ``alertlevel`` (Green/Orange/Red), ``alertscore``,
``episodealertlevel``, ``iscurrent``, ``fromdate``/``todate``/``datemodified`` (naive UTC),
``affectedcountries`` ``[{iso2, iso3, countryname}]``, ``severitydata`` ``{severity, severitytext,
severityunit}`` and ``url`` ``{geometry, report, details}``. Footprints (TC cones/wind buffers,
tracks, flood areas) come from ``/gdacsapi/api/polygons/getgeometry`` as features with the same
``eventtype``/``eventid``/``episodeid`` and Polygon/MultiPolygon/LineString geometry; a fixture
(demo pack) may embed them in the same collection.

One signal per event (native key ``<type>:<eventid>``); a new episode or a changed advisory is a
new *version* of the same signal (the store bumps it when the content hash changes). Licence:
CC BY 4.0 (attribution in the UI).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from typing import Any, Final
from urllib.parse import urlsplit

from signal_hub import geo, scoring
from signal_hub.gazetteer import Gazetteer
from signal_hub.models import SignalDraft, SourceContext, clip, safe_source_url
from signal_hub.timeutil import parse_ts

__all__ = ["CONNECTOR_ID", "LICENSE", "footprint_urls", "latest_episodes", "parse_gdacs"]

CONNECTOR_ID: Final = "gdacs"
SOURCE_NAME: Final = "GDACS (EC JRC / UN OCHA)"
LICENSE: Final = "CC BY 4.0"
_SUBTYPES: Final[dict[str, str]] = {
    "TC": "tropical_cyclone",
    "EQ": "earthquake",
    "FL": "flood",
    "VO": "volcano",
    "DR": "drought",
    "WF": "wildfire",
}
_TYPE_LABEL: Final[dict[str, str]] = {
    "TC": "tropical cyclone", "EQ": "earthquake", "FL": "flood", "VO": "volcano", "DR": "drought", "WF": "wildfire",
}
_EXPIRY: Final[dict[str, dt.timedelta]] = {
    "TC": dt.timedelta(hours=48),
    "EQ": dt.timedelta(days=7),
    "FL": dt.timedelta(hours=72),
}
_FOOTPRINT_TYPES: Final = {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes"}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f == f and abs(f) != float("inf") else None


def _key(props: Mapping[str, Any]) -> tuple[str, int] | None:
    etype = str(props.get("eventtype") or "").strip().upper()
    eid = _int(props.get("eventid"))
    if etype not in _SUBTYPES or eid is None:
        return None
    return etype, eid


def _features(doc: Any) -> list[dict[str, Any]]:
    features = doc.get("features") if isinstance(doc, dict) else None
    return [f for f in features if isinstance(f, dict)] if isinstance(features, list) else []


def latest_episodes(doc: Any) -> dict[tuple[str, int], int | None]:
    """Latest ``episodeid`` per ``(eventtype, eventid)`` of an event list (event ids are per type;
    ``eventid``/``episodeid`` may be numbers or numeric strings)."""
    out: dict[tuple[str, int], int | None] = {}
    for feature in _features(doc):
        key = _key(_dict(feature.get("properties")))
        if key is None:
            continue
        episode = _int(_dict(feature.get("properties")).get("episodeid"))
        current = out.get(key)
        if key not in out or (episode is not None and (current is None or episode > current)):
            out[key] = episode
    return out


def footprint_urls(doc: Any, *, allowed_host: str) -> dict[tuple[str, int], str]:
    """``url.geometry`` per TC event of an event list, only on ``allowed_host`` (no SSRF via feed data)."""
    out: dict[tuple[str, int], str] = {}
    for feature in _features(doc):
        props = _dict(feature.get("properties"))
        key = _key(props)
        urls = _dict(props.get("url"))
        url = urls.get("geometry")
        if key is None or key[0] != "TC" or not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url)
        except ValueError:
            continue
        if parts.scheme in ("http", "https") and (parts.hostname or "").lower() == allowed_host.lower():
            out[key] = url
    return out


def parse_gdacs(
    doc: Any,
    ctx: SourceContext,
    gaz: Gazetteer,
    *,
    extra_footprints: Mapping[tuple[str, int], list[dict[str, Any]]] | None = None,
) -> list[SignalDraft]:
    """Event-list GeoJSON (+ optional footprint features per event) → signal drafts."""
    if not isinstance(doc, dict):
        return []
    events: dict[tuple[str, int], dict[str, Any]] = {}
    footprints: dict[tuple[str, int, int | None], list[dict[str, Any]]] = {}
    for feature in _features(doc):
        props = _dict(feature.get("properties"))
        key = _key(props)
        geom = feature.get("geometry") or {}
        if key is None or not isinstance(geom, dict):
            continue
        episode = _int(props.get("episodeid"))
        if geom.get("type") in _FOOTPRINT_TYPES:
            footprints.setdefault((key[0], key[1], episode), []).append(geom)
            continue
        if geom.get("type") != "Point":
            continue
        klass, label = props.get("Class"), props.get("polygonlabel")
        if (klass is not None and klass != "Point_Centroid") or (klass is None and label not in (None, "", "Centroid")):
            continue  # track points etc. of a getgeometry collection
        current = events.get(key)
        if current is None or (episode or 0) >= (_int(current["properties"].get("episodeid")) or 0):
            events[key] = feature
    for key, features in (extra_footprints or {}).items():
        for f in features:
            if not isinstance(f, dict):
                continue
            geom = f.get("geometry") or {}
            if isinstance(geom, dict) and geom.get("type") in _FOOTPRINT_TYPES:
                ep = _int(_dict(f.get("properties")).get("episodeid"))
                footprints.setdefault((key[0], key[1], ep), []).append(geom)

    drafts: list[SignalDraft] = []
    for key in sorted(events):
        draft = _draft(events[key], key, footprints, ctx, gaz)
        if draft is not None:
            drafts.append(draft)
    return drafts


def _draft(
    feature: dict[str, Any],
    key: tuple[str, int],
    footprints: Mapping[tuple[str, int, int | None], list[dict[str, Any]]],
    ctx: SourceContext,
    gaz: Gazetteer,
) -> SignalDraft | None:
    props: dict[str, Any] = _dict(feature.get("properties"))
    etype, eid = key
    episode = _int(props.get("episodeid"))
    level = str(props.get("alertlevel") or props.get("episodealertlevel") or "green").strip().lower()
    if level not in ("green", "orange", "red"):
        level = "green"
    sevdata = _dict(props.get("severitydata"))
    severity_value = _float(sevdata.get("severity"))
    alertscore = _float(props.get("alertscore"))
    starts = parse_ts(props.get("fromdate")) or parse_ts(props.get("datemodified")) or ctx.retrieved_at
    todate = parse_ts(props.get("todate"))
    observed = parse_ts(props.get("datemodified")) or todate or starts
    current = _truthy(props.get("iscurrent", "true"))
    ends = None if current and etype == "TC" else (todate if todate and todate >= starts else None)
    if not current:
        expires = observed
    else:
        expires = (todate or starts) + _EXPIRY.get(etype, dt.timedelta(hours=72))
        if etype == "EQ":
            expires = starts + _EXPIRY["EQ"]

    # location refs: affected countries (iso2, else iso3 → iso2)
    refs: list[str] = []
    names: list[str] = []
    countries = props.get("affectedcountries")
    for c in countries if isinstance(countries, list) else []:
        if not isinstance(c, dict):
            continue
        iso = gaz.iso2(str(c.get("iso2") or "")) or gaz.iso2(str(c.get("iso3") or ""))
        if iso and f"ISO3166:{iso}" not in refs:
            refs.append(f"ISO3166:{iso}")
            names.append(str(c.get("countryname") or iso))
    if not refs and (iso := gaz.iso2(str(props.get("iso3") or ""))):
        refs.append(f"ISO3166:{iso}")
        names.append(str(props.get("country") or iso))

    # geometry: footprints of this episode (or of an unknown episode), else the centroid
    shapes = list(footprints.get((etype, eid, episode), [])) + list(footprints.get((etype, eid, None), []))
    centroid = geo.clean_geometry(feature.get("geometry"))
    geometry = geo.merge([*shapes, *([centroid] if centroid else [])]) if shapes else centroid

    name = props.get("name") or props.get("eventname") or props.get("description")
    title = clip(str(name), 300) if name else None
    if not title:
        title = f"GDACS {_TYPE_LABEL[etype]} {eid}"
    sevtext = clip(str(sevdata.get("severitytext") or ""), 200)
    summary_parts = [f"GDACS {level.capitalize()} alert ({_TYPE_LABEL[etype]})"]
    if sevtext:
        summary_parts.append(sevtext)
    if names:
        summary_parts.append("affected: " + ", ".join(names[:12]))
    severity_native: dict[str, Any] = {
        "scheme": "gdacs",
        "eventtype": etype,
        "alertlevel": level,
        "episodeid": episode,
        "iscurrent": current,
    }
    if alertscore is not None:
        severity_native["alertscore"] = alertscore
    if severity_value is not None:
        severity_native["severity"] = severity_value
    if sevdata.get("severityunit"):
        severity_native["severityunit"] = str(sevdata["severityunit"])[:40]
    if sevtext:
        severity_native["severitytext"] = sevtext
    if etype == "EQ" and severity_value is not None:
        severity_native["mag"] = severity_value
    ep_level = str(props.get("episodealertlevel") or "").strip().lower()
    if ep_level:
        severity_native["episodealertlevel"] = ep_level[:20]

    urls = _dict(props.get("url"))
    report = safe_source_url(urls.get("report") if isinstance(urls.get("report"), str) else None)
    return SignalDraft(
        tenant=ctx.tenant,
        connector_id=ctx.connector(CONNECTOR_ID),
        native_key=ctx.key(f"{etype}:{eid}"),
        origin=ctx.origin("feed"),
        kind="natural_hazard",
        subtype=_SUBTYPES[etype],
        title=title,
        summary=clip(" · ".join(summary_parts), 2000),
        raw_text=None,
        geometry=geometry,
        location_refs=refs,
        starts_at=starts,
        ends_at=ends,
        observed_at=observed,
        expires_at=expires,
        severity_bp=scoring.gdacs_severity_bp(etype, level, alertscore=alertscore, severity=severity_value),
        severity_native=severity_native,
        provenance=ctx.provenance("fact"),
        extracted_by=None,
        source_name=ctx.source_name(SOURCE_NAME),
        source_url=report,
        license=LICENSE,
        authority_bp=scoring.AUTHORITY["gdacs"],
        retrieved_at=ctx.retrieved_at,
        native_id=f"{etype}:{eid}:{episode if episode is not None else 0}",
        authoritative=True,
        pack_id=ctx.pack_id,
    )
