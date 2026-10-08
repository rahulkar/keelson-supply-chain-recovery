"""M7 Signals admin proxy (sources + cadence, watch regions, alert rules → the Signal Hub with the
service JWT), the cross-source audit timeline + audit categories, and the evals listing
(docs/contracts/110-m7-backoffice-generic.md §5, §6)."""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
import respx
from pydantic import SecretStr
from sqlalchemy import text

from e1_browser import Browser
from e1_env import TENANT_A, TENANT_B
from e1_signal_hub import HUB, HUB_SECRET, FakeHub
from keelson_engine.config import Settings
from keelson_engine.core import network, signals_admin
from keelson_engine.core.db import system_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

SIG = "/api/admin/signals"


@pytest_asyncio.fixture(loop_scope="session")
async def hub(platform: None, settings: Settings, http_mock: respx.Router,
              monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[FakeHub]:
    monkeypatch.setattr(settings, "signal_hub_url", HUB)
    monkeypatch.setattr(settings, "signal_hub_shared_secret", SecretStr(HUB_SECRET))
    fake = FakeHub()
    fake.install(http_mock)
    async with system_session() as s:
        await s.execute(text("UPDATE tenant SET config = config - 'signal_sources' - 'watch_regions' - 'alert_rules' "
                             "WHERE slug = ANY(:t)"), {"t": [TENANT_A, TENANT_B]})
    yield fake


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any, session_browser: Any, platform: None) -> dict[str, Browser]:
    out = {n: await browser_factory(n) for n in ("sam", "ava", "priya")}
    out["nadia"] = await session_browser("nadia", ["tenant-admin"], [TENANT_B])
    return out


@pytest.fixture
def network_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    nodes = [SimpleNamespace(type="port", code="VNSGN"), SimpleNamespace(type="dc", code="DE1"),
             SimpleNamespace(type="store", code="DE1"), SimpleNamespace(type="supplier", code="SUP-VN-01")]

    async def fake_network(tenant: str, lane_ends: Any = None) -> Any:
        return SimpleNamespace(nodes=nodes, lanes=[])

    monkeypatch.setattr(network, "build_tenant_network", fake_network)


POLY = [[106.6, 10.6], [106.9, 10.6], [106.9, 10.9], [106.6, 10.9]]


# ───────────────────────────── sources ─────────────────────────────


async def test_sources_and_cadence(people: dict[str, Browser], hub: FakeHub) -> None:
    sam, priya = people["sam"], people["priya"]
    r = await sam.get(f"{SIG}/sources")
    assert r.status_code == 200, r.text
    items = {s["id"]: s for s in r.json()["items"]}
    assert set(items) == {"gdacs", "news.rss"} and r.json()["hub_error"] is None
    g = items["gdacs"]
    assert g["enabled"] is True and g["licence"] == "CC BY 4.0" and g["name"] == "GDACS disaster alerts"
    assert g["last_status"] == "ok" and g["items_total"] == 7 and g["effective_cadence_s"] == 900
    assert (await priya.get(f"{SIG}/sources")).status_code == 403  # admins only

    r = await sam.patch(f"{SIG}/sources/gdacs", json={"enabled": False})
    assert r.status_code == 200 and r.json() == {"id": "gdacs", "enabled": False, "cadence_s": None,
                                                  "hub_synced": True, "hub_error": None}, r.text
    assert hub.subscriptions[TENANT_A] == [{"connector_id": "gdacs", "enabled": False, "cadence_s": None}]
    r = await sam.put(f"{SIG}/sources/gdacs", json={"cadence_s": 3600})
    assert r.json()["enabled"] is False and r.json()["cadence_s"] == 3600  # enabled kept
    assert hub.subscriptions[TENANT_A] == [{"connector_id": "gdacs", "enabled": False, "cadence_s": 3600}]
    items = {s["id"]: s for s in (await sam.get(f"{SIG}/sources")).json()["items"]}
    assert items["gdacs"]["enabled"] is False and items["gdacs"]["cadence_s"] == 3600
    r = await sam.patch(f"{SIG}/sources/gdacs", json={"cadence_s": None})
    assert r.json()["cadence_s"] is None
    for bad in ({}, {"cadence_s": 5}, {"cadence_s": 10**6}, {"enabled": "yes"}, {"enabled": None}, {"x": 1}):
        assert (await sam.patch(f"{SIG}/sources/gdacs", json=bad)).status_code == 422, bad
    assert (await sam.patch(f"{SIG}/sources/Bad%20Id", json={"enabled": True})).status_code == 404
    # tenant-scoped: northwind's subscriptions are its own
    assert TENANT_B not in hub.subscriptions
    assert all(t == TENANT_A for m, p, t in hub.requests if "subscriptions" in p)
    # an older hub (no per-tenant source list) and a hub outage
    hub.tenant_sources = False
    old = {s["id"]: s for s in (await sam.get(f"{SIG}/sources")).json()["items"]}
    assert old["gdacs"]["enabled"] is False and old["gdacs"]["last_status"] == "ok"
    hub.down = True
    down = (await sam.get(f"{SIG}/sources")).json()
    assert down["hub_error"] and [s["id"] for s in down["items"]] == ["gdacs"]  # the stored subscription
    r = await sam.patch(f"{SIG}/sources/gdacs", json={"enabled": True})
    assert r.status_code == 200 and r.json()["hub_synced"] is False and r.json()["hub_error"]
    hub.down = False
    audit = (await sam.get("/api/admin/audit?action=signals.source")).json()["items"]
    assert audit and audit[0]["category"] == "signals"


