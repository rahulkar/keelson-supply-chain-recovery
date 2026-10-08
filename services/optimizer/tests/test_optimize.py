"""reallocate_inventory LP on the golden small-seed fixture: status, invariants, KPI
recomputation, action shape, caps, duals, golden regression."""

from __future__ import annotations

import datetime as dt
import json
import os
from collections import defaultdict
from pathlib import Path
from typing import Any

import pytest
from keelson_contracts.optimizer import ImpactRequest, OptimizeRequest, OptimizeResponse, ProblemData

from keelson_opt.impact import Transfer, kpis, simulate
from keelson_opt.isoweeks import parse_iso_week
from keelson_opt.scn import build_scenario
from keelson_opt.service import PlanSpecError, run_impact, run_optimize

GOLDEN = Path(__file__).parent / "fixtures" / "small_problem.golden.json"


def optimize(raw: dict[str, Any]) -> OptimizeResponse:
    return run_optimize(OptimizeRequest.model_validate(raw))


@pytest.fixture(scope="module")
def solved(small_raw: dict[str, Any]) -> OptimizeResponse:
    return optimize(small_raw)


def transfers_of(resp: OptimizeResponse, raw: dict[str, Any]) -> tuple[Any, list[Transfer]]:
    """Rebuild the plan's transfers from the *decisions* (independently of the LP)."""
    req = OptimizeRequest.model_validate(raw)
    scn = build_scenario(req.data, req.perturbations)
    lanes = {ln.code: ln for ln in scn.lanes}
    week = {w: i for i, w in enumerate(scn.weeks)}
    out = [
        Transfer(lane=lanes[f"{d.from_dc}>{d.to_dc}"], sku=d.sku, qty=d.qty, ship_week=week[d.ship_week])
        for d in resp.decisions
    ]
    return scn, out


def test_status_and_improvement(solved: OptimizeResponse) -> None:
    assert solved.status == "OPTIMAL"
    assert solved.plan_spec_id == "opt-A"
    assert solved.decisions and solved.actions
    d = solved.kpi_delta
    assert d.margin_recovered_cents > 0
    assert d.net_benefit_cents == d.margin_recovered_cents - d.transfer_cost_cents > 0
    assert solved.kpis_after.margin_at_risk_cents < solved.kpis_before.margin_at_risk_cents
    assert solved.kpis_after.units_at_risk < solved.kpis_before.units_at_risk // 10
    assert d.units_recovered == solved.kpis_before.units_at_risk - solved.kpis_after.units_at_risk
    assert d.transfers == len(solved.actions)
    assert solved.solver.name == "GLOP"
    assert solved.solver.ortools == "9.15.6755"
    assert solved.solver.seed == 7
    assert solved.solver.params["glop_parameters"] == "random_seed:7"
    assert solved.solver.status_detail is None


def test_kpis_before_equal_impact_and_after_recomputed_from_rounded_plan(
    solved: OptimizeResponse, small_raw: dict[str, Any]
) -> None:
    impact = run_impact(ImpactRequest(data=small_raw["data"], perturbations=small_raw["perturbations"]))
    assert solved.kpis_before == impact.kpis
    scn, transfers = transfers_of(solved, small_raw)
    assert solved.kpis_after == kpis(simulate(scn, transfers))
    lanes = {ln.code: ln for ln in scn.lanes}
    cost = sum(d.qty * lanes[f"{d.from_dc}>{d.to_dc}"].cost_per_unit_cents for d in solved.decisions)
    assert solved.kpi_delta.transfer_cost_cents == cost


def test_flow_conservation_and_non_negative_inventory(solved: OptimizeResponse, small_raw: dict[str, Any]) -> None:
    scn, transfers = transfers_of(solved, small_raw)
    # Every unit leaving a DC arrives at another DC inside the horizon.
    out_units: dict[str, int] = defaultdict(int)
    in_units: dict[str, int] = defaultdict(int)
    for tr in transfers:
        assert tr.arrive_week < scn.horizon
        out_units[tr.sku] += tr.qty
        in_units[tr.sku] += tr.qty
    assert out_units == in_units
    # Decisions and action lines carry exactly the same units.
    lines = sum(line.qty for a in solved.actions for line in a.payload.lines)
    assert lines == sum(d.qty for d in solved.decisions)
    # Sources never ship more than Merxa ATP (on_hand - allocated - reserved).
    shipped: dict[tuple[str, str], int] = defaultdict(int)
    for tr in transfers:
        shipped[(tr.lane.from_dc, tr.sku)] += tr.qty
    for key, qty in shipped.items():
        assert qty <= scn.dc_atp[key]
    # Simulated DC pools and cluster stocks with the plan stay >= 0 and conserve units.
    res = simulate(scn, transfers)
    assert all(v >= 0 for series in res.dc_end.values() for v in series)
    for sku in scn.skus:
        supply = sum(scn.dc_supply.get((d, sku), 0) + sum(scn.receipts.get((d, sku), [0])) for d in scn.dcs)
        supply += sum(scn.cluster_on_hand.get((c, sku), 0) for c in scn.clusters)
        served = sum(sum(cs.demand) - sum(cs.lost) for (c, k), cs in res.series.items() if k == sku)
        end = sum(max(cs.pab[-1], 0) for (c, k), cs in res.series.items() if k == sku)
        end += sum(res.dc_end[(d, sku)][-1] for d in scn.dcs)
        assert supply == served + end


