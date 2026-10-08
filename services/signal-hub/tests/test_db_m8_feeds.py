"""M8 feeds against PostGIS with a mock HTTP transport (``docs/contracts/124-m8-signal-feeds.md``):
NHC (conditional GET, GIS once per advisory, withdrawal resolves the alert), NHC / Copernicus EMS
anchors joining GDACS events, OFAC (baseline, delta, organisation-name exposure, RLS), GDELT
(MD5, aggregation, corroboration without alerting alone), MeteoAlarm (bounded CAP fetches,
withdrawal), opt-in subscriptions and the feeds-test pack replay."""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
import io
import json
import logging
import re
import zipfile
from typing import Any

import asyncpg
import httpx
import pytest

from conftest import FIXTURES, T0, demo_network, load_fixture, make_token, running_app
from fakes import Clock, FakeLlm
from keelson_contracts.signals import AlertRule, TenantNetwork
from signal_hub import admin, poller
from signal_hub.adapters import rss
from signal_hub.adapters.gdacs import parse_gdacs
from signal_hub.app import build_state, create_app
from signal_hub.config import Settings
from signal_hub.db import tx
from signal_hub.feeds import FeedRunner
from signal_hub.gazetteer import Gazetteer
from signal_hub.httpfetch import Fetcher
from signal_hub.models import SignalDraft, SourceContext
from signal_hub.pipeline import Pipeline

pytestmark = [pytest.mark.postgis, pytest.mark.usefixtures("clean")]
TENANT = "demo-retail"
OTHER = "other-co"
H = dt.timedelta(hours=1)
S3 = "wc2h-sls-prod-public-published.s3.us-gov-west-1.amazonaws.com"
SLOT = "20261007101500"
ALL_ADAPTERS = ("gdacs", "usgs", "rss", "nhc", "copernicus_ems", "ofac", "gdelt", "meteoalarm")
CAP_ORANGE_WIND = "00000000-0000-4000-8000-000000000002"
CAP_RED_COASTAL = "00000000-0000-4000-8000-000000000003"
RED_COASTAL_CAP = b"""<?xml version="1.0" encoding="utf-8"?>
<alert xmlns="urn:oasis:names:tc:emergency:cap:1.2">
  <identifier>2.49.0.0.528.0.NL.20261007100000.test-red-coastal_1</identifier>
  <info><language>en-GB</language><event>Very high water</event>
    <parameter><valueName>awareness_level</valueName><value>4; red; Extreme</value></parameter>
    <parameter><valueName>awareness_type</valueName><value>7; coastalevent</value></parameter>
    <area><areaDesc>Waddenzee</areaDesc><geocode><valueName>EMMA_ID</valueName><value>NL001</value></geocode></area>
  </info>
</alert>"""


