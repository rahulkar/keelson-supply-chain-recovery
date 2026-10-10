"""Replay the product demo packs (``data/scenarios``) end to end — real adapters fetching
``/demo-feeds``, PostGIS exposure, alert rules — against the reference tenant network
(``data/scenarios/network-value-fashion-eu.yaml``) and check the outcome each pack documents.
The M6 packs give the same outcome with clustering v2 on (default) and off (``docs/contracts/123``);
``port-strike-syndication`` shows the v2 memberships and replays identically twice.
Skipped when the pack data is not present."""

from __future__ import annotations

import dataclasses
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import asyncpg
import httpx
import pytest
import yaml

from conftest import GEO_DIR, REPO, T0, make_token, running_app, sea_route
from fakes import Clock, FakeLlm
from keelson_contracts.signals import ClusteringConfigUpdate
from signal_hub.app import build_state, create_app
from signal_hub.clustering_config import load_config, save_config
from signal_hub.config import Settings
from signal_hub.db import tx

pytestmark = [pytest.mark.postgis, pytest.mark.usefixtures("clean")]
PRODUCT = Path(os.environ.get("SCENARIOS_DIR") or REPO / "data" / "scenarios")
NETWORK = PRODUCT / "network-value-fashion-eu.yaml"
TENANT = "demo-retail"


def _network() -> dict[str, Any]:
    """The reference network with positions/paths drawn like the engine (see the YAML header)."""
    spec = yaml.safe_load(NETWORK.read_text(encoding="utf-8"))
    ports = json.loads((GEO_DIR / "ports.json").read_text(encoding="utf-8"))["ports"]
    centroids = json.loads((GEO_DIR / "country_centroids.json").read_text(encoding="utf-8"))["centroids"]
    pos: dict[str, tuple[float, float]] = {c: (p["lon"], p["lat"]) for c, p in ports.items()}
    nodes = []
    for n in spec["nodes"]:
        if "at" in n:
            kind, _, ref = n["at"].partition(":")
            lon, lat = pos[ref] if kind == "port" else tuple(centroids[ref])
        else:
            lon, lat = n["lon"], n["lat"]
        nodes.append({"type": n["type"], "code": n["code"], "name": n["code"], "lat": float(lat), "lon": float(lon)})
        pos.setdefault(n["code"], (float(lon), float(lat)))
    lanes = []
    for ln in spec["lanes"]:
        origin, dest, via = ln["origin"], ln["dest"], ln.get("via") or []
        if ln["mode"] == "sea":
            choke = next((v for v in via if v in ("CAPE", "SUEZ")), None)
            path = sea_route(origin, dest, choke)
        elif ln["mode"] == "sea_air" and via:
            path = [*sea_route(origin, via[0]), list(pos[dest])]
        else:
            path = [list(pos[origin]), list(pos[dest])]
        lanes.append({"code": ln["code"], "mode": ln["mode"], "path": path})
    return {"nodes": nodes, "lanes": lanes}


@pytest.fixture(params=["v2-on", "v2-off"])
async def clustering(request: pytest.FixtureRequest, pool: asyncpg.Pool, clean: None) -> str:
    """Clustering v2 on (the default settings) or off (SimHash and vector attach disabled)."""
    if request.param == "v2-off":
        async with tx(pool, None) as conn:
            cur = await load_config(conn)
            await save_config(conn, ClusteringConfigUpdate(
                base_version=cur.version, simhash=cur.simhash.model_copy(update={"enabled": False}),
                vector=cur.vector.model_copy(update={"enabled": False}), updated_by="test"), T0)
    return str(request.param)


