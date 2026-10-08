"""Deterministic routing: pre_route / route_intake / approach_router, incl. DEFAULT_ROUTE edges."""

from __future__ import annotations

from typing import Any

from fakes.harness import MARCO, PRIYA, Harness
from fakes.scripts import critic_turn, intake_disruption, intake_kind, m4_review_turns
from google.adk.models.llm_request import LlmRequest
from google.adk.workflow import DEFAULT_ROUTE

from keelson_engine.runtime.llm import FakeModelFactory, FakeScript, FakeTurn
from keelson_engine.runtime.spec import AgentSpec
from keelson_engine.runtime.workflow import WorkflowBindings, build_workflow

DECIDER = {"sub": PRIYA["sub"], "username": PRIYA["username"], "name": PRIYA["name"]}


def _texts(h: Harness, run_id: str) -> list[str]:
    return [e["delta"] for e in h.events(run_id) if e["type"] == "TEXT_MESSAGE_CONTENT"]


def test_every_router_has_a_default_route() -> None:
    wf = build_workflow(AgentSpec(tenant="demo-retail"), WorkflowBindings(models=FakeModelFactory(FakeScript())))
    graph = wf.graph
    assert graph is not None
    routed_sources = {e.from_node.name for e in graph.edges if e.route is not None}
    assert routed_sources == {"pre_route", "route_intake", "approach_router", "compile_changeset", "dry_run",
                              "approval_check", "verify_numbers", "critic", "promote_case", "confirm_assumptions",
                              "followup_delta", "explain", "prepare_resimulate", "prepare_reinvestigate",
                              "prepare_rollback", "prepare_replan"}
    for source in routed_sources:
        assert any(e.from_node.name == source and e.route == DEFAULT_ROUTE for e in graph.edges), source
    names = {n.name for n in graph.nodes}
    assert {"intake", "frame_case", "quick_exposure", "approach_gate", "investigate", "impact", "simulate",
            "recommend", "compose", "compile_changeset", "dry_run", "approval_gate", "approval_check", "execute",
            "reconcile", "close_case", "answer_investigator", "answer_structurer", "compose_answer",
            "prepare_rollback", "checklist_step", "prepare_replan"} <= names


def test_llm_agents_are_single_turn_without_history_and_never_mix_tools_and_schema() -> None:
    from keelson_engine.runtime.agents import KeelsonLlmAgent

    wf = build_workflow(AgentSpec(tenant="demo-retail"), WorkflowBindings(models=FakeModelFactory(FakeScript())))
    agents = [n for n in wf.graph.nodes if isinstance(n, KeelsonLlmAgent)]
    assert {a.name for a in agents} == {"intake", "answer_investigator", "answer_structurer", "recommend"}
    for agent in agents:
        assert agent.mode == "single_turn" and agent.include_contents == "none"
        assert callable(agent.instruction)
        assert not (agent.tools and agent.output_schema is not None)


async def test_smalltalk_takes_the_default_route(harness: Harness) -> None:
    h = harness
    h.script.add("intake", intake_kind("SMALLTALK"))
    row = h.row("chat", {"text": "hello there"})
    assert await h.run(row) == "succeeded"
    [text] = _texts(h, row.id)
    assert "supply-chain disruptions" in text
    assert not h.core.repo.scenarios


async def test_unknown_envelope_takes_the_default_route() -> None:
    from types import SimpleNamespace

    from keelson_engine.runtime.nodes import routing

    for envelope in ({"kind": "action", "payload": {"type": "teleport"}},
                     {"kind": "action", "payload": {"type": "rollback"}},
                     {"kind": "signal", "payload": {}}):
        events = [e async for e in routing.pre_route(SimpleNamespace(state={}), envelope)]  # type: ignore[arg-type]
        assert events[-1].actions.route == routing.UNROUTED, envelope  # DEFAULT_ROUTE → reject_run


