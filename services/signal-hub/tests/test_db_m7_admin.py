"""M7 Signals admin against PostGIS (``110`` §5): source subscriptions (alerts + shared schedule),
watch regions in exposure, alert-rule CRUD with validation, tenant scoping; and the provisioned
LiteLLM extraction key end to end (bootstrap → encrypted row → API startup → 402 keeps news pending)."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import pytest

from conftest import T0, demo_network, load_fixture, make_token, running_app
from fakes import Clock, MockLiteLlm
from keelson_contracts.signals import TenantNetwork
from signal_hub import admin
from signal_hub.adapters.gdacs import parse_gdacs
from signal_hub.app import build_state, create_app, refresh_llm_key
from signal_hub.bootstrap import provision_llm_key
from signal_hub.config import RssFeed, Settings
from signal_hub.db import tx
from signal_hub.feeds import FeedRunner
from signal_hub.httpfetch import Fetcher
from signal_hub.llm_key import SECRET_NAME, load_stored_key, resolve_llm_key
from signal_hub.models import NewsItemDraft, SourceContext
from signal_hub.pipeline import Pipeline
from signal_hub.quarantine import LiteLlmClient

pytestmark = [pytest.mark.postgis, pytest.mark.usefixtures("clean")]
TENANT = "demo-retail"
OTHER = "other-co"
BASE = "http://signal-hub-api:8300"
H = dt.timedelta(hours=1)
BKK = {"type": "port", "code": "THBKK", "name": "Bangkok", "lat": 13.70, "lon": 100.58}
GULF = {"type": "Polygon", "coordinates": [[[99, 9], [109, 9], [109, 14.5], [99, 14.5], [99, 9]]]}


def _tc() -> list[Any]:
    """The GDACS TC fixture with a cone off southern Vietnam (lon 108.6–112.5, lat 9.6–12.6)."""
    doc = json.loads(load_fixture("gdacs_eventlist.json"))
    doc["features"] = [doc["features"][0]]
    doc["features"][0]["properties"].update(
        episodeid=14, alertlevel="Orange", iscurrent="true", datemodified="2026-10-06T06:30:00",
        severitydata={"severity": 150.0, "severitytext": "wind 150 km/h", "severityunit": "km/h"})
    ring = [[108.6, 9.6], [112.5, 9.6], [112.5, 12.6], [108.6, 12.6], [108.6, 9.6]]
    doc["features"].append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                            "properties": {"eventtype": "TC", "eventid": 1001102, "episodeid": 14, "Class": "Poly_Cones"}})
    return parse_gdacs(doc, SourceContext(retrieved_at=T0), _GAZ["gaz"])


_GAZ: dict[str, Any] = {}


class Hub:
    def __init__(self, app: Any, state: Any) -> None:
        self.app, self.state = app, state

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)

    @staticmethod
    def auth(tenant: str = TENANT, sub: str | None = "admin@demo") -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token(tenant, sub=sub)}"}


@pytest.fixture
async def hub(pool: asyncpg.Pool, settings: Settings, gaz: Any) -> AsyncIterator[Hub]:
    _GAZ["gaz"] = gaz
    s = dataclasses.replace(settings, rss_feeds=(RssFeed("rss.fixture", "Fixture Daily", "https://news.example.com/feed"),))
    internal = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    state = build_state(s, pool, llm=None, clock=Clock(T0 + H), internal_client=internal)
    app = create_app(s, state=state, background=False)
    async with running_app(app):
        yield Hub(app, state)
    await internal.aclose()


async def _alerts(pool: asyncpg.Pool, tenant: str) -> list[asyncpg.Record]:
    async with tx(pool, tenant) as conn:
        return list(await conn.fetch("SELECT * FROM alert WHERE tenant = $1 ORDER BY id", tenant))


async def _exposures(pool: asyncpg.Pool, tenant: str) -> dict[tuple[str, str], asyncpg.Record]:
    async with tx(pool, tenant) as conn:
        return {(r["node_type"], r["node_code"]): r
                for r in await conn.fetch("SELECT * FROM exposure WHERE tenant = $1", tenant)}


# ─────────────────────────── sources ───────────────────────────

async def test_source_subscription_controls_alerts(hub: Hub, pool: asyncpg.Pool) -> None:
    pipe: Pipeline = hub.state.pipeline
    await pipe.put_network(TENANT, TenantNetwork.model_validate(demo_network()), T0)
    await pipe.put_network(OTHER, TenantNetwork.model_validate(demo_network()), T0)
    await pipe.ingest(_tc(), now=T0)
    assert [a["status"] for a in await _alerts(pool, TENANT)] == ["new"]
    async with hub.client() as c:
        r = await c.get(f"/v1/tenants/{TENANT}/sources", headers=hub.auth())
        assert r.status_code == 200
        items = {i["connector_id"]: i for i in r.json()["items"]}
        assert list(items) == ["gdacs", "usgs", "rss.fixture"]
        assert items["gdacs"]["enabled"] is True and items["gdacs"]["license"] == "CC BY 4.0"
        assert items["gdacs"]["effective_cadence_s"] == 600 and items["gdacs"]["paused"] is False
        # disable GDACS: the TC alert resolves for this tenant only
        r = await c.put(f"/v1/tenants/{TENANT}/sources/gdacs", json={"enabled": False}, headers=hub.auth())
        assert r.status_code == 200 and r.json()["alerts_changed"] == 1
        assert r.json()["source"]["enabled"] is False and r.json()["source"]["updated_by"] == "admin@demo"
        assert [a["status"] for a in await _alerts(pool, TENANT)] == ["resolved"]
        assert await _exposures(pool, TENANT) == {}
        assert [a["status"] for a in await _alerts(pool, OTHER)] == ["new"]
        # a new advisory while disabled raises nothing
        await pipe.ingest(_tc(), now=T0 + 2 * H)
        assert [a["status"] for a in await _alerts(pool, TENANT)] == ["resolved"]
        # re-enable: raised again
        r = await c.put(f"/v1/tenants/{TENANT}/sources/gdacs", json={"enabled": True}, headers=hub.auth())
        assert r.json()["alerts_changed"] == 1
        assert [a["status"] for a in await _alerts(pool, TENANT)] == ["new"]
        async with tx(pool, TENANT) as conn:
            feed = [x["change"] for x in await conn.fetch("SELECT change FROM alert_change WHERE tenant = $1 ORDER BY seq", TENANT)]
        assert feed == ["raised", "resolved", "raised"]
        # validation + scoping
        r = await c.put(f"/v1/tenants/{TENANT}/sources/gdacs", json={"enabled": True, "cadence_s": 120}, headers=hub.auth())
        assert r.status_code == 422 and "between 300 and 86400" in r.json()["detail"]["message"]
        assert (await c.put(f"/v1/tenants/{TENANT}/sources/nope", json={"enabled": True}, headers=hub.auth())).status_code == 404
        assert (await c.put(f"/v1/tenants/{TENANT}/sources/gdacs", json={"enabled": 1}, headers=hub.auth())).status_code == 422
        assert (await c.put(f"/v1/tenants/{OTHER}/sources/gdacs", json={"enabled": False}, headers=hub.auth())).status_code == 403
        assert (await c.get(f"/v1/tenants/{TENANT}/sources")).status_code == 401


async def test_tenant_owned_signals_ignore_subscriptions(hub: Hub, pool: asyncpg.Pool) -> None:
    """Disabling the global feeds never hides the tenant's own connector events."""
    pipe: Pipeline = hub.state.pipeline
    await pipe.put_network(TENANT, TenantNetwork.model_validate(demo_network()), T0)
    async with hub.client() as c:
        for cid in ("gdacs", "usgs", "rss.fixture"):
            assert (await c.put(f"/v1/tenants/{TENANT}/sources/{cid}", json={"enabled": False}, headers=hub.auth())).status_code == 200
        r = await c.post("/v1/ingest/connector-event", headers=hub.auth(), json={
            "type": "sailing.updated", "id": "evt-1", "timestamp": "2026-10-07T01:00:00Z",
            "data": {"code": "S-1", "lane_code": "SGN-HAM-SEA", "eta_before": "2026-10-10T00:00:00Z",
                     "eta": "2026-10-19T00:00:00Z"}})
        assert r.status_code == 202
    async with tx(pool, TENANT) as conn:
        own = await conn.fetchval("SELECT count(*) FROM signal WHERE tenant = $1", TENANT)
        ev = await conn.fetchval("SELECT event_id FROM signal WHERE tenant = $1 LIMIT 1", TENANT)
        assert own >= 1
        assert await admin.event_subscribed(conn, TENANT, str(ev))


