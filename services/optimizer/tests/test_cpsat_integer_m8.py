"""M8 whole-unit CP-SAT stage for ``resource_alt_supplier`` + ``expedite_mode_shift``
(``docs/contracts/125-m8-optimizer-cpsat.md``): the opt-in switch, the sourcing terms in both
integer stages, the CP-SAT improvements over the LP rounding, fallbacks, per-solve records and
determinism."""

from __future__ import annotations

import dataclasses
import itertools
import json
import re
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from keelson_contracts.optimizer import (
    ExpediteModeShiftParams,
    OptimizeRequest,
    OptimizeResponse,
    PlanWeights,
    ResourceAltSupplierParams,
    SolverOptions,
    SolverStage,
)

from keelson_opt import cpsat
from keelson_opt.levers import alt_integer, composed
from keelson_opt.levers.composed import Fixed, LeverParams
from keelson_opt.plan import base_lines
from keelson_opt.scn import build_scenario
from keelson_opt.service import run_optimize

from .network import request as network_request
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


def test_stages_are_in_solve_order() -> None:
    """Review fix: the integer stage was appended after the completions it triggers (the repair's
    phase 2 and the CP-SAT units' own phase 2), against the "in solve order" contract."""
    resp = optimize(request(BOTH))
    assert [(s.name, s.used) for s in resp.solver.stages] == [
        ("lp_phase1", True),
        ("integer", True),
        ("lp_phase2", False),
        ("lp_phase2:cpsat", True),
    ]