# ───────────────────────────── watch regions ─────────────────────────────


async def test_watch_regions(people: dict[str, Browser], hub: FakeHub, network_nodes: None) -> None:
    sam = people["sam"]
    assert (await sam.get(f"{SIG}/network")).json() == {"nodes": {"dc": ["DE1"], "port": ["VNSGN"],
                                                                  "store": ["DE1"], "supplier": ["SUP-VN-01"]}}
    regions = [
        # the web's shape: polygon as an open outer ring
        {"id": "hcmc", "name": "Ho Chi Minh City", "kind": "polygon", "polygon": POLY},
        # engine / GeoJSON shape with node {type, code}
        {"id": "port-sgn", "name": "Around Cat Lai", "kind": "radius", "node": {"type": "port", "code": "VNSGN"},
         "radius_km": 150},
        # flat node fields; node_type inferred from the network (one node has this code)
        {"id": "sup", "name": "Supplier VN", "kind": "radius", "node_code": "SUP-VN-01", "radius_km": 50},
    ]
    r = await sam.put(f"{SIG}/watch-regions", json={"regions": regions})
    assert r.status_code == 200 and r.json()["hub_synced"] is True, r.text
    out = {x["id"]: x for x in r.json()["items"]}
    assert out["hcmc"]["geometry"]["type"] == "Polygon" and out["hcmc"]["polygon"] == [*POLY, POLY[0]]
    assert out["port-sgn"]["node_type"] == "port" and out["port-sgn"]["node"] == {"type": "port", "code": "VNSGN"}
    assert out["sup"]["node_type"] == "supplier"
    pushed = {x["id"]: x for x in hub.regions[TENANT_A]}
    assert pushed["hcmc"]["geometry"]["coordinates"][0][-1] == POLY[0] and "polygon" not in pushed["hcmc"]
    assert pushed["port-sgn"]["node_code"] == "VNSGN" and "node" not in pushed["port-sgn"]
    got = {x["id"]: x for x in (await sam.get(f"{SIG}/watch-regions")).json()["items"]}
    assert got == out
    # round trip: the GET answer can be PUT back unchanged
    again = await sam.put(f"{SIG}/watch-regions", json={"regions": list(got.values())})
    assert again.status_code == 200, again.text
    # ambiguous / unknown node code, invalid shapes, duplicates
    for bad in (
        [{"id": "dc", "name": "DE1", "kind": "radius", "node_code": "DE1", "radius_km": 10}],  # dc and store
        [{"id": "x", "name": "X", "kind": "radius", "node_code": "NOPE", "radius_km": 10}],
        [{"id": "x", "name": "X", "kind": "polygon"}],
        [{"id": "x", "name": "X", "kind": "radius", "node": {"type": "port", "code": "VNSGN"}}],
        [{"id": "x", "name": "X", "kind": "polygon", "polygon": [[1, 2], [3, 4]]}],
        [{"id": "x", "name": "X\nY", "kind": "polygon", "polygon": POLY}],
        [{"id": "Bad Id", "name": "X", "kind": "polygon", "polygon": POLY}],
        [{"id": "x", "name": "X", "kind": "polygon", "polygon": POLY, "geometry": {"type": "Polygon",
                                                                                   "coordinates": [POLY]}}],
        [regions[0], regions[0]],
    ):
        r = await sam.put(f"{SIG}/watch-regions", json={"regions": bad})
        assert r.status_code == 422, (bad, r.text)
    assert len(hub.regions[TENANT_A]) == 3  # nothing pushed for invalid input
    # without a hub the settings are kept and pushed later
    hub.down = True
    r = await sam.put(f"{SIG}/watch-regions", json={"regions": regions[:1]})
    assert r.status_code == 200 and r.json()["hub_synced"] is False
    hub.down = False
    pushed = await signals_admin.push_settings(TENANT_A)
    assert pushed == {"subscriptions": True, "watch_regions": True} and len(hub.regions[TENANT_A]) == 1
    r = await sam.put(f"{SIG}/watch-regions", json={"regions": []})
    assert r.status_code == 200 and r.json()["items"] == []