def test_respects_allowed_lanes_caps_and_total(solved: OptimizeResponse, small_raw: dict[str, Any]) -> None:
    allowed = set(small_raw["plan_spec"]["levers"][0]["params"]["allowed_lanes"])
    per_lane_week: dict[tuple[str, str], int] = defaultdict(int)
    for d in solved.decisions:
        per_lane_week[(f"{d.from_dc}>{d.to_dc}", d.ship_week)] += d.qty
        assert f"{d.from_dc}>{d.to_dc}" in allowed
    caps = {f"{ln['from_dc']}>{ln['to_dc']}": ln["max_units_wk"] for ln in small_raw["data"]["lanes"]}
    for (lane, _week), qty in per_lane_week.items():
        assert qty <= caps[lane]
    assert sum(d.qty for d in solved.decisions) <= 20000


def test_actions_are_valid_transfer_order_creates(solved: OptimizeResponse, small_raw: dict[str, Any]) -> None:
    skus = {s["code"] for s in small_raw["data"]["skus"]}
    assert [a.seq for a in solved.actions] == list(range(1, len(solved.actions) + 1))
    keys = [(a.payload.ship_date, a.payload.from_dc, a.payload.to_dc) for a in solved.actions]
    assert keys == sorted(keys) and len(set(keys)) == len(keys)  # grouped + deterministic order
    as_of = dt.date.fromisoformat(small_raw["data"]["as_of"])
    by_group = defaultdict(dict)
    for d in solved.decisions:
        by_group[(parse_iso_week(d.ship_week), d.from_dc, d.to_dc)][d.sku] = d.qty
    for a in solved.actions:
        dumped = a.model_dump(mode="json")
        assert dumped["op"] == "transfer_order.create"
        assert dumped["target_ref"] is None and dumped["expected_version"] is None
        assert set(dumped["payload"]) == {"from_dc", "to_dc", "ship_date", "lines"}
        p = a.payload
        assert p.from_dc != p.to_dc
        assert p.ship_date.weekday() == 0 and p.ship_date >= as_of  # Monday, not in the past
        assert [ln.sku for ln in p.lines] == sorted({ln.sku for ln in p.lines})
        assert all(ln.qty > 0 and ln.sku in skus for ln in p.lines)
        assert {ln.sku: ln.qty for ln in p.lines} == by_group[(p.ship_date, p.from_dc, p.to_dc)]
    for d in solved.decisions:
        lag = (parse_iso_week(d.arrive_week) - parse_iso_week(d.ship_week)).days // 7
        assert lag == 1  # transit 2-4 days -> ceil(days / 7) = 1 week


def test_lane_capacity_scale_binds_and_reports_shadow_price(small: dict[str, Any]) -> None:
    small["perturbations"].append(
        {"op": "capacity_scale", "target": {"kinds": ["lane"], "ids": ["DE1>UK1"]}, "factor_bp": 300, "prov": "fact"}
    )
    resp = optimize(small)
    assert resp.status == "OPTIMAL"
    weekly: dict[str, int] = defaultdict(int)
    for d in resp.decisions:
        if (d.from_dc, d.to_dc) == ("DE1", "UK1"):
            weekly[d.ship_week] += d.qty
    assert weekly and all(q <= 360 for q in weekly.values())  # 12000 * 3%
    names = {b.name: b.shadow_price_cents for b in resp.explain.binding_constraints}
    assert "lane DE1>UK1 cap W41" in names
    # One more unit of lane capacity is worth at most the best margin minus lane cost.
    assert 0 < names["lane DE1>UK1 cap W41"] <= 1900 - 60
    assert resp.explain.binding_constraints == sorted(
        resp.explain.binding_constraints, key=lambda b: (-b.shadow_price_cents, b.name)
    )