async def test_guide_action_without_a_plan_is_refused(harness: Harness) -> None:
    # M5: Guide is a supported case action (95 §2); without a plan nothing is prepared
    h = harness
    row = h.row("action", {"type": "guide", "option_id": "opt-A", "plan_version": 1})
    assert await h.run(row) == "succeeded"
    [text] = _texts(h, row.id)
    assert "There is no plan to apply yet." in text
    assert not any(e["type"] == "SUBAGENT_STARTED" for e in h.events(row.id))


async def test_promote_switches_case_to_actual(harness: Harness) -> None:
    h = harness
    h.core.repo.cases[h.case_id]["mode"] = "what_if"
    row = h.row("action", {"type": "promote", "option_id": "opt-A", "plan_version": 1})
    assert await h.run(row) == "succeeded"
    assert h.core.repo.cases[h.case_id]["mode"] == "actual"
    updates = [e for e in h.events(row.id) if e["type"] == "CUSTOM"]
    assert updates and updates[0]["name"] == "sc.case.updated" and updates[0]["value"]["mode"] == "actual"


async def test_question_path_composes_a_cited_answer_with_calc(harness: Harness) -> None:
    h = harness
    h.script.add("intake", intake_kind("QUESTION"))

    def notes(request: LlmRequest) -> FakeTurn:
        ids = [
            (p.function_response.response or {})
            for c in request.contents or [] for p in c.parts or [] if p.function_response is not None
        ]
        calc = next(r for r in ids if "calc_id" in r)
        return FakeTurn(text=f"PO-BD-0001 has lines [{calc['calc_id']}].")

    h.script.add(
        "answer_investigator",
        FakeTurn(function_calls=[("get_purchase_order", {"code": "PO-BD-0001"})]),
        lambda req: FakeTurn(function_calls=[("calc", {
            "op": "sum", "evidence_id": next(
                (p.function_response.response or {})["_evidence_id"]
                for c in req.contents for p in c.parts or [] if p.function_response is not None
            ), "json_pointer": "/lines", "field": "qty_ordered"})]),
        notes,
    )

    def structured(request: LlmRequest) -> FakeTurn:
        calc = h.core.repo.evidence_by_tool("calc")[-1]["id"]
        po = h.core.repo.evidence_by_tool("get_purchase_order")[-1]["id"]
        return FakeTurn(json={
            "task_id": "answer", "status": "ok",
            "claims": [
                {"text": f"PO-BD-0001 orders {{{{calc:{calc}}}}} units in total.", "kind": "fact", "evidence_id": None,
                 "json_pointer": None, "calc_id": calc, "confidence": "high"},
                {"text": f"It is due in UK1 on {{{{ev:{po}#/eta_dc}}}}.", "kind": "fact", "evidence_id": po,
                 "json_pointer": "/eta_dc", "calc_id": None, "confidence": "high"},
                {"text": "It has 7 lines.", "kind": "fact", "evidence_id": po, "json_pointer": None, "calc_id": None,
                 "confidence": "low"},
            ],
            "affected": [], "gaps": [],
        })

    h.script.add("answer_structurer", structured)
    row = h.row("chat", {"text": "How many units are on PO-BD-0001?"})
    assert await h.run(row) == "succeeded"
    [text] = _texts(h, row.id)
    assert "1,800" in text and "29 Oct 2026" in text  # calc + ev placeholders resolved
    assert "7 lines" not in text and "could not be verified" in text  # free number stripped
    assert [r for r in h.core.repo.evidence_by_tool("calc")][0]["result"] == {"value": 1800}
    assert {c.principal for c in h.mcp_calls} == {"reader"}
    # the answer roles are not SUBAGENT roles; only intake announces itself
    assert [e["name"] for e in h.events(row.id) if e["type"] == "SUBAGENT_STARTED"] == ["intake"]
    tool_events = [e for e in h.events(row.id) if e["type"].startswith("TOOL_CALL")]
    assert not tool_events  # TOOL_CALL_* only for specialist subagents


