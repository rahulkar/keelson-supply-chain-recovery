"""M8 whole-unit CP-SAT stage for ``resource_alt_supplier`` + ``expedite_mode_shift``
(``docs/contracts/125-m8-optimizer-cpsat.md``): the opt-in switch, the sourcing terms in both
integer stages, the CP-SAT improvements over the LP rounding, fallbacks, per-solve records and
determinism."""

from __future__ import annotations

import dataclasses
import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from keelson_contracts.optimizer import OptimizeRequest, OptimizeResponse, SolverStage

from keelson_opt import cpsat
from keelson_opt.levers import alt_integer
from keelson_opt.service import run_optimize

from .network_m8 import TRIM_DELAYS, check_invariants, random_problem, request, sourcing_problem

BOTH = [{"code": "expedite_mode_shift"}, {"code": "resource_alt_supplier"}]
ALT = [{"code": "resource_alt_supplier"}]
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def optimize(raw: dict[str, Any]) -> OptimizeResponse:
    resp = run_optimize(OptimizeRequest.model_validate(raw))
    check_invariants(OptimizeRequest.model_validate(raw), resp)
    return resp


def both_modes(raw: dict[str, Any]) -> tuple[OptimizeResponse, OptimizeResponse]:
    rep = dict(raw, solver={**raw["solver"], "integer_stage": "repair"})
    cps = dict(raw, solver={**raw["solver"], "integer_stage": "cpsat"})
    return optimize(rep), optimize(cps)


def body(resp: OptimizeResponse) -> dict[str, Any]:
    out = resp.model_dump(mode="json")
    out["solver"].pop("solve_ms")
    return out


def plan_of(resp: OptimizeResponse) -> dict[str, Any]:
    """Everything the plan decides (actions, decisions, KPIs), without the solver record."""
    out = body(resp)
    out.pop("solver")
    out["warnings"] = [w for w in out["warnings"] if "whole-unit CP-SAT" not in w]
    return out


def creates(resp: OptimizeResponse) -> list[tuple[str, list[tuple[str, int]]]]:
    return [
        (a.payload.eta_dc.isoformat(), [(line.sku, line.qty) for line in a.payload.lines])
        for a in resp.actions
        if a.op == "po.create"
    ]


def integer_stage(resp: OptimizeResponse) -> SolverStage:
    (stage,) = [s for s in resp.solver.stages if s.name == "integer"]
    return stage


# ───────────────────────────── opt-in switch ─────────────────────────────


def test_repair_is_the_default_and_byte_identical() -> None:
    omitted = request(BOTH, stage=None)
    explicit = request(BOTH, stage="repair")
    req = OptimizeRequest.model_validate(explicit)
    assert "integer_stage" not in req.model_dump(mode="json")["solver"]
    a, b = run_optimize(OptimizeRequest.model_validate(omitted)), run_optimize(req)
    assert body(a) == body(b)
    assert a.solver.inputs_hash == b.solver.inputs_hash
    assert a.solver.stages == [] and "stages" not in a.model_dump(mode="json")["solver"]
    assert not {"integer_stage", "integer_method", "cpsat_num_workers"} & set(a.solver.params)
    cps = run_optimize(OptimizeRequest.model_validate(request(BOTH)))
    assert cps.solver.inputs_hash != a.solver.inputs_hash  # the switch is part of the request


def test_cpsat_records_every_solve_without_wall_times() -> None:
    resp = optimize(request(BOTH))
    names = [s.name for s in resp.solver.stages]
    assert names[0] == "lp_phase1" and "integer" in names and "lp_phase2:cpsat" in names
    for s in resp.solver.stages:
        assert s.seed == 3 and s.model_hash is not None and _HEX64.match(s.model_hash)
        assert s.num_variables > 0
    stage = integer_stage(resp)
    assert stage.solver == "CP-SAT" and stage.used and stage.num_workers == 1 and stage.fallback_reason is None
    assert stage.det_time_limit_ms == round(alt_integer.INTEGER_DET_TIME * 1000)
    assert stage.det_time_ms is not None and stage.det_time_ms <= stage.det_time_limit_ms
    assert stage.objective is not None and stage.bound is not None and stage.bound <= stage.objective
    for dumped in resp.model_dump(mode="json")["solver"]["stages"]:
        assert not [k for k in dumped if "wall" in k or (k.endswith("_ms") and not k.startswith("det_"))]
    p = resp.solver.params
    assert p["integer_stage"] == "cpsat" and p["integer_method"] == "cpsat_optimal" and p["integer_gap_bp"] == 0
    assert p["cpsat_num_workers"] == 1 and p["cpsat_deterministic"] is True
    assert "CP-SAT" in resp.solver.name.split("+")
    assert resp.status == "OPTIMAL"


