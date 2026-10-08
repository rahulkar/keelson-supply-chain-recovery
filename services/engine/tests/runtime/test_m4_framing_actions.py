"""Case actions and the approach card (``90`` §2, §5, §8): ``promote_case`` re-simulates on the
current snapshot, ``confirm_assumptions`` re-runs impact + simulate, typed **Edit** decisions are
checked against the card, **Looks right** confirms through the validating path, and stored pre-M4
intakes still frame."""

from __future__ import annotations

from typing import Any

from fakes.harness import Harness
from keelson_contracts.blocks import ApproachCardProps
from keelson_contracts.common import Provenance
from keelson_contracts.runs import ApproachEdit, ResumePayload
from keelson_contracts.scenario import DisruptionScenario
from test_m4_framing_support import TaskRepo, progress_labels, routes, run_ctx, run_node

from keelson_engine.runtime.nodes.framing import (
    APPROACH_CARD_STATE_KEY,
    INTAKE_STATE_KEY,
    approach_gate,
    case_findings,
    approach_router,
    confirm_assumptions,
    frame_case,
    promote_case,
)


def scenario(mode: str = "what_if", *, pending: bool = False) -> DisruptionScenario:
    assumptions: list[dict[str, Any]] = [
        {"key": "delay_days", "label": "Delay", "value_text": "7 days", "prov": "assumption"},
        {"key": "affected_receipts", "label": "Affected purchase-order lines",
         "value_text": "1 open line on PO-BD-0001", "prov": "fact"},
    ]
    if pending:
        assumptions[0] = {"key": "delay_days", "label": "Delay", "value_text": "12 days (inferred)", "prov": "inference",
                          "unconfirmed": True, "source_evidence_ids": ["ev_msg1"]}
    return DisruptionScenario.model_validate({
        "id": "scn-1", "tenant": "demo-retail", "mode": mode, "event_family": "SUP", "event_type": "SUP.delay",
        "description": "Trims late",
        "perturbations": [{"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": ["PO-BD-0001#1"]},
                           "days": 12 if pending else 7, "prov": "inference" if pending else "assumption"}],
        "matched": [{"type": "supplier", "code": "TRIM-BD-01", "label": "Meghna Trims"}],
        "assumptions": assumptions, "config_version": "default/p-1",
    })


async def test_promote_re_simulates_on_the_current_data(harness: Harness) -> None:
    h = harness
    h.core.repo = TaskRepo()
    h.core.repo.tasks = [{"task_id": "task-inventory_demand-r1", "specialist": "inventory_demand", "status": "done",
                          "findings": {"task_id": "task-inventory_demand-r1", "status": "ok", "claims": [],
                                       "affected": [], "gaps": []}}]
    run = run_ctx(h.core, case_mode="what_if")
    await h.core.repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario())
    events, out, state = await run_node(promote_case, {"plan_version": 1}, run)
    assert routes(events)[-1] == "RESIMULATE"
    assert (out["mode"], out["scenario_version"], out["promoted"]) == ("actual", 2, True)
    assert [f["task_id"] for f in out["findings"]] == ["task-inventory_demand-r1"]
    assert h.core.repo.cases[run.case_id]["mode"] == "actual" and run.case_mode == "actual"
    saved = (await h.core.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)).scenario
    assert saved.mode == "actual"
    assert h.core.interrupts.superseded == 1
    assert state["problem_evidence_id"] == out["problem_evidence_id"] and state["problem_source"] == "mcp"
    assert any(e.custom_metadata and e.custom_metadata.get("sc_case", {}).get("mode") == "actual" for e in events)
    assert any("re-simulating the options on live Merxa data" in lbl for lbl in progress_labels(events))


async def test_promote_without_a_scenario_just_marks_the_case(harness: Harness) -> None:
    run = run_ctx(harness.core, case_mode="what_if")
    events, out, _ = await run_node(promote_case, {}, run)
    assert routes(events)[-1] == "DONE" and out == {"promoted": True}
    assert harness.core.repo.cases[run.case_id]["mode"] == "actual"