async def test_edit_reframes_and_cancel_closes(harness: Harness) -> None:
    h = harness
    from fakes.scripts import investigator_turns, recommendation, structurer_turn

    h.script.add("intake", intake_disruption(delay_days=None))
    m4_review_turns(h.script)
    chat = h.row("chat", {"text": "Meghna trims are late"})
    assert await h.run(chat) == "paused"
    first = h.core.interrupts.created[-1]
    # EDIT → frame_case again with the user's correction; now every input is grounded → auto-confirm
    h.script.add("inventory_demand_investigator", *investigator_turns())
    h.script.add("inventory_demand_structurer", structurer_turn(h.core.repo))
    h.script.add("recommend", recommendation())
    edit = h.resume_row(chat, "edit", [DECIDER], edits="It is 10 days, not a week")
    assert await h.run(edit) == "succeeded"
    assert len(h.core.interrupts.created) == 1 and first["interrupt_id"].endswith("-1")
    versions = h.core.repo.scenarios[h.case_id]
    scenario = versions[-1][1]
    lts = next(p for p in scenario.perturbations if p.op == "lead_time_shift")
    assert lts.days == 10 and lts.prov.value == "decision"
    cards = [b for b in h.core.repo.blocks_of("approach_card")]
    assert len({c["id"] for c in cards}) == 2  # a new card for the reframed scenario
    assert h.core.repo.plans[h.case_id][-1]["version"] == 1


async def test_cancel_closes_case(harness: Harness) -> None:
    h = harness
    h.script.add("intake", intake_disruption(delay_days=None))
    chat = h.row("chat", {"text": "Meghna trims are late"})
    assert await h.run(chat) == "paused"
    cancel = h.resume_row(chat, "cancel", [DECIDER])
    assert await h.run(cancel) == "succeeded"
    assert "closed this case" in _texts(h, cancel.id)[0]
    assert h.core.repo.plans == {}


async def test_what_if_decision_marks_case(harness: Harness) -> None:
    h = harness
    h.script.add("intake", intake_disruption(delay_days=None))
    m4_review_turns(h.script)
    from fakes.scripts import investigator_turns, recommendation, structurer_turn

    h.script.add("inventory_demand_investigator", *investigator_turns())
    h.script.add("inventory_demand_structurer", structurer_turn(h.core.repo))
    h.script.add("recommend", recommendation())
    chat = h.row("chat", {"text": "What if Meghna trims slip?"})
    assert await h.run(chat) == "paused"
    resume = h.resume_row(chat, "what_if", [DECIDER])
    assert await h.run(resume) == "succeeded"
    assert h.core.repo.cases[h.case_id]["mode"] == "what_if"
    scenario = (await h.core.repo.latest_scenario(tenant="demo-retail", case_id=h.case_id)).scenario
    assert scenario.mode == "what_if"


async def test_broken_tool_binding_fails_specialist_loudly_but_case_continues(tmp_path: Any) -> None:
    from fakes.scripts import recommendation

    from keelson_engine.runtime.spec import SpecialistSpec

    h = await Harness(tmp=tmp_path, spec_overrides={
        "specialists": (SpecialistSpec(slug="inventory_demand", tools=("get_inventory", "list_supplier_messages")),)
    }).setup()
    try:
        h.script.add("intake", intake_disruption(delay_days=7))
        h.script.add("critic", critic_turn())
        h.script.add("recommend", recommendation())
        row = h.row("chat", {"text": "Meghna trims 7 days late"})
        assert await h.run(row) == "succeeded"
        [finding] = h.core.repo.findings
        assert finding["status"] == "failed" and finding["findings"]["status"] == "failed"
        events = h.events(row.id)
        errors = [e for e in events if e["type"] == "SUBAGENT_ERROR"]
        # loud for operators (logged with an error id), generic for the user (no config detail)
        assert len(errors) == 1 and "list_supplier_messages" not in errors[0]["message"]
        assert errors[0]["metadata"]["errorId"] in errors[0]["message"]
        assert [e["name"] for e in events if e["type"] == "SUBAGENT_STARTED"] == [
            "intake", "inventory_demand", "critic", "recommender"]
        assert h.script.remaining("inventory_demand_investigator") == 0
        assert not h.script.requests.get("inventory_demand_investigator")  # failed before any model call
    finally:
        await h.close()


def _unused(*_: Any) -> None:  # keeps MARCO import used for readers of this module
    _ = MARCO