def test_cpsat_requests_are_deterministic() -> None:
    raw = request(BOTH)
    assert body(optimize(raw)) == body(optimize(json.loads(json.dumps(raw))))


# ───────────────────────────── what the CP-SAT stage improves ─────────────────────────────


def test_one_op_per_po_line_resources_one_line_and_splits_the_others() -> None:
    """The repair keeps the larger of split / re-source per line; CP-SAT decides the lines
    together: it re-sources the large TOP-M line and splits the smaller lines onto the shared
    sea-air capacity, which the repair used up on the TOP-M line."""
    rep, cps = both_modes(request(BOTH))
    assert {a.target_ref for a in rep.actions if a.op == "po_line.split"} == {"PO-1#1", "PO-1#2"}
    assert {a.target_ref for a in cps.actions if a.op == "po_line.split"} == {"PO-1#2", "PO-1#3"}
    assert creates(cps) == [("2026-10-16", [("TOP-M", 200)])]
    assert cps.scorecard is not None and rep.scorecard is not None
    assert cps.scorecard.net_benefit_cents > rep.scorecard.net_benefit_cents


def test_moq_per_po_tops_up_like_the_repair() -> None:
    data = sourcing_problem({"moq_units": 300})
    raw = request(ALT, data, perturbations=TRIM_DELAYS[:1])
    rep, cps = both_modes(raw)
    for resp in (rep, cps):
        assert all(sum(q for _s, q in lines) >= 300 for _eta, lines in creates(resp))
    assert cps.scorecard is not None and rep.scorecard is not None
    assert cps.scorecard.net_benefit_cents >= rep.scorecard.net_benefit_cents


def test_order_multiples_whole_cases_in_both_modes() -> None:
    raw = request(BOTH, sourcing_problem({"order_multiple_units": 12, "moq_units": 250}))
    rep, cps = both_modes(raw)
    # moq_effective = ceil(250 / 12) * 12 = 252
    for resp in (rep, cps):
        for _eta, lines in creates(resp):
            assert all(q % 12 == 0 for _s, q in lines) and sum(q for _s, q in lines) >= 252
    assert creates(rep) == []  # the rounding dropped every buy below the MOQ
    assert creates(cps) == [("2026-10-19", [("TEE-M", 120), ("TOP-S", 132)])]
    assert cps.scorecard is not None and rep.scorecard is not None
    assert cps.scorecard.net_benefit_cents > rep.scorecard.net_benefit_cents
    assert cps.solver.params["integer_method"] == "cpsat_optimal"


def test_moq_per_style_counts_each_style_colour() -> None:
    raw = request(ALT, sourcing_problem({"moq_basis": "style", "moq_units": 150}))
    rep, cps = both_modes(raw)
    for resp in (rep, cps):
        assert creates(resp)
        for _eta, lines in creates(resp):
            top = sum(q for s, q in lines if s.startswith("TOP"))
            tee = sum(q for s, q in lines if s.startswith("TEE"))
            assert top in (0,) or top >= 150
            assert tee in (0,) or tee >= 150
    # per PO the M7 MOQ would have accepted a 120-unit TEE line next to TOP: per style it may not
    assert ("2026-10-19", [("TEE-M", 120), ("TOP-M", 200), ("TOP-S", 100)]) not in creates(cps)


def test_capacity_group_shares_the_factory_between_lanes() -> None:
    data = sourcing_problem(
        {"capacity_group": "F-N", "weekly_capacity_units": 300, "moq_units": 100},
        {"capacity_group": "F-N", "weekly_capacity_units": 250, "moq_units": 50},
    )
    rep, cps = both_modes(request(ALT, data))
    for resp in (rep, cps):
        per_week: dict[str, int] = {}
        for eta, lines in creates(resp):
            per_week[eta] = per_week.get(eta, 0) + sum(q for _s, q in lines)
        assert per_week and max(per_week.values()) <= 250  # the group's smallest row


def test_split_limits_max_orders_and_buys_per_line() -> None:
    data = sourcing_problem({"max_orders": 1})
    raw = request([{"code": "resource_alt_supplier", "params": {"max_buys_per_line": 1}}], data)
    rep, cps = both_modes(raw)
    for resp in (rep, cps):
        assert len(creates(resp)) <= 1
    assert cps.scorecard is not None and rep.scorecard is not None
    assert cps.scorecard.net_benefit_cents > rep.scorecard.net_benefit_cents
    assert len({d.replaces_po_line_ref for d in cps.decisions if d.lever == "resource_alt_supplier"}) == 3


