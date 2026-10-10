"""The batch simulator (``robust/batch.py``) equals the scalar ``impact.simulate`` exactly when
nothing is sampled (zero noise, no bands): the small fixture, the full profile (trims delay, VN
closure, both), every option A-D (transfers, splits, alternate buys, resequence moves, firm
overrides) and the M5 booking re-plan, for N = 1 and N = 8. The dated arrivals mirror
``plan.plan_lines`` exactly."""

from __future__ import annotations

import copy
import datetime as dt
from typing import Any

import numpy as np
import pytest
from keelson_contracts.optimizer import RobustnessSpec

from keelson_opt.impact import SimResult, kpis, simulate
from keelson_opt.isoweeks import iso_week_label
from keelson_opt.plan import Plan, base_lines, plan_lines
from keelson_opt.planner import make_plan
from keelson_opt.robust.batch import BatchResult, ration, simulate_batch
from keelson_opt.robust.sample import Arrival, Futures, plan_arrivals
from keelson_opt.scn import Scenario, build_scenario
from keelson_opt.service import _simulate_plan
from keelson_opt.wire_m5 import OptimizeRequestM5

from .fixtures.full_problem import BD_TRIMS_DELAY, CAPE_CLOSURE, OPTIONS, VN_PORT_CLOSURE, full_request
from .test_replan_m5 import cut
from .test_replan_m5 import request as replan_request

QUIET = RobustnessSpec(samples=8, value_bands=False)


def _scenario_and_plan(raw: dict[str, Any]) -> tuple[Scenario, Plan, list[Any]]:
    req = OptimizeRequestM5.model_validate(raw)
    scn = build_scenario(req.data, req.perturbations)
    outcome = make_plan(scn, req.plan_spec, req.solver, kpis(simulate(scn)).margin_at_risk_cents)
    return scn, outcome.plan, list(req.perturbations)


def _expected(res: SimResult) -> dict[str, Any]:
    k = kpis(res)
    return {
        "lost_units": k.units_at_risk,
        "lost_margin": k.margin_at_risk_cents,
        "short_cw": k.shortage_cluster_weeks,
        "demand": sum(sum(cs.demand) for cs in res.series.values()),
        "by_key": {key: sum(cs.lost) for key, cs in res.series.items() if sum(cs.lost)},
    }


def _assert_equal(batch: BatchResult, res: SimResult, n: int) -> None:
    exp = _expected(res)
    assert batch.lost_by_key is not None
    for s in range(n):
        assert int(batch.lost_units[s]) == exp["lost_units"]
        assert int(batch.lost_margin_cents[s]) == exp["lost_margin"]
        assert int(batch.short_cluster_weeks[s]) == exp["short_cw"]
        assert int(batch.demand[s]) == exp["demand"]
        got = {key: int(v[s]) for key, v in batch.lost_by_key.items() if int(v[s])}
        assert got == exp["by_key"]


def _key(a: Arrival) -> str:
    return a.ref if a.kind in ("line", "revert", "moved") and a.ref is not None else a.key


def _projection(arrivals: list[Arrival]) -> list[tuple[str, str, str, int, int]]:
    return sorted((_key(a), a.dc, a.sku, a.qty, a.week) for a in arrivals)


def _check(raw: dict[str, Any], n: int) -> None:
    scn, plan, perts = _scenario_and_plan(raw)
    futures = Futures(scn, perts, QUIET, n)
    base = plan_arrivals(scn, None)
    assert _projection(base) == sorted((ln.key, ln.dc, ln.sku, ln.qty, ln.week) for ln in base_lines(scn))
    _assert_equal(simulate_batch(futures, base, keep_keys=True), simulate(scn), n)
    if plan.empty:
        return
    arrivals = plan_arrivals(scn, plan)
    assert _projection(arrivals) == sorted((ln.key, ln.dc, ln.sku, ln.qty, ln.week) for ln in plan_lines(scn, plan))
    batch = simulate_batch(futures, arrivals, plan.transfers, overrides=plan.overrides, keep_keys=True)
    _assert_equal(batch, _simulate_plan(scn, plan), n)


def test_small_fixture(small: dict[str, Any]) -> None:
    _check(small, 1)
    _check(small, 8)


