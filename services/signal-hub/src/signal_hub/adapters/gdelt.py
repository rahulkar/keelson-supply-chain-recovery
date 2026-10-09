"""GDELT 2.0 event-export adapter (``124`` §2): 15-minute CAMEO-coded news events near ports and
chokepoints → low-authority ``inference`` signals (``extracted_by: gdelt-cameo``, machine-coded by
GDELT, never by an LLM).

Feed (checked 2026-10-08): ``/gdeltv2/lastupdate.txt`` lists three files as ``<size> <md5> <url>``;
the ``…export.CSV.zip`` one is a zip with one tab-separated, header-less ``.export.CSV`` of 61
columns (GDELT 2.0 event table: ``GLOBALEVENTID``, ``SQLDATE`` … ``IsRootEvent`` 25,
``EventCode`` 26, ``NumArticles`` 33, ``ActionGeo_Type`` 51, ``ActionGeo_Lat`` 56,
``ActionGeo_Long`` 57, ``DATEADDED`` 59, ``SOURCEURL`` 60). ``data.gdeltproject.org`` now redirects
``http`` to ``https``; a listed URL is accepted only on that host and the export path pattern
(:func:`parse_lastupdate`), fetched over ``https``, size- and MD5-checked, and unzipped with
:mod:`signal_hub.safezip`.

Row filter (:class:`Filter`): ``IsRootEvent = 1``; ``EventCode`` starting with a configured CAMEO code
(:data:`CAMEO` maps each to a kind/subtype); ``NumArticles >= min_articles``; ``ActionGeo_Type`` 3 or
4 (city level); and within ``port_radius_km`` of a gazetteer port or ``chokepoint_buffer_km`` of a
chokepoint polygon (the nearest port wins, else the chokepoint).

Aggregation: per UTC day of ``DATEADDED`` × subtype × place (``native_key``
``<YYYYMMDD>:<subtype>:<SCHEME>:<code>``); counts are cumulative over the day's files (the poller's
cursor) and reported in powers of two, so only a bucket change makes a new version. Geometry is the
gazetteer port point or chokepoint polygon, refs ``UNLOCODE`` (+ the port's ``ISO3166``) or
``CHOKEPOINT``. Severity is a subtype prior capped at ``GDELT_MAX_SEVERITY_BP`` (3900: below the
default alert threshold, so GDELT corroborates other reports but never alerts on its own).

Untrusted text: none is used. Titles and summaries are built from the CAMEO label, the gazetteer
place name and counts; actor and place names from the feed are never read. ``source_url`` is the
first ``SOURCEURL`` that passes :func:`signal_hub.models.safe_source_url`.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Iterable, Mapping, MutableMapping
from dataclasses import dataclass
from typing import Any, Final
from urllib.parse import urlsplit

from signal_hub import geo, safezip, scoring
from signal_hub.gazetteer import Gazetteer
from signal_hub.models import SignalDraft, SourceContext, clip, safe_source_url

__all__ = [
    "ATTRIBUTION",
    "CAMEO",
    "CONNECTOR_ID",
    "DEFAULT_FILTER",
    "EXTRACTED_BY",
    "HOST",
    "LICENSE",
    "SOURCE_NAME",
    "Filter",
    "GdeltError",
    "LastUpdate",
    "Row",
    "aggregate",
    "drafts_for",
    "matching_rows",
    "missed_slots",
    "parse_lastupdate",
    "processed_slot",
    "prune",
    "read_export",
    "slot_url",
]

CONNECTOR_ID: Final = "gdelt"
SOURCE_NAME: Final = "GDELT Project 2.0 events"
LICENSE: Final = "GDELT Project (open; citation required); linked articles © their publishers"
ATTRIBUTION: Final = "Source: The GDELT Project (gdeltproject.org)"
EXTRACTED_BY: Final = "gdelt-cameo"
HOST: Final = "data.gdeltproject.org"
MAX_EXPORT_BYTES: Final = 32 * 1024 * 1024
"""Unzipped export cap (a 15-minute file is about 0.5 MB)."""
MAX_ROWS: Final = 500_000
MAX_KEYS: Final = 2000
"""Aggregates kept in the cursor (the newest days win)."""
KEEP_DAYS: Final = 2
EXPIRY_AFTER_DAY: Final = dt.timedelta(hours=72)
"""``expires_at`` = start of the UTC day + 72 h (the day's end + 48 h)."""
COLUMNS: Final = 61
_ROOT, _CODE, _ARTICLES, _GEO_TYPE, _LAT, _LON, _ADDED, _URL = 25, 26, 33, 51, 56, 57, 59, 60
_SLOT_RE: Final = re.compile(r"^\d{14}$")
_EXPORT_PATH_RE: Final = re.compile(r"^/gdeltv2/(\d{14})\.export\.CSV\.zip$")
_MD5_RE: Final = re.compile(r"^[0-9a-f]{32}$")
SLOT: Final = dt.timedelta(minutes=15)

CAMEO: Final[dict[str, tuple[str, str]]] = {
    "143": ("labor", "strike"),
    "144": ("logistics", "blockade"),
    "145": ("geopolitical", "civil_unrest"),
    "163": ("regulatory", "trade_restriction"),
    "1724": ("geopolitical", "state_of_emergency"),
    "190": ("geopolitical", "armed_conflict"),
    "191": ("logistics", "blockade"),
    "193": ("geopolitical", "armed_conflict"),
    "194": ("geopolitical", "armed_conflict"),
    "195": ("geopolitical", "armed_conflict"),
    "196": ("geopolitical", "armed_conflict"),
}
"""CAMEO event-code prefix → (kind, subtype): 143x conduct strike or boycott; 144x obstruct
passage, block; 145x protest violently, riot; 163 impose embargo, boycott or sanctions; 1724 impose
state of emergency or martial law; 191 impose blockade, restrict movement; 190 use conventional
military force; 193-196 fight with small arms / artillery and tanks / aerial weapons, violate ceasefire.
Each subtype has exactly one kind (a blockade at a port or chokepoint — protesters' 144x or a
state's 191 — is a ``logistics`` disruption), so the day aggregate key needs no kind."""
_LABELS: Final[dict[str, str]] = {
    "strike": "Strike or boycott",
    "blockade": "Blockade or obstruction",
    "civil_unrest": "Violent protest or riot",
    "trade_restriction": "Embargo or sanctions",
    "state_of_emergency": "State of emergency",
    "armed_conflict": "Armed conflict",
}


class GdeltError(ValueError):
    """A lastupdate.txt or export file the adapter refuses."""


@dataclass(frozen=True, slots=True)
class LastUpdate:
    slot: str
    """``YYYYMMDDHHMMSS`` of the newest 15-minute file."""
    url: str
    size: int
    md5: str


@dataclass(frozen=True, slots=True)
class Filter:
    codes: tuple[str, ...]
    min_articles: int = 3
    port_radius_km: int = 30
    chokepoint_buffer_km: int = 50


DEFAULT_FILTER: Final = Filter(codes=tuple(CAMEO))
"""The code defaults (demo replays always use these, whatever the deployment's ``GDELT_*``)."""


@dataclass(frozen=True, slots=True)
class Row:
    """One kept event: its aggregate key parts and counts."""

    day: str
    subtype: str
    scheme: str
    place: str
    code: str
    articles: int
    added: dt.datetime
    url: str | None

    @property
    def key(self) -> str:
        return f"{self.day}:{self.subtype}:{self.scheme}:{self.place}"


def slot_url(slot: str) -> str:
    return f"https://{HOST}/gdeltv2/{slot}.export.CSV.zip"


def parse_lastupdate(body: bytes, *, allowed_host: str = HOST) -> LastUpdate:
    """The export line of ``lastupdate.txt`` (host, path pattern, size and MD5 format checked)."""
    for line in body.decode("ascii", errors="replace").splitlines():
        parts = line.split()
        if len(parts) != 3 or not parts[2].endswith(".export.CSV.zip"):
            continue
        size, md5, url = parts
        try:
            split = urlsplit(url)
        except ValueError:
            raise GdeltError("export URL is not a URL") from None
        m = _EXPORT_PATH_RE.match(split.path)
        if split.scheme not in ("http", "https") or (split.hostname or "").lower() != allowed_host.lower() \
                or split.username or split.query or not m:
            raise GdeltError(f"export URL not on {allowed_host} or not an export path")
        if not size.isdigit() or not _MD5_RE.match(md5.lower()):
            raise GdeltError("lastupdate.txt export line has no size / md5")
        return LastUpdate(slot=m.group(1), url=slot_url(m.group(1)), size=int(size), md5=md5.lower())
    raise GdeltError("lastupdate.txt has no export line")


def missed_slots(latest: str, done: str | None, max_backfill: int) -> list[str]:
    """Slots to fetch, oldest first: ``latest`` plus up to ``max_backfill`` 15-minute slots between
    the last processed one and it (an older gap is not backfilled). Nothing when ``latest`` is not
    newer than the last processed slot (a stale ``lastupdate.txt`` must not count a file twice)."""
    if not _SLOT_RE.match(latest):
        raise GdeltError("bad slot")
    if done and _SLOT_RE.match(done) and done >= latest:
        return []
    out = [latest]
    if done and _SLOT_RE.match(done):
        t, stop = _slot_time(latest) - SLOT, _slot_time(done)
        while t > stop and len(out) <= max_backfill:
            out.append(t.strftime("%Y%m%d%H%M%S"))
            t -= SLOT
    return sorted(out)


def processed_slot(latest: str, done: str | None) -> str:
    """The slot the cursor keeps after a poll: it never moves backwards, so slots already counted
    are never backfilled again."""
    return done if done and _SLOT_RE.match(done) and done > latest else latest


def _slot_time(slot: str) -> dt.datetime:
    return dt.datetime.strptime(slot, "%Y%m%d%H%M%S").replace(tzinfo=dt.UTC)


def read_export(body: bytes) -> bytes:
    """The export CSV of a zip (or a plain CSV fixture)."""
    if safezip.is_zip(body):
        return safezip.read_member(body, suffixes=(".export.csv",), max_uncompressed=MAX_EXPORT_BYTES)
    if len(body) > MAX_EXPORT_BYTES:
        raise GdeltError(f"export larger than {MAX_EXPORT_BYTES} bytes")
    return body


def _mapped(code: str, codes: Iterable[str]) -> tuple[str, str] | None:
    best = max((c for c in codes if code.startswith(c)), key=len, default=None)
    return CAMEO.get(best) if best else None


def _place(lat: float, lon: float, gaz: Gazetteer, flt: Filter) -> tuple[str, str] | None:
    best: tuple[float, str] | None = None
    for port in gaz.ports.values():
        if abs(port.lat - lat) * 110.0 > flt.port_radius_km + 1:
            continue
        d = geo.haversine_km(lon, lat, port.lon, port.lat)
        if d <= flt.port_radius_km and (best is None or (d, port.code) < best):
            best = (d, port.code)
    if best is not None:
        return "UNLOCODE", best[1]
    near: tuple[float, str] | None = None
    for choke in gaz.chokepoints.values():
        d = geo.distance_to_ring_km(lon, lat, choke.ring)
        if d <= flt.chokepoint_buffer_km and (near is None or (d, choke.code) < near):
            near = (d, choke.code)
    return ("CHOKEPOINT", near[1]) if near is not None else None


def matching_rows(csv_bytes: bytes, flt: Filter, gaz: Gazetteer) -> list[Row]:
    """The export rows that pass the filter (malformed rows are skipped)."""
    out: list[Row] = []
    text = csv_bytes.decode("utf-8", errors="replace")
    for n, line in enumerate(text.splitlines()):
        if n >= MAX_ROWS:
            raise GdeltError(f"export has more than {MAX_ROWS} rows")
        cols = line.split("\t")
        if len(cols) != COLUMNS or cols[_ROOT] != "1" or cols[_GEO_TYPE] not in ("3", "4"):
            continue
        code = cols[_CODE].strip()
        mapped = _mapped(code, flt.codes) if code.isdigit() else None
        if mapped is None or not cols[_ARTICLES].isdigit() or int(cols[_ARTICLES]) < flt.min_articles:
            continue
        added = cols[_ADDED]
        try:
            lat, lon = float(cols[_LAT]), float(cols[_LON])
            when = _slot_time(added) if _SLOT_RE.match(added) else None
        except ValueError:
            continue
        if when is None or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        place = _place(lat, lon, gaz, flt)
        if place is None:
            continue
        out.append(Row(day=added[:8], subtype=mapped[1], scheme=place[0], place=place[1], code=code[:4],
                       articles=min(int(cols[_ARTICLES]), 1_000_000), added=when,
                       url=safe_source_url(cols[_URL].strip() or None)))
    return out


def aggregate(agg: MutableMapping[str, dict[str, Any]], rows: Iterable[Row]) -> set[str]:
    """Add rows to the cumulative per-day aggregates (``agg`` is the cursor's map) → keys touched."""
    touched: set[str] = set()
    for r in rows:
        a = agg.setdefault(r.key, {"events": 0, "articles": 0, "codes": [], "url": None, "added": None})
        a["events"] += 1
        a["articles"] += r.articles
        if r.code not in a["codes"]:
            a["codes"] = sorted([*a["codes"], r.code])[:20]
        if a["url"] is None and r.url is not None:
            a["url"] = r.url
        added = r.added.isoformat()
        if a["added"] is None or added > a["added"]:
            a["added"] = added
        touched.add(r.key)
    return touched


def prune(agg: MutableMapping[str, dict[str, Any]], now: dt.datetime) -> None:
    """Drop aggregates older than :data:`KEEP_DAYS` days, then the oldest beyond :data:`MAX_KEYS`."""
    oldest = (now - dt.timedelta(days=KEEP_DAYS)).strftime("%Y%m%d")
    for key in [k for k in agg if k[:8] < oldest]:
        del agg[key]
    if len(agg) > MAX_KEYS:
        for key in sorted(agg, key=lambda k: (k[:8], agg[k]["articles"], k))[: len(agg) - MAX_KEYS]:
            del agg[key]


def _bucket(n: int) -> int:
    """The largest power of two <= ``n`` (``n >= 1``)."""
    return 1 << (max(1, n).bit_length() - 1)


def drafts_for(
    agg: Mapping[str, Mapping[str, Any]],
    keys: Iterable[str],
    ctx: SourceContext,
    gaz: Gazetteer,
    *,
    cap_bp: int = 3900,
    flt: Filter = DEFAULT_FILTER,
) -> list[SignalDraft]:
    """One draft per aggregate key (sorted; unknown places are skipped)."""
    drafts: list[SignalDraft] = []
    for key in sorted(set(keys)):
        a = agg.get(key)
        parts = key.split(":")
        if a is None or len(parts) != 4:
            continue
        day, subtype, scheme, code = parts
        kind = next((k for k, s in CAMEO.values() if s == subtype), None)
        try:
            start = dt.datetime.strptime(day, "%Y%m%d").replace(tzinfo=dt.UTC)
        except ValueError:
            continue
        if kind is None:
            continue
        if scheme == "UNLOCODE" and code in gaz.ports:
            port = gaz.ports[code]
            geometry, refs, place = port.geometry, [f"UNLOCODE:{code}", f"ISO3166:{port.country}"], port.name
            radius = flt.port_radius_km
        elif scheme == "CHOKEPOINT" and code in gaz.chokepoints:
            choke = gaz.chokepoints[code]
            geometry, refs, place = choke.geometry, [f"CHOKEPOINT:{code}"], choke.name
            radius = flt.chokepoint_buffer_km
        else:
            continue
        events, articles = int(a.get("events") or 0), int(a.get("articles") or 0)
        added = a.get("added")
        observed = dt.datetime.fromisoformat(added) if isinstance(added, str) else start
        codes = ",".join(str(c) for c in a.get("codes") or [])[:200]
        label = _LABELS[subtype]
        summary = (f"GDELT 2.0: {_bucket(events)}+ machine-coded event(s) (CAMEO {codes}) in {_bucket(articles)}+ "
                   f"news articles within {radius} km of {place} on {start:%Y-%m-%d} (UTC). Low-authority inference "
                   "coded automatically from news coverage; not verified.")
        drafts.append(SignalDraft(
            tenant=ctx.tenant,
            connector_id=ctx.connector(CONNECTOR_ID),
            native_key=ctx.key(key),
            origin=ctx.origin("news"),
            kind=kind,
            subtype=subtype,
            title=clip(f"{label} reported near {place} (GDELT, {_bucket(articles)}+ articles)", 300) or label,
            summary=clip(summary, 2000),
            raw_text=None,
            geometry=geometry,
            location_refs=refs,
            starts_at=start,
            ends_at=None,
            observed_at=min(observed, ctx.retrieved_at),
            expires_at=start + EXPIRY_AFTER_DAY,
            severity_bp=scoring.gdelt_severity_bp(subtype, cap_bp),
            severity_native={"scheme": "gdelt", "day": day, "cameo": codes or None, "events_bucket": _bucket(events),
                             "articles_bucket": _bucket(articles), "place": f"{scheme}:{code}"},
            provenance=ctx.provenance("inference"),
            extracted_by=EXTRACTED_BY,
            source_name=ctx.source_name(SOURCE_NAME),
            source_url=a.get("url") if isinstance(a.get("url"), str) else None,
            license=LICENSE,
            authority_bp=scoring.AUTHORITY["gdelt"],
            retrieved_at=ctx.retrieved_at,
            native_id=key,
            authoritative=False,
            pack_id=ctx.pack_id,
        ))
    return drafts