async def test_shared_schedule_from_subscriptions(hub: Hub, pool: asyncpg.Pool) -> None:
    pipe: Pipeline = hub.state.pipeline
    settings = hub.state.settings
    for t in (TENANT, OTHER):
        await pipe.ensure_tenant(t)
    async with hub.client() as c:
        async def put(tenant: str, cid: str, body: dict[str, Any]) -> httpx.Response:
            return await c.put(f"/v1/tenants/{tenant}/sources/{cid}", json=body, headers=hub.auth(tenant))

        assert (await put(TENANT, "gdacs", {"enabled": True, "cadence_s": 1800})).status_code == 200
        sched = await admin.load_schedule(pool, settings)
        assert sched["gdacs"].cadence_s == 600  # OTHER still wants the default
        r = await put(OTHER, "gdacs", {"enabled": True, "cadence_s": 300})
        assert r.json()["source"]["effective_cadence_s"] == 300
        assert r.json()["source"]["cadence_s"] == 300
        # TENANT does not see OTHER's choice, only the shared cadence
        mine = {i["connector_id"]: i for i in (await c.get(f"/v1/tenants/{TENANT}/sources", headers=hub.auth())).json()["items"]}
        assert mine["gdacs"]["cadence_s"] == 1800 and mine["gdacs"]["effective_cadence_s"] == 300
        # a source every tenant disabled is paused; one tenant re-enabling un-pauses it
        await put(TENANT, "usgs", {"enabled": False})
        assert not (await admin.load_schedule(pool, settings))["usgs"].paused
        await put(OTHER, "usgs", {"enabled": False})
        assert (await admin.load_schedule(pool, settings))["usgs"].paused
        await pipe.ensure_tenant("third-co")  # a newly registered tenant is subscribed by default
        assert not (await admin.load_schedule(pool, settings))["usgs"].paused
        async with tx(pool, None) as conn:
            await conn.execute("DELETE FROM tenant_registry WHERE tenant = 'third-co'")
    # the poller applies it: paused sources skipped, lowered cadence due early
    runner = FeedRunner(settings, pipe, hub.state.gazetteer, Fetcher(httpx.AsyncClient(), min_interval_s=0))
    await runner.refresh_schedule()
    assert "usgs" in runner.paused and dict((c, s) for _a, c, s in runner.connectors())["gdacs"] == 300
    async with tx(pool, None) as conn:
        await conn.execute(
            """INSERT INTO source_cursor (connector_id, last_polled_at, last_status, next_poll_at)
               VALUES ('gdacs', $1, 'ok', $2)""", T0, T0 + dt.timedelta(seconds=600))
    assert not await runner.due("gdacs", T0 + dt.timedelta(seconds=299), 300)
    assert await runner.due("gdacs", T0 + dt.timedelta(seconds=300), 300)
    assert not await runner.due("gdacs", T0 + dt.timedelta(seconds=300))  # no cadence → next_poll_at only


