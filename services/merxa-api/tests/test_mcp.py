"""MCP server over streamable HTTP (in-process ASGI) with locally signed realm tokens."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError

from helpers import autocommit, independent_hash, key, marco, preparer, reader
from merxa_api.mcp.server import READ_TOOLS, TOOL_ORDER, WRITE_TOOLS
from support import FakeKeelson, FakeRealm

ClientFactory = Callable[[str | None], Client]


async def test_unauthenticated_gets_401_with_resource_metadata(http: httpx.AsyncClient) -> None:
    r = await http.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert r.status_code == 401
    assert "resource_metadata" in r.headers["www-authenticate"]
    assert "/.well-known/oauth-protected-resource/mcp" in r.headers["www-authenticate"]


async def test_wrong_audience_rejected(mcp_client: ClientFactory, realm: FakeRealm) -> None:
    token = realm.token(aud="account")
    with pytest.raises(Exception):  # noqa: B017 - transport-level 401
        async with mcp_client(token) as c:
            await c.list_tools()


async def test_tool_listing_order_annotations_and_scopes(mcp_client: ClientFactory, realm: FakeRealm) -> None:
    async with mcp_client(reader(realm)) as c:
        tools = await c.list_tools()
    names = [t.name for t in tools]
    # Every authenticated principal sees the complete list in contract order: the Keelson
    # bootstrap snapshots tools/list with the *reader* and classifies writes by annotations
    # (55-engine-internals). Scopes are enforced per call (test_scope_denial_on_call).
    assert names == list(TOOL_ORDER)
    by_name = {t.name: t for t in tools}
    for t in tools:
        assert t.annotations is not None and t.annotations.open_world_hint is False
        assert t.output_schema and t.output_schema["type"] == "object"
        assert t.input_schema["type"] == "object"
        assert "Requires scope:" in (t.description or "")
    for name in READ_TOOLS:
        assert by_name[name].annotations.read_only_hint is True
    for name in WRITE_TOOLS:
        assert by_name[name].annotations.read_only_hint is False
        assert "idempotency_key" in by_name[name].input_schema["required"]
    assert by_name["search_entities"].input_schema["required"] == ["query"]
    apply = by_name["apply_change_set"]
    assert apply.annotations.destructive_hint is True and apply.annotations.idempotent_hint is True
    create = by_name["create_change_set"]
    assert create.annotations.destructive_hint is False and create.annotations.idempotent_hint is True

    # The listing (and therefore any definition hash a client keeps) does not depend on the token.
    for token in (autocommit(realm), realm.token(scopes=["catalog:read"]), preparer(realm)):
        async with mcp_client(token) as c:
            other = await c.list_tools()
        assert [t.name for t in other] == names
        assert [t.input_schema for t in other] == [t.input_schema for t in tools]


async def test_scope_denial_on_call(mcp_client: ClientFactory, realm: FakeRealm) -> None:
    async with mcp_client(reader(realm)) as c:
        with pytest.raises(ToolError, match=r"insufficient_scope: .*changes:preview"):
            await c.call_tool("create_change_set", {"title": "nope", "idempotency_key": key()})
        with pytest.raises(ToolError, match=r"insufficient_scope: .*changes:apply or changes:apply:auto"):
            await c.call_tool(
                "apply_change_set",
                {
                    "change_set_id": "CS-0001",
                    "preview_hash": "a" * 64,
                    "approval_ref": "APR-1",
                    "approval_receipt": "x",
                    "idempotency_key": key(),
                },
            )
    async with mcp_client(realm.token(scopes=["catalog:read"])) as c:
        with pytest.raises(ToolError, match=r"insufficient_scope: .*inventory:read"):
            await c.call_tool("get_inventory", {"location_codes": ["UK1"]})
        with pytest.raises(ToolError, match="insufficient_scope"):
            await c.call_tool("list_purchase_orders", {})
        assert (await c.call_tool("list_locations", {})).structured_content["dcs"]
    # The auto-commit principal can only apply; it can neither read nor prepare.
    async with mcp_client(autocommit(realm)) as c:
        with pytest.raises(ToolError, match="insufficient_scope"):
            await c.call_tool("get_change_set", {"change_set_id": "CS-0001"})
        with pytest.raises(ToolError, match="insufficient_scope"):
            await c.call_tool("validate_change_set", {"change_set_id": "CS-0001", "idempotency_key": key()})


async def test_read_tools_structured_content(mcp_client: ClientFactory, realm: FakeRealm, reseed: None) -> None:
    async with mcp_client(reader(realm)) as c:
        res = await c.call_tool("search_entities", {"query": "PUF-W-BLK", "types": ["style", "sku"]})
        assert res.structured_content["items"][0] == {
            "type": "style",
            "code": "PUF-W-BLK",
            "label": "Women's Longline Puffer Jacket — Black",
        }
        assert res.content[0].text.startswith("7 match(es)")

        loc = (await c.call_tool("list_locations", {})).structured_content
        assert [d["code"] for d in loc["dcs"]] == ["DE1", "NL1", "PL1", "UK1"]
        assert sum(cl["store_count"] for cl in loc["clusters"]) == 62

        po = (await c.call_tool("get_purchase_order", {"code": "PO-BD-0001"})).structured_content
        assert po["dest_dc"] == "UK1" and len(po["lines"]) == 30 and po["lines"][0]["version"] >= 1

        pos = (await c.call_tool("list_purchase_orders", {"factory_code": "F-BD-01"})).structured_content
        assert {p["code"] for p in pos["items"]} == {"PO-BD-0001", "PO-BD-0003", "PO-BD-0005"}

        bom = (await c.call_tool("get_bom_where_used", {"supplier_code": "TRIM-BD-01"})).structured_content
        assert {f["code"] for f in bom["factories"]} == {"F-BD-01", "F-BD-02"}
        assert {ln["po_code"] for ln in bom["open_po_lines"]} == {f"PO-BD-000{i}" for i in range(1, 7)}

        inv = (
            await c.call_tool("get_inventory", {"location_codes": ["UK1"], "style_code": "PUF-W-BLK"})
        ).structured_content
        assert len(inv["items"]) == 6 and all(
            i["atp"] == i["on_hand"] - i["allocated"] - i["reserved"] for i in inv["items"]
        )

        fc = (
            await c.call_tool(
                "get_demand_forecast",
                {"style_code": "PUF-W-BLK", "cluster_codes": ["UK1-COLD-A"], "weeks": ["2026-W45", "2026-W48"]},
            )
        ).structured_content
        assert len(fc["items"]) == 2 and len(fc["size_curves"]) == 6
        assert sum(sc["share_bp"] for sc in fc["size_curves"]) == 10000

        al = (await c.call_tool("list_allocations", {"cluster_code": "UK1-COLD-A", "limit": 5})).structured_content
        assert len(al["items"]) == 5 and al["next_cursor"]
        al2 = (
            await c.call_tool(
                "list_allocations", {"cluster_code": "UK1-COLD-A", "limit": 5, "cursor": al["next_cursor"]}
            )
        ).structured_content
        assert al2["items"][0]["code"] > al["items"][-1]["code"]

        msgs = (await c.call_tool("list_supplier_messages", {"supplier_code": "TRIM-BD-01"})).structured_content
        assert len(msgs["items"]) >= 1

        ent = (await c.call_tool("get_entity", {"type": "sku", "code": "PUF-W-BLK-M"})).structured_content
        assert ent["data"]["style_code"] == "PUF-W-BLK" and set(ent["data"]["dc_atp"]) == {"DE1", "NL1", "PL1", "UK1"}

        with pytest.raises(ToolError, match="not_found"):
            await c.call_tool("get_entity", {"type": "po", "code": "PO-NOPE"})


async def test_write_flow_over_mcp(
    mcp_client: ClientFactory, realm: FakeRealm, keelson: FakeKeelson, reseed: None
) -> None:
    async with mcp_client(preparer(realm)) as c:
        po = (await c.call_tool("get_purchase_order", {"code": "PO-BD-0004"})).structured_content
        line = po["lines"][4]
        k_create = key()
        created = (
            await c.call_tool(
                "create_change_set",
                {
                    "title": "BD delay remediation",
                    "idempotency_key": k_create,
                    "origin": {"system": "Keelson", "case_ref": "CASE-7", "plan_version": 3},
                },
            )
        ).structured_content
        assert created["status"] == "draft" and created["code"].startswith("CS-")
        again = (
            await c.call_tool(
                "create_change_set",
                {
                    "title": "BD delay remediation",
                    "idempotency_key": k_create,
                    "origin": {"system": "Keelson", "case_ref": "CASE-7", "plan_version": 3},
                },
            )
        ).structured_content
        assert again == created  # replay-safe

        items: list[dict[str, Any]] = [
            {
                "op": "po_line.reschedule",
                "target_ref": f"PO-BD-0004#{line['line_no']}",
                "expected_version": line["version"],
                "payload": {"eta_dc": "2026-11-05"},
            },
            {
                "op": "transfer_order.create",
                "target_ref": None,
                "expected_version": None,
                "payload": {
                    "from_dc": "DE1",
                    "to_dc": "NL1",
                    "ship_date": "2026-10-20",
                    "lines": [{"sku": line["sku"], "qty": 12}],
                },
            },
        ]
        added = (
            await c.call_tool(
                "add_change_set_items",
                {"change_set_id": created["change_set_id"], "items": items, "idempotency_key": key()},
            )
        ).structured_content
        assert added["items_count"] == 2
        val = await c.call_tool("validate_change_set", {"change_set_id": created["code"], "idempotency_key": key()})
        v = val.structured_content
        assert v["status"] == "validated" and v["preview_hash"] == independent_hash(items)
        assert v["totals"]["units_moved"] == 12 and "preview_hash" in val.content[0].text

        # The preparer cannot apply.
        with pytest.raises(ToolError):
            await c.call_tool(
                "apply_change_set",
                {
                    "change_set_id": created["change_set_id"],
                    "preview_hash": v["preview_hash"],
                    "approval_ref": "APR-0900",
                    "approval_receipt": "x",
                    "idempotency_key": key(),
                },
            )
        guide = (await c.call_tool("get_operator_guide", {"change_set_id": created["code"]})).structured_content
        assert len(guide["steps"]) == 2

    receipt = keelson.receipt(
        change_set_ref=created["change_set_id"], change_set_hash=v["preview_hash"], approval_id="APR-0900"
    )
    args = {
        "change_set_id": created["change_set_id"],
        "preview_hash": v["preview_hash"],
        "approval_ref": "APR-0900",
        "approval_receipt": receipt,
        "idempotency_key": key(),
    }
    async with mcp_client(marco(realm)) as c:
        with pytest.raises(ToolError, match="invalid_receipt"):
            bad = dict(
                args,
                approval_receipt=keelson.receipt(
                    change_set_ref=created["change_set_id"], change_set_hash="f" * 64, approval_id="APR-0900"
                ),
            )
            await c.call_tool("apply_change_set", bad)
        res = await c.call_tool("apply_change_set", args)
        assert res.structured_content["status"] == "applied" and res.structured_content["applied"] == 2
        replay = await c.call_tool("apply_change_set", args)
        assert replay.structured_content == res.structured_content

    async with mcp_client(reader(realm)) as c:
        cs = (await c.call_tool("get_change_set", {"change_set_id": created["code"]})).structured_content
        assert cs["status"] == "applied" and cs["via"] == "Keelson"
        trail = (await c.call_tool("get_audit_trail", {"change_set_code": created["code"]})).structured_content
        actions = [r["action"] for r in trail["items"]]
        assert actions[:3] == ["create", "add_items", "validate"] and "reschedule" in actions and "apply" in actions
        tos = (await c.call_tool("list_transfer_orders", {"from_dc": "DE1", "to_dc": "NL1"})).structured_content
        assert any(to["created_via"] == f"change_set:{created['code']}" for to in tos["items"])


async def test_autocommit_over_mcp_caps(
    mcp_client: ClientFactory, realm: FakeRealm, keelson: FakeKeelson, reseed: None
) -> None:
    async with mcp_client(preparer(realm)) as c:
        cs = (await c.call_tool("create_change_set", {"title": "auto", "idempotency_key": key()})).structured_content
        po = (await c.call_tool("get_purchase_order", {"code": "PO-BD-0005"})).structured_content
        ln = po["lines"][0]
        await c.call_tool(
            "add_change_set_items",
            {
                "change_set_id": cs["change_set_id"],
                "idempotency_key": key(),
                "items": [
                    {
                        "op": "po_line.change_qty",
                        "target_ref": f"PO-BD-0005#{ln['line_no']}",
                        "expected_version": ln["version"],
                        "payload": {"qty": ln["qty_ordered"] + 10},
                    }
                ],
            },
        )
        v = (
            await c.call_tool("validate_change_set", {"change_set_id": cs["change_set_id"], "idempotency_key": key()})
        ).structured_content
    receipt = keelson.receipt(
        change_set_ref=cs["change_set_id"],
        change_set_hash=v["preview_hash"],
        approval_id="APR-0950",
        approved_by="policy:P-12@3",
        ext_sub="svc-autocommit",
    )
    async with mcp_client(autocommit(realm)) as c:
        names = [t.name for t in await c.list_tools()]
        assert names == list(TOOL_ORDER)
        with pytest.raises(ToolError, match="auto_cap_exceeded"):
            await c.call_tool(
                "apply_change_set",
                {
                    "change_set_id": cs["change_set_id"],
                    "preview_hash": v["preview_hash"],
                    "approval_ref": "APR-0950",
                    "approval_receipt": receipt,
                    "idempotency_key": key(),
                },
            )
