"""Perturbation union, scenario model and fingerprint."""

from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from keelson_contracts.scenario import (
    PERTURBATION_OPS,
    CapacityScale,
    DemandScale,
    DisruptionScenario,
    LeadTimeShift,
    Perturbation,
    fingerprint,
)

PERTURBATION: TypeAdapter[Any] = TypeAdapter(Perturbation)

# The three examples from docs/contracts/40-optimizer.md must parse unchanged.
OPTIMIZER_EXAMPLES = [
    {"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": ["PO-26-0101#1"]}, "days": 7, "prov": "decision"},
    {"op": "demand_scale", "target": {"kinds": ["cluster"], "ids": ["UK1-COLD-A"]}, "factor_bp": 11800, "weeks": ["2026-W45"], "prov": "assumption"},
    {"op": "capacity_scale", "target": {"kinds": ["lane"], "ids": ["DE1>NL1"]}, "factor_bp": 3000, "prov": "fact"},
]

ALL_OPS = [
    *OPTIMIZER_EXAMPLES,
    {"op": "cost_scale", "target": {"kinds": ["lane"], "ids": ["DE1>UK1"]}, "factor_bp": 12500, "cost_component": "freight", "prov": "assumption"},
    {"op": "yield_scale", "target": {"kinds": ["factory"], "ids": ["F-BD-01"]}, "factor_bp": 9000, "p10": 8000, "p90": 9500, "prov": "inference"},
    {"op": "compliance_block", "target": {"kinds": ["supplier"], "ids": ["TRIM-BD-01"]}, "reason": "Failed audit", "window": {"start": "2026-10-05", "end": "2026-11-01"}, "prov": "fact"},
    {"op": "inventory_hold", "target": {"kinds": ["dc"], "ids": ["NL1"]}, "qty": 500, "reason": "QA hold", "prov": "fact"},
]


@pytest.mark.parametrize("raw", ALL_OPS, ids=lambda r: r["op"])
def test_every_op_parses_and_round_trips(raw: dict[str, Any]) -> None:
    parsed = PERTURBATION.validate_python(raw)
    assert parsed.op == raw["op"]
    assert PERTURBATION.dump_python(parsed, mode="json", exclude_none=True) == {
        **raw,
        **({"cost_component": "all"} if raw["op"] == "cost_scale" and "cost_component" not in raw else {}),
    }


def test_union_covers_all_ops() -> None:
    assert sorted(PERTURBATION_OPS) == sorted({r["op"] for r in ALL_OPS})


def test_optimizer_examples_types() -> None:
    lt, ds, cs = (PERTURBATION.validate_python(r) for r in OPTIMIZER_EXAMPLES)
    assert isinstance(lt, LeadTimeShift) and lt.days == 7
    assert isinstance(ds, DemandScale) and ds.weeks == ["2026-W45"]
    assert isinstance(cs, CapacityScale) and cs.factor_bp == 3000


@pytest.mark.parametrize(
    "raw",
    [
        {**OPTIMIZER_EXAMPLES[0], "days": 7.5},  # float
        {**OPTIMIZER_EXAMPLES[1], "factor_bp": 1.18},  # float factor
        {**OPTIMIZER_EXAMPLES[1], "factor_bp": -1},
        {**OPTIMIZER_EXAMPLES[1], "weeks": ["2026-W99"]},
        {**OPTIMIZER_EXAMPLES[0], "p10": 9},  # p10 > p50
        {**OPTIMIZER_EXAMPLES[0], "p90": 3},  # p90 < p50
        {**OPTIMIZER_EXAMPLES[0], "op": "teleport"},
        {**OPTIMIZER_EXAMPLES[0], "target": {"kinds": ["receipt"], "ids": []}},
        {**OPTIMIZER_EXAMPLES[0], "target": {"kinds": ["planet"], "ids": ["X"]}},
        {**OPTIMIZER_EXAMPLES[0], "prov": "guess"},
        {**OPTIMIZER_EXAMPLES[2], "window": {"start": "2026-10-10", "end": "2026-10-01"}},
        {"op": "inventory_hold", "target": {"kinds": ["dc"], "ids": ["NL1"]}, "p10": 1, "reason": "x", "prov": "fact"},
        {**OPTIMIZER_EXAMPLES[2], "unknown": 1},
    ],
)
def test_invalid_perturbations(raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PERTURBATION.validate_python(raw)


def scenario(**kw: Any) -> DisruptionScenario:
    data: dict[str, Any] = {
        "id": "scn-1",
        "tenant": "demo-retail",
        "mode": "actual",
        "event_family": "SUP",
        "event_type": "SUP.SUPPLIER_DELAY",
        "description": "Meghna Trims delayed 7 days",
        "perturbations": copy.deepcopy(OPTIMIZER_EXAMPLES),
        "matched": [
            {"type": "po", "code": "PO-26-0101", "label": "PO-26-0101"},
            {"type": "supplier", "code": "TRIM-BD-01", "label": "Meghna Trims Ltd"},
        ],
        "assumptions": [
            {"key": "delay_days", "label": "Delay", "value_text": "7 days", "prov": "decision"},
            {"key": "ttr", "label": "Time to recover", "value_text": "3 weeks", "prov": "assumption"},
        ],
        "snapshot_ref": None,
        "config_version": "tax-1",
    }
    data.update(kw)
    return DisruptionScenario.model_validate(data)


class TestFingerprint:
    def test_stable_and_hex(self) -> None:
        fp = fingerprint(scenario())
        assert len(fp) == 64 and fp == scenario().fingerprint()

    def test_ignores_id_description_and_orderings(self) -> None:
        base = scenario()
        reordered = scenario(
            id="scn-2",
            description="different words",
            perturbations=list(reversed(OPTIMIZER_EXAMPLES)),
            matched=list(reversed(base.model_dump(mode="json")["matched"])),
            assumptions=list(reversed(base.model_dump(mode="json")["assumptions"])),
        )
        assert fingerprint(reordered) == fingerprint(base)

    @pytest.mark.parametrize(
        "change",
        [
            {"mode": "what_if"},
            {"config_version": "tax-2"},
            {"snapshot_ref": "snap-1"},
            {"perturbations": [{**OPTIMIZER_EXAMPLES[0], "days": 8}]},
            {"assumptions": [{"key": "ttr", "label": "Time to recover", "value_text": "3 weeks", "prov": "decision"}]},
        ],
    )
    def test_sensitive_to_inputs(self, change: dict[str, Any]) -> None:
        assert fingerprint(scenario(**change)) != fingerprint(scenario())

    def test_unconfirmed_assumptions_listed(self) -> None:
        s = scenario()
        assert [a.key for a in s.unconfirmed_assumptions] == ["ttr"]


def test_scenario_rejects_duplicate_assumption_keys() -> None:
    dup = {"key": "ttr", "label": "x", "value_text": "y", "prov": "fact"}
    with pytest.raises(ValidationError, match="unique"):
        scenario(assumptions=[dup, dup])