def test_premium_budget_is_respected() -> None:
    raw = request([{"code": "resource_alt_supplier", "params": {"max_premium_cents": 90_000}}])
    rep, cps = both_modes(raw)
    for resp in (rep, cps):
        assert sum(d.premium_cents for d in resp.decisions if d.lever == "resource_alt_supplier") <= 90_000


@pytest.mark.parametrize("seed", range(30))
def test_random_networks_cpsat_never_worse_than_repair(seed: int) -> None:
    data, levers = random_problem(seed)
    rep, cps = both_modes(request(levers, data, seed=seed))
    assert rep.scorecard is not None and cps.scorecard is not None
    assert cps.scorecard.net_benefit_cents >= rep.scorecard.net_benefit_cents
    assert cps.status == rep.status
    if cps.solver.params.get("integer_method") == "lp_rounding":
        assert plan_of(cps) == plan_of(rep)


# ───────────────────────────── repair with the M8 terms ─────────────────────────────


def test_repair_without_m8_terms_keeps_the_m7_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(alt_integer, "repair_terms", lambda *a, **k: calls.append(1) or {})
    optimize(request(BOTH, stage="repair"))
    assert calls == []
    run_optimize(OptimizeRequest.model_validate(request(BOTH, sourcing_problem({"order_multiple_units": 6}), stage="repair")))
    assert calls == [1]


# ───────────────────────────── fallbacks ─────────────────────────────


def _fallback(monkeypatch: pytest.MonkeyPatch, reason: str, patch: Callable[[], None], *, skipped: bool) -> None:
    raw = request(BOTH)
    rep = optimize(dict(raw, solver={**raw["solver"], "integer_stage": "repair"}))
    patch()
    resp = optimize(raw)
    assert plan_of(resp) == plan_of(rep), "a fallback returns the repair plan"
    assert resp.status == rep.status
    stage = integer_stage(resp)
    assert stage.used is False and stage.fallback_reason == reason
    assert (stage.status == "SKIPPED") is skipped
    assert resp.solver.params["integer_method"] == "lp_rounding" and resp.solver.params["integer_fallback"] == reason
    assert f"whole-unit CP-SAT stage fell back to LP rounding ({reason})" in " ".join(resp.warnings)
    assert "CP-SAT" not in resp.solver.name.split("+")


def test_fallback_time_limit_without_solution(monkeypatch: pytest.MonkeyPatch) -> None:
    real = alt_integer.run_cpsat

    def unknown(*a: Any, **k: Any) -> cpsat.CpRun:
        return dataclasses.replace(real(*a, **k), status="UNKNOWN", objective=None, bound=None)

    _fallback(monkeypatch, "time_limit_no_solution", lambda: monkeypatch.setattr(alt_integer, "run_cpsat", unknown), skipped=False)


def test_fallback_model_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    _fallback(monkeypatch, "model_too_large", lambda: monkeypatch.setattr(alt_integer, "INTEGER_MAX_VARS", 1), skipped=True)


def test_fallback_wall_clock_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    real = alt_integer.run_cpsat

    def guarded(*a: Any, **k: Any) -> cpsat.CpRun:
        return dataclasses.replace(real(*a, **k), status="FEASIBLE", guard_hit=True)

    _fallback(monkeypatch, "wall_clock_guard", lambda: monkeypatch.setattr(alt_integer, "run_cpsat", guarded), skipped=False)


def test_fallback_objective_overflow(monkeypatch: pytest.MonkeyPatch) -> None:
    _fallback(monkeypatch, "objective_overflow", lambda: monkeypatch.setattr(cpsat, "OBJECTIVE_LIMIT", 10**6), skipped=True)


def test_fallback_solver_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: Any, **k: Any) -> None:
        raise RuntimeError("synthetic failure")

    _fallback(monkeypatch, "solver_error", lambda: monkeypatch.setattr(alt_integer, "_IntegerModel", boom), skipped=True)


def test_budget_exhausted_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    _fallback(monkeypatch, "budget_exhausted", lambda: monkeypatch.setattr(cpsat, "REQUEST_DET_BUDGET", 0.0), skipped=True)


