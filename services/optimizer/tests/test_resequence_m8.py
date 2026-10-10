"""M8 ``resequence_production`` v2 (``integer_stage="cpsat"``) and the frozen zone
(``docs/contracts/125-m8-optimizer-cpsat.md``): lost margin by arrival week instead of the
weighted-completion proxy, per-order pinning, the due-date tie-break, the frozen prefix in both
modes and the M3 path left untouched in ``repair`` mode."""

from __future__ import annotations

import copy
import datetime as dt
import random
from functools import cache
from typing import Any

import pytest
from keelson_contracts.optimizer import OptimizeRequest, OptimizeResponse, ResequenceProductionParams

from keelson_opt.cpsat import StageLog
from keelson_opt.levers import resequence as rs
from keelson_opt.levers import resequence_cpsat as v2
from keelson_opt.plan import base_lines
from keelson_opt.scn import build_scenario
from keelson_opt.service import run_optimize

from .fixtures.full_problem import BD_TRIMS_DELAY, VN_PORT_CLOSURE, full_request
from .network import network_problem
from .test_levers_m3 import reseq_problem


def optimize(raw: dict[str, Any], stage: str = "cpsat") -> OptimizeResponse:
    raw = copy.deepcopy(raw)
    raw["solver"]["integer_stage"] = stage
    return run_optimize(OptimizeRequest.model_validate(raw))


def body(resp: OptimizeResponse) -> dict[str, Any]:
    out = resp.model_dump(mode="json")
    out["solver"].pop("solve_ms")
    return out


def sequence(resp: OptimizeResponse) -> list[str]:
    (a,) = [a for a in resp.actions if a.op == "production_order.resequence"]
    return list(a.payload.order_codes)


@cache
def vn_resequence(stage: str) -> OptimizeResponse:
    return optimize(full_request([{"code": "resequence_production"}], [VN_PORT_CLOSURE], plan_id="opt-r"), stage)


def test_v2_recovers_what_the_weighted_completion_proxy_misses() -> None:
    """VN port closure (2026-10-06..15): Smith's rule orders by weighted completion, which
    ignores the week buckets and the closure; v2 prices every arrival week with the simulator
    and recovers far more margin."""
    m3, cps = vn_resequence("repair"), vn_resequence("cpsat")
    assert m3.solver.params["resequence_methods"] == "FL-VN01-A:wspt"
    assert cps.solver.params["resequence_methods"] in ("FL-VN01-A:cpsat_v2_optimal", "FL-VN01-A:cpsat_v2_feasible")
    assert m3.scorecard is not None and cps.scorecard is not None
    assert cps.scorecard.net_benefit_cents > m3.scorecard.net_benefit_cents
    assert cps.kpis_after.units_at_risk < m3.kpis_after.units_at_risk
    (stage,) = [s for s in cps.solver.stages if s.name == "resequence:FL-VN01-A"]
    assert stage.used and stage.solver == "CP-SAT" and stage.seed == 7 and stage.num_workers == 1
    assert stage.det_time_limit_ms is not None and stage.det_time_limit_ms <= round(v2.RESEQ_V2_DET_TIME * 1000)
    # the Merxa op lists every movable order of the line
    line = next(p for p in OptimizeRequest.model_validate(full_request([], [VN_PORT_CLOSURE])).data.production
                if p.line_code == "FL-VN01-A")  # fmt: skip
    assert sorted(sequence(cps)) == sorted(o.code for o in line.orders if o.movable)
    assert "CP-SAT" in cps.solver.name.split("+")


def test_v2_is_deterministic() -> None:
    again = optimize(full_request([{"code": "resequence_production"}], [VN_PORT_CLOSURE], plan_id="opt-r"))
    assert body(again) == body(vn_resequence("cpsat"))


def test_nothing_to_gain_changes_nothing() -> None:
    resp = optimize(full_request([{"code": "resequence_production"}], [BD_TRIMS_DELAY]))
    assert resp.actions == []
    stages = [s for s in resp.solver.stages if s.name.startswith("resequence:")]
    assert stages and all(not s.used and s.fallback_reason == "no_improvement" for s in stages)
    raw = reseq_problem()
    raw["perturbations"] = [
        {"op": "lead_time_shift", "target": {"kinds": ["po_line"], "ids": ["PO-T#1"]}, "days": 7, "prov": "fact"}
    ]
    assert optimize(raw).actions == []


def test_ties_keep_the_m3_sequence() -> None:
    m3, cps = optimize(reseq_problem(), "repair"), optimize(reseq_problem())
    assert sequence(cps) == sequence(m3) == ["MO-3", "MO-1", "MO-2"]
    assert cps.solver.params["resequence_methods"] == "L-1:wspt"
    (stage, _tiebreak) = [s for s in cps.solver.stages if s.name.startswith("resequence:L-1")]
    assert not stage.used and stage.fallback_reason == "no_improvement"


def test_m3_path_untouched_in_repair_mode() -> None:
    resp = optimize(reseq_problem(), "repair")
    assert resp.solver.params["resequence_methods"] == "L-1:wspt" and resp.solver.stages == []
    assert "integer_stage" not in resp.solver.params


