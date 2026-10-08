"""Translator: ADK events → AG-UI (allowed SUBAGENT roles only, no LLM partials as TEXT_MESSAGE,
reasoning live-only + one aggregate, tool calls, interrupts, replay dedupe)."""

from __future__ import annotations

from typing import Any

from fakes.core import FakeBus, FakeInterrupts
from google.adk.events.event import Event, NodeInfo
from google.genai import types

from keelson_engine.runtime.translator import (
    Translator,
    humanise_tool,
    seen_output_keys,
)

INV = "e-run-1"


def ev(path: str, *, parts: list[types.Part] | None = None, partial: bool | None = None, output: Any = None,
       meta: dict[str, Any] | None = None, error: str | None = None, lr: set[str] | None = None) -> Event:
    return Event(
        invocation_id=INV, author="keelson", node_info=NodeInfo(path=path),
        content=types.Content(role="model", parts=parts) if parts else None,
        partial=partial, output=output, custom_metadata=meta, error_code=error, long_running_tool_ids=lr,
    )


def make(bus: FakeBus, interrupts: FakeInterrupts | None = None, seen: set[tuple[str, str]] | None = None) -> Translator:
    return Translator(case_id="case-1", run_id="run-1", tenant="demo-retail", bus=bus, interrupts=interrupts, seen=seen)


async def test_subagents_only_for_display_roles_and_text_partials_are_dropped() -> None:
    bus = FakeBus()
    t = make(bus)
    await t.start()
    intake = "keelson@1/intake@1"
    await t.handle(ev(intake, parts=[types.Part(text="Classif", thought=False)], partial=True))
    await t.handle(ev(intake, parts=[types.Part(text='{"kind":"QUESTION"}')], output={"kind": "QUESTION"}))
    # a plumbing node and an unknown agent marker never announce subagents
    await t.handle(ev("keelson@1/route_intake@1", output={"x": 1}))
    await t.handle(ev("keelson@1/investigate@1/rogue@task", meta={"lifecycle": "agent_started", "agent": "rogue"}))
    # the specialist marker does
    spec = "keelson@1/investigate@1/inventory_demand@task-inventory_demand-r1"
    await t.handle(ev(spec, meta={"lifecycle": "agent_started", "agent": "inventory_demand"}, partial=True))
    await t.handle(ev(f"{spec}/inventory_demand_investigator@investigator",
                      parts=[types.Part(text="some analyst text")], partial=True))
    await t.handle(ev(spec, output={"task_id": "t"}))
    await t.finish_success()
    types_ = bus.types()
    started = bus.of_type("SUBAGENT_STARTED")
    assert [e["name"] for e in started] == ["intake", "inventory_demand"]
    assert [e["description"] for e in started] == ["Intake", "Inventory & demand analyst"]
    assert started[0]["subagentRunId"] == f"{INV}:{intake}"
    assert len(bus.of_type("SUBAGENT_FINISHED")) == 2
    assert not any(x.startswith("TEXT_MESSAGE") for x in types_)
    assert [e["stepName"] for e in bus.of_type("STEP_STARTED")] == ["understanding", "checking_systems"]
    assert types_[0] == "RUN_STARTED" and types_[-1] == "RUN_FINISHED"


async def test_reasoning_deltas_live_only_and_aggregate_persisted_once() -> None:
    bus = FakeBus()
    t = make(bus)
    path = "keelson@1/recommend@1"
    for chunk in ("Weighing ", "the options"):
        await t.handle(ev(path, parts=[types.Part(text=chunk, thought=True)], partial=True))
    await t.handle(ev(path, parts=[types.Part(text="Weighing the options", thought=True), types.Part(text="{}")],
                      output={"headline": "x"}))
    contents = [e for e in bus.events if e["event"]["type"] == "REASONING_MESSAGE_CONTENT"]
    live = [e for e in contents if e["live"] and not e["persist"]]
    persisted = [e for e in contents if e["persist"]]
    assert [e["event"]["delta"] for e in live] == ["Weighing ", "the options"]
    assert [e["event"]["delta"] for e in persisted] == ["Weighing the options"] and not persisted[0]["live"]
    order = [e["event"]["type"] for e in bus.events if e["event"]["type"].startswith("REASONING")]
    assert order[0] == "REASONING_START" and order[-2:] == ["REASONING_MESSAGE_END", "REASONING_END"]
    assert all(e["event"]["metadata"]["sc"]["agent"] == "recommender" for e in contents)


async def test_reasoning_through_coalescer_routes_content_and_end() -> None:
    bus = FakeBus()

    class Coalescer:
        def __init__(self) -> None:
            self.got: list[str] = []

        async def append(self, *, tenant: str, case_id: str, run_id: str | None, event: dict[str, Any]) -> int:
            self.got.append(event["type"])
            return await bus.append(tenant=tenant, case_id=case_id, run_id=run_id, event=event, persist=False)

    co = Coalescer()
    t = Translator(case_id="case-1", run_id="run-1", tenant="demo-retail", bus=bus, reasoning_sink=co)
    path = "keelson@1/intake@1"
    await t.handle(ev(path, parts=[types.Part(text="hmm", thought=True)], partial=True))
    await t.handle(ev(path, parts=[types.Part(text="hmm", thought=True)], output={"kind": "SMALLTALK"}))
    assert co.got == ["REASONING_MESSAGE_CONTENT", "REASONING_MESSAGE_END"]


