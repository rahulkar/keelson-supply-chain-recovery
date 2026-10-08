"""keelson_contracts.optimizer: shapes from docs/contracts/40-optimizer.md."""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pytest
from keelson_contracts import optimizer as opt
from pydantic import TypeAdapter, ValidationError


def problem(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tenant": "demo-retail",
        "as_of": "2026-10-05",
        "horizon": {"start_week": "2026-W41", "weeks": 9},
        "dcs": [{"code": "UK1", "outbound_cap_units_wk": 50000}, {"code": "NL1"}, {"code": "DE1"}],
        "clusters": [{"code": "UK1-COLD-A", "dc_code": "UK1"}],
        "skus": [
            {"code": "PUF-W-BLK-M", "style_code": "PUF-W-BLK", "retail_price_cents": 2500, "unit_cost_cents": 900}
        ],
        "inventory": [
            {"location_type": "dc", "location_code": "UK1", "sku": "PUF-W-BLK-M", "on_hand": 1200, "allocated": 0,
             "in_transit": 0}
        ],
        "cluster_inventory": [{"cluster_code": "UK1-COLD-A", "sku": "PUF-W-BLK-M", "on_hand": 80}],
        "receipts": [{"ref": "PO-26-0101#1", "dc_code": "UK1", "sku": "PUF-W-BLK-M", "qty": 3000, "eta": "2026-11-02"}],
        "demand": [{"cluster_code": "UK1-COLD-A", "sku": "PUF-W-BLK-M", "iso_week": "2026-W45", "units": 140}],
        "lanes": [{"from_dc": "DE1", "to_dc": "NL1", "transit_days": 2, "cost_per_unit_cents": 35,
                   "max_units_wk": 20000}],
        "protected_min": [{"cluster_code": "UK1-COLD-A", "sku": "PUF-W-BLK-M", "units": 10}],
    }
    data.update(over)
    return data


CONTRACT_PERTURBATIONS = [
    {"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": ["PO-26-0101#1"]}, "days": 7, "prov": "decision"},
    {"op": "demand_scale", "target": {"kinds": ["cluster"], "ids": ["UK1-COLD-A"]}, "factor_bp": 11800,
     "weeks": ["2026-W45"], "prov": "assumption"},
    {"op": "capacity_scale", "target": {"kinds": ["lane"], "ids": ["DE1>NL1"]}, "factor_bp": 3000, "prov": "fact"},
]

PLAN_SPEC = {
    "id": "opt-A",
    "title": "Rebalance between DCs",
    "levers": [{"code": "reallocate_inventory",
                "params": {"max_units_total": 20000, "allowed_lanes": ["DE1>NL1", "DE1>UK1", "PL1>NL1"]}}],
    "weights": {"lost_margin": 10000, "transfer_cost": 10000},
}


def test_problem_data_contract_example_round_trips() -> None:
    pd_ = opt.ProblemData.model_validate(problem())
    assert pd_.as_of == dt.date(2026, 10, 5)
    assert pd_.receipts[0].eta == dt.date(2026, 11, 2)
    assert pd_.lanes[0].code == "DE1>NL1" == opt.lane_code("DE1", "NL1")
    assert pd_.inventory[0].reserved == 0  # optional, defaults to 0
    dumped = pd_.model_dump(mode="json")
    assert dumped["receipts"][0]["eta"] == "2026-11-02"
    assert opt.ProblemData.model_validate_json(json.dumps(dumped)) == pd_


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("skus", 0, "retail_price_cents"), 25.0),  # floats never allowed for money
        (("skus", 0, "retail_price_cents"), "2500"),  # nor numeric strings
        (("demand", 0, "units"), True),  # nor booleans
        (("demand", 0, "units"), -1),  # quantities are non-negative
        (("demand", 0, "iso_week"), "2026-W54"),
        (("horizon", "weeks"), 0),
        (("receipts", 0, "eta"), "02/11/2026"),
    ],
)
def test_strict_scalars(path: tuple[Any, ...], value: Any) -> None:
    data = problem()
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        opt.ProblemData.model_validate(data)


