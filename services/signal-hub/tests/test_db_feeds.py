"""Feed runner + poller against PostGIS with a mock HTTP transport: conditional requests
(ETag / If-Modified-Since → 304), persisted cursors, failure backoff, GDACS footprint fetch (once per
episode, configured host only), RSS queueing, per-source rate limit, and the poller's locks."""

from __future__ import annotations

import asyncio
import dataclasses
import datetime as dt
import json
import logging
import time

import asyncpg
import httpx
import pytest

from conftest import T0, load_fixture
from signal_hub import poller
from signal_hub.config import RssFeed, Settings
from signal_hub.db import tx
from signal_hub.feeds import FeedRunner
from signal_hub.gazetteer import Gazetteer
from signal_hub.httpfetch import Fetcher, FetchError
from signal_hub.pipeline import Pipeline

pytestmark = [pytest.mark.postgis, pytest.mark.usefixtures("clean")]


class Upstream:
    """Mock public feeds with ETag / Last-Modified support."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.fail: set[str] = set()

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        if host in self.fail:
            return httpx.Response(503)
        if host == "www.gdacs.org" and path.endswith("/geteventlist/SEARCH"):
            if request.headers.get("if-none-match") == '"gd-1"':
                return httpx.Response(304)
            return httpx.Response(200, content=load_fixture("gdacs_eventlist.json"), headers={"ETag": '"gd-1"'})
        if host == "www.gdacs.org" and path.endswith("/getgeometry"):
            return httpx.Response(200, content=load_fixture("gdacs_tc_geometry.json"))
        if host == "earthquake.usgs.gov":
            if request.headers.get("if-modified-since") == "Wed, 07 Oct 2026 00:00:00 GMT":
                return httpx.Response(304)
            return httpx.Response(200, content=load_fixture("usgs_4.5_day.geojson"),
                                  headers={"Last-Modified": "Wed, 07 Oct 2026 00:00:00 GMT"})
        if host == "news.example.com":
            return httpx.Response(200, content=load_fixture("loadstar_feed.xml"))
        return httpx.Response(404)

    def paths(self) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests]


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


def _runner(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream, **over: object) -> FeedRunner:
    settings = Settings(rss_feeds=(RssFeed("rss.fixturestar", "Fixture Star", "https://news.example.com/feed/"),),
                        min_interval_s=0, **over)  # type: ignore[arg-type]
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler))
    return FeedRunner(settings, Pipeline(pool, gaz), gaz, Fetcher(client, min_interval_s=0))


async def test_gdacs_poll_conditional_and_footprints(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    runner = _runner(pool, gaz, upstream)
    (r,) = await runner.poll("gdacs", now=T0)
    assert r.status == "ok" and r.items == 3 and r.report is not None and r.report.new == 3
    assert upstream.paths() == ["SEARCH", "getgeometry"]  # one TC footprint fetched
    list_req = upstream.requests[0]
    assert list_req.url.params["fromdate"] == "2026-09-30" and list_req.url.params["todate"] == "2026-10-07"
    assert list_req.url.params["eventlist"] == "TC;EQ;FL"
    cur = await runner.cursor("gdacs")
    assert cur["etag"] == '"gd-1"' and cur["cursor"]["episodes"] == {"TC:1001102": 14}
    assert cur["next_poll_at"] == T0 + dt.timedelta(seconds=600) and cur["last_status"] == "ok"
    assert not await runner.due("gdacs", T0 + dt.timedelta(seconds=599))
    assert await runner.due("gdacs", T0 + dt.timedelta(seconds=600))
    async with tx(pool, None) as conn:
        geom_type = await conn.fetchval("SELECT doc->'geometry'->>'type' FROM signal WHERE native_key = 'TC:1001102'")
    assert geom_type == "GeometryCollection"
    # same window: conditional request → 304, no re-ingest, no footprint re-fetch
    (r,) = await runner.poll("gdacs", now=T0 + dt.timedelta(minutes=10))
    assert r.status == "not_modified" and upstream.paths()[-1] == "SEARCH"
    assert upstream.requests[-1].headers["if-none-match"] == '"gd-1"'
    # a new day (new window URL): no stale ETag; same episode → footprint not fetched again
    (r,) = await runner.poll("gdacs", now=T0 + dt.timedelta(days=1))
    assert r.status == "ok" and "if-none-match" not in upstream.requests[-1].headers
    assert upstream.paths().count("getgeometry") == 1
    assert r.report is not None and r.report.unchanged == 3


async def test_footprints_only_from_the_configured_host(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    runner = _runner(pool, gaz, upstream, gdacs_url="https://gdacs-mirror.example/gdacsapi/api/events/geteventlist/SEARCH")
    await runner.poll("gdacs", now=T0)
    assert "getgeometry" not in upstream.paths()


async def test_usgs_if_modified_since(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    runner = _runner(pool, gaz, upstream)
    (r,) = await runner.poll("usgs", now=T0)
    assert r.status == "ok" and r.items == 2
    (r,) = await runner.poll("usgs", now=T0 + dt.timedelta(minutes=5))
    assert r.status == "not_modified"
    assert upstream.requests[-1].headers["if-modified-since"] == "Wed, 07 Oct 2026 00:00:00 GMT"


async def test_failures_back_off_and_keep_validators(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    runner = _runner(pool, gaz, upstream)
    await runner.poll("usgs", now=T0)
    upstream.fail.add("earthquake.usgs.gov")
    (r,) = await runner.poll("usgs", now=T0 + dt.timedelta(minutes=5))
    assert r.status == "error" and "503" in (r.error or "")
    cur = await runner.cursor("usgs")
    assert cur["consecutive_failures"] == 1 and cur["last_modified"] == "Wed, 07 Oct 2026 00:00:00 GMT"
    assert cur["next_poll_at"] == T0 + dt.timedelta(minutes=5) + dt.timedelta(seconds=600)  # 300 s x 2
    await runner.poll("usgs", now=T0 + dt.timedelta(minutes=20))
    cur = await runner.cursor("usgs")
    assert cur["consecutive_failures"] == 2 and cur["next_poll_at"] == T0 + dt.timedelta(minutes=20, seconds=1200)
    for i in range(6):
        await runner.poll("usgs", now=T0 + dt.timedelta(hours=1 + i))
    cur = await runner.cursor("usgs")
    assert cur["next_poll_at"] - cur["last_polled_at"] == dt.timedelta(hours=1)  # capped
    upstream.fail.clear()
    (r,) = await runner.poll("usgs", now=T0 + dt.timedelta(hours=9))
    assert r.status == "not_modified" and (await runner.cursor("usgs"))["consecutive_failures"] == 0


async def test_rss_poll_queues_items_without_llm(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    runner = _runner(pool, gaz, upstream)
    (r,) = await runner.poll("rss", now=T0)
    assert r.status == "ok" and r.items == 3 and r.report is not None and r.report.news_queued == 3
    (r,) = await runner.poll("rss", now=T0 + dt.timedelta(minutes=15))
    assert r.report is not None and r.report.news_queued == 0  # dedupe by guid
    async with tx(pool, None) as conn:
        statuses = [x["status"] for x in await conn.fetch("SELECT status FROM news_item")]
    assert statuses == ["pending_extraction"] * 3  # the poller never calls an LLM; the API extracts


async def test_fetcher_rate_limit_and_size_cap() -> None:
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda _r: httpx.Response(200, content=b"x" * 2000)))
    fetcher = Fetcher(client, min_interval_s=0.3, max_bytes=1000)
    with pytest.raises(FetchError):
        await fetcher.get("a", "https://a.example/")
    fetcher.max_bytes = 10_000
    start = time.monotonic()
    await fetcher.get("b", "https://b.example/")
    await fetcher.get("b", "https://b.example/")
    await fetcher.get("c", "https://c.example/")  # other source: not throttled by b
    assert 0.3 <= time.monotonic() - start < 0.6


async def test_poller_once_and_locks(pool: asyncpg.Pool, gaz: Gazetteer, db_dsn: str,
                                     caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(database_url=db_dsn, geo_dir=gaz.geo_dir, enabled_adapters=())
    assert await poller.run_once(settings, "sweep") == 0
    assert await poller.run_once(settings, "extraction") == 0  # no key: nothing pending is touched
    # a concurrent run of the same adapter is skipped
    async with pool.acquire() as conn:
        assert await poller._adapter_lock(conn, "sweep")
        with caplog.at_level(logging.INFO, logger="signal_hub.poller"):
            assert await poller.run_once(settings, "sweep") == 0
        assert any("another run" in r.getMessage() for r in caplog.records)
        await poller._adapter_unlock(conn, "sweep")
    # leader election: while another session holds the leader lock, the loop only waits
    caplog.clear()
    async with pool.acquire() as holder:
        assert await holder.fetchval("SELECT pg_try_advisory_lock($1)", poller.LEADER_LOCK)
        stop = asyncio.Event()
        with caplog.at_level(logging.INFO, logger="signal_hub.poller"):
            task = asyncio.create_task(poller.run_loop(settings, stop))
            await asyncio.sleep(1.0)
            stop.set()
            await asyncio.wait_for(task, 20)
        await holder.execute("SELECT pg_advisory_unlock($1)", poller.LEADER_LOCK)
    messages = [r.getMessage() for r in caplog.records]
    assert "not the leader; waiting" in messages and "poller leader" not in messages
    caplog.clear()
    stop = asyncio.Event()
    with caplog.at_level(logging.INFO, logger="signal_hub.poller"):
        task = asyncio.create_task(poller.run_loop(dataclasses.replace(settings), stop))
        await asyncio.sleep(1.0)
        stop.set()
        await asyncio.wait_for(task, 20)
    assert "poller leader" in [r.getMessage() for r in caplog.records]


# ───────────────────────────── review regressions ─────────────────────────────


class StringIdUpstream(Upstream):
    """GDACS event list whose ``eventid``/``episodeid`` are numeric strings (JSON numbers are not
    guaranteed by every GDACS endpoint/mirror)."""

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "www.gdacs.org" and request.url.path.endswith("/geteventlist/SEARCH"):
            self.requests.append(request)
            doc = json.loads(load_fixture("gdacs_eventlist.json"))
            for f in doc["features"]:
                p = f["properties"]
                p["eventid"] = str(p["eventid"])
                if p.get("episodeid") is not None:
                    p["episodeid"] = str(p["episodeid"])
            return httpx.Response(200, content=json.dumps(doc).encode())
        return super().handler(request)


async def test_gdacs_footprint_fetched_for_string_ids(pool: asyncpg.Pool, gaz: Gazetteer) -> None:
    upstream = StringIdUpstream()
    runner = _runner(pool, gaz, upstream)
    (r,) = await runner.poll("gdacs", now=T0)
    assert r.status == "ok" and r.items == 3
    assert upstream.paths() == ["SEARCH", "getgeometry"]  # the footprint is not skipped as "unchanged"
    assert (await runner.cursor("gdacs"))["cursor"]["episodes"] == {"TC:1001102": 14}
    async with tx(pool, None) as conn:
        geom_type = await conn.fetchval("SELECT doc->'geometry'->>'type' FROM signal WHERE native_key = 'TC:1001102'")
    assert geom_type == "GeometryCollection"
    (r,) = await runner.poll("gdacs", now=T0 + dt.timedelta(days=1))
    assert upstream.paths().count("getgeometry") == 1 and r.report is not None and r.report.unchanged == 3


class BrokenUpstream(Upstream):
    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "www.gdacs.org":
            return httpx.Response(200, content=b'[{"type": "Feature"}]')  # JSON, but not a FeatureCollection
        if request.url.host == "earthquake.usgs.gov":
            return httpx.Response(200, content=b'{"type": "FeatureCollection", "features": [{"id": "x", "properties": [1]}]}')
        return httpx.Response(404)


async def test_malformed_feeds_are_failed_polls_not_crashes(pool: asyncpg.Pool, gaz: Gazetteer,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    runner = _runner(pool, gaz, BrokenUpstream())
    (r,) = await runner.poll("gdacs", now=T0)
    assert r.status == "error" and "FeatureCollection" in (r.error or "")
    assert (await runner.cursor("gdacs"))["consecutive_failures"] == 1
    (r,) = await runner.poll("usgs", now=T0)
    assert r.status == "ok" and r.items == 0
    # anything unforeseen inside an adapter is recorded as a failed poll (with backoff), never raised
    from signal_hub import feeds

    def boom(*_a: object, **_k: object) -> list[object]:
        raise RuntimeError("parser bug")

    monkeypatch.setattr(feeds, "parse_usgs", boom)
    (r,) = await runner.poll("usgs", now=T0 + dt.timedelta(hours=1))
    assert r.status == "error" and "parser bug" in (r.error or "")
    cur = await runner.cursor("usgs")
    assert cur["last_status"] == "error" and cur["consecutive_failures"] == 1
    assert cur["next_poll_at"] == T0 + dt.timedelta(hours=1, seconds=600)