# ───────────────────────────── pinned PO lines ─────────────────────────────


def _plan(pinned: frozenset[str], *, cpsat: bool, raw: dict[str, Any] | None = None) -> rs.ReseqOutcome:
    req = OptimizeRequest.model_validate(raw or reseq_problem())
    scn = build_scenario(req.data, req.perturbations)
    log = StageLog(enabled=cpsat, seed=0, inputs_hash="0" * 64)
    lever = req.plan_spec.levers[0].params
    params = lever if isinstance(lever, ResequenceProductionParams) else ResequenceProductionParams()
    return rs.plan_resequence(scn, base_lines(scn), params, seed=0, w_lost_bp=10_000, pinned=pinned, stages=log)


def test_a_booked_line_keeps_its_eta_while_the_rest_of_the_line_moves() -> None:
    """M3 rejects the whole line when its sequence would move a capacity-booked PO line; v2 only
    restricts that line to the days that keep its eta."""
    pinned = frozenset({"PO-K2#1"})
    m3 = _plan(pinned, cpsat=False)
    assert m3.moves == {} and any("carried by a capacity booking" in w for w in m3.warnings)
    out = _plan(pinned, cpsat=True)
    assert out.methods == {"L-1": "cpsat_v2_optimal"}
    assert "PO-K2#1" not in out.moves and out.moves["PO-T#1"].eta_after == dt.date(2026, 10, 17)
    assert [s.after for s in out.sequences] == [("MO-3", "MO-2", "MO-1")]


# ───────────────────────────── frozen zone ─────────────────────────────


def _frozen_problem(frozen_days: int) -> dict[str, Any]:
    """:func:`reseq_problem` with a one-day KNIT order first and a five-day one second: with
    one frozen day the first stays first and TOP still arrives in W42 instead of W43."""
    raw = reseq_problem()
    orders = raw["data"]["production"][0]["orders"]
    orders[0].update(qty=100, planned_end="2026-10-05")
    orders[1].update(qty=500, planned_start="2026-10-06", planned_end="2026-10-10")
    orders[2].update(planned_start="2026-10-11", planned_end="2026-10-13")
    raw["plan_spec"]["levers"][0]["params"] = {"frozen_days": frozen_days}
    return raw


@pytest.mark.parametrize("stage", ["repair", "cpsat"])
def test_frozen_days_keep_the_prefix(stage: str) -> None:
    free = optimize(_frozen_problem(0), stage)
    assert sequence(free)[0] == "MO-3"
    frozen = optimize(_frozen_problem(1), stage)
    assert sequence(frozen) == ["MO-1", "MO-3", "MO-2"]
    assert "PO-K1#1" not in {a.target_ref for a in frozen.actions if a.op == "po_line.reschedule"}
    assert frozen.kpis_after.units_at_risk < frozen.kpis_before.units_at_risk
    # everything frozen but one order: nothing left to resequence
    assert optimize(_frozen_problem(5), stage).actions == []


def test_frozen_prefix_on_the_full_profile_line() -> None:
    raw = full_request([{"code": "resequence_production", "params": {"frozen_days": 10}}], [VN_PORT_CLOSURE])
    req = OptimizeRequest.model_validate(raw)
    line = next(p for p in req.data.production if p.line_code == "FL-VN01-A")
    movable = sorted((o for o in line.orders if o.movable), key=lambda o: (o.seq, o.code))
    limit = req.data.as_of + dt.timedelta(days=10)
    prefix = [o.code for o in movable if o.planned_start < limit]
    assert prefix and len(prefix) < len(movable) - 1
    for stage in ("repair", "cpsat"):
        resp = optimize(raw, stage)
        if any(a.op == "production_order.resequence" for a in resp.actions):
            assert sequence(resp)[: len(prefix)] == prefix


# ───────────────────────────── due dates ─────────────────────────────


def test_lateness_tie_break_prefers_fewer_unit_days_late() -> None:
    """Both KNIT lines are irrelevant for the margin (DE1 holds 2000 units): among the
    margin-optimal sequences (TOP first) v2 finishes the line due earliest first; a high
    ``lateness_cents_per_unit_day`` outweighs the margin (then the smallest change wins)."""
    raw = reseq_problem()
    for p in raw["data"]["po_lines"]:
        if p["ref"] == "PO-K2#1":
            p["ex_factory"] = "2026-10-08"
    req = OptimizeRequest.model_validate(raw)
    scn = build_scenario(req.data, req.perturbations)
    lines = base_lines(scn)
    pl = req.data.production[0]
    cases = [(0, ["MO-3", "MO-2", "MO-1"]), (1, ["MO-3", "MO-2", "MO-1"]), (1_000, ["MO-2", "MO-1", "MO-3"])]
    for weight, expected in cases:
        model = rs._line_model(scn, pl, {("UK1", "TOP-M"): 300}, {}, 400, 0, rs.ReseqOutcome())
        assert model is not None
        params = ResequenceProductionParams(lateness_cents_per_unit_day=weight)
        log = StageLog(enabled=True, seed=0, inputs_hash="0" * 64)
        res = v2.solve_line(scn, model, lines, params, 10_000, frozenset(), [2, 0, 1], 0, log, 5)
        assert res.fallback is None
        assert [model.jobs[i].order.code for i in res.seq] == expected, weight
        assert res.run_a is not None and res.run_a.status == "OPTIMAL"


