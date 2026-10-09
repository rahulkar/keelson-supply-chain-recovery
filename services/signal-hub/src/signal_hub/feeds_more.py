"""Polling of the M8 feeds (``124`` §2): NHC, Copernicus EMS, OFAC SDN, GDELT 2.0, MeteoAlarm.

Each ``poll_<adapter>(runner, spec, now)`` follows the :class:`~signal_hub.feeds.FeedRunner` pattern
(cursor + conditional GET, ``_save`` with backoff on failure, ingest through the pipeline) and is
dispatched from the registry (:data:`POLLERS`). Follow-up downloads named by feed data (NHC KMZ,
MeteoAlarm CAP documents, GDELT exports) are fetched only from the adapter's fixed host over https,
are bounded per poll, and go through the same guarded, proxied client as every feed request.

Snapshot feeds (NHC storms, MeteoAlarm warnings) list what is active *now*: a current signal that
left the feed is withdrawn (:func:`withdraw_missing`: a new version that expires now, so its event
resolves and its alerts with it). A malformed feed raises before anything is withdrawn.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Final

from signal_hub import geo
from signal_hub.adapters import copernicus_ems, gdelt, kml, meteoalarm, nhc, ofac
from signal_hub.db import tx
from signal_hub.feeds import PollResult
from signal_hub.httpfetch import FetchError
from signal_hub.logs import get_logger
from signal_hub.models import SignalDraft, SourceContext
from signal_hub.pipeline import IngestReport
from signal_hub.sources import SourceSpec
from signal_hub.timeutil import parse_ts

if TYPE_CHECKING:
    from signal_hub.feeds import FeedRunner

__all__ = [
    "POLLERS",
    "poll_copernicus_ems",
    "poll_gdelt",
    "poll_meteoalarm",
    "poll_nhc",
    "poll_ofac",
    "stored_geometries",
    "withdraw_missing",
]

log = get_logger(__name__)
OFAC_MIN_KEEP_RATIO: Final = 0.5
"""A publication with fewer than half the organisations of the last snapshot is refused (a truncated
download must never look like mass delistings)."""
MAX_DETAIL_CACHE: Final = 2000


def _message(error: Exception) -> str:
    """A fetch error already names the source; a parse error gets its type for the admin page."""
    return str(error)[:500] if isinstance(error, FetchError) else f"{type(error).__name__}: {error}"[:500]


async def _error(runner: FeedRunner, cid: str, cadence: int, now: dt.datetime, exc: Exception) -> PollResult:
    message = _message(exc)
    await runner._save(cid, now=now, cadence_s=cadence, status="error", error=message)
    log.warning("poll failed", extra={"connector": cid, "error": message[:300]})
    return PollResult(cid, "error", error=message[:300])


def _draft_from_row(row: Any, now: dt.datetime) -> SignalDraft:
    """A stored current signal as a draft that expires ``now`` (the withdrawal version)."""
    doc = row["doc"]
    src = doc["source"]

    def ts(value: Any) -> dt.datetime | None:
        return parse_ts(value) if value is not None else None

    starts = ts(doc["starts_at"]) or now
    return SignalDraft(
        tenant=row["tenant"],
        connector_id=row["connector_id"],
        native_key=row["native_key"],
        origin=doc["origin"],
        kind=doc["kind"],
        subtype=doc["subtype"],
        title=doc["title"],
        summary=doc.get("summary"),
        raw_text=doc.get("raw_text"),
        geometry=doc.get("geometry"),
        location_refs=list(doc.get("location_refs") or []),
        starts_at=starts,
        ends_at=ts(doc.get("ends_at")),
        observed_at=min(ts(doc["observed_at"]) or now, now),
        expires_at=now,
        severity_bp=int(doc["severity_bp"]),
        severity_native=dict(doc.get("severity_native") or {"scheme": "none"}),
        provenance=doc["provenance"],
        extracted_by=doc.get("extracted_by"),
        source_name=src["name"],
        source_url=src.get("url"),
        license=src["license"],
        authority_bp=int(src["authority_bp"]),
        retrieved_at=now,
        native_id=src["native_id"],
        authoritative=bool(row["authoritative"]),
        pack_id=row["pack_id"],
        match_keys=list(row["match_keys"] or []),
    )


async def withdraw_missing(runner: FeedRunner, connector_id: str, keep: Iterable[str],
                           now: dt.datetime) -> tuple[int, IngestReport]:
    """Expire every current, unexpired global signal of ``connector_id`` whose native key is not in
    ``keep`` (it left the snapshot feed) → (signals withdrawn, ingest report)."""
    async with tx(runner.pool, None) as conn:
        rows = await conn.fetch(
            """SELECT tenant, connector_id, native_key, authoritative, pack_id, match_keys, doc FROM signal
               WHERE tenant IS NULL AND connector_id = $1 AND is_current
                 AND (expires_at IS NULL OR expires_at > $2) AND NOT (native_key = ANY($3::text[]))
               ORDER BY native_key""",
            connector_id, now, sorted(set(keep)))
    if not rows:
        return 0, IngestReport()
    drafts = [_draft_from_row(r, now) for r in rows]
    log.info("withdrawing signals that left the feed", extra={"connector": connector_id, "count": len(drafts)})
    return len(drafts), await runner.pipeline.ingest(drafts, now=now)


async def stored_geometries(runner: FeedRunner, connector_id: str, native_keys: Sequence[str]) -> dict[str, Any]:
    """``native_key → geometry`` of the current global signals (reused instead of re-downloading)."""
    if not native_keys:
        return {}
    async with tx(runner.pool, None) as conn:
        rows = await conn.fetch(
            """SELECT native_key, doc->'geometry' AS geometry FROM signal WHERE tenant IS NULL AND connector_id = $1
               AND is_current AND native_key = ANY($2::text[]) AND doc->'geometry' <> 'null'::jsonb""",
            connector_id, sorted(set(native_keys)))
    return {r["native_key"]: r["geometry"] for r in rows if r["geometry"]}


async def _current_keys(runner: FeedRunner, connector_id: str, native_keys: Sequence[str]) -> set[str]:
    if not native_keys:
        return set()
    async with tx(runner.pool, None) as conn:
        rows = await conn.fetch(
            """SELECT native_key FROM signal WHERE tenant IS NULL AND connector_id = $1 AND is_current
               AND native_key = ANY($2::text[])""", connector_id, sorted(set(native_keys)))
    return {r["native_key"] for r in rows}


def _json(body: bytes) -> Any:
    return json.loads(body.decode("utf-8-sig")) if body.strip() else {}


# ───────────────────────────── NHC ─────────────────────────────


async def poll_nhc(runner: FeedRunner, spec: SourceSpec, now: dt.datetime) -> PollResult:
    cid, s = spec.connector_id, runner.settings
    cadence = runner.cadence(cid, spec.default_cadence_s)
    cur = await runner.cursor(cid)
    try:
        result = await runner.fetcher.get(cid, s.nhc_url, etag=cur.get("etag"), last_modified=cur.get("last_modified"),
                                          accept="application/json")
        if result.not_modified:
            await runner._save(cid, now=now, cadence_s=cadence, status="not_modified")
            return PollResult(cid, "not_modified")
        doc = _json(result.body)
        active = nhc.storms(doc)  # NhcError (a ValueError) on a malformed feed: nothing is withdrawn
        cursor = dict(cur.get("cursor") or {})
        advisories: dict[str, str] = dict(cursor.get("advisories") or {})
        gis: dict[str, list[dict[str, Any]]] = {}
        if s.nhc_fetch_gis:
            stored = await stored_geometries(runner, cid, [st["id"].lower() for st in active])
            fetched = 0
            for storm in sorted(active, key=lambda st: st["id"].lower()):
                sid, tag, urls = storm["id"].lower(), nhc.advisory_tag(storm), nhc.gis_urls(storm)
                if not urls or tag is None:
                    continue
                if advisories.get(sid) == tag:  # unchanged advisory: reuse the stored footprint
                    shapes = [g for g in geo.flatten(stored.get(sid)) if g.get("type") != "Point"]
                    if shapes:
                        gis[sid] = shapes
                        continue
                if fetched + len(urls) > s.nhc_max_gis_fetches:
                    continue
                shapes, ok = [], True
                for url in urls:
                    fetched += 1
                    try:
                        body = (await runner.fetcher.get(cid, url, accept="application/vnd.google-earth.kmz")).body
                        shapes += kml.kml_geometries(kml.read_kml_or_kmz(body))
                    except (FetchError, ValueError) as exc:
                        ok = False
                        log.warning("nhc GIS fetch failed", extra={"storm": sid, "error": str(exc)[:200]})
                if shapes:
                    gis[sid] = shapes
                if ok:
                    advisories[sid] = tag
        drafts = nhc.parse_nhc(doc, SourceContext(retrieved_at=now), gis=gis)
        report = await runner.pipeline.ingest(drafts, now=now)
        live = {st["id"].lower() for st in active}  # the native keys of the global storm signals
        withdrawn, gone = await withdraw_missing(runner, cid, live, now)
        report.merge(gone)
        cursor["advisories"] = {k: v for k, v in sorted(advisories.items()) if k in live}
        await runner._save(cid, now=now, cadence_s=cadence, status="ok", etag=result.etag,
                           last_modified=result.last_modified, cursor=cursor, items=len(drafts))
        return PollResult(cid, "ok", len(drafts), report, skipped=len(active) - len(drafts), withdrawn=withdrawn)
    except (FetchError, ValueError, UnicodeDecodeError) as exc:
        return await _error(runner, cid, cadence, now, exc)


# ───────────────────────────── Copernicus EMS ─────────────────────────────


async def poll_copernicus_ems(runner: FeedRunner, spec: SourceSpec, now: dt.datetime) -> PollResult:
    cid, s = spec.connector_id, runner.settings
    cadence = runner.cadence(cid, spec.default_cadence_s)
    cur = await runner.cursor(cid)
    try:
        result = await runner.fetcher.get(cid, s.copernicus_ems_url, etag=cur.get("etag"),
                                          last_modified=cur.get("last_modified"), accept="application/json")
        if result.not_modified:
            await runner._save(cid, now=now, cadence_s=cadence, status="not_modified")
            return PollResult(cid, "not_modified")
        drafts, skipped = copernicus_ems.parse_ems(_json(result.body), SourceContext(retrieved_at=now), runner.gaz,
                                                   ttl_days=s.copernicus_ems_ttl_days)
        # the list is the activation history: an already-expired activation is only ingested to
        # close a signal the hub still holds (no resolved events for years-old activations)
        stale = [d.native_key for d in drafts if d.expires_at is not None and d.expires_at <= now]
        known = await _current_keys(runner, cid, stale)
        fresh = [d for d in drafts if d.expires_at is None or d.expires_at > now or d.native_key in known]
        report = await runner.pipeline.ingest(fresh, now=now)
        await runner._save(cid, now=now, cadence_s=cadence, status="ok", etag=result.etag,
                           last_modified=result.last_modified, items=len(fresh))
        return PollResult(cid, "ok", len(fresh), report, skipped=skipped + len(drafts) - len(fresh))
    except (FetchError, ValueError, UnicodeDecodeError) as exc:
        return await _error(runner, cid, cadence, now, exc)


# ───────────────────────────── OFAC SDN ─────────────────────────────


async def _snapshot(runner: FeedRunner, cid: str) -> dict[str, str]:
    async with tx(runner.pool, None) as conn:
        rows = await conn.fetch("SELECT item_key, item_hash FROM source_snapshot WHERE connector_id = $1", cid)
    return {r["item_key"]: r["item_hash"] for r in rows}


async def _write_snapshot(runner: FeedRunner, cid: str, current: dict[str, str], upsert: Iterable[str],
                          removed: Sequence[str], now: dt.datetime) -> None:
    rows = [(cid, k, current[k], now) for k in upsert]
    async with tx(runner.pool, None) as conn:
        if rows:
            await conn.executemany(
                """INSERT INTO source_snapshot (connector_id, item_key, item_hash, first_seen_at, updated_at)
                   VALUES ($1, $2, $3, $4, $4)
                   ON CONFLICT (connector_id, item_key) DO UPDATE SET item_hash = EXCLUDED.item_hash,
                     updated_at = EXCLUDED.updated_at""", rows)
        if removed:
            await conn.execute("DELETE FROM source_snapshot WHERE connector_id = $1 AND item_key = ANY($2::text[])",
                               cid, list(removed))


def _published(last_modified: str | None, now: dt.datetime) -> dt.datetime:
    try:
        parsed = parsedate_to_datetime(last_modified) if last_modified else None
    except (TypeError, ValueError):
        parsed = None
    if parsed is None or parsed.tzinfo is None:
        return now.replace(microsecond=0)
    return min(parsed.astimezone(dt.UTC), now)


async def poll_ofac(runner: FeedRunner, spec: SourceSpec, now: dt.datetime) -> PollResult:
    """Conditional GET of the SDN/ALT/ADD trio (validators per file in the cursor). When any file
    changed, all three are read in full and diffed against ``source_snapshot``; the first
    successful poll only stores the baseline (no signal)."""
    cid, s = spec.connector_id, runner.settings
    cadence = runner.cadence(cid, spec.default_cadence_s)
    cur = await runner.cursor(cid)
    cursor = dict(cur.get("cursor") or {})
    validators: dict[str, dict[str, str | None]] = dict(cursor.get("files") or {})
    urls = {"SDN": s.ofac_sdn_url, "ALT": s.ofac_alt_url, "ADD": s.ofac_add_url}
    try:
        previous = await _snapshot(runner, cid)
        bodies: dict[str, bytes] = {}
        fresh: dict[str, dict[str, str | None]] = {}
        unchanged: list[str] = []
        for name, url in urls.items():
            v = (validators.get(name) or {}) if previous else {}  # no baseline yet: fetch in full
            result = await runner.fetcher.get(cid, url, etag=v.get("etag"), last_modified=v.get("last_modified"),
                                              accept="text/csv", max_bytes=s.ofac_max_bytes)
            if result.not_modified:
                unchanged.append(name)
                continue
            bodies[name] = result.body
            fresh[name] = {"etag": result.etag, "last_modified": result.last_modified}
        if not bodies:
            await runner._save(cid, now=now, cadence_s=cadence, status="not_modified")
            return PollResult(cid, "not_modified")
        for name in unchanged:  # one file changed: the delta needs all three in full
            result = await runner.fetcher.get(cid, urls[name], accept="text/csv", max_bytes=s.ofac_max_bytes)
            bodies[name] = result.body
            fresh[name] = {"etag": result.etag, "last_modified": result.last_modified}
        entities = ofac.parse_entities(bodies["SDN"], bodies["ALT"], bodies["ADD"])
        if not entities:
            raise ofac.OfacError("the SDN file lists no organisations")
        if previous and len(entities) < OFAC_MIN_KEEP_RATIO * len(previous):
            raise ofac.OfacError(f"{len(entities)} organisations against {len(previous)} in the last snapshot; "
                                 "refusing a likely truncated publication")
        current = ofac.hashes(entities)
        published = _published(fresh["SDN"].get("last_modified"), now)
        cursor["files"] = fresh
        if not previous:
            await _write_snapshot(runner, cid, current, sorted(current), [], now)
            cursor["baseline_at"] = now.isoformat()
            log.info("ofac baseline stored (no signal)", extra={"organisations": len(current)})
            await runner._save(cid, now=now, cadence_s=cadence, status="ok", cursor=cursor)
            return PollResult(cid, "ok", 0, IngestReport())
        delta = ofac.diff(current, previous)
        drafts = ([ofac.publication_draft(entities, delta, SourceContext(retrieved_at=now), published_at=published,
                                          ttl_days=s.ofac_event_ttl_days)] if delta.any else [])
        report = await runner.pipeline.ingest(drafts, now=now)
        if report.invalid:
            raise ofac.OfacError("the publication signal failed validation; the snapshot is kept")
        await _write_snapshot(runner, cid, current, [*delta.new, *delta.changed], delta.removed, now)
        cursor["last_delta"] = {"published_at": published.isoformat(), "new": len(delta.new),
                                "changed": len(delta.changed), "removed": len(delta.removed)}
        await runner._save(cid, now=now, cadence_s=cadence, status="ok", cursor=cursor,
                           items=len(drafts))
        return PollResult(cid, "ok", len(drafts), report)
    except (FetchError, ValueError, UnicodeDecodeError) as exc:
        return await _error(runner, cid, cadence, now, exc)


# ───────────────────────────── GDELT ─────────────────────────────


async def poll_gdelt(runner: FeedRunner, spec: SourceSpec, now: dt.datetime) -> PollResult:
    """``lastupdate.txt`` (conditional), then the new 15-minute export (MD5-checked) plus up to
    ``GDELT_MAX_BACKFILL`` missed ones; the day aggregates live in the cursor."""
    cid, s = spec.connector_id, runner.settings
    cadence = runner.cadence(cid, spec.default_cadence_s)
    cur = await runner.cursor(cid)
    try:
        result = await runner.fetcher.get(cid, s.gdelt_lastupdate_url, etag=cur.get("etag"),
                                          last_modified=cur.get("last_modified"), accept="text/plain")
        if result.not_modified:
            await runner._save(cid, now=now, cadence_s=cadence, status="not_modified")
            return PollResult(cid, "not_modified")
        latest = gdelt.parse_lastupdate(result.body)
        cursor = dict(cur.get("cursor") or {})
        agg: dict[str, dict[str, Any]] = {k: dict(v) for k, v in (cursor.get("agg") or {}).items()}
        flt = gdelt.Filter(codes=s.gdelt_event_codes, min_articles=s.gdelt_min_articles,
                           port_radius_km=s.gdelt_port_radius_km, chokepoint_buffer_km=s.gdelt_chokepoint_buffer_km)
        touched: set[str] = set()
        done = cursor.get("slot") if isinstance(cursor.get("slot"), str) else None
        slots = gdelt.missed_slots(latest.slot, done, max(0, s.gdelt_max_backfill))
        for slot in slots:
            try:
                body = (await runner.fetcher.get(cid, gdelt.slot_url(slot), accept="application/zip",
                                                 max_bytes=gdelt.MAX_EXPORT_BYTES)).body
            except FetchError as exc:
                if slot != latest.slot:  # a missed slot that is not there (any more) is skipped
                    log.warning("gdelt backfill slot skipped", extra={"slot": slot, "error": str(exc)[:200]})
                    continue
                raise
            if slot == latest.slot and (len(body) != latest.size or hashlib.md5(body).hexdigest() != latest.md5):
                raise gdelt.GdeltError(f"export {slot}: size/MD5 does not match lastupdate.txt")
            touched |= gdelt.aggregate(agg, gdelt.matching_rows(gdelt.read_export(body), flt, runner.gaz))
        gdelt.prune(agg, now)
        drafts = gdelt.drafts_for(agg, touched & set(agg), SourceContext(retrieved_at=now), runner.gaz,
                                  cap_bp=s.gdelt_max_severity_bp, flt=flt)
        report = await runner.pipeline.ingest(drafts, now=now)
        cursor.update(slot=gdelt.processed_slot(latest.slot, done), agg=agg)
        await runner._save(cid, now=now, cadence_s=cadence, status="ok", etag=result.etag,
                           last_modified=result.last_modified, cursor=cursor, items=len(drafts))
        return PollResult(cid, "ok", len(drafts), report)
    except (FetchError, ValueError, UnicodeDecodeError) as exc:
        return await _error(runner, cid, cadence, now, exc)


# ───────────────────────────── MeteoAlarm ─────────────────────────────


async def poll_meteoalarm(runner: FeedRunner, spec: SourceSpec, now: dt.datetime) -> PollResult:
    """One country feed: warnings at or above the level, CAP polygons for at most
    ``METEOALARM_MAX_DETAIL_FETCHES`` new alerts per poll (later polls fill the rest; known ones
    keep their stored polygon), and withdrawal of warnings that left the feed or were cancelled."""
    cid, s = spec.connector_id, runner.settings
    cadence = runner.cadence(cid, spec.default_cadence_s)
    cur = await runner.cursor(cid)
    slug = cid.split(".", 1)[1]
    try:
        result = await runner.fetcher.get(cid, spec.url, etag=cur.get("etag"), last_modified=cur.get("last_modified"),
                                          accept="application/atom+xml, application/xml;q=0.9")
        if result.not_modified:
            await runner._save(cid, now=now, cadence_s=cadence, status="not_modified")
            return PollResult(cid, "not_modified")
        warnings = meteoalarm.parse_atom(result.body)
        cursor = dict(cur.get("cursor") or {})
        known: dict[str, list[int | None]] = dict(cursor.get("details") or {})
        floor = max(2, min(4, s.meteoalarm_min_level))
        # CAP documents only for warnings that can become signals (not fog / avalanche / cancelled)
        wanted = [w for w in warnings if w.message_type != "Cancel" and (w.level or 0) >= floor
                  and not (w.type_no is not None and meteoalarm.TYPES.get(w.type_no) is None)]
        stored = await stored_geometries(runner, cid, [w.native for w in wanted])
        details: dict[str, meteoalarm.CapDetail] = {
            ident: meteoalarm.CapDetail(level=v[0], type_no=v[1]) for ident, v in known.items()
            if isinstance(v, list) and len(v) == 2}
        fetched = 0
        for ident, url in sorted(meteoalarm.detail_urls(wanted).items()):
            if ident in known:
                continue
            if fetched >= s.meteoalarm_max_detail_fetches:
                break
            fetched += 1
            try:
                detail = meteoalarm.parse_cap_detail(
                    (await runner.fetcher.get(cid, url, accept="application/cap+xml, application/xml;q=0.9")).body)
            except (FetchError, ValueError) as exc:
                log.warning("meteoalarm CAP fetch failed", extra={"connector": cid, "error": str(exc)[:200]})
                continue
            details[ident] = detail
            known[ident] = [detail.level, detail.type_no]
        iso = runner.gaz.country(slug.replace("-", " "))
        drafts, skipped = meteoalarm.parse_meteoalarm(
            warnings, SourceContext(retrieved_at=now), connector=cid, country_iso=iso, min_level=floor,
            details=details, stored=stored)
        report = await runner.pipeline.ingest(drafts, now=now)
        withdrawn, gone = await withdraw_missing(runner, cid, {d.native_key for d in drafts}, now)
        report.merge(gone)
        live = {w.identifier for w in warnings}
        cursor["details"] = {k: v for k, v in sorted(known.items()) if k in live}
        if len(cursor["details"]) > MAX_DETAIL_CACHE:
            cursor["details"] = dict(sorted(cursor["details"].items())[-MAX_DETAIL_CACHE:])
        await runner._save(cid, now=now, cadence_s=cadence, status="ok", etag=result.etag,
                           last_modified=result.last_modified, cursor=cursor, items=len(drafts))
        return PollResult(cid, "ok", len(drafts), report, skipped=skipped, withdrawn=withdrawn)
    except (FetchError, ValueError, UnicodeDecodeError) as exc:
        return await _error(runner, cid, cadence, now, exc)


Poller = Callable[["FeedRunner", SourceSpec, dt.datetime], Awaitable[PollResult]]
POLLERS: Final[dict[str, Poller]] = {
    "nhc": poll_nhc,
    "copernicus_ems": poll_copernicus_ems,
    "ofac": poll_ofac,
    "gdelt": poll_gdelt,
    "meteoalarm": poll_meteoalarm,
}