async def test_tool_calls_are_humanised_and_cite_evidence() -> None:
    bus = FakeBus()
    t = make(bus)
    spec = "keelson@1/investigate@1/inventory_demand@task-inventory_demand-r1"
    inv = f"{spec}/inventory_demand_investigator@investigator"
    await t.handle(ev(spec, meta={"lifecycle": "agent_started", "agent": "inventory_demand"}, partial=True))
    fc = types.FunctionCall(id="fc-1", name="list_purchase_orders", args={"status": "open"})
    await t.handle(ev(inv, parts=[types.Part(function_call=fc)]))
    fr = types.FunctionResponse(id="fc-1", name="list_purchase_orders",
                                response={"items": [{}, {}, {}], "next_cursor": None, "_evidence_id": "ev_abc123"})
    await t.handle(Event(invocation_id=INV, author="keelson", node_info=NodeInfo(path=inv),
                         content=types.Content(role="user", parts=[types.Part(function_response=fr)])))
    start, end, result = bus.of_type("TOOL_CALL_START")[0], bus.of_type("TOOL_CALL_END")[0], bus.of_type("TOOL_CALL_RESULT")[0]
    assert start["toolCallName"] == "list_purchase_orders" and start["subagentRunId"] == f"{INV}:{spec}"
    assert end["toolCallId"] == "fc-1"
    # running sentence while the call is in flight, then the done sentence (70-backoffice-m2.md §6)
    assert start["metadata"]["sc"]["label"] == "Reading open purchase orders from Merxa"
    assert result["content"] == "Read 3 open purchase orders from Merxa"
    assert result["metadata"]["sc"]["label"] == result["content"]
    assert result["metadata"]["sc"]["evidenceRefs"] == ["ev_abc123"]
    assert result["metadata"]["sc"]["agent"] == "inventory_demand"


async def test_interrupt_creates_row_then_finishes_with_interrupt_outcome() -> None:
    bus, interrupts = FakeBus(), FakeInterrupts()
    t = make(bus, interrupts)
    fc = types.FunctionCall(
        id="approach-case-1-1", name="adk_request_input",
        args={"interruptId": "approach-case-1-1", "message": "Confirm?",
              "payload": {"kind": "approach", "scenario_version": 1, "quorum_spec": {"roles": [], "count": 1}}},
    )
    await t.handle(ev("keelson@1/approach_gate@1", parts=[types.Part(function_call=fc)], lr={"approach-case-1-1"}))
    assert not bus.of_type("TOOL_CALL_START")
    await t.finish_interrupted(invocation_id=INV, session_id="run-1")
    [row] = interrupts.created
    assert row["kind"] == "approach" and row["interrupt_id"] == "approach-case-1-1"
    assert row["invocation_id"] == INV and row["session_id"] == "run-1" and row["message"] == "Confirm?"
    [finished] = bus.of_type("RUN_FINISHED")
    assert finished["outcome"]["type"] == "interrupt"
    assert finished["outcome"]["interrupts"][0]["responseSchema"]["required"] == ["decision"]


async def test_replayed_outputs_are_deduplicated() -> None:
    bus = FakeBus()
    prior = [ev("keelson@1/intake@1", output={"kind": "NEW_DISRUPTION"}), ev("keelson@1/frame_case@1", output={"v": 1})]
    t = make(bus, seen=seen_output_keys(prior))
    for event in prior:
        await t.handle(event)
    await t.handle(ev("keelson@1/approach_router@1", output={"scenario_version": 2}))
    assert not bus.of_type("SUBAGENT_STARTED")
    assert [e["stepName"] for e in bus.of_type("STEP_STARTED")] == ["understanding"]


async def test_blocks_text_and_case_updates() -> None:
    from keelson_contracts.blocks import NarrativeProps, block_to_wire, make_block

    bus = FakeBus()
    t = make(bus)
    block = block_to_wire(make_block("narrative", NarrativeProps(markdown="Hi"), title="T",
                                     provenance={"kind": "inference", "engine": "x@1", "as_of": "2026-10-05"}))
    await t.handle(ev("keelson@1/compose@1", meta={"sc_block": block}, partial=True))
    await t.handle(ev("keelson@1/compose@1", meta={"sc_text": {"message_id": "m1", "text": "Hello"}}, partial=True))
    await t.handle(ev("keelson@1/promote_case@1", meta={"sc_case": {"mode": "actual", "status": "open", "title": "T"}},
                      partial=True))
    assert bus.of_type("ACTIVITY_SNAPSHOT")[0]["content"]["id"] == block["id"]
    assert [e["event"]["type"] for e in bus.events if e["event"]["type"].startswith("TEXT")] == [
        "TEXT_MESSAGE_START", "TEXT_MESSAGE_CONTENT", "TEXT_MESSAGE_END"]
    assert bus.of_type("CUSTOM")[0]["value"] == {"mode": "actual", "status": "open", "title": "T"}


def test_humanise_never_returns_raw_payload() -> None:
    assert humanise_tool("get_inventory", {}, {"items": [1, 2]}, "Merxa") == "Read 2 inventory positions from Merxa"
    assert humanise_tool("list_locations", {}, {"dcs": [1], "clusters": [1, 2]}, "Merxa") == "Read 1 DC and 2 store clusters from Merxa"
    assert humanise_tool("get_inventory", {}, {"error": "not_found: x"}, "Merxa") == "Couldn't read inventory positions from Merxa"