# ─────────────────────────── watch regions ───────────────────────────

async def test_watch_regions_extend_exposure(hub: Hub, pool: asyncpg.Pool) -> None:
    pipe: Pipeline = hub.state.pipeline
    net = {"nodes": [BKK, {"type": "dc", "code": "DE1", "name": "DC Germany", "lat": 53.4, "lon": 10.2}], "lanes": []}
    await pipe.put_network(TENANT, TenantNetwork.model_validate(net), T0)
    await pipe.put_network(OTHER, TenantNetwork.model_validate(net), T0)
    (ev,) = (await pipe.ingest(_tc(), now=T0)).events
    assert await _alerts(pool, TENANT) == []  # Bangkok is ~850 km from the cone (TC radius 300 km)
    async with hub.client() as c:
        r = await c.post(f"/v1/tenants/{TENANT}/watch-regions", headers=hub.auth(), json={
            "id": "bkk-1000", "name": "Bangkok 1000 km", "kind": "radius", "node_type": "port", "node_code": "THBKK",
            "radius_km": 1000})
        assert r.status_code == 201, r.text
        assert r.json()["alerts_changed"] == 1 and r.json()["region"]["node_found"] is True
        exp = await _exposures(pool, TENANT)
        assert exp[("port", "THBKK")]["method"] == "dwithin_km" and 700 < exp[("port", "THBKK")]["distance_km"] < 1000
        assert 2000 <= exp[("port", "THBKK")]["weight_bp"] < 5000
        (alert,) = await _alerts(pool, TENANT)
        assert alert["status"] == "new" and str(alert["event_id"]) == ev
        assert await _alerts(pool, OTHER) == []  # regions are per tenant
        # disabling the region drops the exposure; like any exposure change, the open alert is kept
        # and its facts are refreshed quietly (alerts resolve with their event, never on their own)
        r = await c.put(f"/v1/tenants/{TENANT}/watch-regions/bkk-1000", headers=hub.auth(), json={
            "name": "Bangkok 1000 km", "kind": "radius", "node_type": "port", "node_code": "THBKK", "radius_km": 1000,
            "enabled": False})
        assert r.status_code == 200 and r.json()["created"] is False and r.json()["region"]["enabled"] is False
        assert await _exposures(pool, TENANT) == {}
        (alert,) = await _alerts(pool, TENANT)
        assert alert["status"] == "new" and alert["exposure_summary"]["nodes_by_type"]["port"] == 0
        # polygon over the Gulf of Thailand touching the cone: nodes inside are exposed
        r = await c.put(f"/v1/tenants/{TENANT}/watch-regions/gulf", headers=hub.auth(), json={
            "name": "Gulf of Thailand", "kind": "polygon", "geometry": GULF})
        assert r.status_code == 201 and r.json()["created"] is True and 400_000 < r.json()["region"]["area_km2"] < 900_000
        exp = await _exposures(pool, TENANT)
        assert exp[("port", "THBKK")]["method"] == "dwithin_km" and exp[("port", "THBKK")]["weight_bp"] == 5000
        (alert,) = await _alerts(pool, TENANT)
        assert alert["exposure_summary"]["nodes_by_type"]["port"] == 1
        # node_types filter: only DCs → Bangkok port no longer exposed
        r = await c.put(f"/v1/tenants/{TENANT}/watch-regions/gulf", headers=hub.auth(), json={
            "name": "Gulf of Thailand", "kind": "polygon", "geometry": GULF, "node_types": ["dc"]})
        assert r.status_code == 200 and await _exposures(pool, TENANT) == {}
        # list / get / delete, scoped to the tenant
        listed = (await c.get(f"/v1/tenants/{TENANT}/watch-regions", headers=hub.auth())).json()
        assert [x["id"] for x in listed["items"]] == ["bkk-1000", "gulf"] and listed["limits"]["max_regions"] == 100
        assert (await c.get(f"/v1/tenants/{OTHER}/watch-regions", headers=hub.auth(OTHER))).json()["items"] == []
        assert (await c.get(f"/v1/tenants/{TENANT}/watch-regions/gulf", headers=hub.auth(OTHER))).status_code == 403
        got = (await c.get(f"/v1/tenants/{TENANT}/watch-regions/gulf", headers=hub.auth())).json()
        assert got["geometry"] == GULF and got["node_types"] == ["dc"] and got["updated_by"] == "admin@demo"
        assert (await c.delete(f"/v1/tenants/{TENANT}/watch-regions/gulf", headers=hub.auth())).status_code == 200
        assert (await c.delete(f"/v1/tenants/{TENANT}/watch-regions/gulf", headers=hub.auth())).status_code == 404
        assert (await c.get(f"/v1/tenants/{TENANT}/watch-regions/gulf", headers=hub.auth())).status_code == 404
    async with tx(pool, OTHER) as conn:  # RLS: the other tenant's context cannot see the rows at all
        assert await conn.fetchval("SELECT count(*) FROM watch_region") == 0