def test_wall_guard_env_stops_the_solve_and_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard of 0 ms makes CP-SAT stop on wall time before its deterministic limit: the result
    is machine-dependent, so it is discarded (or the solve had no solution at all)."""
    monkeypatch.setenv(cpsat.WALL_GUARD_ENV, "0")
    resp = optimize(request(BOTH))
    stage = integer_stage(resp)
    assert stage.used is False and stage.fallback_reason in ("wall_clock_guard", "time_limit_no_solution")


def test_several_workers_are_flagged_non_deterministic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CPSAT_NUM_WORKERS", "2")
    resp = optimize(request(BOTH))
    assert resp.solver.params["cpsat_num_workers"] == 2 and resp.solver.params["cpsat_deterministic"] is False
    assert any("CPSAT_NUM_WORKERS=2" in w for w in resp.warnings)
    assert integer_stage(resp).num_workers == 2


# ───────────────────────────── runner helpers ─────────────────────────────


@pytest.mark.parametrize(("env", "guard"), [(None, 3000), ("1500", 1500), ("99999", 5000), ("-1", 3000), ("x", 3000)])
def test_wall_guard_from_env(monkeypatch: pytest.MonkeyPatch, env: str | None, guard: int) -> None:
    if env is None:
        monkeypatch.delenv(cpsat.WALL_GUARD_ENV, raising=False)
    else:
        monkeypatch.setenv(cpsat.WALL_GUARD_ENV, env)
    assert cpsat.wall_guard_ms(5) == guard


def test_objective_guard_and_stage_hash() -> None:
    assert cpsat.objective_fits([(4, 0, 10), (-3, -5, 5)], offset=7)
    assert not cpsat.objective_fits([(2**40, 0, 2**22)])
    h = cpsat.stage_hash("a" * 64, "integer", {"alt": [["F-N", 1]]})
    assert _HEX64.match(h) and h == cpsat.stage_hash("a" * 64, "integer", {"alt": [["F-N", 1]]})
    assert h != cpsat.stage_hash("a" * 64, "lp_phase2", {"alt": [["F-N", 1]]})


def test_budget_takes_and_caps() -> None:
    b = cpsat.Budget(left=0.3)
    assert b.take(0.2) == 0.2
    b.spend(0.25, kind="resequence")
    assert b.take(0.2) == pytest.approx(0.05)
    assert b.take(0.2, kind="resequence", cap=0.252) is None  # 0.002 left of the cap < MIN_STAGE_DET
    assert b.take(0.2, kind="resequence", cap=0.26) == pytest.approx(0.01)
    b.spend(0.05)
    assert b.take(0.1) is None


def test_stage_log_is_inert_in_repair_mode() -> None:
    log = cpsat.StageLog(enabled=False, seed=1, inputs_hash="x")
    log.lp("lp_phase1", "OPTIMAL", {"num_variables": 3}, {"a": 1})
    log.cp("integer", None, {}, used=False, fallback="no_candidates")
    assert log.stages == [] and log.hash("x", {}) is None


# ───────────────────────────── HTTP ─────────────────────────────


def test_cpsat_over_http(client: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    r = client.post("/v1/optimize", json=request(BOTH), headers=auth())
    assert r.status_code == 200, r.text[:500]
    solver = r.json()["solver"]
    assert solver["params"]["integer_stage"] == "cpsat"
    assert {s["name"] for s in solver["stages"]} >= {"lp_phase1", "integer", "lp_phase2:cpsat"}
    levers = {lv["code"]: lv for lv in client.get("/v1/levers", headers=auth()).json()}
    alt = levers["resource_alt_supplier"]["params_schema"]
    assert "max_buys_per_line" in alt["properties"] and "$ref" not in json.dumps(alt)
    reseq = levers["resequence_production"]["params_schema"]
    assert {"frozen_days", "lateness_cents_per_unit_day"} <= set(reseq["properties"])
    assert "$ref" not in json.dumps(reseq)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda b: b["data"]["alt_sources"][0].__setitem__("order_multiple_units", 0), id="multiple-0"),
        pytest.param(lambda b: b["data"]["alt_sources"][0].__setitem__("moq_basis", "x"), id="bad-basis"),
        pytest.param(lambda b: b["data"]["alt_sources"][0].__setitem__("max_orders", 0), id="max-orders-0"),
        pytest.param(lambda b: b["solver"].__setitem__("integer_stage", "mip"), id="bad-stage"),
        pytest.param(
            lambda b: b["plan_spec"]["levers"][1].__setitem__("params", {"max_buys_per_line": 0}), id="max-buys-0"
        ),
    ],
)
def test_bad_m8_values_are_422(
    client: TestClient, auth: Callable[..., dict[str, str]], mutate: Callable[[dict[str, Any]], None]
) -> None:
    raw = request(BOTH)
    mutate(raw)
    r = client.post("/v1/optimize", json=raw, headers=auth())
    assert r.status_code == 422, r.text[:300]
    assert len(r.content) < 2000
