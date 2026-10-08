"""ProblemData gathering with the M2 aggregates: call count and wall time on a stub sized like
the ``full`` seed (451 stores, 18 clusters, 180 SKUs, 12 POs), aggregate correctness against
the store-level ground truth, and the fallback for a connector without aggregates."""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from typing import Any

import pytest
from fakes.core import LANES, FakeCore
from fakes.harness import Harness, snapshot_from
from fakes.merxa_full import CLUSTERS, N_STORES, FullMerxaStub
from fakes.merxa_stub import MerxaStub
from fastmcp.client.transports import FastMCPTransport

from keelson_engine.runtime.errors import McpToolError
from keelson_engine.runtime.mcp_client import McpGateway
from keelson_engine.runtime.problem import gather_problem_data

AS_OF = dt.date(2026, 10, 5)
MAX_GATHER_S = 5.0


def _gateway(mcp: Any, core: FakeCore) -> McpGateway:
    return McpGateway(lambda: core.deps(), url_for=lambda t, c: "http://merxa.localhost/mcp",
                      transport_factory=lambda url, token: FastMCPTransport(mcp))


async def _gather(mcp: Any, core: FakeCore, *, record: Any = None) -> Any:
    async with _gateway(mcp, core).session(tenant="demo-retail", connector="merxa", principal="reader") as session:
        return await gather_problem_data("demo-retail", session, lanes=LANES, as_of=AS_OF, horizon_weeks=9,
                                         record=record)


async def test_full_size_gathering_is_fast_and_uses_aggregates() -> None:
    stub = FullMerxaStub(latency_s=0.03)  # ~30 ms per round trip through the egress proxy
    core = FakeCore()
    recorded: list[str] = []

    async def record(tool: str, args: dict[str, Any], result: dict[str, Any]) -> str:
        recorded.append(tool)
        return f"ev_{len(recorded)}"

    started = time.perf_counter()
    bundle = await _gather(stub.mcp, core, record=record)
    elapsed = time.perf_counter() - started

    data = bundle.data
    assert len(data.dcs) == 4 and len(data.clusters) == len(CLUSTERS) == 18
    assert len(data.skus) == 180
    assert bundle.counts["purchase_orders"] == 10 and len(data.receipts) == 60
    assert bundle.counts["forecast_rows"] == 30 * 18 * 9
    # call budget: locations + forecast + 30 styles + 4 aggregate inventory pages + PO list + 10 POs
    assert stub.count("get_inventory") == 4
    assert {a["aggregate"] for t, a in stub.calls if t == "get_inventory"} == {"cluster"}
    assert stub.count("list_stores") == 0  # no store → cluster map needed any more
    assert stub.count("get_entity") == 30 and stub.count("get_purchase_order") == 10
    assert bundle.counts["connector_calls"] == len(stub.calls) == 47
    # independent reads overlap on the one session; evidence order stays deterministic
    assert 1 < stub.max_in_flight <= 8
    assert recorded[:2] == ["list_locations", "get_demand_forecast"] and recorded[2:32] == ["get_entity"] * 30
    assert len(recorded) == len(bundle.evidence_ids) == 47
    assert elapsed < MAX_GATHER_S, f"gathering took {elapsed:.2f}s"
    print(f"full-size gather: {len(stub.calls)} calls, {elapsed:.2f}s, max in flight {stub.max_in_flight}")
    # cluster stock equals the sum of the 451 stores' ATP per cluster × SKU
    assert sum(1 for _ in stub.stores) == N_STORES
    got = {(c.cluster_code, c.sku): c.on_hand for c in data.cluster_inventory}
    assert got == {k: v for k, v in stub.cluster_truth.items() if v > 0}
    assert len(data.inventory) == 4 * 180


