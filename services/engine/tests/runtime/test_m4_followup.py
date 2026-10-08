"""Follow-ups (``90`` §6): classification routing (FakeLlm through the agent factory), constraint
repair, deterministic dirty sets, simulation overrides and the explain / prepare nodes."""

from __future__ import annotations

import json
from typing import Any

import pytest
from fakes.core import FakeCore
from fakes.harness import Harness
from keelson_contracts.common import Provenance
from keelson_contracts.llm import ConstraintDelta, FollowupClassification
from keelson_contracts.scenario import DisruptionScenario
from test_m4_framing_support import TaskRepo, full_problem, progress_labels, routes, run_ctx, run_node

from keelson_engine.runtime.llm import FakeModelFactory, FakeScript, FakeTurn
from keelson_engine.runtime.nodes.followup import (
    CLASSIFIER_NAME,
    FOLLOWUP_STATE_KEY,
    SIM_OVERRIDES_STATE_KEY,
    SimulationOverrides,
    TaskRecord,
    changed_keys,
    dirty_task_ids,
    explain,
    explain_text,
    levers_in,
    make_followup_classifier,
    make_followup_delta,
    merge_options,
    next_task_id,
    over_budget,
    planning_with_overrides,
    prepare_reinvestigate,
    prepare_resimulate,
    repair_classification,
    sku_matches,
)
from keelson_engine.runtime.spec import PlanningDefaults

ALL = ["reallocate_inventory", "expedite_mode_shift", "resource_alt_supplier", "prioritize_allocation",
       "resequence_production"]


def scenario(mode: str = "actual") -> DisruptionScenario:
    return DisruptionScenario.model_validate({
        "id": "scn-1", "tenant": "demo-retail", "mode": mode, "event_family": "SUP", "event_type": "SUP.delay",
        "description": "Trims late", "perturbations": [
            {"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": ["PO-BD-0001#1"]}, "days": 7,
             "prov": "decision"}],
        "matched": [], "assumptions": [{"key": "delay_days", "label": "Delay", "value_text": "7 days", "prov": "decision"}],
        "config_version": "default/p-1",
    })


def option(oid: str, title: str, levers: list[str], *, recovered: int, cost: int, net: int, guided: bool = False) -> dict[str, Any]:
    return {"id": oid, "title": title, "levers": levers, "guided_only": guided, "actions": [{"seq": 1}],
            "kpi_delta": {"units_recovered": recovered, "net_benefit_cents": net, "transfer_cost_cents": cost},
            "kpis_after": {}, "kpis_before": {}, "scorecard": {"incremental_cost_cents": cost}}


PLAN_OPTIONS = [
    option("opt-A", "Rebalance between DCs", ["reallocate_inventory"], recovered=800, cost=120_000, net=900_000),
    option("opt-C", "Near-shore top-up", ["resource_alt_supplier"], recovered=1500, cost=2_500_000, net=400_000),
    {"id": "do-nothing", "title": "Do nothing", "levers": [], "actions": [], "kpi_delta": {}, "kpis_after": {},
     "kpis_before": {}},
]


async def seed(core: FakeCore, *, options: list[dict[str, Any]] | None = None, snapshot_id: str | None = None) -> Any:
    run = run_ctx(core)
    await core.repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario())
    await core.repo.save_plan(tenant=run.tenant, case_id=run.case_id, version=1, options=options or PLAN_OPTIONS,
                              recommended_option_id="opt-A", kpis={"snapshot_id": snapshot_id})
    return run


def classification(kind: str, **delta: Any) -> FakeTurn:
    body = {"delay_days": None, "duration_days": None, "exclude_levers": [], "sku_filter": [], "max_cost_cents": None, **delta}
    return FakeTurn(json={"kind": kind, "constraint_delta": body, "question": "q"})


# ─────────────────────────────── classifier routing ───────────────────────────────