def test_max_units_total_binds(small: dict[str, Any]) -> None:
    small["plan_spec"]["levers"][0]["params"]["max_units_total"] = 500
    resp = optimize(small)
    assert resp.status == "OPTIMAL"
    assert sum(d.qty for d in resp.decisions) <= 500
    assert sum(d.qty for d in resp.decisions) >= 490  # floor rounding loses < 1 unit per line
    names = {b.name: b.shadow_price_cents for b in resp.explain.binding_constraints}
    assert names.get("max_units_total", 0) > 1000  # ~ margin of a recovered unit minus cost
    assert resp.kpi_delta.margin_recovered_cents > 0


def test_do_nothing_plan(small: dict[str, Any]) -> None:
    small["plan_spec"] = {"id": "opt-0", "title": "Do nothing", "levers": []}
    resp = optimize(small)
    assert resp.status == "OPTIMAL"
    assert resp.decisions == [] and resp.actions == []
    assert resp.kpis_after == resp.kpis_before
    assert resp.kpi_delta.units_recovered == resp.kpi_delta.transfers == 0
    assert resp.solver.name == "none"


def test_no_perturbation_needs_no_transfers(small: dict[str, Any]) -> None:
    small["perturbations"] = []
    resp = optimize(small)
    assert resp.status == "OPTIMAL"
    assert resp.kpis_before.units_at_risk == 0
    assert resp.actions == []  # transfers only cost money when nothing is at risk


def test_zero_lost_weight_moves_nothing(small: dict[str, Any]) -> None:
    small["plan_spec"]["weights"] = {"lost_margin": 0, "transfer_cost": 10000}
    assert optimize(small).actions == []


def test_unknown_allowed_lane_warns_and_duplicate_lever_rejected(small: dict[str, Any]) -> None:
    small["plan_spec"]["levers"][0]["params"]["allowed_lanes"] = ["DE1>NL1", "XX1>NL1"]
    resp = optimize(small)
    assert any("XX1>NL1" in w for w in resp.warnings)
    assert {(d.from_dc, d.to_dc) for d in resp.decisions} == {("DE1", "NL1")}
    small["plan_spec"]["levers"].append(small["plan_spec"]["levers"][0])
    with pytest.raises(PlanSpecError):
        optimize(small)


def test_golden_regression(solved: OptimizeResponse) -> None:
    """Exact regression on the small story (everything but ``solve_ms``).

    Regenerate after an intentional model change with ``KEELSON_UPDATE_GOLDEN=1``."""
    got = solved.model_dump(mode="json")
    got["solver"].pop("solve_ms")
    if os.environ.get("KEELSON_UPDATE_GOLDEN") == "1":
        GOLDEN.write_text(json.dumps(got, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    expected = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert got == expected


class _FakeSolver:
    status_string = "time limit reached"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("OPTIMAL", "OPTIMAL"),
        ("FEASIBLE", "FEASIBLE"),
        ("INFEASIBLE", "INFEASIBLE"),
        ("NOT_SOLVED", "INFEASIBLE"),
        ("ABNORMAL", "INFEASIBLE"),
        ("UNBOUNDED", "INFEASIBLE"),
    ],
)
def test_status_mapping(raw: str, expected: str) -> None:
    from ortools.linear_solver.python import model_builder as mb

    from keelson_opt.levers.reallocate import _map_status

    status, detail = _map_status(getattr(mb.SolveStatus, raw), _FakeSolver())  # type: ignore[arg-type]
    assert status == expected
    assert (detail is None) == (raw == "OPTIMAL")
    if detail is not None:
        assert detail.startswith(raw)


