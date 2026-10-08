"""REST + MCP + demo pack replay against PostGIS: alerts API (list/detail/status, open-case
idempotency, change feed), events, network push, connector-event ingest (with 402 → pending),
MCP tools scoped by the token's tenant (untrusted markers), and pack replay determinism through
the real adapters fetching ``/demo-feeds``."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from typing import Any

import asyncpg
import httpx
import httpx2
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.exceptions import ToolError

from conftest import T0, demo_network, load_fixture, make_token, running_app
from fakes import Clock, FakeLlm
from keelson_contracts.signals import AlertDetail, DisruptionSignal
from signal_hub.adapters.gdacs import parse_gdacs
from signal_hub.app import build_state, create_app
from signal_hub.config import Settings
from signal_hub.db import tx
from signal_hub.models import SourceContext

pytestmark = [pytest.mark.postgis, pytest.mark.usefixtures("clean")]
TENANT = "demo-retail"
OTHER = "other-co"
BASE = "http://signal-hub-api:8300"


class Harness:
    def __init__(self, app: Any, state: Any, clock: Clock) -> None:
        self.app, self.state, self.clock = app, state, clock

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url=BASE)

    @staticmethod
    def auth(tenant: str = TENANT) -> dict[str, str]:
        return {"Authorization": f"Bearer {make_token(tenant)}"}

    def mcp(self, tenant: str = TENANT) -> Client:
        def factory(**kwargs: Any) -> httpx2.AsyncClient:
            kwargs.pop("follow_redirects", None)
            return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=self.app), follow_redirects=True, **kwargs)

        return Client(StreamableHttpTransport(f"{BASE}/mcp", auth=make_token(tenant), httpx_client_factory=factory))


@pytest.fixture
async def hub(pool: asyncpg.Pool, settings: Settings) -> AsyncIterator[Harness]:
    holder: dict[str, Any] = {}

    async def deferred(scope: Any, receive: Any, send: Any) -> None:  # the replay fetches /demo-feeds from this app
        await holder["app"](scope, receive, send)

    internal = httpx.AsyncClient(transport=httpx.ASGITransport(app=deferred))
    clock = Clock(T0)
    state = build_state(settings, pool, llm=FakeLlm(unavailable=402), clock=clock, internal_client=internal)
    app = create_app(settings, state=state, background=False)
    holder["app"] = app
    async with running_app(app):
        yield Harness(app, state, clock)
    await internal.aclose()


def _tc_drafts(state: Any, **props: Any) -> list[Any]:
    doc = json.loads(load_fixture("gdacs_eventlist.json"))
    doc["features"] = [doc["features"][0]]
    doc["features"][0]["properties"].update(props)
    footprints = {("TC", 1001102): json.loads(load_fixture("gdacs_tc_geometry.json"))["features"]}
    return parse_gdacs(doc, SourceContext(retrieved_at=T0), state.gazetteer, extra_footprints=footprints)


async def test_alerts_api_flow(hub: Harness) -> None:
    async with hub.client() as c:
        r = await c.put(f"/v1/tenants/{TENANT}/network", json=demo_network(), headers=hub.auth())
        assert r.status_code == 200 and r.json()["network_version"] == 1 and r.json()["nodes"] == 6
        await hub.state.pipeline.ingest(_tc_drafts(hub.state), now=T0)
        r = await c.get("/v1/alerts", headers=hub.auth())
        body = r.json()
        assert r.status_code == 200 and body["new_count"] == 1
        (alert,) = body["items"]
        assert alert["severity"] == "medium" and alert["status"] == "new" and alert["simulated"] is False
        assert alert["exposure_summary"]["nodes_by_type"] == {"supplier": 1, "factory": 1, "port": 1, "dc": 0, "store": 0, "lane": 0}
        assert (await c.get("/v1/alerts?status=seen", headers=hub.auth())).json()["items"] == []
        assert (await c.get("/v1/alerts?status=bogus", headers=hub.auth())).status_code == 422
        # detail = contract AlertDetail (event + members with attribution + exposures)
        r = await c.get(f"/v1/alerts/{alert['id']}", headers=hub.auth())
        detail = AlertDetail.model_validate(r.json())
        assert detail.event.anchor_signal_id == detail.signals[0].id
        assert detail.signals[0].source.license == "CC BY 4.0" and len(detail.exposures) == 3
        # another tenant sees neither the alert nor its detail
        assert (await c.get(f"/v1/alerts/{alert['id']}", headers=hub.auth(OTHER))).status_code == 404
        assert (await c.get("/v1/alerts", headers=hub.auth(OTHER))).json()["items"] == []
        # status transitions + open-case idempotency (one case per alert)
        r = await c.post(f"/v1/alerts/{alert['id']}/status", json={"status": "seen"}, headers=hub.auth())
        assert r.status_code == 200 and r.json()["status"] == "seen"
        assert (await c.post(f"/v1/alerts/{alert['id']}/status", json={"status": "opened"}, headers=hub.auth())).status_code == 422
        r1 = await c.post(f"/v1/alerts/{alert['id']}/status", json={"status": "opened", "case_id": "case-42"}, headers=hub.auth())
        r2 = await c.post(f"/v1/alerts/{alert['id']}/status", json={"status": "opened", "case_id": "case-42"}, headers=hub.auth())
        assert r1.status_code == r2.status_code == 200 and r2.json()["case_id"] == "case-42"
        r = await c.post(f"/v1/alerts/{alert['id']}/status", json={"status": "opened", "case_id": "case-43"}, headers=hub.auth())
        assert r.status_code == 409
        assert (await c.post(f"/v1/alerts/{alert['id']}/status", json={"status": "dismissed"}, headers=hub.auth())).status_code == 409
        assert (await c.post(f"/v1/alerts/{alert['id']}/status", json={"status": "seen"}, headers=hub.auth(OTHER))).status_code == 404
        # change feed for the engine (raised → status changes)
        feed = (await c.get("/v1/alerts/changes?after=0", headers=hub.auth())).json()
        assert [i["change"] for i in feed["items"]] == ["raised", "status", "status"]
        assert feed["items"][0]["action"] == "alert" and feed["items"][-1]["alert"]["case_id"] == "case-42"
        more = (await c.get(f"/v1/alerts/changes?after={feed['next_after']}", headers=hub.auth())).json()
        assert more["items"] == [] and more["next_after"] == feed["next_after"]
        # event endpoint
        r = await c.get(f"/v1/events/{alert['event_id']}", headers=hub.auth())
        assert r.status_code == 200 and r.json()["exposure_summary"]["nodes_by_type"]["port"] == 1
        assert (await c.get("/v1/events/not-a-uuid", headers=hub.auth())).status_code == 422
        # rules: GET defaults, PUT custom (raise the threshold → alert facts refreshed)
        rules = (await c.get(f"/v1/tenants/{TENANT}/alert-rules", headers=hub.auth())).json()
        assert not rules["custom"] and [r["id"] for r in rules["rules"]] == ["default-alert", "default-draft-case"]
        r = await c.put(f"/v1/tenants/{TENANT}/alert-rules", headers=hub.auth(), json={"rules": [
            {"id": "strict", "when": {"min_severity_bp": 9000, "kinds": [], "min_confidence_bp": 0, "min_exposed_nodes": 1,
                                     "node_types": []}, "action": "alert", "hysteresis_bp": 500}]})
        assert r.status_code == 200 and r.json()["rules"] == 1
        r = await c.get("/v1/signals?query=KAIMANA", headers=hub.auth())
        assert r.json()["content_trust"] == "untrusted_text" and len(r.json()["items"]) == 1
        assert (await c.get("/v1/sources", headers=hub.auth())).json()["llm_configured"] is False


async def test_network_summary_read(hub: Harness) -> None:
    """``GET /v1/tenants/{t}/network``: version + codes of what the last push stored (own tenant only)."""
    async with hub.client() as c:
        r = await c.get(f"/v1/tenants/{TENANT}/network", headers=hub.auth())
        assert r.status_code == 200 and r.json()["network_version"] == 0 and r.json()["nodes"] == {}
        put = await c.put(f"/v1/tenants/{TENANT}/network", json=demo_network(), headers=hub.auth())
        body = (await c.get(f"/v1/tenants/{TENANT}/network", headers=hub.auth())).json()
        assert body["network_version"] == put.json()["network_version"] >= 1 and body["updated_at"]
        net = demo_network()
        assert sum(len(v) for v in body["nodes"].values()) == len(net["nodes"])
        assert sorted(body["lanes"]) == sorted(ln["code"] for ln in net["lanes"])
        assert all("name" not in str(v) for v in body["nodes"].values())
        assert (await c.get(f"/v1/tenants/{TENANT}/network", headers=hub.auth(OTHER))).status_code == 403
        assert (await c.get(f"/v1/tenants/{TENANT}/network")).status_code == 401


async def test_connector_event_endpoint(hub: Harness, pool: asyncpg.Pool) -> None:
    async with hub.client() as c:
        await c.put(f"/v1/tenants/{TENANT}/network", json=demo_network(), headers=hub.auth())
        r = await c.post("/v1/ingest/connector-event", headers=hub.auth(), json={
            "type": "lane.capacity_changed", "id": "evt-1", "timestamp": "2026-10-07T02:00:00Z",
            "data": {"lane_code": "SGN-HAM-SEA", "factor_bp": 3000, "weeks": 2, "from_week": "2026-W42",
                     "to_week": "2026-W43", "origin": "VNSGN", "dest": "DE1", "carrier_note": "Vessel omits Cat Lai."}})
        body = r.json()
        assert r.status_code == 202 and body["accepted"] and body["new"] == 1 and body["news_queued"] == 1
        assert body["news_status"] == {"pending_extraction": 1}  # LLM unavailable (402): text waits, fact proceeds
        assert [a["change"] for a in body["alerts"]] == ["raised"]
        # replay of the same delivery is a duplicate
        r = await c.post("/v1/ingest/connector-event", headers=hub.auth(), json={
            "type": "lane.capacity_changed", "id": "evt-1", "timestamp": "2026-10-07T02:00:00Z",
            "data": {"lane_code": "SGN-HAM-SEA", "factor_bp": 3000, "weeks": 2, "from_week": "2026-W42",
                     "to_week": "2026-W43", "origin": "VNSGN", "dest": "DE1", "carrier_note": "Vessel omits Cat Lai."}})
        assert r.json()["unchanged"] == 1 and r.json()["news_queued"] == 0
        r = await c.post("/v1/ingest/connector-event", headers=hub.auth(), json={"type": "inventory.adjusted", "id": "x", "data": {}})
        assert r.status_code == 202 and r.json()["accepted"] is False
        assert (await c.post("/v1/ingest/connector-event", headers=hub.auth(), json={"id": "x"})).status_code == 422
        # /v1/sources: the caller's own free-text items by status (another tenant sees none of them)
        assert (await c.get("/v1/sources", headers=hub.auth())).json()["tenant_news"] == {"pending_extraction": 1}
        assert (await c.get("/v1/sources", headers=hub.auth(OTHER))).json()["tenant_news"] == {}
    async with tx(pool, OTHER) as conn:
        assert await conn.fetchval("SELECT count(*) FROM news_item") == 0
    async with tx(pool, TENANT) as conn:
        doc = await conn.fetchval("SELECT doc FROM signal WHERE tenant = $1", TENANT)
    sig = DisruptionSignal.model_validate(doc)
    assert sig.origin == "connected_system" and sig.provenance == "fact" and sig.tenant == TENANT and sig.raw_text is None


async def test_mcp_tools(hub: Harness) -> None:
    async with hub.client() as c:
        await c.put(f"/v1/tenants/{TENANT}/network", json=demo_network(), headers=hub.auth())
    rep = await hub.state.pipeline.ingest(_tc_drafts(hub.state), now=T0)
    (ev,) = rep.events
    async with hub.mcp() as client:
        res = await client.call_tool("search_signals", {"query": "kaimana", "kinds": ["natural_hazard"]})
        data = res.structured_content
        assert data["content_trust"] == "untrusted_text" and "signals[].raw_text" in data["untrusted_fields"]
        assert [s["title"] for s in data["signals"]] == ["Tropical Cyclone KAIMANA-26"]
        res = await client.call_tool("search_signals", {"near_node_type": "port", "near_node_code": "VNSGN", "radius_km": 50})
        assert res.structured_content["signals"] == []  # centroid is 400 km away
        res = await client.call_tool("get_event", {"event_id": ev})
        assert res.structured_content["event"]["id"] == ev and len(res.structured_content["exposures"]) == 3
        res = await client.call_tool("hazards_near_node", {"node_type": "port", "node_code": "VNSGN", "radius_km": 300})
        (hz,) = res.structured_content["events"]
        assert hz["event_id"] == ev and 190 <= hz["distance_km"] <= 205
        res = await client.call_tool("assess_exposure", {"event_id": ev})
        assert res.structured_content["summary"]["nodes_by_type"]["port"] == 1
        with pytest.raises(ToolError):
            await client.call_tool("hazards_near_node", {"node_type": "port", "node_code": "NOPE"})
        with pytest.raises(ToolError):
            await client.call_tool("get_event", {"event_id": "../../v1/demo"})
    async with hub.mcp(OTHER) as client:  # OTHER has no network: no exposure, global event still visible
        res = await client.call_tool("get_event", {"event_id": ev})
        assert res.structured_content["exposures"] == []
        res = await client.call_tool("assess_exposure", {"event_id": ev})
        assert res.structured_content["exposures"] == []


def _snapshot_rows(rows: list[asyncpg.Record]) -> list[tuple[Any, ...]]:
    return [tuple((v.isoformat() if isinstance(v, dt.datetime) else str(v) if v is not None else None)
                  for v in r.values()) for r in rows]


async def _snapshot(pool: asyncpg.Pool) -> dict[str, Any]:
    async with tx(pool, TENANT) as conn:
        return {
            "signals": _snapshot_rows(await conn.fetch(
                "SELECT id, version, supersedes, is_current, event_id, severity_bp, confidence_bp, content_hash, doc::text "
                "FROM signal WHERE tenant = $1 ORDER BY id", TENANT)),
            "events": _snapshot_rows(await conn.fetch(
                "SELECT id, kind, subtype, title, anchor_signal_id, severity_bp, confidence_bp, status, taxonomy_key, "
                "geometry::text, window_start, window_end, updated_at, merged_into FROM event WHERE tenant = $1 ORDER BY id", TENANT)),
            "exposures": _snapshot_rows(await conn.fetch(
                "SELECT event_id, node_type, node_code, method, distance_km, weight_bp, computed_at FROM exposure "
                "ORDER BY event_id, node_type, node_code")),
            "alerts": _snapshot_rows(await conn.fetch(
                "SELECT id, event_id, status, severity, severity_bp, title, why, exposure_summary::text, first_raised_at, "
                "updated_at, simulated, action, notified_severity_bp FROM alert ORDER BY id")),
            "changes": [(r["change"], r["action"], r["at"].isoformat()) for r in await conn.fetch(
                "SELECT change, action, at FROM alert_change ORDER BY seq")],
            "news": _snapshot_rows(await conn.fetch(
                "SELECT id, status, extracted_by, signal_id, extraction::text FROM news_item ORDER BY id")),
        }


async def _play(c: httpx.AsyncClient, hub: Harness) -> list[dict[str, Any]]:
    states = [(await c.post("/v1/demo/packs/typhoon-test/start", headers=hub.auth())).json()]
    for _ in range(3):
        r = await c.post("/v1/demo/packs/typhoon-test/advance", headers=hub.auth(), json={})
        assert r.status_code == 200, r.text
        states.append(r.json())
    return states


async def test_demo_pack_replay_is_deterministic(hub: Harness, pool: asyncpg.Pool) -> None:
    async with hub.client() as c:
        await c.put(f"/v1/tenants/{TENANT}/network", json=demo_network(), headers=hub.auth())
        await c.put(f"/v1/tenants/{OTHER}/network", json=demo_network(), headers=hub.auth(OTHER))
        packs = (await c.get("/v1/demo/packs", headers=hub.auth())).json()["packs"]
        assert [p["id"] for p in packs] == ["typhoon-test"]
        states = await _play(c, hub)
        assert [len(s["applied_now"]) for s in states] == [1, 2, 2, 0]
        assert states[0]["sim_now"] == "2026-10-07T00:00:00+00:00" and states[1]["sim_now"] == "2026-10-07T06:00:00+00:00"
        assert states[-1]["status"] == "finished" and states[-1]["next_step"] is None
        hint = states[2]["applied_now"][1]
        assert hint["adapter"] == "chat_hint" and hint["hint"].startswith("Ask:")
        first = await _snapshot(pool)

        # expected Act-1 outcome: one simulated event, the HCMC nodes + SGN lane exposed, alert escalated
        (event,) = [e for e in first["events"] if e[13] is None]
        assert event[1:3] == ("natural_hazard", "tropical_cyclone") and event[7] == "confirmed"
        exposed = {(e[1], e[2]): e[3] for e in first["exposures"] if e[0] == event[0]}
        assert {k for k in exposed} == {("port", "VNSGN"), ("factory", "F-VN-01"), ("supplier", "SUP-VN-01"),
                                        ("lane", "SGN-HAM-SEA")}
        assert exposed[("port", "VNSGN")] == "intersects" and exposed[("lane", "SGN-HAM-SEA")] == "lane_crosses"
        (alert,) = first["alerts"]
        assert alert[3] == "critical" and alert[10] == "True" and alert[11] == "alert_and_draft_case"
        assert [c[0] for c in first["changes"]][:3] == ["raised", "updated", "renotified"]
        assert first["changes"][0][2] == "2026-10-07T00:00:00+00:00"  # sim clock, not wall clock
        assert {n[1] for n in first["news"]} == {"extracted"} and {n[2] for n in first["news"]} == {"prebaked"}
        for row in first["signals"]:
            doc = json.loads(row[-1])
            assert doc["origin"] == "demo" and doc["provenance"] == "simulated" and doc["tenant"] == TENANT

        # the engine's change feed reports each change's own action (draft-case escalation is visible)
        feed = (await c.get("/v1/alerts/changes?after=0", headers=hub.auth())).json()["items"]
        assert [(i["change"], i["action"]) for i in feed][:3] == [
            ("raised", "alert"), ("updated", "alert"), ("renotified", "alert_and_draft_case")]
        assert {i["alert"]["id"] for i in feed} == {first["alerts"][0][0]}

        # demo data stays in the presenter's tenant
        r = await c.get("/v1/alerts", headers=hub.auth(OTHER))
        assert r.json()["items"] == []
        r = await c.get("/v1/alerts?status=new,seen,snoozed,dismissed,opened,resolved", headers=hub.auth())
        (api_alert,) = r.json()["items"]
        assert api_alert["simulated"] is True
        detail = AlertDetail.model_validate((await c.get(f"/v1/alerts/{api_alert['id']}", headers=hub.auth())).json())
        assert all(s.simulated for s in detail.signals)

        # reset + replay: byte-identical ids, numbers and sim timestamps
        r = await c.post("/v1/demo/packs/typhoon-test/reset", headers=hub.auth())
        assert r.json()["status"] == "idle" and r.json()["deleted"]["events"] >= 1
        assert (await _snapshot(pool))["alerts"] == []
        hub.clock.advance(hours=5)  # the wall clock moved; the replay must not care
        await _play(c, hub)
        assert await _snapshot(pool) == first

        # fixture route: serves pack files, refuses traversal / unknown types
        r = await c.get("/demo-feeds/typhoon-test/adv1.geojson")
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/geo+json")
        for bad in ("/demo-feeds/typhoon-test/../../conftest.py", "/demo-feeds/typhoon-test/pack.yaml",
                    "/demo-feeds/typhoon-test/%2e%2e/%2e%2e/conftest.py", "/demo-feeds/nope/adv1.geojson"):
            assert (await c.get(bad)).status_code == 404, bad


async def test_demo_controls(hub: Harness, settings: Settings) -> None:
    async with hub.client() as c:
        assert (await c.post("/v1/demo/packs/typhoon-test/advance", headers=hub.auth(), json={})).status_code == 409
        assert (await c.post("/v1/demo/packs/missing/start", headers=hub.auth())).status_code == 404
        assert (await c.post("/v1/demo/packs/Bad_Id/start", headers=hub.auth())).status_code == 422
        st = (await c.get("/v1/demo/packs/typhoon-test", headers=hub.auth())).json()
        assert st["status"] == "idle" and st["steps_total"] == 5
        r = await c.post("/v1/demo/packs/typhoon-test/start", headers=hub.auth(), json={"speed": 0})
        assert r.json()["steps_applied"] == 1
        r = await c.post("/v1/demo/packs/typhoon-test/advance", headers=hub.auth(), json={"seconds": 3600})
        assert r.json()["applied_now"] == [] and r.json()["elapsed_s"] == 3600
        r = await c.post("/v1/demo/packs/typhoon-test/advance", headers=hub.auth(), json={"to_step": 3})
        assert r.json()["steps_applied"] == 5  # steps at the same offset as step 3 are due too
    # speed multiplier: the background tick applies steps as sim time passes
    async with hub.client() as c:
        await c.post("/v1/demo/packs/typhoon-test/start", headers=hub.auth(), json={"speed": 3600})
        hub.clock.advance(seconds=6)  # 6 s x 3600 = 6 h of sim time
        assert await hub.state.replay.tick(wall_now=hub.clock()) == 2
        st = (await c.get("/v1/demo/packs/typhoon-test", headers=hub.auth())).json()
        assert st["steps_applied"] == 3
    # demo endpoints are off outside DEMO_MODE and limited to demo tenants
    object.__setattr__(hub.state, "settings", Settings(**{**{f: getattr(settings, f) for f in settings.__slots__},
                                                          "demo_tenants": ("someone-else",)}))
    async with hub.client() as c:
        assert (await c.post("/v1/demo/packs/typhoon-test/start", headers=hub.auth())).status_code == 403
    object.__setattr__(hub.state, "settings", Settings(**{**{f: getattr(settings, f) for f in settings.__slots__},
                                                          "demo_mode": False}))
    async with hub.client() as c:
        assert (await c.post("/v1/demo/packs/typhoon-test/start", headers=hub.auth())).status_code == 404
        assert (await c.get("/demo-feeds/typhoon-test/adv1.geojson")).status_code == 404


async def test_demo_reset_all_packs(hub: Harness, pool: asyncpg.Pool, settings: Settings) -> None:
    """``POST /v1/demo/reset`` (the engine's demo reset calls it first): every pack of the caller's
    tenant, nothing of another tenant."""
    async with hub.client() as c:
        await c.put(f"/v1/tenants/{TENANT}/network", json=demo_network(), headers=hub.auth())
        await _play(c, hub)
        await c.post("/v1/demo/packs/typhoon-test/start", headers=hub.auth(OTHER))
        r = await c.post("/v1/demo/reset", headers=hub.auth(), json={})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["packs"] == ["typhoon-test"] and body["counts"]["packs"] == 1
        assert body["counts"]["events"] >= 1 and body["counts"]["signals"] >= 1 and body["counts"]["alert_changes"] >= 1
        assert (await c.get("/v1/alerts", headers=hub.auth())).json()["items"] == []
        assert (await c.get("/v1/demo/packs/typhoon-test", headers=hub.auth())).json()["status"] == "idle"
        assert (await c.get("/v1/demo/packs/typhoon-test", headers=hub.auth(OTHER))).json()["status"] == "running"
        async with tx(pool, OTHER) as conn:
            assert await conn.fetchval("SELECT count(*) FROM signal WHERE tenant = $1", OTHER) >= 1
    object.__setattr__(hub.state, "settings", Settings(**{**{f: getattr(settings, f) for f in settings.__slots__},
                                                          "demo_mode": False}))
    async with hub.client() as c:
        assert (await c.post("/v1/demo/reset", headers=hub.auth(), json={})).status_code == 404
        assert (await c.post("/v1/demo/reset", json={})).status_code == 401


async def test_demo_reset_also_clears_the_tenants_connector_data_and_alerts(hub: Harness, pool: asyncpg.Pool) -> None:
    """M6 integration: the engine's demo reset resets the connected system to its seed, so the hub's
    ``POST /v1/demo/reset`` also drops the tenant's connector-event signals / events / free text and
    every remaining alert (+ change rows, exposures) — not only the packs. Another tenant keeps its
    data; global feed data stays."""
    lane_event = {
        "type": "lane.capacity_changed", "id": "evt-reset-1", "timestamp": "2026-10-07T02:00:00Z",
        "data": {"lane_code": "SGN-HAM-SEA", "factor_bp": 3000, "weeks": 2, "from_week": "2026-W42",
                 "to_week": "2026-W43", "origin": "VNSGN", "dest": "DE1", "carrier_note": "Vessel omits Cat Lai."}}
    async with hub.client() as c:
        for tenant in (TENANT, OTHER):
            await c.put(f"/v1/tenants/{tenant}/network", json=demo_network(), headers=hub.auth(tenant))
            r = await c.post("/v1/ingest/connector-event", headers=hub.auth(tenant), json=lane_event)
            assert [a["change"] for a in r.json()["alerts"]] == ["raised"]
        await hub.state.pipeline.ingest(_tc_drafts(hub.state), now=T0)
        async with tx(pool, None) as conn:
            global_signals = await conn.fetchval("SELECT count(*) FROM signal WHERE tenant IS NULL")
        assert global_signals >= 1
        r = await c.post("/v1/demo/reset", headers=hub.auth(), json={})
        assert r.status_code == 200, r.text
        counts = r.json()["counts"]
        assert counts["alerts"] >= 1 and counts["signals"] >= 1 and counts["news_items"] == 1 and counts["events"] >= 1
        assert (await c.get("/v1/alerts", headers=hub.auth())).json()["items"] == []
        assert (await c.get("/v1/alerts/changes?after=0", headers=hub.auth())).json()["items"] == []
        assert len((await c.get("/v1/alerts", headers=hub.auth(OTHER))).json()["items"]) == 2  # lane + global TC
    async with tx(pool, TENANT) as conn:
        for table in ("signal", "news_item", "event", "alert", "exposure"):
            assert await conn.fetchval(f"SELECT count(*) FROM {table} WHERE tenant = $1", TENANT) == 0, table
        assert await conn.fetchval("SELECT count(*) FROM network_node WHERE tenant = $1", TENANT) > 0
    async with tx(pool, None) as conn:
        assert await conn.fetchval("SELECT count(*) FROM signal WHERE tenant IS NULL") == global_signals
    async with tx(pool, OTHER) as conn:
        assert await conn.fetchval("SELECT count(*) FROM news_item WHERE tenant = $1", OTHER) == 1
