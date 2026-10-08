"""Read-only integration against the running stack (``pytest -m integration``, needs
``KEELSON_INTEGRATION=1``): E1's real core (DB, Token Broker with private_key_jwt, connector
snapshot) + the egress proxy + the real Merxa MCP. Nothing is written to Merxa."""

from __future__ import annotations

import datetime as dt
import os

import pytest
from keelson_contracts.optimizer import ProblemData
from keelson_contracts.runs import Actor

from keelson_engine.runtime.context import RunContext, bind_run
from keelson_engine.runtime.spec import INVENTORY_DEMAND_TOOLS, AgentSpec

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(os.environ.get("KEELSON_INTEGRATION") != "1", reason="needs the running compose stack"),
]


@pytest.fixture
async def core_deps():  # type: ignore[no-untyped-def]
    from keelson_engine.core.db import close_db, init_db
    from keelson_engine.runtime import deps as deps_mod
    from keelson_engine.runtime import mcp_client

    await init_db()
    deps_mod.set_deps(None)
    mcp_client.set_gateway(None)
    try:
        yield deps_mod.get_deps()
    finally:
        deps_mod.set_deps(None)
        mcp_client.set_gateway(None)
        await close_db()


async def test_problem_data_from_real_merxa(core_deps) -> None:  # type: ignore[no-untyped-def]
    from keelson_engine.runtime.mcp_client import get_gateway
    from keelson_engine.runtime.problem import gather_problem_data

    info = await core_deps.connectors.get_connector(tenant="demo-retail", slug="merxa")
    assert info.lanes and info.url == "http://merxa.localhost/mcp"
    async with get_gateway().session(tenant="demo-retail", connector="merxa", principal="reader") as session:
        bundle = await gather_problem_data(
            "demo-retail", session, lanes=info.lanes, as_of=dt.date(2026, 10, 5), horizon_weeks=9
        )
    data = ProblemData.model_validate(bundle.data.model_dump(mode="json"))
    print("integration counts:", bundle.counts)
    assert {d.code for d in data.dcs} == {"UK1", "NL1", "DE1", "PL1"}
    assert len(data.clusters) >= 8 and len(data.skus) == 30
    assert any(r.ref.startswith("PO-BD-") for r in data.receipts)
    assert not any(r.ref.startswith("PO-KH-0001") for r in data.receipts)  # received
    assert data.demand and data.cluster_inventory and data.inventory
    assert all(s.retail_price_cents > s.unit_cost_cents > 0 for s in data.skus)


async def test_snapshot_matches_live_definitions(core_deps) -> None:  # type: ignore[no-untyped-def]
    from keelson_engine.runtime.toolsets import RegistryToolset

    run = RunContext(
        tenant="demo-retail", case_id="integration", run_id="integration", kind="chat",
        actor=Actor(sub="it", username="it", name="Integration", roles=[]), payload={},
        spec=AgentSpec(tenant="demo-retail"), deps=core_deps, invocation_id="e-integration",
    )
    toolset = RegistryToolset(connector="merxa", allowlist=INVENTORY_DEMAND_TOOLS, owner="inventory_demand_investigator")
    with bind_run(run):
        tools = await toolset.get_tools()
        assert sorted(t.name for t in tools) == sorted(INVENTORY_DEMAND_TOOLS)
        assert run.toolset_errors == {}
        result = await tools[0].run_async(args={"limit": 1}, tool_context=None)  # type: ignore[arg-type]
    assert "error" not in result or not str(result["error"]).startswith("insufficient_scope")