@pytest.mark.parametrize(
    ("turn", "route", "delta"),
    [
        (classification("EXPLAIN"), "EXPLAIN", {}),
        (classification("RESIMULATE", delay_days=14), "RESIMULATE", {"delay_days": 14}),
        (classification("RESIMULATE", exclude_levers=["resource_alt_supplier"]), "RESIMULATE",
         {"exclude_levers": ["resource_alt_supplier"]}),
        (classification("RESIMULATE", sku_filter=["M"], max_cost_cents=500_000), "RESIMULATE",
         {"sku_filter": ["M"], "max_cost_cents": 500_000}),
        (classification("RESIMULATE"), "EXPLAIN", {}),  # nothing changed → an explanation
        (classification("RESIMULATE", delay_days=-4), "EXPLAIN", {}),  # repaired away
        (classification("REINVESTIGATE", max_cost_cents=100), "RESIMULATE", {"max_cost_cents": 100}),
        (classification("REINVESTIGATE"), "EXPLAIN", {}),
    ],
)
async def test_followup_delta_routes_on_the_typed_classification(turn: FakeTurn, route: str, delta: dict[str, Any]) -> None:
    core = FakeCore(repo=TaskRepo())
    run = await seed(core)
    script = FakeScript().add(CLASSIFIER_NAME, turn)
    node = make_followup_delta(make_followup_classifier(FakeModelFactory(script)))
    events, out, state = await run_node(node, "what about it?", run)
    assert routes(events)[-1] == route and out["kind"] == route
    for key, value in delta.items():
        assert out["constraint_delta"][key] == value
    assert state[FOLLOWUP_STATE_KEY] == out
    # the classifier saw the question and the case values as data in its own turn
    [request] = script.requests[CLASSIFIER_NAME]
    sent = json.loads("".join(p.text or "" for c in request.contents for p in c.parts or []))
    assert sent["question"] == "what about it?"
    assert sent["case"]["scenario"]["values"]["delay_days"] == 7
    assert [o["id"] for o in sent["case"]["options"]] == ["opt-A", "opt-C", "do-nothing"]
    assert "Trims late" not in json.dumps(sent)  # no free text from the scenario


async def test_reinvestigate_names_the_dirty_tasks() -> None:
    repo = TaskRepo()
    repo.tasks = [
        {"task_id": "t-sourcing-r1", "specialist": "sourcing", "status": "done", "input": {"assumption_keys": ["delay_days"]}},
        {"task_id": "t-logistics-r1", "specialist": "logistics", "status": "done", "input": {"assumption_keys": ["affected_lanes"]}},
        {"task_id": "t-inv-r1", "specialist": "inventory_demand", "status": "done",
         "input": {"assumption_keys": [], "depends_on": ["t-sourcing-r1"]}},
    ]
    core = FakeCore(repo=repo)
    run = await seed(core)
    script = FakeScript().add(CLASSIFIER_NAME, classification("REINVESTIGATE", delay_days=10))
    node = make_followup_delta(make_followup_classifier(FakeModelFactory(script)))
    events, out, _ = await run_node(node, {"question": "the supplier now says 10 days"}, run)
    assert routes(events)[-1] == "REINVESTIGATE"
    assert out["changed_keys"] == ["delay_days"]
    assert out["dirty_task_ids"] == ["t-inv-r1", "t-sourcing-r1"]


# ─────────────────────────────── repair / dirty sets ───────────────────────────────


def test_repair_drops_out_of_bounds_values_and_duplicates() -> None:
    cls = FollowupClassification(kind="RESIMULATE", question="x" * 3000, constraint_delta=ConstraintDelta(
        delay_days=3, duration_days=0, exclude_levers=["expedite_mode_shift", "expedite_mode_shift"],
        sku_filter=["M", " ", "M"], max_cost_cents=-1))
    fixed, problems = repair_classification(cls)
    assert problems
    d = fixed.constraint_delta
    assert (d.delay_days, d.duration_days, d.exclude_levers, d.sku_filter, d.max_cost_cents) == (
        3, None, ["expedite_mode_shift"], ["M"], None)
    assert len(fixed.question) == 2000 and fixed.kind == "RESIMULATE"


def rec(tid: str, *, keys: list[str] | None = None, status: str = "done", deps: tuple[str, ...] = (),
        updates: list[str] | None = None) -> TaskRecord:
    findings = None
    if updates is not None:
        from keelson_contracts.llm import Findings  # noqa: PLC0415

        findings = Findings.model_validate({"task_id": tid, "status": "ok", "claims": [], "affected": [], "gaps": [],
                                            "scenario_updates": [{"key": k, "value_int": 1, "value_ids": None,
                                                                  "evidence_id": "ev_a1", "json_pointer": ""} for k in updates]})
    return TaskRecord(task_id=tid, status=status, assumption_keys=None if keys is None else tuple(keys),
                      depends_on=deps, findings=findings)