# ───────────────────────────── size ─────────────────────────────


def long_line_problem(n: int, seed: int) -> dict[str, Any]:
    """One 200-units/day line making ``n`` random orders for four PO lines (two TOP for UK1, two
    KNIT for DE1) whose trims are late."""
    rng = random.Random(seed)
    data = network_problem()
    refs = [
        ("PO-1#1", "TOP-M", "UK1"),
        ("PO-1#2", "TOP-M", "UK1"),
        ("PO-2#1", "KNIT-M", "DE1"),
        ("PO-2#2", "KNIT-M", "DE1"),
    ]
    qty = {ref: 0 for ref, _s, _d in refs}
    orders = []
    day = dt.date(2026, 10, 5)
    for i in range(n):
        ref, sku, _dc = refs[rng.randrange(4)]
        q = rng.randint(20, 160)
        qty[ref] += q
        orders.append({"code": f"MO-{i:04d}", "po_line_ref": ref, "sku": sku, "qty": q,
                       "planned_start": day.isoformat(), "planned_end": day.isoformat(), "seq": i})  # fmt: skip
        if i % 2:
            day += dt.timedelta(days=1)
    etas = {"PO-1#1": "2026-10-26", "PO-1#2": "2026-11-09", "PO-2#1": "2026-10-26", "PO-2#2": "2026-11-09"}
    data["receipts"] = [
        {"ref": ref, "dc_code": dc, "sku": sku, "qty": qty[ref], "eta": etas[ref]} for ref, sku, dc in refs if qty[ref]
    ]
    base = {p["ref"][:4]: p for p in data["po_lines"]}
    data["po_lines"] = [
        {**base[ref[:4]], "ref": ref, "qty": qty[ref], "eta": etas[ref]} for ref, _sku, _dc in refs if qty[ref]
    ]
    data["production"] = [{"line_code": "L-1", "factory_code": "F-1", "daily_capacity_units": 200, "orders": orders}]
    delays = [
        {"op": "lead_time_shift", "target": {"kinds": ["po_line"], "ids": [ref]}, "days": d, "prov": "fact"}
        for ref, d in (("PO-1#1", 9), ("PO-2#1", 5))
    ]
    return {
        "data": data,
        "perturbations": delays,
        "plan_spec": {"id": "opt-l", "title": "long line", "levers": [{"code": "resequence_production"}]},
        "solver": {"time_limit_s": 5, "seed": 11},
    }


def test_long_line_is_deterministic_and_never_worse_than_m3() -> None:
    raw = long_line_problem(200, 5)
    m3, a, b = optimize(raw, "repair"), optimize(raw), optimize(raw)
    assert body(a) == body(b)
    assert m3.scorecard is not None and a.scorecard is not None
    assert a.scorecard.net_benefit_cents >= m3.scorecard.net_benefit_cents
    (stage, *_tiebreak) = [s for s in a.solver.stages if s.name.startswith("resequence:L-1")]
    assert stage.status != "SKIPPED" or stage.fallback_reason == "budget_exhausted"


def test_lines_beyond_the_size_limits_use_the_m3_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(v2, "RESEQ_V2_MAX_ORDERS", 2)
    resp = optimize(reseq_problem())
    assert resp.solver.params["resequence_methods"] == "L-1:wspt"
    (stage,) = [s for s in resp.solver.stages if s.name == "resequence:L-1"]
    assert stage.status == "SKIPPED" and stage.fallback_reason == "model_too_large" and not stage.used
    assert sequence(resp) == ["MO-3", "MO-1", "MO-2"]


def test_the_longest_line_code_gets_its_stage_records() -> None:
    """Review fix: ``resequence:<line_code>:tiebreak`` with a 128-character line code (the longest
    ``Code``) failed the ``SolverStage`` validation and with it the whole request."""
    raw = reseq_problem()
    code = "L" * 128
    raw["data"]["production"][0]["line_code"] = code
    resp = optimize(raw)
    assert resp.solver.params["resequence_methods"] == f"{code}:wspt"
    assert [s.name for s in resp.solver.stages if s.name.startswith("resequence:")] == [
        f"resequence:{code}",
        f"resequence:{code}:tiebreak",
    ]
    assert sequence(resp) == ["MO-3", "MO-1", "MO-2"]


def test_a_failing_v2_solve_keeps_the_m3_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a: Any, **_k: Any) -> None:
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(v2, "_value_tables", boom)
    resp = optimize(reseq_problem())
    assert sequence(resp) == ["MO-3", "MO-1", "MO-2"]
    (stage,) = [s for s in resp.solver.stages if s.name == "resequence:L-1"]
    assert stage.fallback_reason == "solver_error" and resp.status == "OPTIMAL"