@pytest.mark.parametrize(
    ("over", "message"),
    [
        ({"clusters": [{"code": "X-1", "dc_code": "ZZ1"}]}, "unknown dc_code ZZ1"),
        ({"dcs": [{"code": "UK1"}, {"code": "UK1"}, {"code": "DE1"}, {"code": "NL1"}]}, "duplicate dc codes"),
        ({"receipts": [{"ref": "R#1", "dc_code": "UK1", "sku": "NOPE", "qty": 1, "eta": "2026-11-02"}]},
         "unknown sku NOPE"),
        ({"lanes": [{"from_dc": "DE1", "to_dc": "DE1", "transit_days": 1, "cost_per_unit_cents": 1,
                     "max_units_wk": 1}]}, "from_dc == to_dc"),
    ],
)
def test_referential_integrity(over: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        opt.ProblemData.model_validate(problem(**over))


def test_extra_fields_rejected() -> None:
    with pytest.raises(ValidationError):
        opt.ProblemData.model_validate(problem(surprise=1))


def test_contract_perturbations_parse_with_active_union() -> None:
    req = opt.ImpactRequest.model_validate({"data": problem(), "perturbations": CONTRACT_PERTURBATIONS})
    assert [p.op for p in req.perturbations] == ["lead_time_shift", "demand_scale", "capacity_scale"]
    assert req.perturbations[0].days == 7
    assert req.perturbations[1].factor_bp == 11800 and req.perturbations[1].weeks == ["2026-W45"]
    assert req.perturbations[2].target.ids == ["DE1>NL1"]
    assert opt.PERTURBATION_SOURCE in {"keelson_contracts.scenario", "keelson_contracts.optimizer"}
    dumped = req.model_dump(mode="json")["perturbations"]
    for original, out in zip(CONTRACT_PERTURBATIONS, dumped, strict=True):
        assert {k: out[k] for k in original} == original


def test_local_m1_union_accepts_other_ops_and_keeps_fields() -> None:
    ta = TypeAdapter(list[opt.M1Perturbation])
    parsed = ta.validate_python(
        [*CONTRACT_PERTURBATIONS, {"op": "cost_scale", "target": {"kinds": ["sku"], "ids": ["X"]}, "factor_bp": 12000,
                                   "prov": "assumption"}]
    )
    assert isinstance(parsed[0], opt.LeadTimeShift)
    assert isinstance(parsed[1], opt.DemandScale)
    assert isinstance(parsed[2], opt.CapacityScale)
    assert isinstance(parsed[3], opt.OtherPerturbation)
    assert parsed[3].op == "cost_scale" and parsed[3].model_dump()["factor_bp"] == 12000
    with pytest.raises(ValidationError):  # known op with a malformed body is not "other"
        ta.validate_python([{"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": ["R"]}, "days": 1.5,
                             "prov": "fact"}])
    assert set(opt.LeadTimeShift.model_fields) == {"op", "target", "days", "prov"}
    assert set(opt.DemandScale.model_fields) == {"op", "target", "factor_bp", "weeks", "prov"}
    assert set(opt.CapacityScale.model_fields) == {"op", "target", "factor_bp", "prov"}
    assert set(opt.PerturbationTarget.model_fields) == {"kinds", "ids"}


def test_optimize_request_contract_example() -> None:
    req = opt.OptimizeRequest.model_validate(
        {"data": problem(), "perturbations": CONTRACT_PERTURBATIONS, "plan_spec": PLAN_SPEC,
         "solver": {"time_limit_s": 5, "seed": 7}}
    )
    lever = req.plan_spec.levers[0]
    assert isinstance(lever, opt.ReallocateInventoryLever)
    assert lever.params.allowed_lanes == ["DE1>NL1", "DE1>UK1", "PL1>NL1"]
    assert req.plan_spec.weights.lost_margin == 10000
    # Defaults: do-nothing plan, solver defaults.
    minimal = opt.OptimizeRequest.model_validate(
        {"data": problem(), "plan_spec": {"id": "base", "title": "Do nothing"}}
    )
    assert minimal.plan_spec.levers == [] and minimal.perturbations == []
    assert minimal.solver == opt.SolverOptions(time_limit_s=5, seed=0)


@pytest.mark.parametrize(
    "bad",
    [
        {"code": "teleport", "params": {}},
        {"code": "reallocate_inventory", "params": {"allowed_lanes": ["DE1-NL1"]}},
        {"code": "reallocate_inventory", "params": {"max_units_total": -5}},
    ],
)
def test_bad_levers_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        opt.PlanSpec.model_validate({"id": "x", "title": "x", "levers": [bad]})


def kpis(**over: Any) -> dict[str, Any]:
    base = {
        "units_at_risk": 120,
        "revenue_at_risk_cents": 300000,
        "margin_at_risk_cents": 192000,
        "shortage_cluster_weeks": 2,
        "first_breach": [{"dc_code": "UK1", "cluster_code": "UK1-COLD-A", "style_code": "PUF-W-BLK",
                          "week": "2026-W45", "date": "2026-11-02"}],
    }
    base.update(over)
    return base


