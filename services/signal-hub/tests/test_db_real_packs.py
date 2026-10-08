"""Replay the product demo packs (``data/scenarios``) end to end — real adapters fetching
``/demo-feeds``, PostGIS exposure, alert rules — against the reference tenant network
(``data/scenarios/network-value-fashion-eu.yaml``) and check the outcome each pack documents.
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
from signal_hub.app import build_state, create_app
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


async def test_typhoon_hcmc(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
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


async def test_outerwear_trim_delay(client: httpx.AsyncClient) -> None:
    (detail,) = await _replay(client, "a-outerwear-trim-delay")
    assert detail["alert"]["severity"] == "medium" and detail["alert"]["simulated"] is True
    assert _exposed(detail).get(("supplier", "TRIM-BD-01")) == "entity_match"
    assert detail["event"]["taxonomy_key"] == "SUP.delay"
    (sig,) = detail["signals"]
    assert sig["provenance"] == "simulated" and sig["extracted_by"] == "prebaked" and "SUPPLIER:TRIM-BD-01" in sig["location_refs"]


async def test_suez_closure(client: httpx.AsyncClient) -> None:
    (detail,) = await _replay(client, "suez-closure")
    exposed = _exposed(detail)
    lanes = {code: m for (t, code), m in exposed.items() if t == "lane"}
    assert lanes.get("SGN-HAM-SEA") == "lane_crosses" and lanes.get("SGN-GDN-SEA") == "lane_crosses"
    for alt in ("SGN-HAM-CAPE", "SGN-GDN-CAPE", "SGN-DE1-SA", "SGN-PL1-SA", "CGP-FXT-SEA"):
        assert alt not in lanes, alt
    event = detail["event"]
    assert event["anchor_signal_id"] is None and event["taxonomy_key"] == "LOG.chokepoint_closure"
    assert event["status"] == "confirmed" and len(detail["signals"]) == 3  # 2 outlets corroborate; duplicate dropped


async def test_hurricane_cape_route(client: httpx.AsyncClient, pool: asyncpg.Pool) -> None:
    """M8 NHC pack (124 §5): a lane-only exposure, 6500 → 8500 with a re-notify, never a draft case."""
    (detail,) = await _replay(client, "hurricane-cape-route")
    alert, exposed = detail["alert"], _exposed(detail)
    assert exposed == {("lane", code): "lane_crosses"
                       for code in ("CGP-FXT-SEA", "CGP-RTM-SEA", "SGN-HAM-CAPE", "SGN-GDN-CAPE")}
    assert alert["simulated"] is True and alert["severity"] == "critical" and alert["status"] == "new"
    assert detail["event"]["taxonomy_key"] == "NAT.storm" and detail["event"]["anchor_signal_id"]
    assert detail["event"]["severity_bp"] == 8500
    assert detail["signals"]
    for sig in detail["signals"]:  # the storm's advisories (versions of one signal)
        assert sig["source"]["connector_id"] == "demo.nhc" and sig["provenance"] == "simulated"
        assert sig["source"]["name"] == "NOAA National Hurricane Center (demo replay)"
    async with tx(pool, TENANT) as conn:
        changes = [(r["change"], r["action"]) for r in await conn.fetch("SELECT change, action FROM alert_change ORDER BY seq")]
    assert changes[:2] == [("raised", "alert"), ("renotified", "alert")]
    assert all(action != "alert_and_draft_case" for _, action in changes)