def test_dirty_set_rules() -> None:
    changed = {"delay_days"}
    tasks = [rec("a-r1", keys=["delay_days"]), rec("b-r1", keys=["duration_days"]), rec("c-r1", keys=[], status="failed"),
             rec("d-r1", keys=[], deps=("a-r1",)), rec("e-r1", keys=[], deps=("d-r2",)), rec("f-r1", keys=[], updates=["delay_days"])]
    assert dirty_task_ids(tasks, changed) == ["a-r1", "c-r1", "d-r1", "e-r1", "f-r1"]
    # only the latest revision of a task counts
    assert dirty_task_ids([rec("a-r1", keys=["delay_days"]), rec("a-r2", keys=["duration_days"])], changed) == []
    assert dirty_task_ids([rec("a-r1", keys=["duration_days"]), rec("a-r2", keys=["delay_days"])], changed) == ["a-r2"]
    # nothing recorded which assumptions a task used → conservative: all of them
    assert dirty_task_ids([rec("x-r1"), rec("y-r1")], changed) == ["x-r1", "y-r1"]
    assert dirty_task_ids([rec("x-r1")], set()) == []
    assert dirty_task_ids([rec("x-r1", keys=["duration_days"])], changed) == []
    assert next_task_id("task-sourcing-r1", 3) == "task-sourcing-r3" and next_task_id("t1", 2) == "t1-r2"
    assert changed_keys(ConstraintDelta(delay_days=1, duration_days=None, exclude_levers=["expedite_mode_shift"],
                                        sku_filter=[], max_cost_cents=5)) == {"delay_days"}


def test_task_record_from_rows_and_findings_fallback() -> None:
    row = {"id": "t-1-r2", "specialist": "sourcing", "status": "failed",
           "input": {"assumption_keys": ["delay_days"], "depends_on": ["t-0-r2"]},
           "findings": {"task_id": "t-1-r2", "status": "failed", "claims": [], "affected": [], "gaps": ["x"]}}
    t = TaskRecord.from_row(row)
    assert (t.task_id, t.base, t.rev, t.status, t.assumption_keys, t.depends_on) == (
        "t-1-r2", "t-1", 2, "failed", ("delay_days",), ("t-0-r2",))
    assert t.findings is not None and t.findings.scenario_updates == []


# ─────────────────────────────── overrides ───────────────────────────────


def test_planning_overrides() -> None:
    problem = full_problem(weeks=2)
    base = PlanningDefaults()
    assert planning_with_overrides(base, SimulationOverrides()) is base
    p = planning_with_overrides(base, SimulationOverrides(exclude_levers=("resource_alt_supplier",), sku_filter=("M",),
                                                         max_cost_cents=500_000), problem)
    assert "resource_alt_supplier" not in p.enabled_levers and len(p.enabled_levers) == 4
    refs = p.lever_params["expedite_mode_shift"]["po_line_refs"]
    assert refs and all(next(pl.sku for pl in problem.po_lines if pl.ref == r).endswith("-M") for r in refs)
    assert p.lever_params["expedite_mode_shift"]["max_premium_cents"] == 500_000
    assert p.lever_params["prioritize_allocation"] == {"max_changes": 12}  # tenant params kept
    assert p.candidates == base.candidates
    only = planning_with_overrides(base, SimulationOverrides(only_levers=("expedite_mode_shift",)))
    assert only.enabled_levers == ("expedite_mode_shift",)
    assert [(c.id, c.levers) for c in only.candidates] == [("lever-expedite_mode_shift", ("expedite_mode_shift",))]
    assert SimulationOverrides(only_levers=("x",)).keep_previous
    capped = planning_with_overrides(base.model_copy(update={"lever_params": {"expedite_mode_shift": {"max_premium_cents": 100}}}),
                                     SimulationOverrides(max_cost_cents=500))
    assert capped.lever_params["expedite_mode_shift"]["max_premium_cents"] == 100
    rt = SimulationOverrides(exclude_levers=("a",), sku_filter=("M",), max_cost_cents=5)
    assert SimulationOverrides.from_dict(rt.as_dict()) == rt


