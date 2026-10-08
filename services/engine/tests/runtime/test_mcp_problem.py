"""MCP client (structuredContent, error codes, principals, transport errors) and ProblemData."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fakes.core import LANES
from fakes.harness import Harness
from keelson_contracts.optimizer import ProblemData
from mcp import types as mcp_types

from keelson_engine.runtime.errors import McpToolError, McpTransportError
from keelson_engine.runtime.mcp_client import (
    McpGateway,
    UserPrincipal,
    parse_tool_error,
    proxied_transport_factory,
    structured_result,
)
from keelson_engine.runtime.problem import gather_problem_data


@pytest.mark.parametrize(
    ("text", "code", "detail"),
    [
        ("insufficient_scope: this tool requires scope changes:preview", "insufficient_scope",
         "this tool requires scope changes:preview"),
        ("auto_cap_exceeded: 51 items > 50", "auto_cap_exceeded", "51 items > 50"),
        ("Error calling tool 'apply_change_set': preview_hash_mismatch: differs", "preview_hash_mismatch", "differs"),
        ("something odd happened", "tool_error", "something odd happened"),
    ],
)
def test_parse_tool_error(text: str, code: str, detail: str) -> None:
    assert parse_tool_error(text) == (code, detail)


def test_structured_result_and_errors() -> None:
    ok = mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="1 PO")], structured_content={"items": []})
    assert structured_result("t", ok) == {"items": []}
    err = mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="receipt_jwks_unavailable: retry")], is_error=True)
    with pytest.raises(McpToolError) as info:
        structured_result("apply_change_set", err)
    assert info.value.code == "receipt_jwks_unavailable" and info.value.transient
    no_struct = mcp_types.CallToolResult(content=[mcp_types.TextContent(type="text", text="x")])
    with pytest.raises(McpToolError, match="no_structured_content"):
        structured_result("t", no_struct)


async def test_gateway_uses_principal_tokens_and_raises_tool_errors(harness: Harness) -> None:
    from keelson_engine.runtime.mcp_client import get_gateway

    h = harness
    gw = get_gateway()
    data = await gw.call(tenant="demo-retail", connector="merxa", principal="reader", tool="get_purchase_order",
                         args={"code": "PO-BD-0001"})
    assert data["code"] == "PO-BD-0001" and len(data["lines"]) == 2
    with pytest.raises(McpToolError) as info:
        await gw.call(tenant="demo-retail", connector="merxa", principal=UserPrincipal("sub-marco"),
                      tool="get_purchase_order", args={"code": "PO-NOPE"})
    assert info.value.code == "not_found"
    assert h.core.tokens.issued == [("service", "reader"), ("user", "sub-marco")]
    assert [(c.principal, c.ok, c.error_code) for c in h.mcp_calls] == [("reader", True, None), ("user", False, "not_found")]


async def test_transport_failures_become_transport_errors(harness: Harness) -> None:
    h = harness
    gw = McpGateway(
        lambda: h.core.deps(),
        url_for=lambda tenant, connector: "http://127.0.0.1:9/mcp",
        transport_factory=proxied_transport_factory(None),
    )
    with pytest.raises(McpTransportError):
        await gw.call(tenant="demo-retail", connector="merxa", principal="reader", tool="list_locations")


async def test_gather_problem_data(harness: Harness) -> None:
    from keelson_engine.runtime.mcp_client import get_gateway

    h = harness
    recorded: list[tuple[str, dict[str, Any]]] = []

    async def record(tool: str, args: dict[str, Any], result: dict[str, Any]) -> str:
        recorded.append((tool, args))
        return f"ev_{len(recorded)}"

    async with get_gateway().session(tenant="demo-retail", connector="merxa", principal="reader") as session:
        bundle = await gather_problem_data("demo-retail", session, lanes=LANES, as_of=dt.date(2026, 10, 5),
                                           horizon_weeks=9, record=record)
    data = ProblemData.model_validate(bundle.data.model_dump(mode="json"))
    assert data.horizon.start_week == "2026-W41" and data.horizon.weeks == 9
    assert [d.code for d in data.dcs] == ["DE1", "NL1", "PL1", "UK1"]
    assert {s.code for s in data.skus} == {"PUF-W-BLK-S", "PUF-W-BLK-M"}
    # received PO excluded; open lines become receipts "<po>#<line>"
    assert sorted(r.ref for r in data.receipts) == ["PO-BD-0001#1", "PO-BD-0001#2", "PO-BD-0002#1", "PO-BD-0002#2"]
    # list_purchase_orders paginated explicitly (stub pages by 2)
    assert [t for t, _ in recorded].count("list_purchase_orders") == 2
    # demand split by size curve keeps the style total
    total = sum(d.units for d in data.demand if d.cluster_code == "UK1-COLD-A" and d.iso_week == "2026-W45")
    assert total == 260
    s_units = next(d.units for d in data.demand if d.cluster_code == "UK1-COLD-A" and d.iso_week == "2026-W45" and d.sku.endswith("-S"))
    assert s_units == 104  # 40 % of 260
    # reserved passed through; store stock aggregated to clusters
    h.merxa.inventory[("dc", "DE1", "PUF-W-BLK-M")]["reserved"] = 50
    async with get_gateway().session(tenant="demo-retail", connector="merxa", principal="reader") as session:
        again = await gather_problem_data("demo-retail", session, lanes=LANES, as_of=dt.date(2026, 10, 5), horizon_weeks=9)
    de1 = next(i for i in again.data.inventory if i.location_code == "DE1" and i.sku == "PUF-W-BLK-M")
    assert de1.reserved == 50 and de1.allocated == 100
    assert {(c.cluster_code, c.on_hand) for c in again.data.cluster_inventory if c.sku == "PUF-W-BLK-M"} == {
        ("UK1-COLD-A", 40), ("NL1-MILD-A", 40), ("DE1-COLD-A", 40), ("PL1-COLD-A", 40)}
    assert {lane.code for lane in data.lanes} == {"DE1>NL1", "DE1>UK1", "PL1>NL1"}
    assert bundle.counts["purchase_orders"] == 2 and len(bundle.evidence_ids) == len(recorded)