async def test_alert_rules_proxy(people: dict[str, Browser], hub: FakeHub) -> None:
    sam = people["sam"]
    got = (await sam.get(f"{SIG}/alert-rules")).json()
    assert got["custom"] is False and got["rules"] == got["defaults"]
    rule = dict(got["defaults"][0])
    rule["id"] = "custom-1"
    r = await sam.put(f"{SIG}/alert-rules", json={"rules": [rule]})
    assert r.status_code == 200 and r.json()["custom"] is True and r.json()["hub_synced"] is True, r.text
    assert [x["id"] for x in hub.rules[TENANT_A]] == ["custom-1"]
    assert (await sam.put(f"{SIG}/alert-rules", json={"rules": [rule, rule]})).status_code == 422
    assert (await sam.put(f"{SIG}/alert-rules", json={"rules": []})).json()["custom"] is False


# ───────────────────────────── audit timeline ─────────────────────────────


async def test_audit_activity_filters(people: dict[str, Browser], hub: FakeHub) -> None:
    sam = people["sam"]
    await sam.patch(f"{SIG}/sources/news.rss", json={"enabled": False})
    r = await sam.get("/api/admin/audit/activity?limit=200")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["categories"] == ["runs", "approvals", "change_sets", "admin", "signals"]
    ats = [x["at"] for x in body["items"]]
    assert ats == sorted(ats, reverse=True)
    assert any(x["category"] == "admin" and x["action"] == "signals.source" for x in body["items"])
    resp = await sam.get("/api/admin/audit/activity?category=admin&actor=sam&limit=5")
    assert resp.status_code == 200, resp.text
    admin_only = resp.json()
    assert admin_only["categories"] == ["admin"] and all(x["category"] == "admin" and x["actor"] == "sam"
                                                         for x in admin_only["items"])
    # cursor pagination: no duplicates, no gaps
    seen: list[str] = []
    cursor = None
    for _ in range(50):
        url = "/api/admin/audit/activity?category=admin&limit=3" + (f"&before={cursor}" if cursor else "")
        page = (await sam.get(url)).json()
        seen += [x["key"] for x in page["items"]]
        cursor = page["next_before"]
        if not cursor:
            break
    full = [x["key"] for x in (await sam.get("/api/admin/audit/activity?category=admin&limit=200")).json()["items"]]
    assert seen == full[: len(seen)] and len(seen) == len(set(seen)) and len(seen) == min(len(full), 150)
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).isoformat().replace("+", "%2B")
    assert (await sam.get(f"/api/admin/audit/activity?since={future}")).json()["items"] == []
    assert (await sam.get("/api/admin/audit/activity?since=2026-10-01T00:00:00")).status_code == 422  # naive
    assert (await sam.get("/api/admin/audit/activity?before=garbage")).status_code == 422
    assert (await sam.get("/api/admin/audit/activity?category=nope")).status_code == 422
    # runs / approvals / change sets / signals SQL runs against the real schema for each category
    for cat in ("runs", "approvals", "change_sets", "signals"):
        assert (await sam.get(f"/api/admin/audit/activity?category={cat}")).status_code == 200, cat
    # the admin_audit page: category filter + tag
    sig = (await sam.get("/api/admin/audit?category=signals&limit=200")).json()["items"]
    assert sig and all(x["category"] == "signals" for x in sig)
    adm = (await sam.get("/api/admin/audit?category=admin&limit=200")).json()["items"]
    assert all(x["category"] == "admin" for x in adm)
    assert (await sam.get("/api/admin/audit?category=bogus")).status_code == 422
    # tenant isolation
    nadia = people["nadia"]
    assert all(x["actor"] != "sam" for x in (await nadia.get("/api/admin/audit/activity")).json()["items"])