def zipped(name: str, data: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return buf.getvalue()


class Upstream:
    """Mock public feeds of the five M8 sources (validators, redirects, GIS / CAP follow-ups)."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.nhc_doc: Any = json.loads(load_fixture("nhc_current_storms.json"))
        self.nhc_etag = '"nhc-1"'
        self.ems: Any = json.loads(load_fixture("copernicus_ems_activations.json"))
        self.ofac_pub = 1
        self.ofac_override: dict[str, bytes] = {}
        self.gdelt_slot = SLOT
        self.gdelt_export = zipped(f"{SLOT}.export.CSV", load_fixture("gdelt_export.CSV"))
        self.gdelt_md5: str | None = None
        self.gdelt_etag = '"lu-1"'
        self.meteo = load_fixture("meteoalarm_netherlands.atom")
        self.meteo_etag = '"ma-1"'

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        host, path = request.url.host, request.url.path
        inm = request.headers.get("if-none-match")
        if host == "www.nhc.noaa.gov":
            if path == "/CurrentStorms.json":
                if inm == self.nhc_etag:
                    return httpx.Response(304)
                return httpx.Response(200, content=json.dumps(self.nhc_doc).encode(), headers={"ETag": self.nhc_etag})
            if "EP192026" in path:  # Petrel's GIS products are not there (yet)
                return httpx.Response(404)
            if path.endswith("_CONE.kmz"):
                return httpx.Response(200, content=zipped("cone.kml", load_fixture("nhc_al_cone.kml")))
            if path.endswith("_TRACK.kmz"):
                return httpx.Response(200, content=zipped("track.kml", load_fixture("nhc_al_track.kml")))
        if host == "rapidmapping.emergency.copernicus.eu":
            return httpx.Response(200, content=json.dumps(self.ems).encode())
        if host == "sanctionslistservice.ofac.treas.gov":  # the export URL redirects to a presigned S3 object
            name = path.rsplit("/", 1)[-1]
            return httpx.Response(302, headers={"Location": f"https://{S3}/Published/{self.ofac_pub}/{name}?X-Amz-Expires=3600"})
        if host == S3:
            name = path.rsplit("/", 1)[-1].split(".")[0]
            etag = f'"{name}-{self.ofac_pub}"'
            if inm == etag:
                return httpx.Response(304)
            body = self.ofac_override.get(name) or (FIXTURES / "ofac" / f"{name}_{min(self.ofac_pub, 2)}.CSV").read_bytes()
            return httpx.Response(200, content=body, headers={
                "ETag": etag, "Last-Modified": f"Mon, 0{4 + self.ofac_pub} Oct 2026 19:35:12 GMT"})
        if host == "data.gdeltproject.org":
            if path == "/gdeltv2/lastupdate.txt":
                if inm == self.gdelt_etag:
                    return httpx.Response(304)
                md5 = self.gdelt_md5 or hashlib.md5(self.gdelt_export).hexdigest()
                body = (f"{len(self.gdelt_export)} {md5} http://data.gdeltproject.org/gdeltv2/{self.gdelt_slot}.export.CSV.zip\n"
                        f"1 {'0' * 32} http://data.gdeltproject.org/gdeltv2/{self.gdelt_slot}.mentions.CSV.zip\n")
                return httpx.Response(200, content=body.encode(), headers={"ETag": self.gdelt_etag})
            if path == f"/gdeltv2/{self.gdelt_slot}.export.CSV.zip":
                return httpx.Response(200, content=self.gdelt_export)
            return httpx.Response(404)
        if host == "feeds.meteoalarm.org":
            if path == "/feeds/meteoalarm-legacy-atom-netherlands":
                if inm == self.meteo_etag:
                    return httpx.Response(304)
                return httpx.Response(200, content=self.meteo, headers={"ETag": self.meteo_etag})
            if path.endswith(CAP_RED_COASTAL):
                return httpx.Response(200, content=RED_COASTAL_CAP)  # an area without a polygon
            if path.startswith("/api/v1/warnings/feeds-netherlands/"):
                return httpx.Response(200, content=load_fixture("meteoalarm_cap_detail.xml"))
        return httpx.Response(404)

    def paths(self, host: str) -> list[str]:
        return [r.url.path.rsplit("/", 1)[-1] for r in self.requests if r.url.host == host]


@pytest.fixture
def upstream() -> Upstream:
    return Upstream()


def _settings(**over: Any) -> Settings:
    base: dict[str, Any] = {"rss_feeds": (), "min_interval_s": 0, "meteoalarm_countries": ("netherlands",),
                            "enabled_adapters": ALL_ADAPTERS}
    base.update(over)
    return Settings(**base)


def _runner(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream, **over: Any) -> FeedRunner:
    client = httpx.AsyncClient(transport=httpx.MockTransport(upstream.handler), follow_redirects=True)
    return FeedRunner(_settings(**over), Pipeline(pool, gaz), gaz, Fetcher(client, min_interval_s=0))


async def _network(pool: asyncpg.Pool, gaz: Gazetteer, tenant: str, extra: list[dict[str, Any]]) -> None:
    net = demo_network()
    net["nodes"] = [*net["nodes"], *extra]
    await Pipeline(pool, gaz).put_network(tenant, TenantNetwork.model_validate(net), T0)


async def _current(pool: asyncpg.Pool, connector: str) -> dict[str, asyncpg.Record]:
    async with tx(pool, None) as conn:
        rows = await conn.fetch("SELECT * FROM signal WHERE tenant IS NULL AND connector_id = $1 AND is_current", connector)
    return {r["native_key"]: r for r in rows}


async def _alerts(pool: asyncpg.Pool, tenant: str) -> list[asyncpg.Record]:
    async with tx(pool, tenant) as conn:
        return list(await conn.fetch("SELECT * FROM alert WHERE tenant = $1 ORDER BY first_raised_at, id", tenant))


# ───────────────────────────── NHC ─────────────────────────────


async def test_nhc_gis_once_per_advisory_conditional_get_and_withdrawal(pool: asyncpg.Pool, gaz: Gazetteer,
                                                                         upstream: Upstream) -> None:
    await _network(pool, gaz, TENANT, [{"type": "port", "code": "PORT-T1", "name": "Port in the cone", "lat": 27.0, "lon": -74.0}])
    runner = _runner(pool, gaz, upstream)
    (r,) = await runner.poll("nhc", now=T0)
    assert r.status == "ok" and r.items == 3 and r.report is not None and r.report.new == 3 and r.skipped == 0
    # Tern's cone + track once; Petrel's products 404 (tried again next poll); Fifteen's links are off-host / http
    assert upstream.paths("www.nhc.noaa.gov") == ["CurrentStorms.json", "AL142026_012adv_CONE.kmz",
                                                  "AL142026_012adv_TRACK.kmz", "EP192026_004adv_CONE.kmz",
                                                  "EP192026_004adv_TRACK.kmz"]
    assert not any(req.url.host == "gis.example.net" for req in upstream.requests)
    cur = await runner.cursor("nhc")
    assert cur["cursor"]["advisories"] == {"al142026": "012"} and cur["etag"] == '"nhc-1"'
    signals = await _current(pool, "nhc")
    assert signals["al142026"]["doc"]["geometry"]["type"] == "GeometryCollection"
    assert signals["ep192026"]["doc"]["geometry"]["type"] == "Point"  # no GIS: the position
    assert signals["al152026"]["doc"]["geometry"]["type"] == "Point"
    (alert,) = await _alerts(pool, TENANT)
    assert alert["severity"] == "critical" and alert["action"] == "alert_and_draft_case" and alert["status"] == "new"
    # same ETag: 304, nothing re-fetched
    (r,) = await runner.poll("nhc", now=T0 + 0.25 * H)
    assert r.status == "not_modified" and upstream.paths("www.nhc.noaa.gov").count("AL142026_012adv_CONE.kmz") == 1
    # a changed feed with the same Tern advisory: its stored footprint is reused (no new version)
    upstream.nhc_etag = '"nhc-2"'
    (r,) = await runner.poll("nhc", now=T0 + 0.5 * H)
    assert r.status == "ok" and r.report is not None and r.report.unchanged == 3 and r.withdrawn == 0
    assert upstream.paths("www.nhc.noaa.gov").count("AL142026_012adv_CONE.kmz") == 1
    assert upstream.paths("www.nhc.noaa.gov").count("EP192026_004adv_CONE.kmz") == 2  # not recorded: retried
    # Tern leaves the feed: withdrawn (a version that expires now), its event and alert resolve
    upstream.nhc_doc["activeStorms"] = [s for s in upstream.nhc_doc["activeStorms"] if s.get("id") != "al142026"]
    upstream.nhc_etag = '"nhc-3"'
    (r,) = await runner.poll("nhc", now=T0 + H)
    assert r.status == "ok" and r.withdrawn == 1 and r.summary()["withdrawn"] == 1
    tern = (await _current(pool, "nhc"))["al142026"]
    assert tern["version"] == 2 and tern["expires_at"] == T0 + H
    async with tx(pool, None) as conn:
        assert await conn.fetchval("SELECT status FROM event WHERE id = $1", tern["event_id"]) == "resolved"
    (alert,) = await _alerts(pool, TENANT)
    assert alert["status"] == "resolved"
    assert (await runner.cursor("nhc"))["cursor"]["advisories"] == {}
    # a malformed feed is a failed poll that withdraws nothing
    upstream.nhc_doc, upstream.nhc_etag = {"error": "maintenance"}, '"nhc-4"'
    (r,) = await runner.poll("nhc", now=T0 + 2 * H)
    assert r.status == "error" and "activeStorms" in (r.error or "")
    assert (await _current(pool, "nhc"))["ep192026"]["version"] == 1


async def test_nhc_and_ems_anchors_join_gdacs_events(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    footprints = {("TC", 1001102): json.loads(load_fixture("gdacs_tc_geometry.json"))["features"]}
    gd = parse_gdacs(json.loads(load_fixture("gdacs_eventlist.json")), SourceContext(retrieved_at=T0), gaz,
                     extra_footprints=footprints)
    await Pipeline(pool, gaz).ingest(gd, now=T0)
    storm = dict(upstream.nhc_doc["activeStorms"][0])
    storm.update(id="cp012026", name="Kaimana", latitudeNumeric=11.2, longitudeNumeric=110.0, intensity="80",
                 publicAdvisory={"advNum": "010", "issuance": "2026-10-06T21:00:00.000Z",
                                 "url": "https://www.nhc.noaa.gov/text/MIATCPCP1.shtml"})
    upstream.nhc_doc = {"activeStorms": [storm]}
    upstream.ems = {"count": 1, "next": None, "previous": None, "results": [{
        "code": "EMSR996", "countries": ["Bangladesh"], "eventTime": "2026-10-03T00:00:00",
        "name": "Flood in Dhaka Division, Bangladesh", "centroid": "POINT (90.6 23.9)",
        "activationTime": "2026-10-03T09:00:00", "category": "Flood", "lastUpdate": "2026-10-05T10:00:00",
        "closed": False, "gdacsId": "FL1102800", "n_aois": 2, "n_products": 3}]}
    runner = _runner(pool, gaz, upstream, nhc_fetch_gis=False)
    assert (await runner.poll("nhc", now=T0))[0].status == "ok"
    assert (await runner.poll("copernicus_ems", now=T0))[0].status == "ok"
    async with tx(pool, None) as conn:
        rows = {f"{r['connector_id']}:{r['native_key']}": str(r["event_id"]) for r in await conn.fetch(
            "SELECT connector_id, native_key, event_id FROM signal WHERE is_current")}
        tc = await conn.fetchrow("SELECT * FROM event WHERE id = $1", rows["gdacs:TC:1001102"])
    # same subtype, within 100 km, genesis 39 h apart (NHC dates from its advisory count): one event
    assert rows["nhc:cp012026"] == rows["gdacs:TC:1001102"]
    assert rows["copernicus_ems:EMSR996"] == rows["gdacs:FL:1102800"]  # a flood 2 days after GDACS's start
    assert tc["status"] == "confirmed" and tc["subtype"] == "tropical_cyclone"
    assert tc["severity_bp"] == 6500  # max over the anchors (NHC 80 kt = H1 6500 > GDACS orange 6155)


async def test_ems_history_is_not_ingested_but_closures_are(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    runner = _runner(pool, gaz, upstream)
    (r,) = await runner.poll("copernicus_ems", now=T0)
    # the closed wildfire (expired before the poll) is skipped as history; 3 open activations stored
    assert r.status == "ok" and r.items == 3 and r.skipped == 3
    assert set(await _current(pool, "copernicus_ems")) == {"EMSR995", "EMSR994", "EMSR991"}
    upstream.ems["results"][0].update(closed=True, lastUpdate="2026-10-06T23:00:00")
    (r,) = await runner.poll("copernicus_ems", now=T0 + H)
    flood = (await _current(pool, "copernicus_ems"))["EMSR995"]
    assert r.items == 3 and flood["version"] == 2 and flood["expires_at"] == dt.datetime(2026, 10, 6, 23, tzinfo=dt.UTC)


# ───────────────────────────── OFAC ─────────────────────────────


async def test_ofac_baseline_delta_name_match_and_isolation(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    await _network(pool, gaz, TENANT, [
        {"type": "supplier", "code": "SUP-SHELL", "name": "Example Shell Fabrics Co.", "lat": 29.87, "lon": 121.55},
        {"type": "port", "code": "CNNGB", "name": "Example Shell Fabrics Co.", "lat": 29.87, "lon": 121.55}])
    await _network(pool, gaz, OTHER, [
        {"type": "supplier", "code": "SUP-OTHER", "name": "Other Textiles Ltd", "lat": 29.87, "lon": 121.55}])
    async with tx(pool, TENANT) as conn:
        keys = {r["code"]: r["name_key"] for r in await conn.fetch("SELECT code, name_key FROM network_node WHERE tenant = $1", TENANT)}
    assert keys["SUP-SHELL"] == keys["CNNGB"] == "example shell fabrics" and keys["SUP-VN-01"] == "supplier vn 1"
    runner = _runner(pool, gaz, upstream)
    # publication 1: the baseline (snapshot only, no signal)
    (r,) = await runner.poll("ofac", now=T0)
    assert r.status == "ok" and r.items == 0
    async with tx(pool, None) as conn:
        assert await conn.fetchval("SELECT count(*) FROM source_snapshot WHERE connector_id = 'ofac'") == 4
    assert await _current(pool, "ofac") == {} and (await runner.cursor("ofac"))["cursor"]["baseline_at"]
    assert [p for p in upstream.paths(S3)] == ["SDN.CSV", "ALT.CSV", "ADD.CSV"]
    # unchanged: three conditional requests answered 304 (through the redirect)
    (r,) = await runner.poll("ofac", now=T0 + H)
    assert r.status == "not_modified"
    assert [req.headers.get("if-none-match") for req in upstream.requests if req.url.host == S3][-3:] == [
        '"SDN-1"', '"ALT-1"', '"ADD-1"']
    # publication 2: one signal; only the tenant whose supplier carries a new name is exposed
    upstream.ofac_pub = 2
    (r,) = await runner.poll("ofac", now=T0 + 2 * H)
    assert r.status == "ok" and r.items == 1 and r.report is not None and r.report.new == 1
    (sig,) = (await _current(pool, "ofac")).values()
    assert sig["native_key"] == "sdn:20261006T193512Z" and sig["kind"] == "regulatory"
    assert sig["subtype"] == "sanctions_designation" and sig["severity_bp"] == 8000 and sig["geog"] is None
    assert list(sig["location_refs"]) == [] and "example shell fabrics" in sig["match_keys"]
    async with tx(pool, TENANT) as conn:
        exp = {(e["node_type"], e["node_code"]): e["method"] for e in await conn.fetch(
            "SELECT * FROM exposure WHERE tenant = $1", TENANT)}
    assert exp == {("supplier", "SUP-SHELL"): "entity_match"}  # suppliers/factories only, never the port
    (alert,) = await _alerts(pool, TENANT)
    assert alert["action"] == "alert_and_draft_case" and alert["severity"] == "high"
    assert await _alerts(pool, OTHER) == []
    async with tx(pool, OTHER) as conn:
        assert await conn.fetchval("SELECT count(*) FROM exposure") == 0  # RLS: never another tenant's rows
        assert await conn.fetchval("SELECT count(*) FROM source_snapshot") == 0  # system context only
        assert await conn.fetchval("SELECT count(*) FROM signal WHERE connector_id = 'ofac'") == 1  # the global row
    async with tx(pool, None) as conn:
        snap = {r["item_key"] for r in await conn.fetch("SELECT item_key FROM source_snapshot WHERE connector_id = 'ofac'")}
        assert await conn.fetchval("SELECT count(*) FROM exposure") == 0  # nothing tenant-scoped in the system view
    assert snap == {"9001", "9002", "9005", "9007", "9008"}  # 9006 removed
    # a truncated publication is refused and the snapshot kept
    upstream.ofac_pub = 3
    upstream.ofac_override = {"SDN": b'9001,"EXAMPLE TRADING HOUSE LLC",-0- ,"IRAN",-0-\r\n'}
    (r,) = await runner.poll("ofac", now=T0 + 3 * H)
    assert r.status == "error" and "truncated" in (r.error or "")
    async with tx(pool, None) as conn:
        assert await conn.fetchval("SELECT count(*) FROM source_snapshot") == 5


# ───────────────────────────── GDELT ─────────────────────────────


def _news_closure(gaz: Gazetteer) -> SignalDraft:
    at = T0 + 8 * H
    return SignalDraft(
        tenant=None, connector_id="rss.fixture", native_key="https://news.example/ctg-closed", origin="news",
        kind="logistics", subtype="port_closure", title="Chittagong port suspends operations", summary=None,
        raw_text="Chittagong port suspends operations", geometry=gaz.ports["BDCGP"].geometry,
        location_refs=["UNLOCODE:BDCGP", "ISO3166:BD"], starts_at=at, ends_at=None, observed_at=at,
        expires_at=at + 72 * H, severity_bp=6500, severity_native={"scheme": "extraction_prior", "subtype": "port_closure"},
        provenance="inference", extracted_by="extract-fast", source_name="Fixture Daily", source_url=None,
        license=rss.LICENSE, authority_bp=7000, retrieved_at=at, native_id="https://news.example/ctg-closed")


async def test_gdelt_corroborates_without_alerting_alone(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    await _network(pool, gaz, TENANT, [{"type": "port", "code": "NLRTM", "name": "Rotterdam", "lat": 51.9496, "lon": 4.1453}])
    settings = _settings()
    await admin.put_subscription(pool, settings, TENANT, "gdelt", admin.SubscriptionUpdate(enabled=True), "admin@demo")
    pipe = Pipeline(pool, gaz)
    report = await pipe.ingest([_news_closure(gaz)], now=T0 + 8 * H)
    (event_id,) = report.events
    async with tx(pool, None) as conn:
        before = await conn.fetchrow("SELECT * FROM event WHERE id = $1", event_id)
    (alert,) = await _alerts(pool, TENANT)
    runner = _runner(pool, gaz, upstream, gdelt_event_codes=("143", "144", "191"))
    (r,) = await runner.poll("gdelt", now=T0 + 12 * H)
    assert r.status == "ok" and r.items == 3
    assert upstream.paths("data.gdeltproject.org") == ["lastupdate.txt", f"{SLOT}.export.CSV.zip"]
    signals = await _current(pool, "gdelt")
    assert set(signals) == {"20261007:strike:UNLOCODE:BDCGP", "20261007:strike:UNLOCODE:NLRTM",
                            "20261007:blockade:CHOKEPOINT:BAB_EL_MANDEB"}
    ctg = signals["20261007:strike:UNLOCODE:BDCGP"]
    assert str(ctg["event_id"]) == event_id and ctg["provenance"] == "inference" and ctg["severity_bp"] == 3900
    async with tx(pool, None) as conn:
        after = await conn.fetchrow("SELECT * FROM event WHERE id = $1", event_id)
        rtm_event = await conn.fetchrow("SELECT * FROM event WHERE id = $1",
                                        signals["20261007:strike:UNLOCODE:NLRTM"]["event_id"])
    assert after["confidence_bp"] > before["confidence_bp"] and after["status"] == "confirmed"  # 2 sources now
    assert after["severity_bp"] == 6500  # the news prior still leads
    # the Rotterdam strike is GDELT-only (3900 bp): exposed (entity match) but below every default rule
    assert rtm_event["severity_bp"] == 3900
    async with tx(pool, TENANT) as conn:
        assert await conn.fetchval("SELECT count(*) FROM exposure WHERE event_id = $1", rtm_event["id"]) == 1
    assert [a["id"] for a in await _alerts(pool, TENANT)] == [alert["id"]]
    cur = await runner.cursor("gdelt")
    assert cur["cursor"]["slot"] == SLOT and cur["cursor"]["agg"]["20261007:strike:UNLOCODE:BDCGP"]["articles"] == 17
    # unchanged lastupdate: 304; a new ETag for the same slot: nothing to fetch
    (r,) = await runner.poll("gdelt", now=T0 + 12.25 * H)
    assert r.status == "not_modified"
    upstream.gdelt_etag = '"lu-2"'
    (r,) = await runner.poll("gdelt", now=T0 + 12.5 * H)
    assert r.status == "ok" and r.items == 0 and upstream.paths("data.gdeltproject.org").count(f"{SLOT}.export.CSV.zip") == 1
    # an MD5 mismatch fails the poll (backoff) and leaves the cursor alone
    upstream.gdelt_slot, upstream.gdelt_etag, upstream.gdelt_md5 = "20261007103000", '"lu-3"', "f" * 32
    (r,) = await runner.poll("gdelt", now=T0 + 13 * H)
    assert r.status == "error" and "MD5" in (r.error or "")
    cur = await runner.cursor("gdelt")
    assert cur["consecutive_failures"] == 1 and cur["cursor"]["slot"] == SLOT


# ───────────────────────────── MeteoAlarm ─────────────────────────────


async def test_meteoalarm_bounded_cap_fetches_and_withdrawal(pool: asyncpg.Pool, gaz: Gazetteer, upstream: Upstream) -> None:
    runner = _runner(pool, gaz, upstream, meteoalarm_max_detail_fetches=1)
    now = T0 + 12 * H
    (r,) = await runner.poll("meteoalarm", now=now, connector_id="meteoalarm.netherlands")
    assert r.status == "ok" and r.items == 4 and r.skipped == 3
    details = [p for p in upstream.paths("feeds.meteoalarm.org") if p != "meteoalarm-legacy-atom-netherlands"]
    assert details == [CAP_ORANGE_WIND]  # one CAP document per poll (the cap), fog and the cancel never fetched
    signals = await _current(pool, "meteoalarm.netherlands")
    by_area = {k.split("#")[1]: v for k, v in signals.items()}
    assert set(by_area) == {"NL812", "NL811", "NL001", "NL808"}
    assert by_area["NL812"]["doc"]["geometry"]["type"] == "Polygon" and by_area["NL811"]["doc"]["geometry"]["type"] == "Polygon"
    assert by_area["NL001"]["doc"]["geometry"] is None and list(by_area["NL001"]["location_refs"]) == ["ISO3166:NL"]
    # next poll (changed feed): the second CAP document; the known one and the http link are not fetched
    upstream.meteo_etag = '"ma-2"'
    (r,) = await runner.poll("meteoalarm", now=now + 0.25 * H)
    details = [p for p in upstream.paths("feeds.meteoalarm.org") if p != "meteoalarm-legacy-atom-netherlands"]
    assert details == [CAP_ORANGE_WIND, CAP_RED_COASTAL]
    assert r.report is not None and r.report.unchanged == 4  # stored polygons reused, no churn
    cursor = (await runner.cursor("meteoalarm.netherlands"))["cursor"]
    assert set(cursor["details"]) == {"2.49.0.0.528.0.NL.20261007091503.test-orange-wind_3",
                                      "2.49.0.0.528.0.NL.20261007100000.test-red-coastal_1"}
    # the red coastal warning leaves the feed: withdrawn
    upstream.meteo = re.sub(rb"<entry>(?:(?!</entry>).)*?test-red-coastal_1.*?</entry>", b"", upstream.meteo, flags=re.S)
    upstream.meteo_etag = '"ma-3"'
    (r,) = await runner.poll("meteoalarm", now=now + 0.5 * H)
    assert r.withdrawn == 1 and r.items == 3
    coastal = next(v for k, v in (await _current(pool, "meteoalarm.netherlands")).items() if k.endswith("#NL001"))
    assert coastal["version"] == 2 and coastal["expires_at"] == now + 0.5 * H


# ───────────────────────────── opt-in subscriptions ─────────────────────────────


async def test_opt_in_sources_pause_until_a_tenant_subscribes(pool: asyncpg.Pool, gaz: Gazetteer, db_dsn: str,
                                                              caplog: pytest.LogCaptureFixture) -> None:
    settings = _settings()
    await _network(pool, gaz, TENANT, [])
    await _network(pool, gaz, OTHER, [])
    schedule = await admin.load_schedule(pool, settings)
    assert schedule["gdelt"].paused and schedule["meteoalarm.netherlands"].paused and not schedule["nhc"].paused
    await admin.recompute_schedule(pool, settings)
    schedule = await admin.load_schedule(pool, settings)
    assert schedule["gdelt"].paused and schedule["gdelt"].enabled_tenants == 0 and schedule["gdelt"].disabled_tenants == 2
    rows = {s["connector_id"]: s for s in await admin.list_sources(pool, settings, TENANT)}
    assert rows["gdelt"]["enabled"] is False and rows["gdelt"]["opt_in"] is True and rows["gdelt"]["paused"] is True
    assert rows["nhc"]["enabled"] is True and rows["nhc"]["opt_in"] is False and rows["nhc"]["attribution"]
    assert rows["meteoalarm.netherlands"]["polled"] is True and rows["meteoalarm.netherlands"]["licence_class"] == "terms_apply"
    # --once skips a paused source (exit 0, nothing fetched)
    with caplog.at_level(logging.INFO, logger="signal_hub.poller"):
        assert await poller.run_once(dataclasses.replace(settings, database_url=db_dsn, geo_dir=gaz.geo_dir), "gdelt") == 0
    assert any("paused" in r.getMessage() for r in caplog.records)
    # a GDELT event at the tenant's port, with a rule low enough to alert on it
    low = AlertRule.model_validate({"id": "low", "when": {"min_severity_bp": 3000, "kinds": [], "min_confidence_bp": 0,
                                                          "min_exposed_nodes": 1, "node_types": []},
                                    "action": "alert", "hysteresis_bp": 1000})
    async with tx(pool, TENANT) as conn:
        await admin.save_rules(conn, TENANT, [low], "admin@demo")
    agg: dict[str, dict[str, Any]] = {}
    from signal_hub.adapters import gdelt

    keys = gdelt.aggregate(agg, gdelt.matching_rows(load_fixture("gdelt_export.CSV"), gdelt.DEFAULT_FILTER, gaz))
    drafts = [d for d in gdelt.drafts_for(agg, keys, SourceContext(retrieved_at=T0 + 12 * H), gaz)
              if d.native_key == "20261007:strike:UNLOCODE:BDCGP"]
    pipe = Pipeline(pool, gaz)
    await pipe.ingest(drafts, now=T0 + 12 * H)
    assert await _alerts(pool, TENANT) == []  # not subscribed: an opt-in source does not count
    item = await admin.put_subscription(pool, settings, TENANT, "gdelt", admin.SubscriptionUpdate(enabled=True), "admin@demo")
    assert item["enabled"] is True and item["paused"] is False and item["effective_cadence_s"] == 900
    await pipe.refresh_tenant(TENANT, T0 + 12 * H)
    (alert,) = await _alerts(pool, TENANT)
    assert alert["status"] == "new"
    # the engine's whole-map sync without gdelt puts it back to not subscribed; the alert resolves
    out = await admin.put_subscription_set(pool, settings, TENANT, admin.SubscriptionSet.model_validate(
        {"sources": [{"connector_id": "nhc", "enabled": True}]}), "engine")
    assert {i["connector_id"]: i["enabled"] for i in out["items"]}["gdelt"] is False
    await pipe.refresh_tenant(TENANT, T0 + 12 * H)
    assert [a["status"] for a in await _alerts(pool, TENANT)] == ["resolved"]
    assert (await admin.load_schedule(pool, settings))["gdelt"].paused


async def test_tenant_sources_api_reports_the_new_fields(pool: asyncpg.Pool, settings: Settings) -> None:
    s = dataclasses.replace(settings, meteoalarm_countries=("netherlands",), enabled_adapters=ALL_ADAPTERS)
    internal = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    app = create_app(s, state=build_state(s, pool, llm=None, clock=Clock(T0), internal_client=internal), background=False)
    async with running_app(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as c:
        auth = {"Authorization": f"Bearer {make_token(TENANT)}"}
        items = {i["connector_id"]: i for i in (await c.get(f"/v1/tenants/{TENANT}/sources", headers=auth)).json()["items"]}
        nhc = items["nhc"]
        assert nhc["license"] == "Public domain (NOAA/NWS)" and nhc["kind"] == "hazard" and nhc["provenance"] == "fact"
        assert nhc["licence_class"] == "public_domain" and nhc["terms_url"] == "https://www.weather.gov/disclaimer"
        assert nhc["opt_in"] is False and nhc["enabled"] is True and nhc["coverage"] and nhc["host"] == "www.nhc.noaa.gov"
        assert items["gdelt"]["opt_in"] is True and items["gdelt"]["enabled"] is False and items["gdelt"]["paused"] is True
        assert items["ofac"]["kind"] == "regulatory" and "not a sanctions-screening service" in items["ofac"]["description"]
        r = await c.put(f"/v1/tenants/{TENANT}/sources/gdelt", json={"enabled": True}, headers=auth)
        assert r.status_code == 200 and r.json()["source"]["paused"] is False
        assert r.json()["source"]["effective_cadence_s"] == 900
        r = await c.put(f"/v1/tenants/{TENANT}/sources/ofac", json={"enabled": True, "cadence_s": 600}, headers=auth)
        assert r.status_code == 422  # below OFAC's 30-minute floor
        hub_rows = {x["connector_id"]: x for x in (await c.get("/v1/sources", headers=auth)).json()["sources"]}
        assert hub_rows["meteoalarm.netherlands"]["opt_in"] is True and hub_rows["copernicus_ems"]["attribution"]
    await internal.aclose()


# ───────────────────────────── replay ─────────────────────────────


async def test_feeds_test_pack_replays_deterministically(pool: asyncpg.Pool, settings: Settings) -> None:
    holder: dict[str, Any] = {}

    async def deferred(scope: Any, receive: Any, send: Any) -> None:  # /demo-feeds of this same app
        await holder["app"](scope, receive, send)

    s = dataclasses.replace(settings, scenarios_dir=FIXTURES / "scenarios-m8")
    internal = httpx.AsyncClient(transport=httpx.ASGITransport(app=deferred))
    clock = Clock(T0)
    holder["app"] = app = create_app(s, state=build_state(s, pool, llm=FakeLlm(unavailable=402), clock=clock,
                                                          internal_client=internal), background=False)

    async def play(c: httpx.AsyncClient, auth: dict[str, str]) -> list[tuple[Any, ...]]:
        assert (await c.post("/v1/demo/packs/feeds-test/start", headers=auth)).status_code == 200
        for _ in range(5):
            state = (await c.post("/v1/demo/packs/feeds-test/advance", json={}, headers=auth)).json()
            if state["status"] == "finished":
                break
        assert state["status"] == "finished"
        async with tx(pool, TENANT) as conn:
            return [tuple(r) for r in await conn.fetch(
                """SELECT id, connector_id, native_key, version, event_id, content_hash, provenance, origin
                   FROM signal WHERE tenant = $1 ORDER BY id""", TENANT)]

    async with running_app(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub") as c:
        auth = {"Authorization": f"Bearer {make_token(TENANT)}"}
        await c.put(f"/v1/tenants/{TENANT}/network", json=demo_network(), headers=auth)
        first = await play(c, auth)
        connectors = {row[1] for row in first}
        assert connectors == {"demo.nhc", "demo.copernicus_ems", "demo.meteoalarm.netherlands", "demo.gdelt"}
        assert {row[6] for row in first} == {"simulated"} and {row[7] for row in first} == {"demo"}
        assert (await c.post("/v1/demo/packs/feeds-test/reset", headers=auth)).json()["status"] == "idle"
        clock.advance(hours=3)  # the wall clock moved; the replay must not care
        assert await play(c, auth) == first
        r = await c.get("/demo-feeds/feeds-test/nhc-1-cone.kml")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/vnd.google-earth.kml+xml")
        assert (await c.get("/demo-feeds/feeds-test/gdelt-1.CSV")).headers["content-type"].startswith("text/csv")
    await internal.aclose()
