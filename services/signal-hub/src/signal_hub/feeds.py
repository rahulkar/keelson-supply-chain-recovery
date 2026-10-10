"""Feed polling: one ``poll`` per adapter run (scheduled by the poller, or ``--once``).

Persisted per source in ``source_cursor``: ETag / Last-Modified (conditional GET), an adapter
cursor (GDACS: last footprint episode per TC; RSS: newest item date), last status/error,
consecutive failures and the next poll time (cadence, exponential backoff on failure, max 1 h).

Sources come from the registry (:mod:`signal_hub.sources`); the M8 adapters (NHC, Copernicus EMS,
OFAC, GDELT, MeteoAlarm) poll in :mod:`signal_hub.feeds_more`.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import asyncpg

from signal_hub import geo, sources
from signal_hub.adapters.gdacs import CONNECTOR_ID as GDACS_ID
from signal_hub.adapters.gdacs import footprint_urls, latest_episodes, parse_gdacs
from signal_hub.adapters.rss import parse_feed
from signal_hub.adapters.usgs import CONNECTOR_ID as USGS_ID
from signal_hub.adapters.usgs import parse_usgs
from signal_hub.admin import load_schedule
from signal_hub.config import RssFeed, Settings
from signal_hub.db import tx
from signal_hub.gazetteer import Gazetteer
from signal_hub.httpfetch import Fetcher, FetchError, FetchResult
from signal_hub.logs import get_logger
from signal_hub.models import SourceContext
from signal_hub.pipeline import IngestReport, Pipeline

__all__ = ["FeedRunner", "PollResult", "gdacs_list_url"]

log = get_logger(__name__)
MAX_BACKOFF = dt.timedelta(hours=1)


@dataclass(slots=True)
class PollResult:
    connector_id: str
    status: str  # ok | not_modified | error
    items: int = 0
    report: IngestReport | None = None
    error: str | None = None
    skipped: int = 0
    """Feed items the adapter did not turn into signals (unmapped category, below the level …)."""
    withdrawn: int = 0
    """Current signals of a snapshot feed that left it (NHC storms, MeteoAlarm warnings): expired now."""

    def _counts(self, out: dict[str, Any]) -> dict[str, Any]:
        if self.skipped:
            out["skipped"] = self.skipped
        if self.withdrawn:
            out["withdrawn"] = self.withdrawn
        return out

    def summary(self) -> dict[str, Any]:
        out: dict[str, Any] = {"connector_id": self.connector_id, "status": self.status, "items": self.items}
        if self.error:
            out["error"] = self.error
        if self.report is not None:
            out.update(new=self.report.new, updated=self.report.updated, invalid=self.report.invalid,
                       alerts=len(self.report.alert_changes), queued=self.report.news_queued)
        return self._counts(out)

    def as_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"connector_id": self.connector_id, "status": self.status, "items": self.items}
        if self.error:
            out["error"] = self.error
        if self.report is not None:
            out["report"] = self.report.as_dict()
        return self._counts(out)


def gdacs_list_url(base: str, now: dt.datetime, window_days: int) -> str:
    """The configured event-list URL with ``fromdate``/``todate`` set to the polling window."""
    parts = urlsplit(base)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k.lower() not in ("fromdate", "todate")]
    params += [("fromdate", (now - dt.timedelta(days=window_days)).date().isoformat()), ("todate", now.date().isoformat())]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(params, safe=";"), ""))


def _json(result: FetchResult) -> dict[str, Any]:
    """A GeoJSON FeatureCollection-shaped object (``ValueError`` for anything else)."""
    if result.empty:
        return {"type": "FeatureCollection", "features": []}
    doc = json.loads(result.body.decode("utf-8-sig"))
    if not isinstance(doc, dict) or not isinstance(doc.get("features", []), list):
        raise ValueError("feed response is not a GeoJSON FeatureCollection object")
    return doc


class FeedRunner:
    def __init__(self, settings: Settings, pipeline: Pipeline, gaz: Gazetteer, fetcher: Fetcher) -> None:
        self.settings = settings
        self.pipeline = pipeline
        self.pool: asyncpg.Pool = pipeline.pool
        self.gaz = gaz
        self.fetcher = fetcher
        self.cadence_override: dict[str, int] = {}
        """Per-source poll cadence from the tenants' subscriptions (``110`` §5, :func:`refresh_schedule`)."""
        self.paused: set[str] = set()
        """Sources every registered tenant disabled (not polled by the loop)."""

    async def refresh_schedule(self) -> None:
        """Load the subscription-derived schedule (cadence overrides, paused sources)."""
        schedule = await load_schedule(self.pool, self.settings)
        self.cadence_override = {cid: s.cadence_s for cid, s in schedule.items()}
        self.paused = {cid for cid, s in schedule.items() if s.paused}

    def cadence(self, connector_id: str, default: int) -> int:
        return self.cadence_override.get(connector_id, default)

    # ───────────── cursor ─────────────
    async def cursor(self, connector_id: str) -> dict[str, Any]:
        async with tx(self.pool, None) as conn:
            row = await conn.fetchrow("SELECT * FROM source_cursor WHERE connector_id = $1", connector_id)
        return dict(row) if row is not None else {"connector_id": connector_id, "cursor": {}, "consecutive_failures": 0}

    async def _save(self, connector_id: str, *, now: dt.datetime, cadence_s: int, status: str,
                    etag: str | None = None, last_modified: str | None = None, cursor: dict[str, Any] | None = None,
                    error: str | None = None, items: int = 0) -> None:
        async with tx(self.pool, None) as conn:
            row = await conn.fetchrow("SELECT consecutive_failures FROM source_cursor WHERE connector_id = $1", connector_id)
            failures = (int(row["consecutive_failures"]) if row else 0) + 1 if status == "error" else 0
            delay = dt.timedelta(seconds=cadence_s)
            if failures:
                delay = min(MAX_BACKOFF, delay * (2 ** min(failures, 6)))
            await conn.execute(
                """
                INSERT INTO source_cursor (connector_id, etag, last_modified, cursor, last_polled_at, last_status,
                                           last_error, consecutive_failures, next_poll_at, items_total)
                VALUES ($1, $2, $3, coalesce($4, '{}'::jsonb), $5, $6, $7, $8, $9, $10)
                ON CONFLICT (connector_id) DO UPDATE SET
                  etag = CASE WHEN $6 = 'ok' THEN EXCLUDED.etag ELSE source_cursor.etag END,
                  last_modified = CASE WHEN $6 = 'ok' THEN EXCLUDED.last_modified ELSE source_cursor.last_modified END,
                  cursor = coalesce($4, source_cursor.cursor),
                  last_polled_at = EXCLUDED.last_polled_at, last_status = EXCLUDED.last_status,
                  last_error = EXCLUDED.last_error, consecutive_failures = EXCLUDED.consecutive_failures,
                  next_poll_at = EXCLUDED.next_poll_at, items_total = source_cursor.items_total + EXCLUDED.items_total
                """,
                connector_id, etag, last_modified, cursor, now, status, error, failures, now + delay, items,
            )

    async def due(self, connector_id: str, now: dt.datetime, cadence_s: int | None = None) -> bool:
        """``next_poll_at`` reached; after a healthy poll also ``last_polled_at + cadence_s`` (so a
        cadence lowered by a tenant applies without waiting for the previously scheduled poll)."""
        cur = await self.cursor(connector_id)
        nxt = cur.get("next_poll_at")
        if nxt is None or nxt <= now:
            return True
        last = cur.get("last_polled_at")
        if cadence_s is not None and last is not None and not cur.get("consecutive_failures"):
            return bool(last + dt.timedelta(seconds=cadence_s) <= now)
        return False

    # ───────────── adapters ─────────────
    def connectors(self, *, include_disabled_adapters: bool = False) -> list[tuple[str, str, int]]:
        """``(adapter, connector_id, cadence_s)`` of every enabled source (``SIGNAL_ADAPTERS``, which the
        loop and ``--once`` both honour; all configured sources with ``include_disabled_adapters``), in
        catalogue order."""
        enabled = set(sources.ADAPTERS if include_disabled_adapters else self.settings.enabled_adapters)
        return [(spec.adapter, spec.connector_id, self.cadence(spec.connector_id, spec.default_cadence_s))
                for spec in sources.catalogue(self.settings) if spec.adapter in enabled]

    async def poll(self, adapter: str, *, now: dt.datetime, connector_id: str | None = None) -> list[PollResult]:
        """Poll every configured source of ``adapter`` (or only ``connector_id``)."""
        if adapter not in sources.ADAPTERS:
            raise ValueError(f"unknown adapter {adapter!r}")
        results: list[PollResult] = []
        for spec in sources.catalogue(self.settings):
            if spec.adapter != adapter or connector_id not in (None, spec.connector_id):
                continue
            cadence = self.cadence(spec.connector_id, spec.default_cadence_s)
            results.append(await self._guarded(spec.connector_id, cadence, now, self._run(spec, now)))
        return results

    def _run(self, spec: sources.SourceSpec, now: dt.datetime) -> Awaitable[PollResult]:
        if spec.adapter == "gdacs":
            return self.poll_gdacs(now)
        if spec.adapter == "usgs":
            return self.poll_usgs(now)
        if spec.adapter == "rss":
            return self.poll_rss(next(f for f in self.settings.rss_feeds if f.connector_id == spec.connector_id), now)
        from signal_hub import feeds_more  # the M8 pollers (they use PollResult / FeedRunner from here)

        return feeds_more.POLLERS[spec.adapter](self, spec, now)

    async def _guarded(self, cid: str, cadence_s: int, now: dt.datetime, run: Awaitable[PollResult]) -> PollResult:
        """One source's poll; an unexpected error (malformed feed data the parser did not foresee,
        a store failure) is recorded as a failed poll with backoff instead of killing the poller."""
        try:
            return await run
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
            log.exception("poll failed unexpectedly", extra={"connector": cid})
            with contextlib.suppress(Exception):
                await self._save(cid, now=now, cadence_s=cadence_s, status="error", error=error)
            return PollResult(cid, "error", error=error[:300])

    async def _stored_footprint(self, connector_id: str, native_key: str, episode: Any) -> list[dict[str, Any]]:
        async with tx(self.pool, None) as conn:
            geometry = await conn.fetchval(
                """SELECT doc->'geometry' FROM signal WHERE tenant IS NULL AND connector_id = $1
                   AND native_key = $2 AND is_current""", connector_id, native_key)
        return [{"type": "Feature", "geometry": g, "properties": {"episodeid": episode}}
                for g in geo.flatten(geometry) if g.get("type") != "Point"]

    async def poll_gdacs(self, now: dt.datetime) -> PollResult:
        cid = GDACS_ID
        cadence = self.cadence(cid, self.settings.cadence_gdacs_s)
        cur = await self.cursor(cid)
        url = gdacs_list_url(self.settings.gdacs_url, now, self.settings.gdacs_window_days)
        try:
            # The window changes daily, so the ETag of yesterday's URL is not reused for today's.
            same_window = (cur.get("cursor") or {}).get("url") == url
            result = await self.fetcher.get(cid, url, etag=cur.get("etag") if same_window else None,
                                            last_modified=cur.get("last_modified") if same_window else None,
                                            accept="application/json")
            if result.not_modified:
                await self._save(cid, now=now, cadence_s=cadence, status="not_modified")
                return PollResult(cid, "not_modified")
            doc = _json(result)
            cursor = dict(cur.get("cursor") or {})
            episodes: dict[str, Any] = dict(cursor.get("episodes") or {})
            extra: dict[tuple[str, int], list[dict[str, Any]]] = {}
            if self.settings.gdacs_fetch_geometry:
                host = urlsplit(self.settings.gdacs_url).hostname or ""
                urls = footprint_urls(doc, allowed_host=host)
                latest = latest_episodes(doc)
                fetched = 0
                for key, gurl in sorted(urls.items()):
                    episode = latest.get(key)
                    tag = f"{key[0]}:{key[1]}"
                    if tag in episodes and episodes[tag] == episode:
                        # unchanged episode: reuse the stored footprint (no re-fetch, no degraded version)
                        stored = await self._stored_footprint(cid, tag, episode)
                        if stored:
                            extra[key] = stored
                        continue
                    if fetched >= self.settings.gdacs_max_geometry_fetches:
                        continue
                    try:
                        geo_doc = _json(await self.fetcher.get(cid, gurl, accept="application/json"))
                        extra[key] = list(geo_doc.get("features") or [])
                        episodes[tag] = episode
                        fetched += 1
                    except (FetchError, ValueError) as exc:
                        log.warning("gdacs footprint fetch failed", extra={"event": tag, "error": str(exc)})
            drafts = parse_gdacs(doc, SourceContext(retrieved_at=now), self.gaz, extra_footprints=extra)
            report = await self.pipeline.ingest(drafts, now=now)
            cursor.update({"url": url, "episodes": episodes})
            await self._save(cid, now=now, cadence_s=cadence, status="ok", etag=result.etag,
                             last_modified=result.last_modified, cursor=cursor, items=len(drafts))
            return PollResult(cid, "ok", len(drafts), report)
        except (FetchError, ValueError, UnicodeDecodeError) as exc:
            await self._save(cid, now=now, cadence_s=cadence, status="error", error=str(exc)[:500])
            log.warning("gdacs poll failed", extra={"error": str(exc)[:300]})
            return PollResult(cid, "error", error=str(exc)[:300])

    async def poll_usgs(self, now: dt.datetime) -> PollResult:
        cid = USGS_ID
        cadence = self.cadence(cid, self.settings.cadence_usgs_s)
        cur = await self.cursor(cid)
        try:
            result = await self.fetcher.get(cid, self.settings.usgs_url, etag=cur.get("etag"),
                                            last_modified=cur.get("last_modified"), accept="application/geo+json")
            if result.not_modified:
                await self._save(cid, now=now, cadence_s=cadence, status="not_modified")
                return PollResult(cid, "not_modified")
            drafts = parse_usgs(_json(result), SourceContext(retrieved_at=now), self.gaz)
            report = await self.pipeline.ingest(drafts, now=now)
            await self._save(cid, now=now, cadence_s=cadence, status="ok", etag=result.etag,
                             last_modified=result.last_modified, items=len(drafts))
            return PollResult(cid, "ok", len(drafts), report)
        except (FetchError, ValueError, UnicodeDecodeError) as exc:
            await self._save(cid, now=now, cadence_s=cadence, status="error", error=str(exc)[:500])
            log.warning("usgs poll failed", extra={"error": str(exc)[:300]})
            return PollResult(cid, "error", error=str(exc)[:300])

    async def poll_rss(self, feed: RssFeed, now: dt.datetime) -> PollResult:
        cid = feed.connector_id
        cadence = self.cadence(cid, self.settings.cadence_rss_s)
        cur = await self.cursor(cid)
        try:
            result = await self.fetcher.get(cid, feed.url, etag=cur.get("etag"), last_modified=cur.get("last_modified"),
                                            accept="application/rss+xml, application/atom+xml, application/xml;q=0.9")
            if result.not_modified:
                await self._save(cid, now=now, cadence_s=cadence, status="not_modified")
                return PollResult(cid, "not_modified")
            items = parse_feed(result.body, SourceContext(retrieved_at=now), connector_id=cid,
                               source_name=feed.name, max_items=self.settings.rss_max_items)
            new_ids = await self.pipeline.queue_news(items, now=now)
            report = IngestReport(news_queued=len(new_ids))
            if new_ids and self.pipeline.llm is not None:
                report.merge(await self.pipeline.process_news(new_ids, tenant=None, now=now))
            await self._save(cid, now=now, cadence_s=cadence, status="ok", etag=result.etag,
                             last_modified=result.last_modified, items=len(items))
            return PollResult(cid, "ok", len(items), report)
        except (FetchError, ValueError) as exc:
            await self._save(cid, now=now, cadence_s=cadence, status="error", error=str(exc)[:500])
            log.warning("rss poll failed", extra={"connector": cid, "error": str(exc)[:300]})
            return PollResult(cid, "error", error=str(exc)[:300])