async def test_confirm_assumptions_confirms_and_re_runs_impact_and_simulate(harness: Harness) -> None:
    h = harness
    run = run_ctx(h.core, case_mode="actual")
    await h.core.repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario("actual", pending=True))
    await h.core.repo.save_plan(tenant=run.tenant, case_id=run.case_id, version=1, options=[], recommended_option_id="do-nothing",
                                kpis={"snapshot_id": None})
    # a key that is not pending changes nothing
    events, out, _ = await run_node(confirm_assumptions, {"assumption_keys": ["affected_receipts"]}, run)
    assert routes(events)[-1] == "DONE" and out == {"confirmed": []}
    events, out, state = await run_node(confirm_assumptions, {"assumption_keys": ["delay_days"]}, run)
    assert routes(events)[-1] == "RESIMULATE" and out["confirmed"] == ["delay_days"]
    saved = (await h.core.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)).scenario
    delay = next(a for a in saved.assumptions if a.key == "delay_days")
    assert (delay.prov, delay.unconfirmed, delay.source_evidence_ids) == (Provenance.DECISION, False, ["ev_msg1"])
    assert saved.perturbations[0].prov == Provenance.DECISION and saved.perturbations[0].days == 12
    assert not saved.pending_confirmation and out["scenario_version"] == 2
    assert state["problem_evidence_id"] == out["problem_evidence_id"]
    # nothing pending any more
    events, _out, _ = await run_node(confirm_assumptions, {}, run)
    assert routes(events)[-1] == "DONE"


async def _gate(h: Harness, scn: DisruptionScenario, data: dict[str, Any]) -> tuple[list[Any], Any, dict[str, Any], Any]:
    run = run_ctx(h.core, case_mode=scn.mode)
    version = await h.core.repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scn)
    events, out, state = await run_node(approach_gate, {"scenario_version": version, **data}, run)
    return events, out, state, run


async def test_approach_card_shows_plan_checks_agents_and_editable_values(harness: Harness) -> None:
    h = harness
    plan = {"rationale": "r", "budget": {"max_parallel": 2, "max_tasks": 3}, "tasks": [
        {"id": "t1", "specialist": "sourcing", "objective": "Check the open trim POs", "tools": [], "depends_on": []},
        {"id": "t2", "specialist": "logistics", "objective": "Check the sailings", "tools": [], "depends_on": []},
        {"id": "t3", "specialist": "sourcing", "objective": "Check the factory schedule", "tools": [], "depends_on": ["t1"]},
    ]}
    grounded = scenario("actual").confirm_assumptions(["delay_days"])
    events, out, state, _run = await _gate(h, grounded, {"plan": plan})
    card = ApproachCardProps.model_validate(state[APPROACH_CARD_STATE_KEY])
    assert [(c.task_id, c.sentence) for c in card.checks] == [("t1", "Check the open trim POs"), ("t2", "Check the sailings"),
                                                              ("t3", "Check the factory schedule")]
    assert [(a.slug, a.display_name) for a in card.agents] == [("sourcing", "Sourcing analyst"), ("logistics", "Logistics analyst")]
    assert {(e.key, e.kind) for e in card.editable} == {("delay_days", "days"), ("affected_receipts", "ids")}
    assert not (isinstance(out, dict) and out.get("auto"))  # 3 tasks → no auto-skip although all is grounded
    assert not any("confirmed automatically" in lbl for lbl in progress_labels(events))
    # ≤ 2 tasks and grounded → auto-confirm
    small = {**plan, "tasks": plan["tasks"][:2]}
    events, out, _state, _ = await _gate(h, grounded, {"plan": small})
    assert out["auto"] is True and out["decision"] == "confirm"


async def _route(h: Harness, decision: str, *, edits: Any = None, removed: list[str] | None = None,
                 state: dict[str, Any] | None = None) -> tuple[list[Any], Any]:
    run = run_ctx(h.core, case_mode="actual")
    await h.core.repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario("actual"))
    iid = "approach-case-1-1"
    run.resume = ResumePayload(interrupt_id=iid, decision=decision, edits=edits, removed_checks=removed or [])  # type: ignore[arg-type]
    events, out, _ = await run_node(approach_router, {"decision": decision, "interrupt_id": iid}, run, state=state)
    return events, out


CARD = {
    "interrupt_id": "approach-case-1-1", "event": {"family": "SUP", "type": "SUP.delay", "description": "d", "source": "chat"},
    "mode": "actual", "matched": {"count": 0, "sample": []}, "assumptions": [],
    "checks": [{"task_id": "t1", "sentence": "Check POs"}, {"task_id": "t2", "sentence": "Check sailings"}],
    "buttons": ["confirm", "edit", "what_if", "cancel"],
    "editable": [{"key": "delay_days", "label": "Delay", "kind": "days", "value_int": 7}],
}


