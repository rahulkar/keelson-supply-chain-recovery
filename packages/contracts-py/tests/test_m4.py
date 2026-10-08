"""M4 additions (``docs/contracts/90-m4-investigation.md``): LLM outputs for the planner,
critic, follow-ups and quarantine, taxonomy enum, unconfirmed assumptions, approach /
recommended-option / narrative block additions and the new action / resume payloads.

Everything is additive: pre-M4 payloads validate and serialize byte-identically (pinned
hashes below were computed with the M3 models)."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from pydantic import ValidationError

from examples import GOOD_PROPS, envelope, good_props
from keelson_contracts.agui import AGENT_DISPLAY_NAMES
from keelson_contracts.blocks import (
    ApproachCardProps,
    NarrativeProps,
    RecommendedOptionProps,
    block_to_wire,
    make_block,
    parse_block,
)
from keelson_contracts.common import (
    EVENT_TAXONOMY_VERSION,
    EVENT_TYPE_KEYS,
    EntityRef,
    Provenance,
    event_family_of,
    sha256_hex,
)
from keelson_contracts.llm import (
    LLM_OUTPUT_MODELS,
    MAX_PLAN_TASKS,
    SCENARIO_UPDATE_KEYS,
    ConstraintDelta,
    Critique,
    DisruptionExtract,
    EntityMention,
    Findings,
    FollowupClassification,
    GuardVerdict,
    InvestigationPlan,
    PlanBudget,
    ScenarioUpdate,
    UntrustedExtraction,
    plan_violations,
    plan_waves,
    strict_schema_violations,
    untrusted_extraction_violations,
)
from keelson_contracts.optimizer import LEVER_CODES
from keelson_contracts.runs import ActionPayload, ApproachEdit, ResumePayload, RunRequest
from keelson_contracts.scenario import Assumption, DisruptionScenario

ACTOR = {"sub": "u1", "username": "ana", "name": "Ana", "roles": ["planner"]}
DECIDER = {"sub": "u1", "username": "ana", "name": "Ana"}

# ───────────────────────────── taxonomy (90 §1) ─────────────────────────────


def test_taxonomy_keys_are_the_contract_set() -> None:
    assert EVENT_TYPE_KEYS == (
        "SUP.delay", "SUP.capacity_loss", "LOG.chokepoint_closure", "LOG.port_closure",
        "LOG.strike", "NAT.storm", "NAT.flood", "NAT.earthquake", "DEM.spike", "DEM.drop", "OTHER",
    )
    assert EVENT_TAXONOMY_VERSION == 2


@pytest.mark.parametrize(("key", "family"), [("SUP.delay", "SUP"), ("LOG.strike", "LOG"), ("OTHER", "OTHER")])
def test_event_family_of(key: str, family: str) -> None:
    assert event_family_of(key) == family


def test_event_family_of_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="unknown taxonomy event type"):
        event_family_of("trim_delay")


def _extract(**over: Any) -> dict[str, Any]:
    return {
        "event_family": "LOG",
        "event_type": "LOG.chokepoint_closure",
        "description": "What if the Suez Canal closes for 3 weeks?",
        "entity_mentions": [{"type": "chokepoint", "text": "Suez Canal"}, {"type": "region", "text": "Red Sea"}],
        "delay_days": None,
        "demand_change_pct": None,
        "start_date": None,
        "duration_days": 21,
        "is_hypothetical": True,
        **over,
    }


def test_disruption_extract_event_type_is_taxonomy_enum() -> None:
    ex = DisruptionExtract.model_validate(_extract())
    assert ex.taxonomy_family == "LOG"
    schema = DisruptionExtract.model_json_schema()
    assert tuple(schema["properties"]["event_type"]["enum"]) == EVENT_TYPE_KEYS
    with pytest.raises(ValidationError):
        DisruptionExtract.model_validate(_extract(event_type="trim_delay"))


def test_entity_mentions_and_refs_know_chokepoints_and_regions() -> None:
    for t in ("chokepoint", "region", "port"):
        assert EntityMention.model_validate({"type": t, "text": "x"}).type == t
    assert EntityRef.model_validate({"type": "chokepoint", "code": "SUEZ", "label": "Suez Canal"}).code == "SUEZ"
    with pytest.raises(ValidationError):
        EntityMention.model_validate({"type": "ocean", "text": "x"})


# ───────────────────────────── strict schemas ───────────────────────────────

M4_LLM_MODELS = {
    "ScenarioUpdate", "PlanBudget", "InvestigationTask", "InvestigationPlan", "CritiqueIssue",
    "Critique", "ConstraintDelta", "FollowupClassification", "GuardVerdict", "UntrustedItem",
    "UntrustedExtraction",
}


def test_every_m4_llm_model_is_registered_and_strict() -> None:
    names = {m.__name__ for m in LLM_OUTPUT_MODELS}
    assert names >= M4_LLM_MODELS
    for model in LLM_OUTPUT_MODELS:
        assert strict_schema_violations(model) == [], model.__name__


def test_findings_requires_scenario_updates_in_schema_but_reads_pre_m4_payloads() -> None:
    schema = Findings.model_json_schema()
    assert "scenario_updates" in schema["required"]
    old = {"task_id": "t1", "status": "ok", "claims": [], "affected": [], "gaps": []}
    parsed = Findings.model_validate(old)
    assert parsed.scenario_updates == []
    assert Findings.model_validate_json(json.dumps(old)).scenario_updates == []


def _update(**over: Any) -> dict[str, Any]:
    return {
        "key": "delay_days", "value_int": 10, "value_ids": None,
        "evidence_id": "ev_01", "json_pointer": "/items/0/delay_days", **over,
    }


def test_findings_with_scenario_updates_round_trip() -> None:
    raw = {
        "task_id": "t-sourcing", "status": "ok", "claims": [], "affected": [], "gaps": [],
        "scenario_updates": [_update(), _update(key="add_receipts", value_int=None, value_ids=["PO-1#1"])],
    }
    assert Findings.model_validate(raw).model_dump(mode="json") == raw
    assert set(SCENARIO_UPDATE_KEYS) == {
        "delay_days", "duration_days", "demand_change_pct", "add_receipts", "remove_receipts"
    }


@pytest.mark.parametrize(
    ("over", "error"),
    [
        ({}, None),
        ({"key": "remove_receipts", "value_int": None, "value_ids": ["PO-1#1"]}, None),
        ({"value_int": None}, "needs value_int"),
        ({"value_ids": ["x"]}, "needs value_int"),
        ({"key": "add_receipts"}, "non-empty value_ids"),
        ({"key": "add_receipts", "value_int": None, "value_ids": []}, "non-empty value_ids"),
        ({"key": "add_receipts", "value_int": None, "value_ids": ["a", "a"]}, "unique"),
        ({"evidence_id": " "}, "evidence_id"),
        ({"json_pointer": "items/0"}, "pointer"),
    ],
)
def test_scenario_update_shape(over: dict[str, Any], error: str | None) -> None:
    result = ScenarioUpdate.model_validate(_update(**over)).shape_error()
    if error is None:
        assert result is None
    else:
        assert result is not None and error in result


def test_scenario_update_rejects_unknown_key() -> None:
    with pytest.raises(ValidationError):
        ScenarioUpdate.model_validate(_update(key="lead_time"))


# ───────────────────────────── planner (90 §2) ──────────────────────────────

CATALOGUE = {
    "sourcing": ["list_purchase_orders", "read_supplier_messages_structured"],
    "logistics": ["list_sailings", "list_lanes"],
    "inventory_demand": ["get_inventory"],
}


def _task(tid: str, specialist: str = "sourcing", deps: list[str] | None = None, **over: Any) -> dict[str, Any]:
    return {
        "id": tid, "specialist": specialist, "objective": f"Check {tid}",
        "tools": CATALOGUE.get(specialist, [])[:1], "depends_on": deps or [], **over,
    }


def _plan(tasks: list[dict[str, Any]], max_parallel: int = 3, max_tasks: int = 6) -> InvestigationPlan:
    return InvestigationPlan.model_validate(
        {"rationale": "r", "tasks": tasks, "budget": {"max_parallel": max_parallel, "max_tasks": max_tasks}}
    )


def test_valid_plan_and_waves() -> None:
    plan = _plan([_task("a"), _task("b", "logistics"), _task("c", "inventory_demand", ["a", "b"]), _task("d", deps=["c"])])
    assert plan_violations(plan, CATALOGUE) == []
    assert plan_waves(plan) == [["a", "b"], ["c"], ["d"]]


@pytest.mark.parametrize(
    ("plan", "needle"),
    [
        (_plan([_task("a")], max_parallel=5), "max_parallel"),
        (_plan([_task("a")], max_parallel=0), "max_parallel"),
        (_plan([_task("a")], max_tasks=7), "max_tasks"),
        (_plan([]), "no tasks"),
        (_plan([_task(str(i)) for i in range(3)], max_tasks=2), "budget allows 2"),
        (_plan([_task(str(i)) for i in range(MAX_PLAN_TASKS + 1)]), "budget allows 6"),
        (_plan([_task("a"), _task("a")]), "duplicate task ids"),
        (_plan([_task("")]), "non-empty"),
        (_plan([_task("a", objective="x" * 201)]), "objective longer"),
        (_plan([_task("a", objective=" ")]), "empty objective"),
        (_plan([_task("a", "compliance")]), "unknown specialist"),
        (_plan([_task("a", tools=["list_purchase_orders", "apply_change_set"])]), "tools not allowed"),
        (_plan([_task("a", deps=["a"])]), "depends on itself"),
        (_plan([_task("a", deps=["zz"])]), "unknown tasks"),
        (_plan([_task("a", deps=["b"]), _task("b", deps=["a"])]), "cycle"),
    ],
)
def test_plan_violations(plan: InvestigationPlan, needle: str) -> None:
    problems = plan_violations(plan, CATALOGUE)
    assert any(needle in p for p in problems), problems


def test_plan_waves_raise_on_cycle_and_unknown() -> None:
    with pytest.raises(ValueError, match="cycle"):
        plan_waves(_plan([_task("a", deps=["b"]), _task("b", deps=["a"])]))
    with pytest.raises(ValueError, match="unknown"):
        plan_waves(_plan([_task("a", deps=["q"])]))


def test_plan_budget_has_no_schema_bounds() -> None:
    # bounds are enforced by plan_violations, never by the (strict) schema
    assert strict_schema_violations(PlanBudget) == []
    assert PlanBudget.model_validate({"max_parallel": 9, "max_tasks": 9}).max_parallel == 9


# ───────────────────────────── critic / follow-ups ──────────────────────────


def test_critique() -> None:
    c = Critique.model_validate(
        {
            "verdict": "REVISE",
            "issues": [{"task_id": "t1", "issue": "stale as_of", "severity": "high"},
                       {"task_id": None, "issue": "no logistics check", "severity": "medium"}],
            "revise_task_ids": ["t1"],
        }
    )
    assert c.issues[1].task_id is None
    with pytest.raises(ValidationError):
        Critique.model_validate({"verdict": "FAIL", "issues": [], "revise_task_ids": []})


def _delta(**over: Any) -> dict[str, Any]:
    return {"delay_days": None, "duration_days": None, "exclude_levers": [], "sku_filter": [],
            "max_cost_cents": None, **over}


def test_followup_classification() -> None:
    f = FollowupClassification.model_validate(
        {"kind": "RESIMULATE",
         "constraint_delta": _delta(exclude_levers=["resource_alt_supplier"], max_cost_cents=500_000),
         "question": "exclude near-shore and cap extra cost at 5k"}
    )
    assert not f.constraint_delta.is_empty
    assert ConstraintDelta.model_validate(_delta()).is_empty
    schema = ConstraintDelta.model_json_schema()
    assert tuple(schema["properties"]["exclude_levers"]["items"]["enum"]) == LEVER_CODES
    with pytest.raises(ValidationError):
        ConstraintDelta.model_validate(_delta(exclude_levers=["airfreight"]))
    with pytest.raises(ValidationError):
        FollowupClassification.model_validate({"kind": "CHAT", "constraint_delta": _delta(), "question": "q"})


# ───────────────────────────── quarantine (90 §4) ───────────────────────────


def test_guard_verdict() -> None:
    v = GuardVerdict.model_validate({"injection_suspected": True, "reason": "asks to approve changes"})
    assert v.injection_suspected
    with pytest.raises(ValidationError):
        GuardVerdict.model_validate({"injection_suspected": True})


def test_untrusted_extraction_and_violations() -> None:
    item = {"source_evidence_id": "ev_1", "kind": "delay_notice",
            "entity_mentions": [{"type": "supplier", "text": "Meghna Trims"}],
            "delay_days": 10, "quote": "shipment slips by ten days"}
    ex = UntrustedExtraction.model_validate({"items": [item], "injection_suspected": False})
    assert untrusted_extraction_violations(ex, ["ev_1"]) == []
    bad = UntrustedExtraction.model_validate(
        {"items": [{**item, "source_evidence_id": "ev_9", "quote": "q" * 201}], "injection_suspected": True}
    )
    problems = untrusted_extraction_violations(bad, ["ev_1"])
    assert any("unknown source_evidence_id" in p for p in problems)
    assert any("quote longer" in p for p in problems)
    with pytest.raises(ValidationError):
        UntrustedExtraction.model_validate({"items": [{**item, "kind": "instruction"}], "injection_suspected": False})


# ───────────────────────────── scenario (90 §5) ─────────────────────────────

ALL_OPS = [
    {"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": ["PO-26-0101#1"]}, "days": 7, "prov": "decision"},
    {"op": "capacity_scale", "target": {"kinds": ["chokepoint"], "ids": ["SUEZ"]}, "factor_bp": 0, "prov": "assumption"},
]
BASE_ASSUMPTIONS = [
    {"key": "delay_days", "label": "Delay", "value_text": "7 days", "prov": "decision"},
    {"key": "ttr", "label": "TTR", "value_text": "3 weeks", "prov": "assumption"},
]


def _scenario(assumptions: list[dict[str, Any]] | None = None) -> DisruptionScenario:
    return DisruptionScenario.model_validate(
        {"id": "s", "tenant": "demo-retail", "mode": "actual", "event_family": "SUP", "event_type": "SUP.delay",
         "description": "d", "perturbations": ALL_OPS, "matched": [{"type": "po", "code": "P1", "label": "P1"}],
         "assumptions": assumptions if assumptions is not None else copy.deepcopy(BASE_ASSUMPTIONS),
         "config_version": "v1"}
    )


def test_assumption_defaults_are_omitted() -> None:
    a = Assumption.model_validate(BASE_ASSUMPTIONS[1])
    assert a.unconfirmed is False and a.source_evidence_ids == []
    assert a.model_dump(mode="json") == BASE_ASSUMPTIONS[1]
    flagged = Assumption.model_validate(
        {**BASE_ASSUMPTIONS[1], "prov": "inference", "unconfirmed": True, "source_evidence_ids": ["ev_1"]}
    )
    assert flagged.model_dump(mode="json")["unconfirmed"] is True


def test_m4_list_defaults_are_not_shared() -> None:
    a, b = (Assumption.model_validate(BASE_ASSUMPTIONS[1]) for _ in range(2))
    assert a.source_evidence_ids is not b.source_evidence_ids
    p, q = (ApproachCardProps.model_validate(good_props("approach_card@1")) for _ in range(2))
    assert p.agents is not q.agents and p.editable is not q.editable
    # the JSON Schema advertises the default so generated Zod fills it in
    schema = Assumption.model_json_schema()
    assert schema["properties"]["source_evidence_ids"]["default"] == []
    assert schema["properties"]["unconfirmed"]["default"] is False


@pytest.mark.parametrize(
    "bad",
    [
        {"prov": "fact", "unconfirmed": True},
        {"prov": "decision", "unconfirmed": True},
        {"prov": "inference", "unconfirmed": "yes"},
        {"prov": "inference", "source_evidence_ids": ["ev_1", "ev_1"]},
        {"prov": "inference", "source_evidence_ids": ["e1"]},
    ],
)
def test_assumption_rejects(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        Assumption.model_validate({**BASE_ASSUMPTIONS[1], **bad})


def test_fingerprint_unchanged_for_pre_m4_scenarios() -> None:
    # Adding the M4 fields must not move existing fingerprints.
    fp = _scenario().fingerprint()
    explicit = _scenario([{**a, "unconfirmed": False, "source_evidence_ids": []} for a in BASE_ASSUMPTIONS])
    assert explicit.fingerprint() == fp
    flagged = _scenario([BASE_ASSUMPTIONS[0], {**BASE_ASSUMPTIONS[1], "unconfirmed": True}])
    assert flagged.fingerprint() != fp


def test_confirm_assumptions() -> None:
    sc = _scenario([BASE_ASSUMPTIONS[0], {**BASE_ASSUMPTIONS[1], "prov": "inference", "unconfirmed": True,
                                          "source_evidence_ids": ["ev_1"]}])
    assert [a.key for a in sc.pending_confirmation] == ["ttr"]
    confirmed = sc.confirm_assumptions(["ttr"])
    assert confirmed.pending_confirmation == []
    ttr = confirmed.assumptions[1]
    assert ttr.prov is Provenance.DECISION and ttr.source_evidence_ids == ["ev_1"]
    assert confirmed.fingerprint() != sc.fingerprint()
    assert sc.pending_confirmation  # original untouched
    with pytest.raises(ValueError, match="unknown assumption keys"):
        sc.confirm_assumptions(["nope"])
    with pytest.raises(ValueError, match="no assumption keys"):
        sc.confirm_assumptions([])


# ───────────────────────────── blocks ───────────────────────────────────────

# sha256 of block_to_wire(...) for the M3 example envelopes, computed before M4.
PRE_M4_BLOCK_HASHES = {
    "approach_card@1": "f90b93529a2ee4a2a5de0b5d4c65b8ef5b4d08223971f16f3c56c02e33bc1bab",
    "recommended_option@1": "04f34390b3800b126153b7fd9a5ce47df2b2004024dd0a88f75f8635eed37147",
    "narrative@1": "559d80a6f19dc5eba3ff616b1337dbe3733a9f10fc585fa7f556700380d4ddc2",
    "kpi_strip@1": "fe763c145764487cbe3346b208b68b5fd39f9659718cb63749e14a7a5934bdf8",
}


@pytest.mark.parametrize("key", sorted(PRE_M4_BLOCK_HASHES))
def test_pre_m4_blocks_serialize_unchanged(key: str) -> None:
    wire = block_to_wire(parse_block(envelope(key)))
    assert not {"agents", "editable", "unconfirmed", "what_if", "what_changed"} & set(wire["props"])
    assert set(wire["props"]) == set(GOOD_PROPS[key])
    assert sha256_hex(wire) == PRE_M4_BLOCK_HASHES[key]


def _approach(**over: Any) -> dict[str, Any]:
    props = good_props("approach_card@1")
    props.update(
        mode="what_if",
        agents=[{"slug": "sourcing", "display_name": "Sourcing analyst"},
                {"slug": "logistics", "display_name": "Logistics analyst"}],
        checks=[{"task_id": "t1", "sentence": "Open POs on VN lanes via Suez"}, "A plain M1 check"],
        editable=[{"key": "duration_days", "label": "Duration", "kind": "days", "value_int": 21},
                  {"key": "scope", "label": "Scope", "kind": "ids", "value_ids": ["SUEZ"]}],
    )
    props.update(over)
    return props


def test_approach_card_m4_round_trip() -> None:
    raw = _approach()
    props = ApproachCardProps.model_validate(raw)
    block = make_block("approach_card", props, title="Approach",
                       provenance={"kind": "inference", "engine": "keelson-frame@0.2", "as_of": "2026-10-07"})
    wire = block_to_wire(block)["props"]
    assert wire["checks"] == raw["checks"]
    assert wire["agents"] == raw["agents"]
    assert wire["editable"][0] == {**raw["editable"][0], "value_ids": None}
    assert wire["editable"][1] == {**raw["editable"][1], "value_int": None}


@pytest.mark.parametrize(
    "over",
    [
        {"checks": [{"task_id": "t1", "sentence": "a"}, {"task_id": "t1", "sentence": "b"}]},
        {"checks": [{"task_id": "t1", "sentence": ""}]},
        {"checks": [{"task_id": "t1"}]},
        {"agents": [{"slug": "sourcing", "display_name": "S"}, {"slug": "sourcing", "display_name": "S2"}]},
        {"editable": [{"key": "d", "label": "D", "kind": "days", "value_int": 1},
                      {"key": "d", "label": "D", "kind": "days", "value_int": 2}]},
        {"editable": [{"key": "d", "label": "D", "kind": "days", "value_ids": ["x"]}]},
        {"editable": [{"key": "s", "label": "S", "kind": "ids", "value_int": 3}]},
        {"editable": [{"key": "s", "label": "S", "kind": "ids", "value_ids": ["a", "a"]}]},
        {"editable": [{"key": "Bad-Key", "label": "S", "kind": "pct", "value_int": 3}]},
        {"editable": [{"key": "s", "label": "S", "kind": "hours", "value_int": 3}]},
    ],
)
def test_approach_card_m4_rejects(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ApproachCardProps.model_validate(_approach(**over))


def _recommended(**over: Any) -> dict[str, Any]:
    props = good_props("recommended_option@1")
    props.update(
        unconfirmed=[{"key": "delay_days", "label": "Delay", "value_text": "10 days",
                      "source": "Supplier message (unverified)"}],
        actions=[
            {"type": "apply", "label": "Apply changes for me", "enabled": False,
             "disabled_reason": "Confirm before applying: Delay"},
            {"type": "confirm_assumptions", "label": "Confirm values", "enabled": True},
        ],
    )
    props.update(over)
    return props


def test_recommended_option_unconfirmed_round_trip() -> None:
    raw = _recommended()
    props = RecommendedOptionProps.model_validate(raw)
    assert props.what_if is False
    wire = props.model_dump(mode="json")
    assert "what_if" not in wire and wire["unconfirmed"] == raw["unconfirmed"]


def test_recommended_option_what_if_with_promote() -> None:
    raw = _recommended(
        unconfirmed=[], what_if=True,
        actions=[
            {"type": "apply", "label": "Apply", "enabled": False,
             "disabled_reason": "What-if case: promote it to an actual case first"},
            {"type": "promote", "label": "Promote to actual", "enabled": True},
        ],
    )
    wire = RecommendedOptionProps.model_validate(raw).model_dump(mode="json")
    assert wire["what_if"] is True and "unconfirmed" not in wire


@pytest.mark.parametrize(
    "over",
    [
        {"actions": [{"type": "apply", "label": "Apply", "enabled": True}]},
        {"unconfirmed": [], "what_if": True, "actions": [{"type": "apply", "label": "Apply", "enabled": True}]},
        {"unconfirmed": [], "actions": [{"type": "confirm_assumptions", "label": "Confirm", "enabled": True}]},
        {"actions": [{"type": "promote", "label": "Promote", "enabled": True}]},
        {"actions": [{"type": "promote", "label": "Promote", "enabled": False}]},
        {"unconfirmed": [{"key": "k", "label": "L", "value_text": "v", "source": "s"}] * 2},
        {"unconfirmed": [{"key": "k", "label": "L", "value_text": "v", "source": ""}]},
        {"actions": [{"type": "rollback", "label": "Undo", "enabled": True}]},
        {"what_if": "yes"},
    ],
)
def test_recommended_option_m4_rejects(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        RecommendedOptionProps.model_validate(_recommended(**over))


def test_narrative_what_changed() -> None:
    n = NarrativeProps.model_validate({"markdown": "Updated plan.", "what_changed": "Plan v2: delay 7 → 14 days"})
    assert n.model_dump(mode="json")["what_changed"].startswith("Plan v2")
    assert NarrativeProps.model_validate({"markdown": "x"}).model_dump(mode="json") == {"markdown": "x"}
    with pytest.raises(ValidationError):
        NarrativeProps.model_validate({"markdown": "x", "what_changed": ""})


# ───────────────────────────── runs ─────────────────────────────────────────

PRE_M4_RUN_HASHES = {
    "action": "a297cce7f4968e11a81cb411e09dfdf57874c5e78ccfad68739be73b9fa144b0",
    "resume": "4dcea0af497f87f03fda34214426052e549c79fefd3c43e2a1948972d0279a3d",
}


def _run(kind: str, payload: dict[str, Any]) -> RunRequest:
    return RunRequest.model_validate(
        {"run_id": "r", "tenant": "demo-retail", "case_id": "c", "kind": kind, "actor": ACTOR, "payload": payload}
    )


def test_pre_m4_run_requests_serialize_unchanged() -> None:
    action = _run("action", {"type": "apply", "option_id": "opt-A", "plan_version": 1})
    resume = _run("resume", {"interrupt_id": "approach-c-1", "decision": "edit", "edits": "make it 10 days",
                             "decided_by": [DECIDER]})
    assert sha256_hex(action.model_dump(mode="json")) == PRE_M4_RUN_HASHES["action"]
    assert sha256_hex(resume.model_dump(mode="json")) == PRE_M4_RUN_HASHES["resume"]


def test_confirm_assumptions_action() -> None:
    raw = {"type": "confirm_assumptions", "option_id": "opt-A", "plan_version": 2, "assumption_keys": ["delay_days"]}
    run = _run("action", raw)
    assert isinstance(run.payload, ActionPayload)
    assert run.payload.model_dump(mode="json") == raw
    promote = ActionPayload.model_validate({"type": "promote", "option_id": "opt-A", "plan_version": 1})
    assert "assumption_keys" not in promote.model_dump(mode="json")


@pytest.mark.parametrize(
    "raw",
    [
        {"type": "confirm_assumptions", "option_id": "o", "plan_version": 1},
        {"type": "confirm_assumptions", "option_id": "o", "plan_version": 1, "assumption_keys": ["a", "a"]},
        {"type": "confirm_assumptions", "option_id": "o", "plan_version": 1, "assumption_keys": [""]},
        {"type": "apply", "option_id": "o", "plan_version": 1, "assumption_keys": ["a"]},
        {"type": "rollback", "option_id": "o", "plan_version": 1},
    ],
)
def test_action_payload_rejects(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ActionPayload.model_validate(raw)


def test_typed_edit_resume() -> None:
    raw = {
        "interrupt_id": "approach-c-2", "decision": "edit",
        "edits": [{"key": "duration_days", "value_int": 14}, {"key": "scope", "value_ids": ["SUEZ", "BAB_EL_MANDEB"]}],
        "removed_checks": ["t3"], "decided_by": [DECIDER],
    }
    run = _run("resume", raw)
    assert isinstance(run.payload, ResumePayload)
    edits = run.payload.edits
    assert isinstance(edits, list) and isinstance(edits[0], ApproachEdit)
    dumped = run.payload.model_dump(mode="json", exclude_none=True)
    assert dumped["removed_checks"] == ["t3"]
    assert dumped["edits"] == [{"key": "duration_days", "value_int": 14},
                               {"key": "scope", "value_ids": ["SUEZ", "BAB_EL_MANDEB"]}]
    only_removed = ResumePayload.model_validate(
        {"interrupt_id": "approach-c-2", "decision": "edit", "removed_checks": ["t1"]}
    )
    assert only_removed.edits is None


@pytest.mark.parametrize(
    "raw",
    [
        {"decision": "edit"},
        {"decision": "edit", "edits": ""},
        {"decision": "edit", "edits": []},
        {"decision": "edit", "edits": [{"key": "d"}]},
        {"decision": "edit", "edits": [{"key": "d", "value_int": 1, "value_ids": ["x"]}]},
        {"decision": "edit", "edits": [{"key": "d", "value_ids": []}]},
        {"decision": "edit", "edits": [{"key": "d", "value_ids": ["x", "x"]}]},
        {"decision": "edit", "edits": [{"key": "d", "value_int": 1}, {"key": "d", "value_int": 2}]},
        {"decision": "edit", "edits": [{"key": "D!", "value_int": 1}]},
        {"decision": "edit", "edits": [{"key": "d", "value_int": 1.5}]},
        {"decision": "edit", "removed_checks": ["t1", "t1"]},
        {"decision": "confirm", "edits": [{"key": "d", "value_int": 1}]},
        {"decision": "confirm", "removed_checks": ["t1"]},
    ],
)
def test_resume_rejects(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ResumePayload.model_validate({"interrupt_id": "approach-c-2", **raw})


# ───────────────────────────── agents ───────────────────────────────────────


def test_playbook_analyst_is_a_known_agent() -> None:
    assert AGENT_DISPLAY_NAMES["playbook"] == "Playbook analyst"
    for slug in ("inventory_demand", "sourcing", "logistics", "critic", "recommender"):
        assert slug in AGENT_DISPLAY_NAMES


def test_m4_integration_stages_and_planner_row() -> None:
    from keelson_contracts.agui import NODE_STAGES, Stage

    assert AGENT_DISPLAY_NAMES["planner"] == "Planner"
    for node in ("prepare_resimulate", "prepare_reinvestigate", "promote_case", "confirm_assumptions"):
        assert NODE_STAGES[node] is Stage.UNDERSTANDING
    assert NODE_STAGES["explain"] is Stage.READY_FOR_REVIEW


def test_approach_response_schema_describes_typed_edits() -> None:
    """The approach interrupt's ``responseSchema`` advertises what the decision route takes:
    M1 text or typed edits (``ApproachEdit``) plus ``removed_checks``."""
    from keelson_contracts.agui import approach_response_schema
    from keelson_contracts.runs import MAX_EDIT_IDS, MAX_PAYLOAD_KEYS

    schema = approach_response_schema()
    props = schema["properties"]
    assert schema["additionalProperties"] is False and schema["required"] == ["decision"]
    assert props["decision"]["enum"] == ["cancel", "confirm", "edit", "what_if"]
    text, typed = props["edits"]["anyOf"]
    assert text == {"type": "string"}
    assert typed["maxItems"] == MAX_PAYLOAD_KEYS
    item = typed["items"]
    assert set(item["properties"]) == {"key", "value_int", "value_ids"} and item["additionalProperties"] is False
    assert item["properties"]["value_ids"]["maxItems"] == MAX_EDIT_IDS
    assert props["removed_checks"]["maxItems"] == MAX_PAYLOAD_KEYS
    json.dumps(schema)  # JSON-serialisable (sent in RUN_FINISHED interrupts)
