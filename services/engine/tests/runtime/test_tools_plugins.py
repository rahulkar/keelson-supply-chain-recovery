"""RegistryToolset binding rules, definition-hash drift, PolicyPlugin denial, EvidencePlugin,
BudgetPlugin, CalcTool."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest
from fakes.harness import PRIYA, Harness
from fakes.scripts import intake_disruption
from google.adk.models.llm_request import LlmRequest
from keelson_contracts.runs import Actor

from keelson_engine.runtime.context import RunContext, bind_run
from keelson_engine.runtime.errors import RuntimeConfigError
from keelson_engine.runtime.plugins import BudgetPlugin, EvidencePlugin, PolicyPlugin
from keelson_engine.runtime.spec import AgentSpec, Budgets
from keelson_engine.runtime.toolsets import (
    CalcTool,
    RegistryTool,
    RegistryToolset,
    require_toolset_ok,
)


@dataclasses.dataclass
class Ctx:
    agent_name: str


def run_ctx(h: Harness, **spec: Any) -> RunContext:
    return RunContext(
        tenant="demo-retail", case_id=h.case_id, run_id="run-x", kind="chat", actor=Actor.model_validate(PRIYA),
        payload={}, spec=AgentSpec(tenant="demo-retail", **spec), deps=h.core.deps(), invocation_id="e-run-x",
    )


async def test_toolset_binds_only_approved_trusted_reads(harness: Harness) -> None:
    h = harness
    ctx = run_ctx(h)
    toolset = RegistryToolset(
        connector="merxa",
        allowlist=["get_inventory", "list_supplier_messages", "apply_change_set", "get_demand_forecast"],
        owner="answer_investigator",
    )
    with bind_run(ctx):
        tools = await toolset.get_tools()
        assert [t.name for t in tools] == ["get_demand_forecast", "get_inventory"]
        assert all(t.custom_metadata["keelson"]["access"] == "read" for t in tools)
        # the untrusted and write tools are missing from the binding → the agent fails loudly
        with pytest.raises(RuntimeConfigError, match="list_supplier_messages"):
            await require_toolset_ok(Ctx("answer_investigator"), LlmRequest())  # type: ignore[arg-type]
        # the live tools/list happened once per run, with the reader principal
        result = await tools[1].run_async(args={"location_type": "dc"}, tool_context=None)  # type: ignore[arg-type]
        assert result["items"]
    assert {c.principal for c in h.mcp_calls} == {"reader"}


async def test_definition_hash_drift_drops_tool_and_audits(harness: Harness) -> None:
    h = harness
    tools = h.core.connectors.tools
    idx = next(i for i, t in enumerate(tools) if t.name == "get_inventory")
    tools[idx] = dataclasses.replace(tools[idx], description="An older description", definition_hash=None)
    ctx = run_ctx(h)
    toolset = RegistryToolset(connector="merxa", allowlist=["get_inventory", "list_locations"], owner="x_investigator")
    with bind_run(ctx):
        bound = await toolset.get_tools()
    assert [t.name for t in bound] == ["list_locations"]
    [audit] = [a for a in h.core.repo.audit if a["action"] == "connector_tool.dropped"]
    assert audit["target"] == "merxa:get_inventory" and "definition_hash" in audit["after"]["reason"]
    assert ctx.toolset_errors["x_investigator"]


async def test_policy_plugin_denies_write_and_off_allowlist_tools(harness: Harness) -> None:
    h = harness
    ctx = run_ctx(h)
    plugin = PolicyPlugin()
    tools = {t.name: t for t in h.core.connectors.tools}
    apply_tool = RegistryTool(tools["apply_change_set"], connector="merxa")
    read_tool = RegistryTool(tools["get_inventory"], connector="merxa")
    messages = RegistryTool(tools["list_supplier_messages"], connector="merxa")
    with bind_run(ctx):
        denied = await plugin.before_tool_callback(tool=apply_tool, tool_args={}, tool_context=Ctx("inventory_demand_investigator"))  # type: ignore[arg-type]
        assert denied == {"error": "tool_denied: destructive tools are never available to LLM agents"}
        off_list = await plugin.before_tool_callback(tool=read_tool, tool_args={}, tool_context=Ctx("intake"))  # type: ignore[arg-type]
        assert off_list and "not on the allowlist" in off_list["error"]
        untrusted = await plugin.before_tool_callback(tool=messages, tool_args={}, tool_context=Ctx("answer_investigator"))  # type: ignore[arg-type]
        assert untrusted and "untrusted_text" in untrusted["error"]
        allowed = await plugin.before_tool_callback(tool=read_tool, tool_args={}, tool_context=Ctx("inventory_demand_investigator"))  # type: ignore[arg-type]
        assert allowed is None
    assert [a["action"] for a in h.core.repo.audit] == ["tool.denied"] * 3


async def test_policy_plugin_enforces_tool_call_cap(harness: Harness) -> None:
    from keelson_engine.runtime.spec import SpecialistSpec

    h = harness
    ctx = run_ctx(h, specialists=(SpecialistSpec(slug="inventory_demand", tools=("get_inventory",), max_tool_calls=2),))
    plugin = PolicyPlugin()
    tool = RegistryTool(next(t for t in h.core.connectors.tools if t.name == "get_inventory"), connector="merxa")
    with bind_run(ctx):
        results = [await plugin.before_tool_callback(tool=tool, tool_args={}, tool_context=Ctx("inventory_demand_investigator"))  # type: ignore[arg-type]
                   for _ in range(3)]
    assert results[:2] == [None, None] and "budget" in results[2]["error"]


async def test_evidence_plugin_records_and_tags_results(harness: Harness) -> None:
    h = harness
    ctx = run_ctx(h)
    tool = RegistryTool(next(t for t in h.core.connectors.tools if t.name == "get_inventory"), connector="merxa")
    with bind_run(ctx):
        out = await EvidencePlugin().after_tool_callback(
            tool=tool, tool_args={"location_type": "dc"}, tool_context=Ctx("inventory_demand_investigator"),  # type: ignore[arg-type]
            result={"items": [{"sku": "A"}]},
        )
        skipped = await EvidencePlugin().after_tool_callback(
            tool=tool, tool_args={}, tool_context=Ctx("x"), result={"error": "tool_denied: no"},  # type: ignore[arg-type]
        )
    assert out is not None and out["_evidence_id"].startswith("ev_") and out["items"] == [{"sku": "A"}]
    row = h.core.repo.evidence[out["_evidence_id"]]
    assert row["tool"] == "get_inventory" and row["prov"] == "fact" and "_evidence_id" not in row["result"]
    assert skipped is None
    assert ctx.evidence_log == [{"id": out["_evidence_id"], "agent": "inventory_demand_investigator",
                                 "tool": "get_inventory", "args": {"location_type": "dc"}, "lists": {"items": 1}}]


async def test_calc_tool_is_deterministic_evidence(harness: Harness) -> None:
    h = harness
    ctx = run_ctx(h)
    ev = await h.core.repo.save_evidence(tenant="demo-retail", case_id=h.case_id, run_id="r", connector="merxa",
                                         tool="get_purchase_order", args={}, result={
                                             "eta_dc": "2026-10-29", "ex_factory_date": "2026-10-10",
                                             "lines": [{"qty": 5}, {"qty": 7}]}, prov="fact")
    calc = CalcTool()
    with bind_run(ctx):
        total = await calc.run_async(args={"op": "sum", "evidence_id": ev, "json_pointer": "/lines", "field": "qty"},
                                     tool_context=Ctx("answer_investigator"))  # type: ignore[arg-type]
        count = await calc.run_async(args={"op": "count", "evidence_id": ev, "json_pointer": "/lines"},
                                     tool_context=Ctx("answer_investigator"))  # type: ignore[arg-type]
        days = await calc.run_async(args={"op": "date_diff", "evidence_id": ev, "json_pointer": "/ex_factory_date",
                                          "json_pointer_end": "/eta_dc"}, tool_context=Ctx("answer_investigator"))  # type: ignore[arg-type]
        bad = await calc.run_async(args={"op": "max", "evidence_id": ev, "json_pointer": "/eta_dc"},
                                   tool_context=Ctx("answer_investigator"))  # type: ignore[arg-type]
    assert (total["value"], count["value"], days["value"]) == (12, 2, 19)
    assert h.core.repo.evidence[total["calc_id"]]["result"] == {"value": 12}
    assert bad == {"error": "calc_error: json_pointer must point at an array"}


async def test_budget_plugin_aborts_run(harness: Harness) -> None:
    h = harness
    ctx = run_ctx(h, budgets=Budgets(max_llm_calls=1, max_total_tokens=1000))
    plugin = BudgetPlugin()
    with bind_run(ctx):
        assert await plugin.before_model_callback(callback_context=Ctx("intake"), llm_request=LlmRequest()) is None  # type: ignore[arg-type]
        response = await plugin.before_model_callback(callback_context=Ctx("intake"), llm_request=LlmRequest())  # type: ignore[arg-type]
    assert response is not None and response.error_code == "BUDGET_EXCEEDED"
    assert ctx.abort_signal.is_set() and ctx.abort_reason == "budget"


async def test_budget_exhaustion_ends_run_with_run_error(tmp_path: Any) -> None:
    h = await Harness(tmp=tmp_path, spec_overrides={"budgets": Budgets(max_llm_calls=1, max_total_tokens=1000)}).setup()
    try:
        h.script.add("intake", intake_disruption(delay_days=7))
        row = h.row("chat", {"text": "Meghna trims 7 days late"})
        assert await h.run(row) == "failed"
        last = h.events(row.id)[-1]
        assert last["type"] == "RUN_ERROR" and last["code"] == "BUDGET_EXCEEDED"
        assert h.core.runs.status_of(row.id) == "failed"
    finally:
        await h.close()


async def test_cancel_ends_run_cancelled(harness: Harness) -> None:
    from keelson_engine.runtime.context import AbortController

    h = harness
    h.script.add("intake", intake_disruption(delay_days=7))
    controller = AbortController()
    controller.abort("cancelled", "user")
    row = h.row("chat", {"text": "Meghna trims 7 days late"})
    assert await h.run(row, controller=controller) == "cancelled"
    assert h.core.runs.status_of(row.id) == "cancelled"
    assert h.events(row.id)[-1]["code"] == "CANCELLED"


async def test_shutdown_abort_leaves_run_for_the_reaper(harness: Harness) -> None:
    from keelson_engine.runtime.context import AbortController

    h = harness
    h.script.add("intake", intake_disruption(delay_days=7))
    controller = AbortController()
    controller.abort("shutdown")
    row = h.row("chat", {"text": "Meghna trims 7 days late"})
    assert await h.run(row, controller=controller) == "abandoned"
    assert h.core.runs.status_of(row.id) is None
    await asyncio.sleep(0)