def test_no_solution_yields_empty_plan(small: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from keelson_opt.levers import composed

    def fake(*_args: Any, **_kw: Any) -> composed.LpOut:
        return _lp_out("INFEASIBLE", "NOT_SOLVED: time limit reached", [])

    monkeypatch.setattr(composed, "solve_composed", fake)
    resp = optimize(small)
    assert resp.status == "INFEASIBLE"
    assert resp.solver.status_detail == "NOT_SOLVED: time limit reached"
    assert resp.decisions == [] and resp.actions == []
    assert resp.kpis_after == resp.kpis_before


def test_feasible_stop_keeps_plan_but_reports_no_shadow_prices(
    small: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Duals are only meaningful at an optimal basis: a FEASIBLE (time-limited) stop must
    not report binding constraints, while its primal plan is still used."""
    from keelson_opt.levers import reallocate

    small["plan_spec"]["levers"][0]["params"]["max_units_total"] = 500  # makes a row bind
    assert optimize(small).explain.binding_constraints  # OPTIMAL: reported

    monkeypatch.setattr(reallocate, "_map_status", lambda raw, solver: ("FEASIBLE", "FEASIBLE: forced"))
    resp = optimize(small)
    assert resp.status == "FEASIBLE"
    assert resp.decisions and resp.actions
    assert resp.explain.binding_constraints == []


def test_status_with_solution_missing_degrades_to_infeasible(
    small: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """model_builder returns pd.NA values when it holds no solution; that must not 500."""
    from keelson_opt.levers import reallocate

    monkeypatch.setattr(reallocate, "_map_status", lambda raw, solver: ("FEASIBLE", "FEASIBLE: forced"))
    monkeypatch.setattr(reallocate, "_primal_values", lambda solver, helper, x: None)
    resp = optimize(small)
    assert resp.status == "INFEASIBLE"
    assert resp.solver.status_detail is not None and "no primal solution" in resp.solver.status_detail
    assert resp.decisions == [] and resp.actions == []
    assert resp.kpis_after == resp.kpis_before


def test_is_finite_number_handles_pandas_na() -> None:
    import pandas as pd

    from keelson_opt.levers.reallocate import _is_finite_number

    assert _is_finite_number(3.0) and _is_finite_number(0)
    assert not _is_finite_number(pd.NA)
    assert not _is_finite_number(float("nan"))
    assert not _is_finite_number(None)


def test_replanning_after_commit_does_not_double_count_reserved(small: dict[str, Any]) -> None:
    """Apply the plan the way Merxa does (reserved += qty at source, in_transit += qty at
    destination) and re-run impact: total network stock must be unchanged, so the
    recovered position cannot exceed what the plan itself achieved."""
    resp = optimize(small)
    assert resp.actions
    physical = sum(r["on_hand"] + r.get("in_transit", 0) for r in small["data"]["inventory"])
    inv = {(r["location_code"], r["sku"]): r for r in small["data"]["inventory"] if r["location_type"] == "dc"}
    for a in resp.actions:
        for line in a.payload.lines:
            src = inv[(a.payload.from_dc, line.sku)]
            src["reserved"] = src.get("reserved", 0) + line.qty
            dst = inv.setdefault(
                (a.payload.to_dc, line.sku),
                {"location_type": "dc", "location_code": a.payload.to_dc, "sku": line.sku, "on_hand": 0},
            )
            dst["in_transit"] = dst.get("in_transit", 0) + line.qty
    small["data"]["inventory"] = list(inv.values())
    req = ImpactRequest(data=small["data"], perturbations=small["perturbations"])
    # The committed units now exist once (in transit to the destination), not twice.
    assert sum(build_scenario(req.data).dc_supply.values()) == physical
    # In transit counts from week 0, i.e. no later than the planned arrival.
    assert run_impact(req).kpis.units_at_risk <= resp.kpis_after.units_at_risk


def test_plan_worse_than_doing_nothing_is_never_returned(
    small: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """If the rounded LP plan does not improve the recomputed weighted net benefit (the
    LP only approximates the simulator), the do-nothing plan is returned with a warning."""
    from keelson_opt.levers import composed

    small["perturbations"] = []  # nothing at risk: any transfer is pure cost
    req = OptimizeRequest.model_validate(small)
    lane = next(ln for ln in build_scenario(req.data).lanes if ln.code == "DE1>NL1")
    useless = [Transfer(lane=lane, sku="PUF-W-BLK-M", qty=10, ship_week=0)]

    def fake(*_args: Any, **_kw: Any) -> composed.LpOut:
        return _lp_out("OPTIMAL", None, useless)

    monkeypatch.setattr(composed, "solve_composed", fake)
    resp = run_optimize(req)
    assert resp.status == "OPTIMAL"
    assert resp.decisions == [] and resp.actions == []
    assert resp.kpis_after == resp.kpis_before
    assert resp.kpi_delta.transfer_cost_cents == resp.kpi_delta.net_benefit_cents == 0
    assert resp.solver.params["plan_fallback"] == "do_nothing"
    assert any("do-nothing plan" in w for w in resp.warnings)


def test_requirements_split_sales_and_buffer() -> None:
    """Buffer = build-up of the protected minimum (non-decreasing level, <= pmin); sales
    + buffer = the simulator's request under ample supply."""
    from keelson_opt.levers.reallocate import _requirements

    data = {
        "tenant": "t1",
        "as_of": "2026-10-05",
        "horizon": {"start_week": "2026-W41", "weeks": 3},
        "dcs": [{"code": "D"}],
        "clusters": [{"code": "D-A", "dc_code": "D"}, {"code": "D-B", "dc_code": "D"}],
        "skus": [{"code": "S1", "style_code": "ST", "retail_price_cents": 2000, "unit_cost_cents": 500}],
        "cluster_inventory": [{"cluster_code": "D-A", "sku": "S1", "on_hand": 10}],
        "demand": [
            {"cluster_code": c, "sku": "S1", "iso_week": f"2026-W{w}", "units": u}
            for c, w, u in [("D-A", 41, 8), ("D-A", 42, 6), ("D-A", 43, 1), ("D-B", 41, 5), ("D-B", 42, 5)]
        ],
        "protected_min": [
            {"cluster_code": "D-A", "sku": "S1", "units": 3},
            {"cluster_code": "D-B", "sku": "S1", "units": 4},
        ],
    }
    scn = build_scenario(ProblemData.model_validate(data))
    reqs = _requirements(scn)
    # D-A: own 10 covers 8, then 2 of 6; buffer of 3 builds up 1 then 2 more.
    assert reqs[("D-A", "S1")] == ([0, 4, 1], [1, 2, 0])
    # D-B: no stock: sales 5, 5, 0; buffer 4 pre-positioned in week 0.
    assert reqs[("D-B", "S1")] == ([5, 5, 0], [4, 0, 0])


def _buffer_heavy_case() -> dict[str, Any]:
    """Randomized case (seed 55 of the review sweep) on which pricing protected-minimum
    buffers at the full lost margin produced a plan with net benefit -574 EUR."""
    import random

    from .synthetic import synthetic_request

    seed = 55
    rng = random.Random(1000 + seed)
    raw = synthetic_request(n_styles=rng.randint(1, 3), clusters_per_dc=rng.randint(1, 3), seed=seed)
    d = raw["data"]
    d["protected_min"] = [
        {"cluster_code": c["code"], "sku": s["code"], "units": rng.randint(0, 60)}
        for c in d["clusters"]
        for s in d["skus"]
        if rng.random() < 0.7
    ]
    for c in d["cluster_inventory"]:
        c["on_hand"] = rng.randint(0, 3 * c["on_hand"] + 5)
    for inv in d["inventory"]:
        inv["on_hand"] = rng.randint(0, inv["on_hand"] * 2 + 10)
        inv["allocated"] = rng.randint(0, inv["on_hand"] // 2)
        inv["in_transit"] = rng.randint(0, 50)
    for ln in d["lanes"]:
        ln["transit_days"] = rng.choice([0, 2, 6, 8, 15])
        ln["cost_per_unit_cents"] = rng.choice([30, 300, 900])
        ln["max_units_wk"] = rng.choice([50, 500, 20000])
    dcs = ["UK1", "NL1", "DE1", "PL1"]
    raw["perturbations"] = [
        {
            "op": "lead_time_shift",
            "target": {"kinds": ["dc"], "ids": [rng.choice(dcs)]},
            "days": rng.randint(-10, 25),
            "prov": "decision",
        },
        {
            "op": "demand_scale",
            "target": {"kinds": ["dc"], "ids": [rng.choice(dcs)]},
            "factor_bp": rng.choice([5000, 13000, 20000]),
            "prov": "assumption",
        },
    ]
    raw["plan_spec"]["levers"][0]["params"] = {
        "max_units_total": rng.choice([None, 100, 3000]),
        "allowed_lanes": None,
    }
    return raw


def test_buffers_are_not_valued_as_lost_sales() -> None:
    resp = optimize(_buffer_heavy_case())
    assert resp.status == "OPTIMAL"
    assert "plan_fallback" not in resp.solver.params  # the LP itself gets it right
    assert resp.actions
    assert resp.kpi_delta.net_benefit_cents > 0


def _lp_out(status: Any, detail: str | None, transfers: list[Transfer]) -> Any:
    """A stand-in for the composed LP result (only transfers, no other lever)."""
    from keelson_opt.levers.composed import LpOut

    return LpOut(
        status=status,
        status_detail=detail,
        transfers=transfers,
        exp_values={},
        alt_values={},
        exp_cands={},
        alt_cands={},
        binding=[],
        params={"time_limit_s": 1},
        solve_ms=5,
    )