async def test_watch_region_validation(hub: Hub, pool: asyncpg.Pool) -> None:
    await hub.state.pipeline.put_network(TENANT, TenantNetwork.model_validate(demo_network()), T0)
    async with hub.client() as c:
        async def post(body: dict[str, Any]) -> httpx.Response:
            return await c.post(f"/v1/tenants/{TENANT}/watch-regions", headers=hub.auth(), json=body)

        bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]}
        r = await post({"id": "bow", "name": "n", "kind": "polygon", "geometry": bowtie})
        assert r.status_code == 422 and "invalid polygon" in r.json()["detail"]["problems"][0]["problem"]
        huge = {"type": "Polygon", "coordinates": [[[-170, -60], [170, -60], [170, 70], [-170, 70], [-170, -60]]]}
        r = await post({"id": "world", "name": "n", "kind": "polygon", "geometry": huge})
        assert r.status_code == 422 and "exceeds" in r.json()["detail"]["problems"][0]["problem"]
        r = await post({"id": "ghost", "name": "n", "kind": "radius", "node_type": "port", "node_code": "XXNOP", "radius_km": 50})
        assert r.status_code == 422 and "not in the tenant network" in r.json()["detail"]["problems"][0]["problem"]
        r = await post({"id": "lane", "name": "Suez lane", "kind": "radius", "node_type": "lane", "node_code": "SGN-HAM-SEA",
                        "radius_km": 100})
        assert r.status_code == 201
        assert (await post({"id": "lane", "name": "dup", "kind": "radius", "node_type": "port", "node_code": "VNSGN",
                            "radius_km": 10})).status_code == 409
        assert (await post({"id": "x", "name": "n", "kind": "radius"})).status_code == 422
        assert (await c.put(f"/v1/tenants/{TENANT}/watch-regions/BAD", headers=hub.auth(), json={
            "name": "n", "kind": "radius", "node_type": "port", "node_code": "VNSGN", "radius_km": 10})).status_code == 422
        async with tx(pool, TENANT) as conn:
            await conn.executemany(
                """INSERT INTO watch_region (tenant, id, name, kind, node_type, node_code, radius_km)
                   VALUES ($1, $2, 'n', 'radius', 'port', 'VNSGN', 10)""",
                [(TENANT, f"r{i}") for i in range(admin.MAX_WATCH_REGIONS - 1)])
        r = await post({"id": "one-too-many", "name": "n", "kind": "radius", "node_type": "port", "node_code": "VNSGN",
                        "radius_km": 10})
        assert r.status_code == 422 and "at most" in r.json()["detail"]


# ─────────────────────────── alert rules ───────────────────────────

def _rule(rid: str, **when: Any) -> dict[str, Any]:
    base = {"min_severity_bp": 5000, "kinds": [], "min_confidence_bp": 0, "min_exposed_nodes": 1, "node_types": []}
    return {"id": rid, "when": {**base, **when}, "action": "alert", "hysteresis_bp": 1000}