@pytest.fixture
async def client(pool: asyncpg.Pool, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    if not NETWORK.is_file():
        pytest.skip("reference network not present")
    holder: dict[str, Any] = {}

    async def deferred(scope: Any, receive: Any, send: Any) -> None:
        await holder["app"](scope, receive, send)

    settings = dataclasses.replace(settings, scenarios_dir=PRODUCT)
    internal = httpx.AsyncClient(transport=httpx.ASGITransport(app=deferred))
    state = build_state(settings, pool, llm=FakeLlm(unavailable=402), clock=Clock(T0), internal_client=internal)
    holder["app"] = app = create_app(settings, state=state, background=False)
    async with running_app(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://signal-hub-api:8300",
                                                   headers={"Authorization": f"Bearer {make_token(TENANT)}"}) as c:
        r = await c.put(f"/v1/tenants/{TENANT}/network", json=_network())
        assert r.status_code == 200, r.text
        yield c
    await internal.aclose()


async def _replay(c: httpx.AsyncClient, pack: str) -> list[dict[str, Any]]:
    if not (PRODUCT / pack / "pack.yaml").is_file():
        pytest.skip(f"pack {pack} not present")
    r = await c.post(f"/v1/demo/packs/{pack}/start")
    assert r.status_code == 200, r.text
    for _ in range(20):
        state = (await c.post(f"/v1/demo/packs/{pack}/advance", json={})).json()
        if state["status"] == "finished":
            break
    assert state["status"] == "finished"
    alerts = (await c.get("/v1/alerts?status=new,seen,snoozed,dismissed,opened,resolved&limit=100")).json()["items"]
    out = []
    for a in alerts:
        detail = (await c.get(f"/v1/alerts/{a['id']}")).json()
        out.append(detail)
    return out


def _exposed(detail: dict[str, Any]) -> dict[tuple[str, str], str]:
    return {(e["node_type"], e["node_code"]): e["method"] for e in detail["exposures"]}


async def test_typhoon_hcmc(clustering: str, client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    (detail,) = await _replay(client, "typhoon-hcmc")
    alert, exposed = detail["alert"], _exposed(detail)
    assert alert["simulated"] is True and alert["severity"] in ("high", "critical") and alert["status"] == "new"
    for key in [("port", "VNSGN"), ("factory", "F-VN-01"), ("supplier", "SUP-VN-01")]:
        assert key in exposed, key
    sgn = {code for (t, code) in exposed if t == "lane" and code.startswith("SGN-")}
    assert sgn == {"SGN-HAM-SEA", "SGN-GDN-SEA", "SGN-HAM-CAPE", "SGN-GDN-CAPE", "SGN-DE1-SA", "SGN-PL1-SA"}
    assert ("port", "KHPNH") in exposed  # Cambodian nodes inside the 300 km TC radius
    assert detail["event"]["taxonomy_key"] == "NAT.storm" and detail["event"]["anchor_signal_id"]
    assert {s["origin"] for s in detail["signals"]} == {"demo"}
    assert any(s["source"]["connector_id"] == "demo.rss.demo-freight-daily" for s in detail["signals"])
    async with tx(pool, TENANT) as conn:
        changes = [(r["change"], r["action"]) for r in await conn.fetch("SELECT change, action FROM alert_change ORDER BY seq")]
    assert changes[0] == ("raised", "alert") or changes[0] == ("raised", "alert_and_draft_case")
    assert ("renotified", "alert_and_draft_case") in changes or changes[0] == ("raised", "alert_and_draft_case")


async def test_outerwear_trim_delay(clustering: str, client: httpx.AsyncClient) -> None:
    (detail,) = await _replay(client, "a-outerwear-trim-delay")
    assert detail["alert"]["severity"] == "medium" and detail["alert"]["simulated"] is True
    assert _exposed(detail).get(("supplier", "TRIM-BD-01")) == "entity_match"
    assert detail["event"]["taxonomy_key"] == "SUP.delay"
    (sig,) = detail["signals"]
    assert sig["provenance"] == "simulated" and sig["extracted_by"] == "prebaked" and "SUPPLIER:TRIM-BD-01" in sig["location_refs"]


async def test_suez_closure(clustering: str, client: httpx.AsyncClient) -> None:
    (detail,) = await _replay(client, "suez-closure")
    exposed = _exposed(detail)
    lanes = {code: m for (t, code), m in exposed.items() if t == "lane"}
    assert lanes.get("SGN-HAM-SEA") == "lane_crosses" and lanes.get("SGN-GDN-SEA") == "lane_crosses"
    for alt in ("SGN-HAM-CAPE", "SGN-GDN-CAPE", "SGN-DE1-SA", "SGN-PL1-SA", "CGP-FXT-SEA"):
        assert alt not in lanes, alt
    event = detail["event"]
    assert event["anchor_signal_id"] is None and event["taxonomy_key"] == "LOG.chokepoint_closure"
    assert event["status"] == "confirmed" and len(detail["signals"]) == 3  # 2 outlets corroborate; duplicate dropped


async def _memberships(pool: asyncpg.Pool) -> list[tuple[Any, ...]]:
    async with tx(pool, TENANT) as conn:
        rows = await conn.fetch(
            """SELECT s.native_key, m.method, m.prior_method, m.distance_km, m.shared_refs, m.similarity_bp,
                      m.similarity_profile, m.hamming_bits, m.duplicate_of, m.config_version, m.explanation, m.decided_at
               FROM signal_membership m JOIN signal s ON s.id = m.signal_id WHERE s.tenant = $1 ORDER BY s.native_key""",
            TENANT)
    return [tuple(r.values()) for r in rows]


async def test_port_strike_syndication(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    (detail,) = await _replay(client, "port-strike-syndication")
    exposed = _exposed(detail)
    assert exposed.get(("port", "DEHAM")) == "intersects"  # geocoded to the port itself
    assert {code for (t, code) in exposed if t == "lane"} == {"SGN-HAM-SEA", "SGN-HAM-CAPE", "HAM-DE1-RD", "HAM-PL1-RD"}
    by_source = {s["source"]["connector_id"]: s for s in detail["signals"]}
    methods = {m["signal_id"]: m for m in detail["memberships"]}
    original = by_source["demo.rss.demo-freight-daily"]
    copy = methods[by_source["demo.rss.simulated-shipping-wire"]["id"]]
    vector = methods[by_source["demo.rss.demo-port-bulletin"]["id"]]
    assert methods[original["id"]]["method"] == "founded"
    assert copy["method"] == "near_duplicate" and copy["duplicate_of_signal_id"] == original["id"]
    assert 0 < copy["hamming_bits"] <= 6
    assert (vector["method"], vector["similarity_bp"], vector["similarity_profile"]) == ("attach_vector", 7977, "local-terms-v1")
    event = detail["event"]
    # 2 independent sources (the syndicated copy is not counted): 7000 x 8000 x 10000
    assert event["status"] == "confirmed" and event["confidence_bp"] == 5600
    titles = [s["title"] for s in detail["signals"]]
    assert all(t not in m["explanation"] for m in detail["memberships"] for t in titles)
    first = await _memberships(pool)
    assert len(first) == 3
    # reset clears the pack's memberships (FK cascade); a second replay is identical
    assert (await client.post("/v1/demo/packs/port-strike-syndication/reset")).status_code == 200
    assert await _memberships(pool) == []
    await _replay(client, "port-strike-syndication")
    assert await _memberships(pool) == first