def test_optimize_response_contract_example() -> None:
    resp = opt.OptimizeResponse.model_validate(
        {
            "status": "OPTIMAL",
            "plan_spec_id": "opt-A",
            "kpis_before": kpis(),
            "kpis_after": kpis(units_at_risk=0, revenue_at_risk_cents=0, margin_at_risk_cents=0,
                               shortage_cluster_weeks=0, first_breach=[]),
            "kpi_delta": {"units_recovered": 120, "margin_recovered_cents": 192000, "transfer_cost_cents": 14000,
                          "net_benefit_cents": 178000, "transfers": 1},
            "decisions": [{"lever": "reallocate_inventory", "from_dc": "DE1", "to_dc": "NL1", "sku": "PUF-W-BLK-M",
                           "qty": 400, "ship_week": "2026-W43", "arrive_week": "2026-W44"}],
            "actions": [{"seq": 1, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
                         "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-19",
                                     "lines": [{"sku": "PUF-W-BLK-M", "qty": 400}]}}],
            "explain": {"binding_constraints": [{"name": "lane DE1>NL1 cap W43", "shadow_price_cents": 1565}]},
            "solver": {"name": "GLOP", "ortools": "9.15.6755", "params": {"time_limit_s": 5}, "seed": 7,
                       "inputs_hash": "a" * 64, "solve_ms": 12},
        }
    )
    action = resp.actions[0].model_dump(mode="json")
    assert action == {
        "seq": 1, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
        "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-19",
                    "lines": [{"sku": "PUF-W-BLK-M", "qty": 400}]},
    }
    assert resp.warnings == [] and resp.solver.status_detail is None


def test_action_rejects_empty_or_non_positive_lines() -> None:
    payload = {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-19", "lines": []}
    with pytest.raises(ValidationError):
        opt.Action(seq=1, op="transfer_order.create", payload=payload)  # type: ignore[arg-type]
    payload["lines"] = [{"sku": "X", "qty": 0}]
    with pytest.raises(ValidationError):
        opt.Action.model_validate({"seq": 1, "op": "transfer_order.create", "payload": payload})


def test_impact_response_shape() -> None:
    resp = opt.ImpactResponse.model_validate(
        {
            "kpis": kpis(),
            "by_dc": [{"dc_code": "UK1", "units_at_risk": 120, "revenue_at_risk_cents": 300000}],
            "timeline": [{"cluster_code": "UK1-COLD-A", "style_code": "PUF-W-BLK", "week": "2026-W45",
                          "projected_available": -60, "demand": 140, "shortage": 60}],
            "baseline_kpis": kpis(units_at_risk=0, revenue_at_risk_cents=0, margin_at_risk_cents=0,
                                  shortage_cluster_weeks=0, first_breach=[]),
            "provenance": {"engine": "keelson-opt", "version": "0.1.0", "inputs_hash": "0" * 64,
                           "computed_at": "2026-10-05T09:00:00Z"},
        }
    )
    assert resp.timeline[0].projected_available == -60
    assert resp.kpis.first_breach[0].date == dt.date(2026, 11, 2)
    with pytest.raises(ValidationError):
        opt.Provenance(version="0.1.0", inputs_hash="not-a-hash", computed_at=dt.datetime.now(dt.UTC))


def test_json_schemas_generate() -> None:
    for model in (opt.ImpactRequest, opt.ImpactResponse, opt.OptimizeRequest, opt.OptimizeResponse, opt.LeverInfo,
                  opt.ReallocateInventoryParams):
        schema = model.model_json_schema()
        assert schema["type"] == "object"
    assert set(opt.ReallocateInventoryParams.model_json_schema()["properties"]) == {"max_units_total", "allowed_lanes"}


@pytest.mark.parametrize("week", ["2025-W53", "2027-W53", "0000-W01"])
def test_nonexistent_iso_weeks_rejected(week: str) -> None:
    with pytest.raises(ValidationError, match="not an ISO week"):
        opt.ProblemData.model_validate(
            problem(demand=[{"cluster_code": "UK1-COLD-A", "sku": "PUF-W-BLK-M", "iso_week": week, "units": 1}])
        )


@pytest.mark.parametrize("week", ["2026-W53", "2020-W53", "2026-W01"])
def test_existing_iso_weeks_accepted(week: str) -> None:
    pd_ = opt.ProblemData.model_validate(problem(horizon={"start_week": week, "weeks": 1}))
    assert pd_.horizon.start_week == week
