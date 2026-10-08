"""Merxa API keys (mint/revoke/hash/show-once/scopes) and the /mcp-lite Tier-A profile."""

from __future__ import annotations

import hashlib
import json
from typing import Any

import httpx
import pytest
from fastmcp.exceptions import ToolError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from helpers import admin, auth, key, marco, preparer, reader
from merxa_api import tables as t
from merxa_api.mcp.lite import LITE_TOOL_ORDER
from support import READ, FakeRealm


async def _mint(
    http: httpx.AsyncClient, realm: FakeRealm, scopes: list[str], name: str = "erp-bridge"
) -> dict[str, Any]:
    r = await http.post("/v1/api-keys", json={"name": name, "scopes": scopes}, headers=auth(admin(realm), key()))
    assert r.status_code == 201, r.text
    return r.json()


async def test_api_key_lifecycle(http: httpx.AsyncClient, realm: FakeRealm, engine: AsyncEngine) -> None:
    adm = admin(realm)
    k = key()
    r = await http.post("/v1/api-keys", json={"name": "Northwind sync", "scopes": READ}, headers=auth(adm, k))
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    minted = r.json()
    raw = minted["key"]
    assert raw.startswith(f"mx_{minted['prefix']}_") and len(raw) == 3 + 8 + 1 + 32
    assert minted["key_shown_once"] is True and minted["scopes"] == READ
    # only the hash is stored
    async with engine.connect() as conn:
        row = (await conn.execute(select(t.api_key).where(t.api_key.c.id == minted["id"]))).one()
    assert row.sha256 == hashlib.sha256(raw.encode()).hexdigest()
    assert raw not in json.dumps({c: str(getattr(row, c)) for c in row._mapping})
    # replay of the mint returns the record without the key
    again = await http.post("/v1/api-keys", json={"name": "Northwind sync", "scopes": READ}, headers=auth(adm, k))
    assert again.status_code == 201 and again.headers["Idempotent-Replayed"] == "true"
    assert again.json()["key"] is None and again.json()["prefix"] == minted["prefix"]
    listed = (await http.get("/v1/api-keys", headers=auth(adm))).json()["items"]
    mine = next(i for i in listed if i["id"] == minted["id"])
    assert mine["status"] == "active" and "key" not in mine and "sha256" not in mine and mine["last_used_at"] is None
    # the key authenticates REST (header or Bearer) within its scopes
    r = await http.get("/v1/purchase-orders", headers={"X-Merxa-Api-Key": raw})
    assert r.status_code == 200 and r.json()["items"]
    r = await http.get("/v1/dcs", headers={"Authorization": f"Bearer {raw}"})
    assert r.status_code == 200
    r = await http.post(
        "/v1/change-sets", json={"title": "x"}, headers={"X-Merxa-Api-Key": raw, "Idempotency-Key": key()}
    )
    assert r.status_code == 403  # no changes:preview
    listed = (await http.get("/v1/api-keys", headers=auth(adm))).json()["items"]
    assert next(i for i in listed if i["id"] == minted["id"])["last_used_at"] is not None
    # wrong / malformed keys
    assert (await http.get("/v1/dcs", headers={"X-Merxa-Api-Key": raw[:-1] + "x"})).status_code == 401
    assert (await http.get("/v1/dcs", headers={"X-Merxa-Api-Key": "mx_nope"})).status_code == 401
    # revoke
    r = await http.delete(f"/v1/api-keys/{minted['id']}", headers=auth(adm))
    assert r.status_code == 200 and r.json()["status"] == "revoked" and r.json()["revoked_at"]
    assert (await http.get("/v1/dcs", headers={"X-Merxa-Api-Key": raw})).status_code == 401
    assert (await http.post(f"/v1/api-keys/{minted['id']}:revoke", headers=auth(adm))).status_code == 200  # idempotent
    # audit
    audit = (await http.get("/v1/audit-log", params={"entity_type": "api_key"}, headers=auth(adm))).json()["items"]
    assert {(a["entity_ref"], a["action"]) for a in audit} >= {
        (minted["prefix"], "create"),
        (minted["prefix"], "revoke"),
    }
    assert all(raw not in json.dumps(a) for a in audit)