async def test_alert_rule_crud(hub: Hub, pool: asyncpg.Pool) -> None:
    pipe: Pipeline = hub.state.pipeline
    await pipe.put_network(TENANT, TenantNetwork.model_validate(demo_network()), T0)
    await pipe.ingest(_tc(), now=T0)
    url = f"/v1/tenants/{TENANT}/alert-rules"
    async with hub.client() as c:
        got = (await c.get(url, headers=hub.auth())).json()
        assert got["custom"] is False and len(got["defaults"]) == 2 and got["updated_by"] is None
        # create on top of the defaults
        r = await c.post(url, headers=hub.auth(), json=_rule("quiet", min_severity_bp=9500))
        assert r.status_code == 201 and r.json()["rules"] == 3
        got = (await c.get(url, headers=hub.auth())).json()
        assert got["custom"] is True and [x["id"] for x in got["rules"]] == ["default-alert", "default-draft-case", "quiet"]
        assert got["updated_by"] == "admin@demo"
        assert (await c.post(url, headers=hub.auth(), json=_rule("quiet"))).status_code == 409
        assert (await c.get(f"{url}/quiet", headers=hub.auth())).json()["when"]["min_severity_bp"] == 9500
        assert (await c.get(f"{url}/nope", headers=hub.auth())).status_code == 404
        # replace one rule: raising the plain alert threshold above the TC severity keeps the alert quiet
        r = await c.put(f"{url}/default-alert", headers=hub.auth(), json=_rule("strict-alert", min_severity_bp=9000))
        assert r.status_code == 200
        ids = [x["id"] for x in (await c.get(url, headers=hub.auth())).json()["rules"]]
        assert ids == ["strict-alert", "default-draft-case", "quiet"]
        assert (await c.put(f"{url}/strict-alert", headers=hub.auth(), json=_rule("quiet"))).status_code == 409
        assert (await c.put(f"{url}/gone", headers=hub.auth(), json=_rule("gone"))).status_code == 404
        # semantic validation → 422 with problems, nothing stored
        bad = {**_rule("draft-all"), "action": "alert_and_draft_case"}
        bad["when"]["min_exposed_nodes"] = 0
        r = await c.post(url, headers=hub.auth(), json=bad)
        assert r.status_code == 422 and r.json()["detail"]["problems"][0]["rule"] == "draft-all"
        v = (await c.post(f"{url}/validate", headers=hub.auth(), json={"rules": [bad]})).json()
        assert v["valid"] is False and "min_exposed_nodes" in v["problems"][0]["problem"]
        assert (await c.post(f"{url}/validate", headers=hub.auth(), json={"rules": [_rule("ok")]})).json() == {"valid": True, "problems": []}
        r = await c.put(url, headers=hub.auth(), json={"rules": [_rule("a"), _rule("a")]})
        assert r.status_code == 422 and "duplicate" in r.json()["detail"]["problems"][0]["problem"]
        # delete down to nothing → defaults again
        for rid in ("strict-alert", "default-draft-case"):
            assert (await c.delete(f"{url}/{rid}", headers=hub.auth())).status_code == 200
        r = await c.delete(f"{url}/quiet", headers=hub.auth())
        assert r.json()["custom"] is False
        assert (await c.get(url, headers=hub.auth())).json()["custom"] is False
        assert (await c.delete(f"{url}/quiet", headers=hub.auth())).status_code == 404
        # scoping
        assert (await c.post(f"/v1/tenants/{OTHER}/alert-rules", headers=hub.auth(), json=_rule("x"))).status_code == 403


# ─────────────────────────── LiteLLM extraction key ───────────────────────────

async def test_bootstrap_provisions_key_and_api_uses_it(pool: asyncpg.Pool, db_dsn: str, settings: Settings, gaz: Any,
                                                        caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    mock = MockLiteLlm(chat_status=402)
    s = dataclasses.replace(settings, litellm_base_url="http://litellm:4000", litellm_wait_s=0.1)
    conn = await asyncpg.connect(db_dsn)
    try:
        async with httpx.AsyncClient(transport=mock.transport()) as http:
            assert await provision_llm_key(s, conn, None, http=http) == "skipped"
            assert await provision_llm_key(s, conn, mock.master_key, http=http) == "generated"
            assert await provision_llm_key(s, conn, mock.master_key, http=http) == "unchanged"
            down = MockLiteLlm(ready=False)
            async with httpx.AsyncClient(transport=down.transport()) as http_down:
                assert await provision_llm_key(s, conn, down.master_key, http=http_down) == "unavailable"
            assert await provision_llm_key(s, conn, "sk-wrong", http=http) == "failed"
    finally:
        await conn.close()
    (raw,) = mock.raw.values()
    async with tx(pool, None) as c:
        row = await c.fetchrow("SELECT * FROM hub_secret WHERE name = $1", SECRET_NAME)
    assert row is not None and raw not in row["ciphertext"] and row["meta"]["models"] == ["extract-fast", "guard-default"]
    async with tx(pool, TENANT) as c:  # never readable in a tenant transaction
        assert await c.fetchval("SELECT count(*) FROM hub_secret") == 0
    # API startup: the stored key is used unless LITELLM_SIGNALS_KEY is set
    resolved = await resolve_llm_key(s, pool)
    assert resolved.litellm_key == raw and resolved.llm_key_source == "db"
    env = await resolve_llm_key(dataclasses.replace(s, litellm_key="sk-from-env"), pool)
    assert env.litellm_key == "sk-from-env" and env.llm_key_source == "env"
    assert await load_stored_key(dataclasses.replace(s, shared_secret="rotated-" + "z" * 40), pool) is None
    # the provisioned key reaches LiteLLM; 402 (credits depleted) keeps the item pending
    async with httpx.AsyncClient(transport=mock.transport()) as http:
        llm = LiteLlmClient(http, base_url=resolved.litellm_base_url, api_key=resolved.litellm_key)
        pipe = Pipeline(pool, gaz, llm)
        item = NewsItemDraft(tenant=None, connector_id="rss.fixture", native_key="n-1", origin="news", source_name="Fixture",
                             license="headline only", authority_bp=7000, url="https://news.example.com/n-1",
                             title="Port of Ho Chi Minh City congested", snippet="Terminal delays reported.",
                             published_at=T0, retrieved_at=T0)
        ids = await pipe.queue_news([item], now=T0)
        await pipe.process_news(ids, tenant=None, now=T0)
        async with tx(pool, None) as c:
            status, attempts, bad = await c.fetchrow("SELECT status, attempts, bad_outputs FROM news_item WHERE id = $1", ids[0])
        assert (status, attempts, bad) == ("pending_extraction", 1, 0)
        chat = [b for b in mock.bodies if isinstance(b, dict) and b.get("model") in ("guard-default", "extract-fast")]
        assert chat and chat[0]["model"] == "guard-default" and "tools" not in chat[0]
    # the API state re-reads a rotated key; an env key is never replaced
    async with httpx.AsyncClient(transport=mock.transport()) as http:
        state = build_state(resolved, pool, clock=Clock(T0), internal_client=http)
        assert await refresh_llm_key(state) is False
        async with tx(pool, None) as c:
            await c.execute("DELETE FROM hub_secret")
        assert await refresh_llm_key(state) is True and state.settings.litellm_key is None
        assert state.llm is not None and not state.llm.has_key  # type: ignore[union-attr]
        env_state = build_state(env, pool, clock=Clock(T0), internal_client=http)
        assert await refresh_llm_key(env_state) is False and env_state.settings.litellm_key == "sk-from-env"
    assert raw not in caplog.text and mock.master_key not in caplog.text


async def test_sources_endpoint_reports_key_source(pool: asyncpg.Pool, settings: Settings) -> None:
    internal = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(404)))
    state = build_state(dataclasses.replace(settings, litellm_key="sk-x", llm_key_source="db"), pool, llm=None,
                        clock=Clock(T0), internal_client=internal)
    app = create_app(state.settings, state=state, background=False)
    async with running_app(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url=BASE) as c:
        body = (await c.get("/v1/sources", headers=Hub.auth())).json()
    await internal.aclose()
    assert body["llm_configured"] is True and body["llm_key_source"] == "db" and "sk-x" not in json.dumps(body)