async def test_full_size_gathering_is_deterministic() -> None:
    first = await _gather(FullMerxaStub().mcp, FakeCore())
    second = await _gather(FullMerxaStub().mcp, FakeCore())
    assert first.data.model_dump(mode="json") == second.data.model_dump(mode="json")
    assert [r.ref for r in first.data.receipts] == sorted(r.ref for r in first.data.receipts)


async def test_connector_without_aggregates_falls_back_to_store_rows(harness: Harness) -> None:
    core = FakeCore()
    m2 = MerxaStub()
    m1 = MerxaStub(aggregates=False)
    new = await _gather(m2.mcp, core)
    old = await _gather(m1.mcp, core)
    assert new.data.model_dump(mode="json") == old.data.model_dump(mode="json")
    m2_tools = m2.tool_names_called()
    assert "list_stores" not in m2_tools
    assert [a["aggregate"] for t, a in m2.calls if t == "get_inventory"] == ["cluster"]
    # FastMCP rejected the unknown ``aggregate`` argument of the M1 signatures before the tool ran;
    # the runtime then read DC + store pages and the store → cluster map
    m1_inventory = [a for t, a in m1.calls if t == "get_inventory"]
    assert [a["location_type"] for a in m1_inventory] == ["dc", "store"]
    assert "list_stores" in m1.tool_names_called()
    assert m1.tool_names_called().count("get_demand_forecast") == 1
    inputs = {t.name: t.input_schema for t in await snapshot_from(m1)}
    assert "aggregate" not in inputs["get_inventory"]["properties"]


async def test_aggregate_rejection_by_fastmcp_is_a_tool_error() -> None:
    """The real FastMCP behaviour the fallback relies on: an unknown argument is a tool error
    (``isError``), not a transport failure."""
    core = FakeCore()
    async with _gateway(MerxaStub(aggregates=False).mcp, core).session(
        tenant="demo-retail", connector="merxa", principal="reader"
    ) as session:
        with pytest.raises(McpToolError):
            await session.call("get_inventory", {"aggregate": "cluster", "limit": 10})


async def test_paged_forecast_is_read_completely() -> None:
    """Regression: Merxa pages ``get_demand_forecast`` (``next_cursor``). Reading only the first
    page silently dropped demand rows (e.g. a long horizon on the full network)."""
    whole = await _gather(FullMerxaStub().mcp, FakeCore())
    stub = FullMerxaStub(forecast_page_size=1000)
    paged = await _gather(stub.mcp, FakeCore())
    assert paged.counts["forecast_rows"] == 30 * 18 * 9
    assert paged.data.demand == whole.data.demand
    cursors = [a["cursor"] for t, a in stub.calls if t == "get_demand_forecast"]
    assert cursors == [None, "1000", "2000", "3000", "4000"]
    assert {a["aggregate"] for t, a in stub.calls if t == "get_demand_forecast"} == {"cluster"}


async def test_a_failing_concurrent_read_cancels_its_siblings() -> None:
    """One failing ``get_purchase_order`` must not leave other reads running on a session that
    the caller is closing; the original error propagates unchanged and nothing is recorded."""
    from keelson_engine.runtime.problem import _Reader

    started: list[str] = []
    cancelled: list[str] = []

    class Caller:
        async def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
            code = (args or {})["code"]
            started.append(code)
            if code == "PO-2":
                raise McpToolError(tool, "not_found", code)
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(code)
                raise
            return {"code": code}

    recorded: list[str] = []

    async def record(tool: str, args: dict[str, Any], result: dict[str, Any]) -> str:
        recorded.append(tool)
        return "ev"

    reader = _Reader(Caller(), record, concurrency=8)
    started_at = time.perf_counter()
    with pytest.raises(McpToolError) as info:
        await reader.many("get_purchase_order", [{"code": f"PO-{n}"} for n in range(1, 5)])
    assert info.value.code == "not_found"
    assert time.perf_counter() - started_at < 2.0
    assert sorted(cancelled) == ["PO-1", "PO-3", "PO-4"] and recorded == []