async def test_api_key_admin_only_and_scope_validation(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    assert (await http.get("/v1/api-keys", headers=auth(reader(realm)))).status_code == 403
    r = await http.post("/v1/api-keys", json={"name": "x", "scopes": READ}, headers=auth(marco(realm), key()))
    assert r.status_code == 403
    for scopes in (["changes:apply:auto"], ["root"], []):
        r = await http.post("/v1/api-keys", json={"name": "x", "scopes": scopes}, headers=auth(admin(realm), key()))
        assert r.status_code in (400, 422), (scopes, r.text)
    r = await http.post("/v1/api-keys", json={"name": "x", "scopes": READ}, headers=auth(admin(realm)))
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"


async def test_mcp_lite_auth_both_ways_and_listing(lite_client: Any, http: httpx.AsyncClient, realm: FakeRealm) -> None:
    minted = await _mint(http, realm, ["catalog:read", "po:read"])
    # no credentials → 401
    r = await http.post(
        "/mcp-lite",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    with pytest.raises(Exception):  # noqa: B017 - transport-level 401
        async with lite_client(api_key="mx_aaaaaaaa_" + "b" * 32) as c:
            await c.list_tools()
    for kwargs in ({"api_key": minted["key"]}, {"token": minted["key"]}, {"token": reader(realm)}):
        async with lite_client(**kwargs) as c:
            tools = await c.list_tools()
            assert [tl.name for tl in tools] == list(LITE_TOOL_ORDER)
            by = {tl.name: tl for tl in tools}
            for name in ("list_items", "get_item"):
                ann = by[name].annotations
                assert ann is not None and ann.read_only_hint is True
                assert ann.destructive_hint is None and ann.idempotent_hint is None and ann.open_world_hint is None
            for name in ("update_item", "create_transfer_order"):
                assert by[name].annotations is None or by[name].annotations.read_only_hint is None
            assert "request_id" in by["update_item"].input_schema["properties"]
            assert "idempotency_key" not in by["update_item"].input_schema["properties"]
            page = (await c.call_tool("list_items", {"type": "purchase_order", "page_size": 5})).structured_content
            assert page["total"] >= 7 and len(page["items"]) == 5 and page["items"][0]["key"] == "PO-BD-0001"
    # the merxa-realm token must carry the MCP audience
    with pytest.raises(Exception):  # noqa: B017
        async with lite_client(token=realm.token(aud="account")) as c:
            await c.list_tools()


async def test_mcp_lite_crud_and_write_scope(
    lite_client: Any, http: httpx.AsyncClient, realm: FakeRealm, engine: AsyncEngine, reseed: None
) -> None:
    read_key = await _mint(http, realm, READ, "readonly")
    write_key = await _mint(http, realm, [*READ, "changes:apply"], "writer")
    async with lite_client(api_key=read_key["key"]) as c:
        page = (
            await c.call_tool(
                "list_items",
                {"type": "po_line", "filter_json": json.dumps({"po_code": "PO-BD-0002"}), "page": 2, "page_size": 10},
            )
        ).structured_content
        assert page["total"] == 30 and [i["line_no"] for i in page["items"]] == list(range(11, 21))
        inv = (await c.call_tool("get_item", {"type": "inventory", "key": "dc/UK1/PUF-W-BLK-M"})).structured_content
        assert inv["item"]["location_code"] == "UK1"
        fc = (
            await c.call_tool(
                "list_items", {"type": "forecast", "filter_json": '{"store_code": "ST-GB-01"}', "page_size": 3}
            )
        ).structured_content
        assert fc["items"][0]["key"].count("|") == 2
        with pytest.raises(ToolError, match="cannot filter"):
            await c.call_tool("list_items", {"type": "sku", "filter_json": '{"secret": 1}'})
        with pytest.raises(ToolError, match="unknown type"):
            await c.call_tool("list_items", {"type": "change_set"})
        with pytest.raises(ToolError, match="insufficient_scope"):
            await c.call_tool(
                "update_item",
                {"type": "po_line", "key": "PO-BD-0002#1", "fields_json": '{"qty_ordered": 5}', "expected_version": 1},
            )
    # bearer without ops-director cannot write either
    async with lite_client(token=preparer(realm)) as c:
        with pytest.raises(ToolError, match=r"insufficient_scope|forbidden"):
            await c.call_tool(
                "create_transfer_order",
                {
                    "from_dc": "DE1",
                    "to_dc": "NL1",
                    "ship_date": "2026-10-12",
                    "lines_json": '[{"sku":"PUF-W-BLK-M","qty":1}]',
                },
            )
    async with lite_client(api_key=write_key["key"]) as c:
        line = (await c.call_tool("get_item", {"type": "po_line", "key": "PO-BD-0002#1"})).structured_content["item"]
        args = {
            "type": "po_line",
            "key": "PO-BD-0002#1",
            "fields_json": json.dumps({"eta_dc": "2026-11-05", "qty_ordered": line["qty_ordered"] + 1}),
            "expected_version": line["version"],
            "request_id": "lite-req-1",
        }
        upd = (await c.call_tool("update_item", args)).structured_content
        assert upd["item"]["eta_dc"] == "2026-11-05" and upd["item"]["version"] == line["version"] + 1
        replay = (await c.call_tool("update_item", args)).structured_content
        assert replay["replayed"] is True and replay["item"] == upd["item"]
        with pytest.raises(ToolError, match="version_conflict"):
            await c.call_tool("update_item", {**args, "request_id": None})
        with pytest.raises(ToolError, match="not updatable"):
            await c.call_tool(
                "update_item",
                {"type": "po_line", "key": "PO-BD-0002#1", "fields_json": '{"sku_code": "X"}', "expected_version": 1},
            )
        with pytest.raises(ToolError, match="invalid_value"):
            await c.call_tool(
                "update_item",
                {
                    "type": "po_line",
                    "key": "PO-BD-0002#1",
                    "fields_json": '{"status": "lost"}',
                    "expected_version": upd["item"]["version"],
                },
            )
        to = (
            await c.call_tool(
                "create_transfer_order",
                {
                    "from_dc": "DE1",
                    "to_dc": "NL1",
                    "ship_date": "2026-10-12",
                    "lines_json": '[{"sku":"PUF-W-BLK-M","qty":4}]',
                    "request_id": "lite-to-1",
                },
            )
        ).structured_content
        assert to["code"].startswith("TO-") and to["status"] == "planned"
        with pytest.raises(ToolError, match="insufficient ATP"):
            await c.call_tool(
                "create_transfer_order",
                {
                    "from_dc": "DE1",
                    "to_dc": "NL1",
                    "ship_date": "2026-10-12",
                    "lines_json": '[{"sku":"PUF-W-BLK-M","qty":999999}]',
                },
            )
    tos = (await http.get("/v1/transfer-orders", params={"limit": 200}, headers=auth(reader(realm)))).json()["items"]
    assert next(x for x in tos if x["code"] == to["code"])["created_via"] == "mcp-lite"
    audit = (
        await http.get("/v1/audit-log", params={"entity_ref": "PO-BD-0002#1", "limit": 50}, headers=auth(reader(realm)))
    ).json()["items"]
    last = audit[-1]
    assert last["via"] == "MCP-lite" and last["actor_username"] == "writer" and last["client_id"].startswith("apikey:")
    async with engine.connect() as conn:
        ev = (
            await conn.execute(
                select(t.outbox.c.event_type, t.outbox.c.changes).order_by(t.outbox.c.seq.desc()).limit(3)
            )
        ).all()
    assert "po_line.eta_changed" in {e.event_type for e in ev}
    # ops-director bearer token may write through /mcp-lite too
    async with lite_client(token=marco(realm)) as c:
        made = (
            await c.call_tool(
                "create_transfer_order",
                {
                    "from_dc": "PL1",
                    "to_dc": "NL1",
                    "ship_date": "2026-10-13",
                    "lines_json": '[{"sku":"PUF-W-BLK-M","qty":2}]',
                },
            )
        ).structured_content
        assert made["status"] == "planned"
        with pytest.raises(ToolError, match="insufficient_scope"):  # Marco's token has no read scopes
            await c.call_tool("get_item", {"type": "allocation", "key": "AL-000001"})
    # revoked key → refused
    await http.delete(f"/v1/api-keys/{write_key['id']}", headers=auth(admin(realm)))
    with pytest.raises(Exception):  # noqa: B017
        async with lite_client(api_key=write_key["key"]) as c:
            await c.list_tools()


async def test_oauth_clients_last_seen(http: httpx.AsyncClient, realm: FakeRealm, state: Any) -> None:
    tok = realm.token(sub="svc-x", azp="keelson-reader")
    assert (await http.get("/v1/dcs", headers=auth(tok))).status_code == 200
    await state.clients_seen.drain()
    items = (await http.get("/v1/oauth-clients", headers=auth(admin(realm)))).json()["items"]
    assert any(i["client_id"] == "keelson-reader" and i["last_seen_at"] for i in items)
    assert (await http.get("/v1/oauth-clients", headers=auth(reader(realm)))).status_code == 403