# ─────────────────────────── review regressions: the engine's whole-set sync ───────────────────────────
# keelson_engine.core.signals_admin is the system of record and pushes whole sets:
# PUT …/subscriptions {sources: [{connector_id, enabled}]}, PUT …/watch-regions {regions: [WatchRegion]}
# (engine shape: polygon / node {type, code}), PUT …/alert-rules {rules}; it reads GET /v1/sources.

async def test_engine_subscription_sync(hub: Hub, pool: asyncpg.Pool) -> None:
    pipe: Pipeline = hub.state.pipeline
    await pipe.put_network(TENANT, TenantNetwork.model_validate(demo_network()), T0)
    await pipe.ingest(_tc(), now=T0)
    url = f"/v1/tenants/{TENANT}/subscriptions"
    gdacs_row = "SELECT updated_at, updated_by FROM source_subscription WHERE tenant = $1 AND connector_id = 'gdacs'"
    async with hub.client() as c:
        # a cadence wish set through the hub's own API survives the engine's map (which has none)
        assert (await c.put(f"/v1/tenants/{TENANT}/sources/usgs", json={"enabled": True, "cadence_s": 1800},
                            headers=hub.auth())).status_code == 200
        body = {"sources": [{"connector_id": "gdacs", "enabled": False}, {"connector_id": "acme:gone", "enabled": False}]}
        r = await c.put(url, json=body, headers=hub.auth())
        assert r.status_code == 200, r.text
        assert r.json()["ignored"] == ["acme:gone"] and r.json()["alerts_changed"] == 1
        items = {i["connector_id"]: i for i in r.json()["items"]}
        assert items["gdacs"]["enabled"] is False and items["usgs"]["cadence_s"] == 1800
        assert [a["status"] for a in await _alerts(pool, TENANT)] == ["resolved"]
        async with tx(pool, TENANT) as conn:
            before = await conn.fetchrow(gdacs_row, TENANT)
        # the same map again (every scn sync re-pushes it): nothing changes, no audit churn
        r = await c.put(url, json=body, headers=hub.auth(TENANT, sub="engine-sync"))
        assert r.status_code == 200 and r.json()["alerts_changed"] == 0
        async with tx(pool, TENANT) as conn:
            assert dict(await conn.fetchrow(gdacs_row, TENANT)) == dict(before)
        # an empty map = everything subscribed again (cadence wishes kept)
        r = await c.put(url, json={"sources": []}, headers=hub.auth())
        assert r.status_code == 200 and r.json()["alerts_changed"] == 1
        assert [a["status"] for a in await _alerts(pool, TENANT)] == ["new"]
        items = {i["connector_id"]: i for i in r.json()["items"]}
        assert items["gdacs"]["enabled"] is True and items["usgs"]["cadence_s"] == 1800
        # review regression: the engine accepts any wish in 60 s–1 d and relies on the hub to clamp it.
        # An out-of-range wish is clamped (warning) and never blocks the rest of the map; null = default
        r = await c.put(url, json={"sources": [{"connector_id": "usgs", "enabled": True, "cadence_s": 60},
                                               {"connector_id": "gdacs", "enabled": False, "cadence_s": 120}]},
                        headers=hub.auth())
        assert r.status_code == 200, r.text
        assert [(w["source"], w["field"]) for w in r.json()["warnings"]] == [("usgs", "cadence_s"), ("gdacs", "cadence_s")]
        items = {i["connector_id"]: i for i in r.json()["items"]}
        assert items["usgs"]["cadence_s"] == 120 and items["gdacs"]["cadence_s"] == 300
        assert items["gdacs"]["enabled"] is False
        async with tx(pool, TENANT) as conn:
            before = await conn.fetchrow(gdacs_row, TENANT)
        r = await c.put(url, json={"sources": [{"connector_id": "usgs", "enabled": True, "cadence_s": 60},
                                               {"connector_id": "gdacs", "enabled": False, "cadence_s": 120}]},
                        headers=hub.auth(TENANT, sub="engine-sync"))
        assert r.status_code == 200 and len(r.json()["warnings"]) == 2
        async with tx(pool, TENANT) as conn:  # the clamped re-push changes nothing
            assert dict(await conn.fetchrow(gdacs_row, TENANT)) == dict(before)
        # the single-source editor stays strict
        bad = await c.put(f"/v1/tenants/{TENANT}/sources/usgs", json={"enabled": True, "cadence_s": 60},
                          headers=hub.auth())
        assert bad.status_code == 422
        r = await c.put(url, json={"sources": [{"connector_id": "usgs", "enabled": True, "cadence_s": None}]},
                        headers=hub.auth())
        assert {i["connector_id"]: i for i in r.json()["items"]}["usgs"]["cadence_s"] is None
        dup = {"sources": [{"connector_id": "gdacs", "enabled": True}, {"connector_id": "gdacs", "enabled": False}]}
        assert (await c.put(url, json=dup, headers=hub.auth())).status_code == 422
        assert (await c.put(f"/v1/tenants/{OTHER}/subscriptions", json=body, headers=hub.auth())).status_code == 403


