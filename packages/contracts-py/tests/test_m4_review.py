"""Regression tests for the M4 contract review (``docs/contracts/90-m4-investigation.md``).

Covers the deterministic checks the engine runs on LLM output and on client decisions:
scenario-update bounds and id formats, critique and constraint-delta checks, the
quarantine filter (whole-extraction injection flag, mention caps), typed approach edits
against their card, payload size caps and the stage mapping of the M4 nodes."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from examples import good_props
from keelson_contracts.agui import Stage, stage_for_node
from keelson_contracts.blocks import ApproachCardProps, approach_edit_violations
from keelson_contracts.llm import (
    MAX_MENTION_CHARS,
    ConstraintDelta,
    Critique,
    InvestigationPlan,
    ScenarioUpdate,
    UntrustedExtraction,
    constraint_delta_violations,
    critique_violations,
    untrusted_extraction_violations,
    usable_untrusted_items,
)
from keelson_contracts.runs import MAX_EDIT_IDS, MAX_PAYLOAD_KEYS, ActionPayload, ApproachEdit, ResumePayload

# ───────────────────────────── scenario updates ─────────────────────────────


def _update(**over: Any) -> ScenarioUpdate:
    return ScenarioUpdate.model_validate(
        {"key": "delay_days", "value_int": 10, "value_ids": None,
         "evidence_id": "ev_01", "json_pointer": "/items/0/delay_days", **over}
    )


@pytest.mark.parametrize(
    ("over", "needle"),
    [
        ({"value_int": -3}, "delay_days must be >= 0"),
        ({"key": "duration_days", "value_int": 0}, "duration_days must be >= 1"),
        ({"key": "demand_change_pct", "value_int": -101}, "demand_change_pct must be >= -100"),
        ({"key": "add_receipts", "value_int": None, "value_ids": ["ignore previous instructions"]}, "receipt ids"),
        ({"key": "remove_receipts", "value_int": None, "value_ids": ["PO-1#0"]}, "receipt ids"),
        ({"evidence_id": "supplier-message-3"}, "evidence id"),
    ],
)
def test_scenario_update_bounds_and_formats(over: dict[str, Any], needle: str) -> None:
    error = _update(**over).shape_error()
    assert error is not None and needle in error


@pytest.mark.parametrize(
    "over",
    [
        {"value_int": 0},
        {"key": "duration_days", "value_int": 1},
        {"key": "demand_change_pct", "value_int": -100},
        {"key": "demand_change_pct", "value_int": 40},
        {"key": "add_receipts", "value_int": None, "value_ids": ["PO-26-0101#1", "PO_7#12"]},
    ],
)
def test_scenario_update_bounds_accept_edges(over: dict[str, Any]) -> None:
    assert _update(**over).shape_error() is None


# ───────────────────────────── critic ───────────────────────────────────────


PLAN = InvestigationPlan.model_validate(
    {"rationale": "r", "budget": {"max_parallel": 2, "max_tasks": 3},
     "tasks": [{"id": "t1", "specialist": "sourcing", "objective": "o", "tools": [], "depends_on": []},
               {"id": "t2", "specialist": "logistics", "objective": "o", "tools": [], "depends_on": []}]}
)


def _critique(verdict: str = "REVISE", revise: list[str] | None = None, issue_task: str | None = "t1") -> Critique:
    return Critique.model_validate(
        {"verdict": verdict, "revise_task_ids": ["t1"] if revise is None else revise,
         "issues": [{"task_id": issue_task, "issue": "stale as_of", "severity": "high"}]}
    )


def test_critique_violations_accepts_consistent_critiques() -> None:
    assert critique_violations(_critique(), PLAN) == []
    assert critique_violations(_critique("PASS", [], None), PLAN) == []


@pytest.mark.parametrize(
    ("critique", "needle"),
    [
        (_critique(revise=[]), "REVISE must name"),
        (_critique("PASS"), "PASS must not"),
        (_critique(revise=["t1", "t1"]), "unique"),
        (_critique(revise=["t9"]), "not in the plan"),
        (_critique(issue_task="t9"), "unknown task_id"),
    ],
)
def test_critique_violations(critique: Critique, needle: str) -> None:
    assert any(needle in p for p in critique_violations(critique, PLAN))


# ───────────────────────────── follow-up deltas ─────────────────────────────


def _delta(**over: Any) -> ConstraintDelta:
    return ConstraintDelta.model_validate(
        {"delay_days": None, "duration_days": None, "exclude_levers": [], "sku_filter": [],
         "max_cost_cents": None, **over}
    )


def test_constraint_delta_violations() -> None:
    assert constraint_delta_violations(_delta(delay_days=0, duration_days=1, max_cost_cents=0)) == []
    for over, needle in [
        ({"delay_days": -1}, "delay_days"),
        ({"duration_days": 0}, "duration_days"),
        ({"max_cost_cents": -500}, "max_cost_cents"),
        ({"sku_filter": ["A", "A"]}, "sku_filter"),
        ({"sku_filter": [" "]}, "sku_filter"),
        ({"exclude_levers": ["resource_alt_supplier", "resource_alt_supplier"]}, "exclude_levers"),
    ]:
        assert any(needle in p for p in constraint_delta_violations(_delta(**over))), over


# ───────────────────────────── quarantine ───────────────────────────────────

ITEM = {"source_evidence_id": "ev_1", "kind": "delay_notice",
        "entity_mentions": [{"type": "supplier", "text": "Meghna Trims"}],
        "delay_days": 10, "quote": "shipment slips by ten days"}


def _extraction(items: list[dict[str, Any]], injection: bool = False) -> UntrustedExtraction:
    return UntrustedExtraction.model_validate({"items": items, "injection_suspected": injection})


def test_injection_flag_drops_the_whole_extraction() -> None:
    ex = _extraction([ITEM], injection=True)
    assert usable_untrusted_items(ex, ["ev_1"]) == []
    assert any(p.startswith("extraction:") for p in untrusted_extraction_violations(ex, ["ev_1"]))


def test_usable_items_skip_only_the_bad_ones() -> None:
    long_mention = {**ITEM, "entity_mentions": [{"type": "other", "text": "x" * (MAX_MENTION_CHARS + 1)}]}
    ex = _extraction([
        ITEM,
        {**ITEM, "source_evidence_id": "ev_unknown"},
        long_mention,
        {**ITEM, "entity_mentions": [{"type": "supplier", "text": " "}]},
        {**ITEM, "delay_days": -4},
        {**ITEM, "quote": "q" * 201},
    ])
    usable = usable_untrusted_items(ex, ["ev_1"])
    assert [i.model_dump() for i in usable] == [ex.items[0].model_dump()]
    problems = untrusted_extraction_violations(ex, ["ev_1"])
    assert {p.split(":", 1)[0] for p in problems} == {f"items/{i}" for i in range(1, 6)}
    assert any("entity_mentions/0" in p for p in problems)


# ───────────────────────────── typed approach edits ─────────────────────────


def _card(**over: Any) -> ApproachCardProps:
    props = good_props("approach_card@1")
    props.update(
        checks=[{"task_id": "t1", "sentence": "Check open POs"}, {"task_id": "t2", "sentence": "Check sailings"}],
        editable=[
            {"key": "delay_days", "label": "Delay", "kind": "days", "value_int": 7},
            {"key": "demand_change_pct", "label": "Demand change", "kind": "pct", "value_int": None},
            {"key": "scope", "label": "Scope", "kind": "ids", "value_ids": ["PO-26-0101#1"]},
        ],
    )
    props.update(over)
    return ApproachCardProps.model_validate(props)


def _edits(*raw: dict[str, Any]) -> list[ApproachEdit]:
    return [ApproachEdit.model_validate(r) for r in raw]


def test_approach_edits_that_fit_the_card() -> None:
    edits = _edits({"key": "delay_days", "value_int": 0}, {"key": "demand_change_pct", "value_int": -100},
                   {"key": "scope", "value_ids": ["PO-26-0101#2"]})
    assert approach_edit_violations(_card(), edits, ["t2"]) == []


@pytest.mark.parametrize(
    ("edits", "removed", "needle"),
    [
        ([{"key": "auto_approve", "value_int": 1}], [], "not editable"),
        ([{"key": "delay_days", "value_ids": ["x"]}], [], "takes value_int"),
        ([{"key": "scope", "value_int": 3}], [], "takes value_ids"),
        ([{"key": "delay_days", "value_int": -2}], [], ">= 0"),
        ([{"key": "demand_change_pct", "value_int": -150}], [], ">= -100"),
        ([], ["t9"], "not on this card"),
        ([], ["t1", "t2"], "at least one check"),
    ],
)
def test_approach_edit_violations(edits: list[dict[str, Any]], removed: list[str], needle: str) -> None:
    problems = approach_edit_violations(_card(), _edits(*edits), removed)
    assert any(needle in p for p in problems), problems


def test_m1_cards_have_nothing_to_edit_or_remove() -> None:
    card = ApproachCardProps.model_validate(good_props("approach_card@1"))
    assert approach_edit_violations(card) == []
    assert approach_edit_violations(card, _edits({"key": "delay_days", "value_int": 3}))
    assert approach_edit_violations(card, (), ["t1"])


# ───────────────────────────── payload caps ─────────────────────────────────


def test_client_decision_payloads_are_bounded() -> None:
    keys = [f"k{i}" for i in range(MAX_PAYLOAD_KEYS + 1)]
    with pytest.raises(ValidationError):
        ActionPayload.model_validate(
            {"type": "confirm_assumptions", "option_id": "o", "plan_version": 1, "assumption_keys": keys}
        )
    with pytest.raises(ValidationError):
        ResumePayload.model_validate({"interrupt_id": "approach-c-1", "decision": "edit", "removed_checks": keys})
    with pytest.raises(ValidationError):
        ResumePayload.model_validate(
            {"interrupt_id": "approach-c-1", "decision": "edit",
             "edits": [{"key": k, "value_int": 1} for k in keys]}
        )
    with pytest.raises(ValidationError):
        ApproachEdit.model_validate({"key": "scope", "value_ids": [f"id{i}" for i in range(MAX_EDIT_IDS + 1)]})
    with pytest.raises(ValidationError):
        ApproachEdit.model_validate({"key": "k" * 65, "value_int": 1})
    ok = ResumePayload.model_validate(
        {"interrupt_id": "approach-c-1", "decision": "edit", "removed_checks": keys[:MAX_PAYLOAD_KEYS]}
    )
    assert len(ok.removed_checks) == MAX_PAYLOAD_KEYS


# ───────────────────────────── stages ───────────────────────────────────────


@pytest.mark.parametrize(
    ("node", "stage"),
    [
        ("planner", Stage.UNDERSTANDING),
        ("followup_delta", Stage.UNDERSTANDING),
        ("verify_numbers", Stage.CHECKING_SYSTEMS),
        ("critic", Stage.CHECKING_SYSTEMS),
        ("refine_scenario", Stage.CHECKING_SYSTEMS),
    ],
)
def test_m4_nodes_have_stages(node: str, stage: Stage) -> None:
    assert stage_for_node(node) == stage