def test_sku_matching_budget_and_merge() -> None:
    assert sku_matches("KNT-03-M", "KNT-03", "m") and sku_matches("KNT-03-M", "KNT-03", "KNT-03")
    assert sku_matches("PUF-W-BLK-M", "PUF-W-BLK", "blk") and not sku_matches("KNT-03-XL", "KNT-03", "M")
    assert not sku_matches("KNT-03-M", "KNT-03", " ")
    assert over_budget(PLAN_OPTIONS, 1_000_000) == ["opt-C"] and over_budget(PLAN_OPTIONS, None) == []
    new = [option("lever-expedite_mode_shift", "Sea-air", ["expedite_mode_shift"], recovered=1, cost=1, net=1),
           {"id": "do-nothing", "title": "Do nothing", "levers": []}]
    merged = merge_options(PLAN_OPTIONS, new)
    assert [o["id"] for o in merged] == ["opt-A", "opt-C", "lever-expedite_mode_shift", "do-nothing"]


# ─────────────────────────────── explain ───────────────────────────────


def test_levers_in_user_language() -> None:
    assert levers_in("Why not air freight?") == ["expedite_mode_shift"]
    assert levers_in("what about near-shore or rebalancing?") == ["reallocate_inventory", "resource_alt_supplier"]
    assert levers_in("why is the repair cost high?") == []  # 'air' only as a word start
    assert levers_in("") == []


def test_explain_text_is_deterministic_and_uses_plan_numbers() -> None:
    plan = {"options": PLAN_OPTIONS, "recommended_option_id": "opt-A"}
    text, sim = explain_text("why not near-shore?", plan, ALL)
    assert sim == []
    assert text.startswith("I recommend Rebalance between DCs: it recovers 800 units for €1,200 of extra cost "
                           "(net benefit €9,000).")
    assert "With re-source from an alternate supplier, Near-shore top-up recovers 1,500 units for €25,000" in text
    text, sim = explain_text("why not air freight?", plan, ALL)
    assert (text, sim) == ("", ["expedite_mode_shift"])
    text, sim = explain_text("why not air freight?", plan, ["reallocate_inventory"])
    assert sim == [] and "not enabled for your organisation" in text
    text, _ = explain_text("why this one?", plan, ALL)
    assert "Alternative: Near-shore top-up" in text
    assert explain_text("x", None, ALL)[0].startswith("There is no plan")


async def test_explain_node_answers_from_the_plan() -> None:
    core = FakeCore(repo=TaskRepo())
    run = await seed(core)
    events, out, _ = await run_node(explain, {"question": "why not near-shore?"}, run)
    assert routes(events)[-1] == "DONE" and out["explained"]
    [msg] = [m for m in core.repo.messages if m["role"] == "assistant"]
    assert "Near-shore top-up" in msg["text"]


async def test_explain_simulates_a_never_simulated_lever(harness: Harness) -> None:
    h = harness
    h.core.repo = TaskRepo()
    run = await seed(h.core)
    events, out, state = await run_node(explain, {"question": "why not air freight?"}, run)
    assert routes(events)[-1] == "RESIMULATE"
    assert out["simulate_levers"] == ["expedite_mode_shift"]
    assert state[SIM_OVERRIDES_STATE_KEY]["only_levers"] == ["expedite_mode_shift"]
    assert out["problem_evidence_id"] in h.core.repo.evidence and out["scenario_version"] == 1
    assert state["problem_evidence_id"] == out["problem_evidence_id"] and state["problem_source"] == "mcp"


# ─────────────────────────────── prepare nodes ───────────────────────────────


async def test_prepare_resimulate_applies_the_delta_and_hands_overrides_to_simulate(harness: Harness) -> None:
    h = harness
    h.core.repo = TaskRepo()
    h.core.repo.tasks = [{"task_id": "t-1-r1", "specialist": "sourcing", "status": "done", "input": {},
                          "findings": {"task_id": "t-1-r1", "status": "ok", "claims": [], "affected": [], "gaps": []}}]
    run = await seed(h.core)
    delta = {"delay_days": 14, "duration_days": None, "exclude_levers": ["resource_alt_supplier"], "sku_filter": [],
             "max_cost_cents": 500_000}
    events, out, state = await run_node(prepare_resimulate, {"kind": "RESIMULATE", "constraint_delta": delta}, run)
    assert routes(events)[-1] == "RESIMULATE"
    assert out["scenario_version"] == 2
    saved = (await h.core.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)).scenario
    assert saved.perturbations[0].days == 14 and saved.perturbations[0].prov == Provenance.DECISION
    assert out["what_changed"] == ["Delay: 7 days → 14 days", "Left out: re-source from an alternate supplier",
                                   "Extra cost capped at €5,000"]
    assert state[SIM_OVERRIDES_STATE_KEY] == {"exclude_levers": ["resource_alt_supplier"], "only_levers": [],
                                              "sku_filter": [], "max_cost_cents": 500_000}
    assert [f["task_id"] for f in out["findings"]] == ["t-1-r1"]  # investigations reused
    assert any(lbl.startswith("What changed: Delay: 7 days → 14 days") for lbl in progress_labels(events))
    # a duration on a scenario without a window is reported, not applied
    delta2 = {**delta, "delay_days": None, "duration_days": 5, "exclude_levers": [], "max_cost_cents": None}
    _e, out2, _ = await run_node(prepare_resimulate, {"constraint_delta": delta2}, run)
    assert out2["scenario_version"] == 2 and out2["what_changed"] == ["Duration: not part of this scenario, so it was not changed"]