async def test_engine_watch_region_sync(hub: Hub, pool: asyncpg.Pool) -> None:
    pipe: Pipeline = hub.state.pipeline
    await pipe.put_network(TENANT, TenantNetwork.model_validate({"nodes": [BKK], "lanes": []}), T0)
    await pipe.ingest(_tc(), now=T0)
    url = f"/v1/tenants/{TENANT}/watch-regions"
    gulf_ts = "SELECT updated_at FROM watch_region WHERE tenant = $1 AND id = 'gulf'"
    # exactly what keelson_engine.core.signals_admin.WatchRegion.model_dump(mode="json") sends
    radius = {"id": "bkk", "name": "Bangkok", "kind": "radius", "polygon": None,
              "node": {"type": "port", "code": "THBKK"}, "radius_km": 1500, "enabled": True}
    ghost = {"id": "ghost", "name": "Not pushed yet", "kind": "radius", "polygon": None,
             "node": {"type": "dc", "code": "DC-NEW"}, "radius_km": 50, "enabled": True}
    gulf = {"id": "gulf", "name": "Gulf", "kind": "polygon", "polygon": GULF, "node": None, "radius_km": None,
            "enabled": False}
    async with hub.client() as c:
        r = await c.put(url, json={"regions": [radius, ghost, gulf]}, headers=hub.auth())
        assert r.status_code == 200, r.text
        items = {x["id"]: x for x in r.json()["items"]}
        assert items["bkk"]["node_type"] == "port" and items["bkk"]["radius_km"] == 1500
        assert items["ghost"]["node_found"] is False  # stored; matches once the node is pushed
        assert items["gulf"]["geometry"] == GULF and items["gulf"]["enabled"] is False
        assert r.json()["alerts_changed"] == 1 and ("port", "THBKK") in await _exposures(pool, TENANT)
        async with tx(pool, TENANT) as conn:
            before = await conn.fetchval(gulf_ts, TENANT)
        # identical re-push: no row rewritten
        assert (await c.put(url, json={"regions": [radius, ghost, gulf]}, headers=hub.auth())).status_code == 200
        async with tx(pool, TENANT) as conn:
            assert await conn.fetchval(gulf_ts, TENANT) == before
        # a geometry problem refuses the whole set and changes nothing
        bowtie = {"type": "Polygon", "coordinates": [[[0, 0], [2, 2], [2, 0], [0, 2], [0, 0]]]}
        r = await c.put(url, json={"regions": [{**gulf, "polygon": bowtie}]}, headers=hub.auth())
        assert r.status_code == 422 and r.json()["detail"]["problems"][0]["region"] == "gulf"
        assert len((await c.get(url, headers=hub.auth())).json()["items"]) == 3
        # both shapes at once, unknown node keys, duplicate ids → 422
        both = {**radius, "node_type": "port", "node_code": "THBKK"}
        assert (await c.put(url, json={"regions": [both]}, headers=hub.auth())).status_code == 422
        odd = {**radius, "node": {"type": "port", "code": "X", "x": 1}}
        assert (await c.put(url, json={"regions": [odd]}, headers=hub.auth())).status_code == 422
        assert (await c.put(url, json={"regions": [radius, radius]}, headers=hub.auth())).status_code == 422
        # the native shape works too; regions left out are deleted
        native = {"id": "bkk", "name": "Bangkok", "kind": "radius", "node_type": "port", "node_code": "THBKK",
                  "radius_km": 10}
        r = await c.put(url, json={"regions": [native]}, headers=hub.auth())
        assert r.status_code == 200 and r.json()["deleted"] == ["ghost", "gulf"]
        assert await _exposures(pool, TENANT) == {}
        assert (await c.put(f"/v1/tenants/{OTHER}/watch-regions", json={"regions": []}, headers=hub.auth())).status_code == 403
        r = await c.put(url, json={"regions": []}, headers=hub.auth())
        assert r.status_code == 200 and r.json()["items"] == [] and r.json()["deleted"] == ["bkk"]


