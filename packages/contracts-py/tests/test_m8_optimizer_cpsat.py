"""keelson_contracts M8 CP-SAT additions (docs/contracts/125-m8-optimizer-cpsat.md): the opt-in
``SolverOptions.integer_stage``, the sourcing terms on ``AltSource`` and the lever params, the
per-solve records ``SolverInfo.stages``, ``CompareOption.integer`` and the guard reason. Every field
is omitted from dumps at its default, so earlier payloads and their hashes are unchanged."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from examples import GOOD_PROPS
from keelson_contracts import admin
from keelson_contracts import blocks as bl
from keelson_contracts import optimizer as opt
from keelson_contracts.common import sha256_hex
from pydantic import ValidationError
from test_m3_optimizer import response, v2_problem
from test_optimizer_models import PLAN_SPEC

ALT = v2_problem()["alt_sources"][0]


def test_defaults_are_omitted_and_hashes_unchanged() -> None:
    raw = {"data": v2_problem(), "plan_spec": PLAN_SPEC, "solver": {"time_limit_s": 5, "seed": 7}}
    req = opt.OptimizeRequest.model_validate(raw)
    dumped = req.model_dump(mode="json")
    assert dumped["solver"] == {"time_limit_s": 5, "seed": 7}
    assert set(dumped["data"]["alt_sources"][0]) == set(ALT)
    explicit = copy.deepcopy(raw)
    explicit["solver"]["integer_stage"] = "repair"
    explicit["data"]["alt_sources"][0].update(order_multiple_units=1, moq_basis="po", capacity_group=None,
                                              max_orders=None)  # fmt: skip
    same = opt.OptimizeRequest.model_validate(explicit).model_dump(mode="json")
    assert same == dumped and sha256_hex(same) == sha256_hex(dumped)
    cpsat = copy.deepcopy(raw)
    cpsat["solver"]["integer_stage"] = "cpsat"
    assert opt.OptimizeRequest.model_validate(cpsat).model_dump(mode="json")["solver"]["integer_stage"] == "cpsat"
    resp = opt.OptimizeResponse.model_validate(response())
    assert "stages" not in resp.model_dump(mode="json")["solver"]


def test_sourcing_terms_round_trip() -> None:
    src = opt.AltSource.model_validate({**ALT, "order_multiple_units": 12, "moq_basis": "style",
                                        "capacity_group": "F-MA-01", "max_orders": 2})  # fmt: skip
    assert src.moq_effective == 600  # 600 is a whole number of cases of 12
    assert opt.AltSource.model_validate({**ALT, "order_multiple_units": 7}).moq_effective == 602
    dumped = src.model_dump(mode="json")
    assert {k: dumped[k] for k in ("order_multiple_units", "moq_basis", "capacity_group", "max_orders")} == {
        "order_multiple_units": 12, "moq_basis": "style", "capacity_group": "F-MA-01", "max_orders": 2}
    assert opt.AltSource.model_validate_json(src.model_dump_json()) == src
    params = opt.ResourceAltSupplierParams.model_validate({"max_buys_per_line": 2})
    assert params.model_dump(mode="json")["max_buys_per_line"] == 2
    assert "max_buys_per_line" not in opt.ResourceAltSupplierParams().model_dump(mode="json")
    reseq = opt.ResequenceProductionParams.model_validate({"frozen_days": 3, "lateness_cents_per_unit_day": 25})
    assert reseq.model_dump(mode="json") == {"line_codes": None, "max_orders_per_line": None, "frozen_days": 3,
                                             "lateness_cents_per_unit_day": 25}  # fmt: skip
    assert set(opt.ResequenceProductionParams().model_dump(mode="json")) == {"line_codes", "max_orders_per_line"}


@pytest.mark.parametrize(
    ("model", "raw"),
    [
        (opt.AltSource, {**ALT, "order_multiple_units": 0}),
        (opt.AltSource, {**ALT, "order_multiple_units": 6.0}),
        (opt.AltSource, {**ALT, "order_multiple_units": "6"}),
        (opt.AltSource, {**ALT, "moq_basis": "sku"}),
        (opt.AltSource, {**ALT, "max_orders": 0}),
        (opt.AltSource, {**ALT, "capacity_group": ""}),
        (opt.ResourceAltSupplierParams, {"max_buys_per_line": 0}),
        (opt.ResequenceProductionParams, {"frozen_days": -1}),
        (opt.ResequenceProductionParams, {"lateness_cents_per_unit_day": 1.5}),
        (opt.SolverOptions, {"integer_stage": "mip"}),
    ],
)
def test_bad_values_are_rejected(model: type[Any], raw: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        model.model_validate(raw)


def _stage(**over: Any) -> dict[str, Any]:
    stage: dict[str, Any] = {"name": "integer", "solver": "CP-SAT", "status": "OPTIMAL", "used": True, "seed": 7,
                             "model_hash": "a" * 64, "objective": 100, "bound": 100, "det_time_limit_ms": 150,
                             "det_time_ms": 12, "num_workers": 1, "num_variables": 40, "num_constraints": 30}  # fmt: skip
    stage.update(over)
    return stage


def test_solver_stages() -> None:
    raw = response()
    raw["solver"]["stages"] = [_stage(name="lp_phase1", solver="GLOP", objective=None, bound=None,
                                      det_time_limit_ms=None, det_time_ms=None, num_workers=None), _stage()]  # fmt: skip
    resp = opt.OptimizeResponse.model_validate(raw)
    assert [s.name for s in resp.solver.stages] == ["lp_phase1", "integer"]
    again = opt.OptimizeResponse.model_validate_json(resp.model_dump_json())
    assert again == resp
    skipped = opt.SolverStage.model_validate(_stage(status="SKIPPED", used=False, fallback_reason="model_too_large"))
    assert skipped.fallback_reason == "model_too_large"
    assert not [k for k in opt.SolverStage.model_fields if "wall" in k]
    # review fix: a name embeds a line code (``Code``, up to 128 characters) with its prefix and
    # suffix, so the longest valid line code still gets its records
    longest = f"resequence:{'L' * 128}:tiebreak"
    assert opt.SolverStage.model_validate(_stage(name=longest)).name == longest
    with pytest.raises(ValidationError):
        opt.SolverStage.model_validate(_stage(name=""))


@pytest.mark.parametrize(
    "over",
    [
        {"model_hash": "A" * 64},
        {"model_hash": "a" * 63},
        {"status": "TIMEOUT"},
        {"fallback_reason": "because"},
        {"num_workers": 0},
        {"used": 1},
        {"det_time_ms": -1},
        {"wall_ms": 12},
    ],
)
def test_bad_solver_stages(over: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        opt.SolverStage.model_validate(_stage(**over))


def test_lever_params_schemas_stay_self_contained() -> None:
    for info in opt.lever_catalogue():
        assert "$ref" not in json.dumps(info.params_schema)
        assert info.params_schema["additionalProperties"] is False
    by_code = {i.code: i.params_schema["properties"] for i in opt.lever_catalogue()}
    assert "max_buys_per_line" in by_code["resource_alt_supplier"]
    assert {"frozen_days", "lateness_cents_per_unit_day"} <= set(by_code["resequence_production"])


def test_compare_option_integer() -> None:
    props = copy.deepcopy(GOOD_PROPS["scenario_compare@1"])
    plain = bl.ScenarioCompareProps.model_validate(props)
    assert all(o.integer is None for o in plain.options)
    assert all("integer" not in o for o in plain.model_dump(mode="json")["options"])
    props["options"][0]["integer"] = {"method": "cpsat_feasible", "gap_bp": 40}
    parsed = bl.ScenarioCompareProps.model_validate(props)
    assert parsed.options[0].integer == bl.CompareInteger(method="cpsat_feasible", gap_bp=40)
    assert parsed.model_dump(mode="json")["options"][0]["integer"] == {
        "method": "cpsat_feasible", "gap_bp": 40, "fallback_reason": None}
    bl.CompareInteger(method="lp_rounding", fallback_reason="time_limit_no_solution")
    for bad in (
        {"method": "lp_rounding", "gap_bp": 10},
        {"method": "cpsat_optimal", "fallback_reason": "no_improvement"},
        {"method": "cpsat_feasible", "gap_bp": 10_001},
        {"method": "mip"},
        {"method": "lp_rounding", "fallback_reason": "because"},
    ):
        with pytest.raises(ValidationError):
            bl.CompareInteger.model_validate(bad)


def test_compliance_block_guard_reason() -> None:
    assert "compliance_block_removed" in admin.GUARD_REASONS_BY_KIND["agent_spec"]
    assert "compliance_block_removed" not in admin.GUARD_REASONS_BY_KIND["policy_set"]
