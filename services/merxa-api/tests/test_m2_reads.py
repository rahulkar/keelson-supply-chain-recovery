"""M2 reads: inventory / forecast aggregates, logistics, production, online orders, promotions,
events — over REST and MCP — and the port-closure demo inject."""

from __future__ import annotations

from collections import defaultdict
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError

from helpers import admin, auth, key, reader
from support import FakeRealm


async def _all(http: httpx.AsyncClient, token: str, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    cursor = None
    while True:
        p = {**params, "limit": 1000, **({"cursor": cursor} if cursor else {})}
        r = await http.get(path, params=p, headers=auth(token))
        assert r.status_code == 200, r.text
        body = r.json()
        items.extend(body["items"])
        cursor = body["next_cursor"]
        if not cursor:
            return items


async def test_inventory_aggregates_equal_sums_of_store_rows(
    http: httpx.AsyncClient, realm: FakeRealm, reseed: None
) -> None:
    tok = reader(realm)
    stores = {s["code"]: s for s in await _all(http, tok, "/v1/stores", {})}
    raw = await _all(http, tok, "/v1/inventory-positions", {"location_type": "store"})
    expect: dict[tuple[str, str, str], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in raw:
        st = stores[row["location_code"]]
        for level, code in (("cluster", st["cluster"]), ("dc_stores", st["home_dc"])):
            acc = expect[(level, code, row["sku"])]
            for f in ("on_hand", "allocated", "reserved", "in_transit", "atp"):
                acc[f] += row[f]
            acc["store_count"] += 1
    for aggregate, level in (("cluster", "cluster"), ("dc", "dc_stores")):
        rows = await _all(http, tok, "/v1/inventory-positions", {"aggregate": aggregate})
        agg = [r for r in rows if r["location_type"] == level]
        dcs = [r for r in rows if r["location_type"] == "dc"]
        assert len(dcs) == 4 * 30 and all("version" in r for r in dcs)
        assert len(agg) == len({k for k in expect if k[0] == level})
        for r in agg:
            want = expect[(level, r["location_code"], r["sku"])]
            assert {f: r[f] for f in want} == dict(want), r
            assert "version" not in r
        # store-level only
        only = await _all(http, tok, "/v1/inventory-positions", {"aggregate": aggregate, "location_type": "store"})
        assert {r["location_type"] for r in only} == {level}
    r = await http.get("/v1/inventory-positions", params={"aggregate": "region"}, headers=auth(tok))
    assert r.status_code == 400


async def test_forecast_cluster_aggregate_equals_store_sums(
    http: httpx.AsyncClient, realm: FakeRealm, mcp_client: Any, reseed: None
) -> None:
    tok = reader(realm)
    store_rows = await _all(http, tok, "/v1/demand-forecast", {"aggregate": "store"})
    assert store_rows and all(r["store_code"] for r in store_rows)
    sums: dict[tuple[str, str, str], int] = defaultdict(int)
    for r in store_rows:
        sums[(r["style_code"], r["cluster_code"], r["iso_week"])] += r["units"]
    cluster_rows = await _all(http, tok, "/v1/demand-forecast", {})
    assert {(r["style_code"], r["cluster_code"], r["iso_week"]): r["units"] for r in cluster_rows} == dict(sums)
    async with mcp_client(tok) as c:
        fc = (
            await c.call_tool("get_demand_forecast", {"style_code": "PUF-W-BLK", "cluster_codes": ["UK1-COLD-A"]})
        ).structured_content
        assert fc["aggregate"] == "cluster" and len(fc["items"]) == 9 and fc["next_cursor"] is None
        for row in fc["items"]:
            assert row["units"] == sums[("PUF-W-BLK", "UK1-COLD-A", row["iso_week"])]
        per_store = (
            await c.call_tool(
                "get_demand_forecast",
                {"style_code": "PUF-W-BLK", "cluster_codes": ["UK1-COLD-A"], "aggregate": "store", "limit": 5},
            )
        ).structured_content
        assert len(per_store["items"]) == 5 and per_store["next_cursor"]
        inv = (
            await c.call_tool(
                "get_inventory", {"aggregate": "cluster", "location_type": "store", "sku_codes": ["PUF-W-BLK-M"]}
            )
        ).structured_content
        assert {r["location_type"] for r in inv["items"]} == {"cluster"}
        assert all(r["store_count"] > 0 for r in inv["items"])


async def test_logistics_reads(http: httpx.AsyncClient, realm: FakeRealm, mcp_client: Any, reseed: None) -> None:
    tok = reader(realm)
    ports = (await http.get("/v1/ports", headers=auth(tok))).json()["items"]
    assert {"BDCGP", "VNSGN", "AEJEA", "GBFXT", "NLRTM", "DEHAM", "PLGDN", "TRIST", "MATNG", "LKCMB", "KHPNH"} == {
        p["unlocode"] for p in ports
    }
    rc = (await http.get("/v1/rate-cards", params={"lane_code": "CGP-UK1-SA"}, headers=auth(tok))).json()["items"]
    assert [(r["valid_from"], r["valid_to"]) for r in rc] == [
        ("2026-07-01", "2026-10-31"),
        ("2026-11-01", "2027-02-28"),
    ]
    assert rc[1]["rate_cents"] > rc[0]["rate_cents"]
    async with mcp_client(tok) as c:
        sail = (await c.call_tool("list_sailings", {"status": "scheduled"})).structured_content
        assert [s["code"] for s in sail["items"]] == ["SAIL-0001", "SAIL-0002", "SAIL-0003", "SAIL-0004"]
        assert sail["items"][0]["lane_code"] == "CGP-FXT-SEA"
        shp = (await c.call_tool("list_shipments", {"po_code": "PO-BD-0001"})).structured_content
        assert shp["items"][0]["mode"] == "sea_air" and shp["items"][0]["booking_code"].startswith("CB-")
        assert len(shp["items"][0]["lines"]) == 30
        opts = (
            await c.call_tool("get_lane_options", {"origin": "BDCGP", "dest": "UK1", "week": "2026-W43"})
        ).structured_content
        by_lane = {o["lane_code"]: o for o in opts["options"]}
        # direct sea-air, sea around the Cape + truck from Felixstowe, sea-air to NL1 + DC transfer
        assert set(by_lane) == {"CGP-UK1-SA", "CGP-FXT-SEA", "CGP-NL1-SA"}
        assert [o["lane_code"] for o in opts["options"]] == ["CGP-UK1-SA", "CGP-NL1-SA", "CGP-FXT-SEA"]
        assert [leg["lane_code"] for leg in by_lane["CGP-NL1-SA"]["legs"]] == ["CGP-NL1-SA", "NL1-UK1-RD"]
        sa, sea = by_lane["CGP-UK1-SA"], by_lane["CGP-FXT-SEA"]
        assert sa["transit_days"] == 16 and sa["via"] == ["AEJEA"] and sa["alt_lane_code"] == "CGP-FXT-SEA"
        assert sea["transit_days"] == 33 and sea["via"] == ["LKCMB", "CAPE", "GBFXT"] and sea["basis"] == "per_teu"
        booked = sum(
            ln["qty"]
            for s in (await c.call_tool("list_shipments", {"po_code": "PO-BD-0002"})).structured_content["items"]
            for ln in s["lines"]
        )
        cap = next(r for r in rc if r["valid_from"] == "2026-07-01")["weekly_capacity"]
        assert sa["remaining_capacity_week"] == cap - booked  # PO-BD-0002 is booked on W43
        with pytest.raises(ToolError, match="invalid_parameter"):
            await c.call_tool("get_lane_options", {"origin": "BDCGP", "dest": "UK1", "week": "W43"})


async def test_production_orders_and_promotions(
    http: httpx.AsyncClient, realm: FakeRealm, mcp_client: Any, reseed: None
) -> None:
    tok = reader(realm)
    async with mcp_client(tok) as c:
        sched = (await c.call_tool("get_factory_schedule", {"factory_code": "F-BD-01"})).structured_content
        assert [ln["code"] for ln in sched["lines"]] == ["FL-BD01-A", "FL-BD01-B"]
        line = sched["lines"][0]
        days = {d["day"]: d for d in line["calendar"]}
        assert days["2026-10-09"]["status"] == "closed"  # Friday weekend in Bangladesh
        assert days["2026-10-20"]["status"] == "closed"  # Durga Puja
        assert {o["status"] for o in line["orders"]} <= {"planned", "running", "done", "held"}
        seqs = [o["seq"] for o in line["orders"]]
        assert seqs == sorted(seqs)
        sched2 = (
            await c.call_tool(
                "get_factory_schedule", {"factory_code": "F-BD-01", "from": "2026-10-01", "to": "2026-10-03"}
            )
        ).structured_content
        assert all("2026-10-01" <= d["day"] <= "2026-10-03" for d in sched2["lines"][0]["calendar"])
        with pytest.raises(ToolError, match="not_found"):
            await c.call_tool("get_factory_schedule", {"factory_code": "F-XX-99"})
        promos = (await c.call_tool("list_promotions", {"active_on": "2026-11-27"})).structured_content
        assert [p["code"] for p in promos["items"]] == ["PROMO-BF26"]
        assert promos["items"][0]["period_start"] == "2026-11-23" and promos["items"][0]["period_end"] == "2026-11-29"
        risk = (await c.call_tool("list_online_orders_at_risk", {})).structured_content
        assert risk["items"] and all(o["at_risk"] and o["shortfall"] > 0 for o in risk["items"])
        all_orders = (await http.get("/v1/online-orders", params={"limit": 200}, headers=auth(tok))).json()["items"]
        assert len(all_orders) == 80
        assert {o["code"] for o in all_orders if o["shortfall"] > 0} == {o["code"] for o in risk["items"]}
    r = await http.get("/v1/production-orders", params={"po_code": "PO-BD-0001", "limit": 1000}, headers=auth(tok))
    assert len(r.json()["items"]) == 30
    r = await http.get("/v1/sales-history", params={"style_code": "PUF-W-BLK", "limit": 1000}, headers=auth(tok))
    assert len(r.json()["items"]) == 62 * 8


async def test_scopes_on_m2_reads(http: httpx.AsyncClient, realm: FakeRealm, mcp_client: Any) -> None:
    only_catalog = realm.token(scopes=["catalog:read"])
    for path in ("/v1/sailings", "/v1/shipments", "/v1/events", "/v1/online-orders", "/v1/changes"):
        assert (await http.get(path, headers=auth(only_catalog))).status_code == 403, path
    assert (await http.get("/v1/promotions", headers=auth(only_catalog))).status_code == 200
    async with mcp_client(only_catalog) as c:
        with pytest.raises(ToolError, match="insufficient_scope"):
            await c.call_tool("list_sailings", {})
        with pytest.raises(ToolError, match="insufficient_scope"):
            await c.call_tool("scn.snapshot_open", {"entities": ["sailing"]})


async def test_events_feed_marks_untrusted_text(
    http: httpx.AsyncClient, realm: FakeRealm, mcp_client: Any, reseed: None
) -> None:
    tok = reader(realm)
    head = (await http.get("/v1/events", params={"limit": 500}, headers=auth(tok))).json()
    while head["has_more"]:
        head = (
            await http.get("/v1/events", params={"after_cursor": head["next_cursor"], "limit": 500}, headers=auth(tok))
        ).json()
    cursor = head["next_cursor"]
    adm = admin(realm)
    r = await http.post("/v1/admin/scenario:inject", json={"kind": "trim_delay"}, headers=auth(adm, key()))
    assert r.status_code == 200, r.text
    r = await http.post(
        "/v1/admin/scenario:inject",
        json={
            "kind": "inventory_adjust",
            "payload": {"location_code": "UK1", "sku": "PUF-W-BLK-M", "delta_on_hand": 3},
        },
        headers=auth(adm, key()),
    )
    assert r.status_code == 200, r.text
    async with mcp_client(tok) as c:
        feed = (await c.call_tool("list_events", {"after_cursor": cursor})).structured_content
    types = [e["type"] for e in feed["items"]]
    assert types == ["supplier_message.received", "inventory.adjusted"]
    msg, inv = feed["items"]
    assert msg["content_trust"] == "untrusted_text" and msg["untrusted_fields"] == ["data.subject", "data.body"]
    assert "delayed 7 days" in msg["data"]["subject"]
    assert inv["data"]["delta"] == 3
    # the demo inject's reason is caller free text (defaulted here): flagged like message text (M4)
    assert inv["content_trust"] == "untrusted_text" and inv["untrusted_fields"] == ["data.reason"]
    assert feed["items"][1]["seq"] == feed["items"][0]["seq"] + 1
    assert feed["next_cursor"] == f"ox:{inv['seq']}"
    r = await http.get("/v1/events", params={"after_cursor": "nope"}, headers=auth(tok))
    assert r.status_code == 400


async def test_port_closure_inject_delays_sailings_and_po_lines(
    http: httpx.AsyncClient, realm: FakeRealm, reseed: None
) -> None:
    tok, adm = reader(realm), admin(realm)
    before = (await http.get("/v1/purchase-orders/PO-BD-0003", headers=auth(tok))).json()
    r = await http.post(
        "/v1/admin/scenario:inject",
        json={"kind": "port_closure", "payload": {"unlocode": "BDCGP", "days": 7}},
        headers=auth(adm, key()),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["sailings"] == ["SAIL-0001", "SAIL-0002"] and body["po_lines"] == 60
    after = (await http.get("/v1/purchase-orders/PO-BD-0003", headers=auth(tok))).json()
    assert after["eta_dc"] == "2026-11-17" and before["eta_dc"] == "2026-11-10"
    assert all(ln["eta_dc"] == "2026-11-17" for ln in after["lines"])
    # sea-air waves are not on the closed sailings
    po1 = (await http.get("/v1/purchase-orders/PO-BD-0001", headers=auth(tok))).json()
    assert po1["eta_dc"] == "2026-10-29"
    sail = (await http.get("/v1/sailings", params={"lane_code": "CGP-FXT-SEA"}, headers=auth(tok))).json()["items"][0]
    assert sail["etd"] == "2026-10-15" and sail["eta"] == "2026-11-16"
    feed = (await http.get("/v1/events", params={"type": "sailing.updated", "limit": 500}, headers=auth(tok))).json()
    assert {e["data"]["code"] for e in feed["items"][-2:]} == {"SAIL-0001", "SAIL-0002"}
    eta = (await http.get("/v1/events", params={"type": "po_line.eta_changed", "limit": 500}, headers=auth(tok))).json()
    assert len(eta["items"][-1]["data"]["lines"]) == 60
    # typhoon = closure of VNSGN + the supplier message (small has no VN POs: no lines move)
    r = await http.post("/v1/admin/scenario:inject", json={"kind": "typhoon"}, headers=auth(adm, key()))
    assert r.status_code == 200, r.text
    assert r.json()["sailings"] == ["SAIL-0003", "SAIL-0004"] and r.json()["po_lines"] == 0
    assert r.json()["message"] is None  # the Vietnamese supplier only exists in the full profile