async def test_engine_alert_rule_sync_not_refused_by_editor_advice(hub: Hub) -> None:
    """The engine stores a structurally valid set and evaluates auto-open with it, so the hub must
    not keep its old rules over editor-only advice (warnings); duplicates / too many still refuse."""
    url = f"/v1/tenants/{TENANT}/alert-rules"
    noisy = {**_rule("noisy"), "hysteresis_bp": 0}
    async with hub.client() as c:
        r = await c.put(url, json={"rules": [noisy]}, headers=hub.auth())
        assert r.status_code == 200, r.text
        assert r.json()["warnings"] and r.json()["warnings"][0]["rule"] == "noisy"
        assert [x["id"] for x in (await c.get(url, headers=hub.auth())).json()["rules"]] == ["noisy"]
        r = await c.put(f"{url}?strict=true", json={"rules": [_rule("other"), noisy]}, headers=hub.auth())
        assert r.status_code == 422 and r.json()["detail"]["problems"][0]["rule"] == "noisy"
        assert [x["id"] for x in (await c.get(url, headers=hub.auth())).json()["rules"]] == ["noisy"]
        r = await c.put(url, json={"rules": [_rule("a"), _rule("a")]}, headers=hub.auth())
        assert r.status_code == 422 and "duplicate" in r.json()["detail"]["problems"][0]["problem"]
        # the hub's single-rule edits stay strict
        assert (await c.post(url, json={**_rule("b"), "hysteresis_bp": 0}, headers=hub.auth())).status_code == 422
        r = await c.put(url, json={"rules": [_rule("ok")]}, headers=hub.auth())
        assert r.status_code == 200 and r.json()["warnings"] == []


async def test_single_rule_edit_not_blocked_by_other_rules_warnings(hub: Hub) -> None:
    """Review regression: an engine-pushed set may carry editor warnings (stored non-strictly). A
    hub single-rule edit must stay strict about the rule it writes only — before the fix every
    create/update/delete of an unrelated rule answered 422 because of the pushed rule."""
    url = f"/v1/tenants/{TENANT}/alert-rules"
    noisy = {**_rule("noisy"), "hysteresis_bp": 0}
    async with hub.client() as c:
        assert (await c.put(url, json={"rules": [noisy, _rule("keep")]}, headers=hub.auth())).status_code == 200
        r = await c.post(url, json=_rule("added"), headers=hub.auth())
        assert r.status_code == 201, r.text
        assert [w["rule"] for w in r.json()["warnings"]] == ["noisy"]
        r = await c.put(f"{url}/keep", json=_rule("keep", min_severity_bp=6000), headers=hub.auth())
        assert r.status_code == 200, r.text
        assert (await c.delete(f"{url}/added", headers=hub.auth())).status_code == 200
        # the written rule itself is still checked strictly; nothing is stored
        r = await c.post(url, json={**_rule("bad"), "hysteresis_bp": 0}, headers=hub.auth())
        assert r.status_code == 422 and [p["rule"] for p in r.json()["detail"]["problems"]] == ["bad"]
        r = await c.put(f"{url}/keep", json={**_rule("keep"), "action": "alert_and_draft_case",
                                              "when": {**_rule("keep")["when"], "min_exposed_nodes": 0}},
                        headers=hub.auth())
        assert r.status_code == 422 and {p["rule"] for p in r.json()["detail"]["problems"]} == {"keep"}
        # duplicate ids (hard problems) still refuse
        assert (await c.put(f"{url}/keep", json=_rule("noisy"), headers=hub.auth())).status_code == 409
        ids = [x["id"] for x in (await c.get(url, headers=hub.auth())).json()["rules"]]
        assert ids == ["noisy", "keep"]
        r = await c.delete(f"{url}/noisy", headers=hub.auth())
        assert r.status_code == 200 and r.json()["warnings"] == []


async def test_sources_lists_catalogue_for_engine(hub: Hub) -> None:
    async with hub.client() as c:
        body = (await c.get("/v1/sources", headers=hub.auth())).json()
    rows = {r["connector_id"]: r for r in body["sources"]}
    assert list(rows) == ["gdacs", "rss.fixture", "usgs"]  # configured, never polled yet
    assert rows["gdacs"]["license"] == "CC BY 4.0" and rows["gdacs"]["name"].startswith("GDACS")
    assert rows["gdacs"]["cadence_s"] == 600 and rows["gdacs"]["paused"] is False
    assert rows["gdacs"]["last_polled_at"] is None