@pytest.mark.parametrize("option", ["A", "B", "C", "D"])
@pytest.mark.parametrize("story", ["bd", "vn", "bd+vn"])
def test_full_profile_options(option: str, story: str) -> None:
    perts = {"bd": [BD_TRIMS_DELAY], "vn": [VN_PORT_CLOSURE], "bd+vn": [BD_TRIMS_DELAY, VN_PORT_CLOSURE]}[story]
    _check(full_request(OPTIONS[option], perts, plan_id=f"opt-{option}"), 8 if option in ("A", "D") else 1)


def test_full_profile_with_lost_lines() -> None:
    """An open-ended closure without a usable fallback loses lines; they stay lost."""
    _check(full_request(OPTIONS["B"], [CAPE_CLOSURE], plan_id="opt-B"), 2)


def test_m5_booking_replan() -> None:
    _check(replan_request(), 8)
    _check(replan_request(levers=[]), 1)


def test_m5_full_profile_bookings() -> None:
    raw = full_request(OPTIONS["D"], [BD_TRIMS_DELAY, cut(3000, "CGP-UK1-SA")], plan_id="opt-D")
    data = raw["data"]
    by_po: dict[str, list[dict[str, Any]]] = {}
    for pl in data["po_lines"]:
        if pl["mode"] == "sea_air":
            by_po.setdefault(pl["po_code"], []).append(pl)
    data["capacity_bookings"] = [
        {
            "code": f"CB-{n:04d}",
            "lane_code": lines[0]["lane_code"],
            "iso_week": iso_week_label(dt.date.fromisoformat(lines[0]["ex_factory"]) + dt.timedelta(days=1)),
            "qty_units": sum(pl["qty"] for pl in lines),
            "lines": [{"po_line_ref": pl["ref"], "qty": pl["qty"]} for pl in lines],
        }
        for n, (_po, lines) in enumerate(sorted(by_po.items()), start=1)
    ]
    _check(raw, 2)


def test_vector_ration_matches_scalar() -> None:
    from keelson_opt.impact import ration as scalar

    rng = np.random.default_rng(3)
    for _ in range(300):
        m = int(rng.integers(1, 7))
        req = rng.integers(0, 40, size=m)
        req[rng.random(m) < 0.3] = 0
        pool = int(rng.integers(0, int(req.sum()) + 5))
        got = ration(np.array([pool], dtype=np.int64), req.astype(np.int64)[None, :])[0].tolist()
        assert got == scalar(pool, req.tolist())


def test_sampled_demand_reproduces_the_scenario_demand() -> None:
    raw = full_request(OPTIONS["A"], [BD_TRIMS_DELAY, {"op": "demand_scale", "target": {"kinds": ["style"],
                       "ids": ["PUF-W-BLK"]}, "factor_bp": 13000, "prov": "decision", "weeks": ["2026-W45"]}])
    req = OptimizeRequestM5.model_validate(raw)
    scn = build_scenario(req.data, req.perturbations)
    futures = Futures(scn, list(req.perturbations), QUIET, 2)
    for dc in scn.dcs:
        clusters = scn.dc_clusters[dc]
        dem = futures.demand(dc, clusters, scn.skus)
        for a, c in enumerate(clusters):
            for k, sku in enumerate(scn.skus):
                expected = scn.demand.get((c, sku), [0] * scn.horizon)
                assert dem[0, k, a].tolist() == expected
                assert dem[1, k, a].tolist() == expected


def test_shuffled_inputs_give_the_same_batch() -> None:
    raw = full_request(OPTIONS["A"], [BD_TRIMS_DELAY], plan_id="opt-A")
    shuffled = copy.deepcopy(raw)
    for key in ("receipts", "demand", "skus", "po_lines", "inventory", "cluster_inventory"):
        shuffled["data"][key] = list(reversed(shuffled["data"][key]))
    out = []
    for r in (raw, shuffled):
        scn, plan, perts = _scenario_and_plan(r)
        futures = Futures(scn, perts, RobustnessSpec(samples=8, seed=7, demand_noise_bp={"p10": 9000, "p50": 10000,
                          "p90": 11000}, lead_time_noise=[{"mode": "default", "days": {"p10": -1, "p50": 0, "p90": 3}}]), 8)
        b = simulate_batch(futures, plan_arrivals(scn, plan), plan.transfers, overrides=plan.overrides)
        out.append((b.lost_units.tolist(), b.lost_margin_cents.tolist(), b.short_cluster_weeks.tolist()))
    assert out[0] == out[1]