def test_a_do_nothing_fallback_uses_no_stage(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review fix: when the never-worse guard returns the do-nothing plan, no solve's result is in
    the returned plan, so no stage may claim ``used``."""
    from keelson_opt import planner
    from keelson_opt.plan import Plan

    def do_nothing(_scn: Any, _spec: Any, _plan: Any, _m: int, params: dict[str, Any], _w: list[str]) -> Plan:
        params["plan_fallback"] = "do_nothing"
        return Plan()

    monkeypatch.setattr(planner, "_keep_improving", do_nothing)
    resp = run_optimize(OptimizeRequest.model_validate(request(BOTH)))
    assert resp.actions == [] and resp.solver.params["plan_fallback"] == "do_nothing"
    assert resp.solver.stages and not [s.name for s in resp.solver.stages if s.used]


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


# ───────────────────────────── brute force and LP consistency (review) ─────────────────────────────


def _tiny_model_inputs() -> tuple[Any, ...]:
    """:mod:`tests.network` scaled down: PO-1#1 (30 TOP, 5 sold a week) lands two weeks late, the
    sea-air lane takes 6 units a week and F-N sells in cases of 10 with an MOQ of 20 - three
    alternate candidates (arrival W43-W45) and one split, so every integer rule bites."""
    raw = network_request([{"code": "expedite_mode_shift"}, {"code": "resource_alt_supplier"}])
    data = raw["data"]
    units = {"UK1-A": 3, "UK1-B": 2, "DE1-A": 30}
    data["demand"] = [d | {"units": units[d["cluster_code"]]} for d in data["demand"]]
    data["inventory"][0]["on_hand"] = 10
    data["cluster_inventory"][0]["on_hand"], data["cluster_inventory"][1]["on_hand"] = 3, 2
    data["receipts"][0]["qty"] = data["po_lines"][0]["qty"] = 30
    data["inbound_lanes"][2]["weekly_capacity_units"] = 6
    data["alt_sources"][0].update(moq_units=20, order_multiple_units=10, lead_time_days=14, weekly_capacity_units=40)
    req = OptimizeRequest.model_validate(raw)
    scn = build_scenario(req.data, req.perturbations)
    lines = base_lines(scn)
    levers = LeverParams(expedite=ExpediteModeShiftParams(), alt=ResourceAltSupplierParams())
    weights, options = PlanWeights(), SolverOptions(time_limit_s=5, seed=3)
    out = composed.solve_composed(scn, lines, levers, weights, options)
    assert out.status == "OPTIMAL"
    alt_c = [out.alt_cands[k] for k in sorted(out.alt_cands)]
    exp_c = [out.exp_cands[k] for k in sorted(out.exp_cands)]
    assert (len(alt_c), len(exp_c)) == (3, 1)
    pairs = sorted({(c.dc, c.sku) for c in exp_c} | {(a.dc, a.sku) for a in alt_c})
    return scn, lines, levers, weights, options, out, alt_c, exp_c, pairs


def test_cpsat_matches_brute_force_and_the_lp_objective(monkeypatch: pytest.MonkeyPatch) -> None:
    """Brief test plan: on a tiny instance the CP-SAT optimum equals an exhaustive search over the
    integer model, and the integer objective is the LP's: for every feasible whole-unit decision
    ``D`` the CP-SAT objective with ``D`` fixed is ``OBJ_SCALE`` x the phase-2 GLOP objective with
    ``D`` fixed (no transfers here, and no pair outside the model loses margin)."""
    scn, lines, levers, weights, options, out, alt_c, exp_c, pairs = _tiny_model_inputs()

    def model() -> Any:
        return alt_integer._IntegerModel(scn, out, lines, levers, weights, alt_c, exp_c, pairs, {}, {})

    def cp_value(qs: tuple[int, ...], e: int) -> int | None:
        m = model()
        for a, q in zip(alt_c, qs, strict=True):
            m.model.add(m.n[a.key] == q)
        m.model.add(m.e[exp_c[0].key] == e)
        run = cpsat.run_cpsat(m.model, seed=0, det_limit=5.0, time_limit_s=5)
        assert run.status in ("OPTIMAL", "INFEASIBLE")
        return run.objective if run.status == "OPTIMAL" else None

    lp_objective: list[float] = []

    class RecordingSolver(composed.mb.Solver):  # type: ignore[misc,name-defined]
        def solve(self, model: Any) -> Any:
            status = super().solve(model)
            lp_objective.append(float(self.objective_value))
            return status

    monkeypatch.setattr(composed.mb, "Solver", RecordingSolver)

    def lp_value(qs: tuple[int, ...], e: int) -> float:
        fixed = Fixed(alt={a.key: q for a, q in zip(alt_c, qs, strict=True) if q}, exp={exp_c[0].key: e} if e else {})
        lp = composed.solve_composed(scn, lines, levers, weights, options, fixed=fixed)
        assert lp.status == "OPTIMAL"
        return lp_objective[-1]

    free = model()
    run = cpsat.run_cpsat(free.model, seed=0, det_limit=5.0, time_limit_s=5)
    assert run.status == "OPTIMAL" and run.objective is not None
    feasible: dict[tuple[tuple[int, ...], int], int] = {}
    for qs in itertools.product(range(0, 31, 10), repeat=len(alt_c)):
        for e in range(exp_c[0].ub + 1):
            value = cp_value(qs, e)
            if value is not None:
                feasible[(qs, e)] = value
    # the integer rules: one op per line, whole cases, MOQ 20, the lane's 6 units, the open qty
    assert feasible and all((sum(qs) == 0 or e == 0) and e <= 6 and sum(qs) <= 30 for qs, e in feasible)
    assert all(q in (0, 20, 30) for qs, _e in feasible for q in qs)
    assert cp_value((15, 0, 0), 0) is None and cp_value((10, 0, 0), 0) is None  # half a case / below MOQ
    best = min(feasible.values())
    assert run.objective == best
    assert feasible[((0, 20, 0), 0)] == best  # 20 units in W44 beat the 6 the lane can fly
    for (qs, e), value in sorted(feasible.items()):
        assert value == pytest.approx(composed.OBJ_SCALE * lp_value(qs, e), abs=1e-3), (qs, e)


# ───────────────────────────── repair with the M8 terms ─────────────────────────────


def test_repair_without_m8_terms_keeps_the_m7_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []
    monkeypatch.setattr(alt_integer, "repair_terms", lambda *a, **k: calls.append(1) or {})
    optimize(request(BOTH, stage="repair"))
    assert calls == []
    raw = request(BOTH, sourcing_problem({"order_multiple_units": 6}), stage="repair")
    run_optimize(OptimizeRequest.model_validate(raw))
    assert calls == [1]


# ───────────────────────────── fallbacks ─────────────────────────────


def _fallback(monkeypatch: pytest.MonkeyPatch, reason: str, target: object, name: str, value: object) -> None:
    """With ``target.name`` patched to ``value`` the integer stage falls back for ``reason``:
    the repair plan is returned unchanged, with a warning and the reason recorded."""
    raw = request(BOTH)
    rep = optimize(dict(raw, solver={**raw["solver"], "integer_stage": "repair"}))
    monkeypatch.setattr(target, name, value)
    resp = optimize(raw)
    assert plan_of(resp) == plan_of(rep), "a fallback returns the repair plan"
    assert resp.status == rep.status
    stage = integer_stage(resp)
    assert stage.used is False and stage.fallback_reason == reason
    # solved but unusable (status / guard) vs not solved at all
    assert (stage.status == "SKIPPED") is (reason not in ("time_limit_no_solution", "wall_clock_guard"))
    assert resp.solver.params["integer_method"] == "lp_rounding" and resp.solver.params["integer_fallback"] == reason
    assert f"whole-unit CP-SAT stage fell back to LP rounding ({reason})" in " ".join(resp.warnings)
    assert "CP-SAT" not in resp.solver.name.split("+")


def _patched_run(**change: Any) -> Callable[..., cpsat.CpRun]:
    real = alt_integer.run_cpsat

    def run(*a: Any, **k: Any) -> cpsat.CpRun:
        return dataclasses.replace(real(*a, **k), **change)

    return run


def _boom(*_a: Any, **_k: Any) -> None:
    raise RuntimeError("synthetic failure")


@pytest.mark.parametrize(
    ("reason", "target", "name", "value"),
    [
        ("time_limit_no_solution", alt_integer, "run_cpsat", _patched_run(status="UNKNOWN", objective=None, bound=None)),
        ("wall_clock_guard", alt_integer, "run_cpsat", _patched_run(status="FEASIBLE", guard_hit=True)),
        ("model_too_large", alt_integer, "INTEGER_MAX_VARS", 1),
        ("objective_overflow", cpsat, "OBJECTIVE_LIMIT", 10**6),
        ("solver_error", alt_integer, "_IntegerModel", _boom),
        ("budget_exhausted", cpsat, "REQUEST_DET_BUDGET", 0.0),
    ],
)
def test_fallbacks_return_the_repair_plan(
    monkeypatch: pytest.MonkeyPatch, reason: str, target: object, name: str, value: object
) -> None:
    _fallback(monkeypatch, reason, target, name, value)


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