async def test_prepare_reinvestigate_reruns_the_dirty_tasks_at_the_next_revision(harness: Harness) -> None:
    h = harness
    h.core.repo = TaskRepo()
    h.core.repo.tasks = [
        {"task_id": "task-sourcing-r1", "specialist": "sourcing", "status": "done", "input": {"assumption_keys": ["delay_days"]}},
        {"task_id": "task-logistics-r1", "specialist": "logistics", "status": "done", "input": {"assumption_keys": ["affected_lanes"]}},
    ]
    run = await seed(h.core)
    delta = {"delay_days": 10, "duration_days": None, "exclude_levers": [], "sku_filter": [], "max_cost_cents": None}
    events, out, _ = await run_node(prepare_reinvestigate, {"constraint_delta": delta, "changed_keys": ["delay_days"]}, run)
    assert routes(events)[-1] == "REINVESTIGATE"
    assert out["scenario_version"] == 2
    assert out["dirty_task_ids"] == ["task-sourcing-r1"]
    assert out["rerun_task_ids"] == {"task-sourcing-r1": "task-sourcing-r2"}
    assert out["what_changed"] == ["Delay: 7 days → 10 days"]
    assert any("re-checking 1 of 2 investigation tasks" in lbl for lbl in progress_labels(events))


async def test_prepare_nodes_without_a_scenario_end_the_run() -> None:
    core = FakeCore(repo=TaskRepo())
    run = run_ctx(core)
    for node in (prepare_resimulate, prepare_reinvestigate):
        events, _out, _ = await run_node(node, {"constraint_delta": {}}, run)
        assert routes(events)[-1] == "DONE"


async def test_rerun_ids_follow_the_task_revision_not_the_scenario_version(harness: Harness) -> None:
    h = harness
    h.core.repo = TaskRepo()
    h.core.repo.tasks = [
        {"task_id": "t1-r1", "status": "done", "input": {"assumption_keys": ["delay_days"]}},
        {"task_id": "t1-r2", "status": "done", "input": {"assumption_keys": ["delay_days"]}},
        {"task_id": "t1-r3", "status": "done", "input": {"assumption_keys": ["delay_days"]}},
        {"task_id": "t2-r1", "status": "done", "input": {"assumption_keys": ["duration_days"]}},
    ]
    run = await seed(h.core)
    delta = {"delay_days": 10, "duration_days": None, "exclude_levers": [], "sku_filter": [], "max_cost_cents": None}
    _events, out, _ = await run_node(prepare_reinvestigate, {"constraint_delta": delta, "changed_keys": ["delay_days"]}, run)
    assert out["scenario_version"] == 2
    assert out["dirty_task_ids"] == ["t1-r3"] and out["rerun_task_ids"] == {"t1-r3": "t1-r4"}


async def test_reinvestigate_with_nothing_dirty_re_simulates_instead_of_re_running_everything() -> None:
    repo = TaskRepo()
    repo.tasks = [
        {"task_id": "t-logistics-r1", "specialist": "logistics", "status": "done",
         "input": {"assumption_keys": ["affected_lanes"]}},
        {"task_id": "t-playbook-r1", "specialist": "playbook", "status": "done", "input": {"assumption_keys": []}},
    ]
    core = FakeCore(repo=repo)
    run = await seed(core)
    script = FakeScript().add(CLASSIFIER_NAME, classification("REINVESTIGATE", delay_days=10))
    node = make_followup_delta(make_followup_classifier(FakeModelFactory(script)))
    events, out, _ = await run_node(node, "the supplier now says 10 days", run)
    assert routes(events)[-1] == "RESIMULATE" and out["kind"] == "RESIMULATE"
    assert out["dirty_task_ids"] == [] and out["constraint_delta"]["delay_days"] == 10