async def test_typed_edit_is_checked_against_the_stored_card(harness: Harness) -> None:
    h = harness
    state = {APPROACH_CARD_STATE_KEY: CARD, INTAKE_STATE_KEY: {"kind": "NEW_DISRUPTION"}}
    events, out = await _route(h, "edit", edits=[ApproachEdit(key="delay_days", value_int=10)], removed=["t2"], state=state)
    assert routes(events)[-1] == "EDIT"
    assert out["edits"] == [{"key": "delay_days", "value_int": 10}] and out["removed_checks"] == ["t2"]
    assert out["intake"] == {"kind": "NEW_DISRUPTION"}
    # a key the card does not offer / every check removed → the edit is refused (re-framed unchanged)
    events, out = await _route(h, "edit", edits=[ApproachEdit(key="duration_days", value_int=10)], state=state)
    assert out["edits"] is None and "not editable" in out["edit_errors"][0]
    _e, out = await _route(h, "edit", removed=["t1", "t2"], state=state)
    assert out["edits"] is None and "at least one check" in out["edit_errors"][0]
    _e, out = await _route(h, "edit", edits=[ApproachEdit(key="delay_days", value_int=3)], state={})
    assert "not available" in out["edit_errors"][0]
    # M1 free text still passes through untouched
    _e, out = await _route(h, "edit", edits="It is 10 days", state=state)
    assert out["edits"] == "It is 10 days" and "edit_errors" not in out


async def test_looks_right_confirms_through_the_validating_path(harness: Harness) -> None:
    events, out = await _route(harness, "confirm")
    assert routes(events)[-1] == "CONFIRM" and out["scenario_version"] == 2
    saved = (await harness.core.repo.latest_scenario(tenant="demo-retail", case_id="case-1")).scenario
    assert not saved.unconfirmed_assumptions and saved.perturbations[0].prov == Provenance.DECISION
    DisruptionScenario.model_validate(saved.model_dump(mode="json"))


async def test_frame_case_accepts_a_stored_pre_m4_intake_and_typed_edits(harness: Harness) -> None:
    h = harness
    run = run_ctx(h.core)
    legacy = {"kind": "NEW_DISRUPTION", "summary": "Trims late", "disruption": {
        "event_family": "SUP", "event_type": "trim_delay", "description": "Meghna Trims reports delayed trims",
        "entity_mentions": [{"type": "supplier", "text": "Meghna Trims"}], "delay_days": None,
        "demand_change_pct": None, "start_date": None, "duration_days": None, "is_hypothetical": False}}
    events, out, state = await run_node(frame_case, {"intake": legacy, "edits": [{"key": "delay_days", "value_int": 10}],
                                                     "removed_checks": ["t2"]}, run)
    assert out["removed_checks"] == ["t2"] and out["mode"] == "actual"
    saved = (await h.core.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)).scenario
    assert saved.event_type == "SUP.delay"
    lts = saved.perturbations[0]
    assert (lts.days, lts.prov) == (10, Provenance.DECISION)
    assert state[INTAKE_STATE_KEY]["disruption"]["event_type"] == "SUP.delay"
    events, _out, _ = await run_node(frame_case, {"intake": legacy, "edits": None, "edit_errors": ["x"]}, run)
    assert any("did not fit the card" in lbl for lbl in progress_labels(events))


# ─────────────── review regression: later runs reuse the *verified*, latest findings ───────────────


def _f(task_id: str, claim: str) -> dict[str, Any]:
    return {"task_id": task_id, "status": "ok", "affected": [], "gaps": [], "scenario_updates": [],
            "claims": [{"text": claim, "kind": "inference", "evidence_id": None, "json_pointer": None, "calc_id": None,
                        "confidence": "low"}]}


async def test_case_findings_prefer_the_verified_state_then_the_latest_revision(harness: Harness) -> None:
    h = harness
    h.core.repo = TaskRepo()
    run = run_ctx(h.core)
    # every revision of every task, oldest first (as list_investigation_tasks returns them)
    h.core.repo.tasks = [
        {"task_id": "t1-r1", "findings": _f("t1", "t1 first look (stale)")},
        {"task_id": "t2-r1", "findings": _f("t2", "t2 only look")},
        {"task_id": "t1-r2", "findings": _f("t1", "t1 second look")},
        {"task_id": "t3-r1", "findings": None},
    ]
    found = await case_findings(run)
    assert [f["claims"][0]["text"] for f in found] == ["t2 only look", "t1 second look"]
    # the verified findings state written after verify/critic wins (unverified claims never come back)
    await h.core.repo.save_evidence(
        tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector="keelson", tool="investigation_findings",
        args={}, prov="inference",
        result={"plan": {"tasks": [{"id": "t2"}, {"id": "t1"}]},
                "findings_by_task": {"t1": _f("t1", "t1 verified"), "t2": _f("t2", "t2 verified")}, "task_revs": {}},
    )
    found = await case_findings(run)
    assert [f["claims"][0]["text"] for f in found] == ["t2 verified", "t1 verified"]