# ───────────────────────────── evals ─────────────────────────────


def _doc(started: str, passed: int) -> dict[str, Any]:
    return {"schema": "keelson-evals/1", "started_at": started, "finished_at": started,
            "summary": {"cases": 2, "passed": passed, "failed": 2 - passed, "errors": 0, "note": "text"},
            "cases": [{"id": "trim-delay", "title": "Trim delay", "tags": ["m4"], "status": "pass", "duration_s": 41.2,
                       "turns": [{"checks": [{"ok": True}, {"ok": False, "soft": True}]}],
                       "checks": [{"ok": False}], "metrics": {"entity_recall": 0.9, "tokens": 1200, "x": "y"}},
                      "not a case"]}


async def test_evals_listing(people: dict[str, Browser], settings: Settings, monkeypatch: pytest.MonkeyPatch,
                             tmp_path: Path) -> None:
    sam, ava = people["sam"], people["ava"]
    monkeypatch.setattr(settings, "evals_results_dir", tmp_path / "missing")
    assert (await ava.get("/api/admin/evals")).json() == {"items": [], "directory_available": False}
    monkeypatch.setattr(settings, "evals_results_dir", tmp_path)
    (tmp_path / "20261007T120000Z.json").write_text(json.dumps(_doc("2026-10-07T12:00:00Z", 1)), encoding="utf-8")
    (tmp_path / "20261008T090000Z.json").write_text(json.dumps(_doc("2026-10-08T09:00:00Z", 2)), encoding="utf-8")
    (tmp_path / "20261006T000000Z.json").write_text("{not json", encoding="utf-8")
    (tmp_path / "20261008T090000Z.md").write_text("# summary", encoding="utf-8")
    (tmp_path / "notes.json").write_text("{}", encoding="utf-8")
    assert (await sam.get("/api/admin/evals")).status_code == 403  # platform-wide results: platform admins
    body = (await ava.get("/api/admin/evals")).json()
    assert body["directory_available"] is True
    assert [x["run"] for x in body["items"]] == ["20261008T090000Z", "20261007T120000Z", "20261006T000000Z"]
    latest = body["items"][0]
    assert latest["summary"] == {"cases": 2, "passed": 2, "failed": 0, "errors": 0} and latest["error"] is None
    case = latest["cases"][0]
    assert case == {"id": "trim-delay", "title": "Trim delay", "tags": ["m4"], "status": "pass", "duration_s": 41.2,
                    "checks": 3, "checks_failed": 1, "scores": {"entity_recall": 0.9, "tokens": 1200}}
    assert len(latest["cases"]) == 1
    assert body["items"][2]["error"].startswith("unreadable")
    assert [x["run"] for x in (await ava.get("/api/admin/evals?limit=1")).json()["items"]] == ["20261008T090000Z"]
