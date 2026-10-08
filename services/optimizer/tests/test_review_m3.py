"""Regression tests from the independent M3 review of the optimizer:

* resequencing: a readiness delay is not cascaded onto the orders behind it (the scenario
  does not), and moved receipts pass the lane-closure rules again;
* expedite / alternate sources wait for a temporary lane closure to end instead of giving
  up on the lane;
* the release-aware greedy order (heap) equals the original quadratic definition;
* a compliance block whose window lies outside the horizon is ignored.
"""

from __future__ import annotations

import datetime as dt
import random
from typing import Any

import pytest
from keelson_contracts.optimizer import ImpactRequest, OptimizeRequest, OptimizeResponse

from keelson_opt.levers import resequence as rs
from keelson_opt.scn import Closure, InboundLaneInfo, Scenario, build_scenario
from keelson_opt.service import run_optimize

from .network import TRIM_DELAY, network_problem, request
from .test_levers_m3 import reseq_problem


def optimize(raw: dict[str, Any]) -> OptimizeResponse:
    return run_optimize(OptimizeRequest.model_validate(raw))


def etas(resp: OptimizeResponse) -> dict[str, str]:
    return {str(a.target_ref): a.payload.eta_dc.isoformat() for a in resp.actions if a.op == "po_line.reschedule"}  # type: ignore[union-attr]


def scn_of(perturbations: list[dict[str, Any]], **over: Any) -> Scenario:
    req = ImpactRequest(data=network_problem(**over), perturbations=perturbations)
    return build_scenario(req.data, req.perturbations)


def closure(kind: str, ident: str, window: dict[str, Any] | None) -> dict[str, Any]:
    p: dict[str, Any] = {"op": "capacity_scale", "target": {"kinds": [kind], "ids": [ident]}, "factor_bp": 0}
    p["prov"] = "fact"
    if window is not None:
        p["window"] = window
    return p


# ───────────────────────────── resequence_production ─────────────────────────────


def test_a_delay_on_another_order_never_makes_resequenced_etas_earlier() -> None:
    """PO-K1#1's trims are 2 days late. In the scenario only PO-K1#1 is late; the orders
    behind it are not. Producing TOP first must give the same ETAs as without the delay
    (MO-1 absorbs its 2 days), never earlier ones (the old model cascaded the delay onto
    MO-2 / MO-3 in the reference and then 'recovered' it: PO-T#1 at 10-15, PO-K2#1 at 10-21)."""
    plain = optimize(reseq_problem())
    raw = reseq_problem()
    raw["perturbations"] = [
        {"op": "lead_time_shift", "target": {"kinds": ["po_line"], "ids": ["PO-K1#1"]}, "days": 2, "prov": "fact"}
    ]
    delayed = optimize(raw)
    assert etas(plain) == {"PO-T#1": "2026-10-17", "PO-K1#1": "2026-10-20", "PO-K2#1": "2026-10-23"}
    assert etas(delayed) == etas(plain)
    assert delayed.kpis_after.units_at_risk == plain.kpis_after.units_at_risk == 0


def test_delay_before_the_reference_start_is_released_from_the_on_time_start() -> None:
    """A delay on the last order (TOP) of the line: its release is its on-time start + delay
    (day 6 + 7), so moving it first gains nothing - no action, as in the M1-era test, and
    the reference objective (not the cascaded identity) is what a new order must beat."""
    raw = reseq_problem()
    raw["perturbations"] = [
        {"op": "lead_time_shift", "target": {"kinds": ["po_line"], "ids": ["PO-T#1"]}, "days": 7, "prov": "fact"}
    ]
    assert optimize(raw).actions == []


def test_moved_receipts_pass_the_closure_rules_again() -> None:
    # PO-1#1 sails 2026-09-25 .. 10-26 on SEA-UK via CAPE; CAPE is closed 09-20 .. 09-29.
    scn = scn_of([closure("chokepoint", "CAPE", {"start": "2026-09-20", "end": "2026-09-29"})])
    (rl,) = [r for r in scn.receipt_lines if r.ref == "PO-1#1"]
    assert rl.transport_delay == 10 and rl.eta_perturbed == dt.date(2026, 11, 5)
    # Ready 5 days later: departs 09-30, after the closure -> no backlog, arrives 10-31.
    assert rs._moved_eta(scn, rl, 5) == dt.date(2026, 10, 31)
    # Ready 3 days earlier: still inside the closure -> backlog again.
    assert rs._moved_eta(scn, rl, -3) == dt.date(2026, 11, 2)
    assert rs._moved_eta(scn, rl, 0) == rl.eta_perturbed
    # SEA-DE closes for good from 10-27 (after PO-2#1 arrives): a later readiness loses it.
    scn = scn_of([closure("lane", "SEA-DE", {"start": "2026-10-27"})])
    (rl,) = [r for r in scn.receipt_lines if r.ref == "PO-2#1"]
    assert not rl.lost
    assert rs._moved_eta(scn, rl, -1) == dt.date(2026, 10, 25)
    assert rs._moved_eta(scn, rl, 5) is None


def _greedy_reference(jobs: list[rs._Job]) -> list[int]:
    """The original O(n^2 log n) definition."""
    left = set(range(len(jobs)))
    seq: list[int] = []
    cursor = 0
    while left:
        ready = [i for i in left if jobs[i].release <= cursor] or [min(left, key=lambda i: (jobs[i].release, i))]
        best = rs._wspt([jobs[i] for i in sorted(ready)])[0]
        pick = sorted(ready)[best]
        seq.append(pick)
        left.remove(pick)
        cursor = max(cursor, jobs[pick].release) + jobs[pick].dur
    return seq


@pytest.mark.parametrize("seed", range(12))
def test_heap_greedy_equals_the_reference_definition(seed: int) -> None:
    rng = random.Random(seed)
    n = rng.randint(1, 60)
    jobs = [
        rs._Job(None, rng.randint(1, 9), rng.choice([0, 0, rng.randint(0, 80)]), rng.randint(0, 5) * rng.randint(0, 9))  # type: ignore[arg-type]
        for _ in range(n)
    ]
    assert rs._greedy(jobs) == _greedy_reference(jobs)


def test_greedy_scales_to_long_lines() -> None:
    rng = random.Random(1)
    jobs = [rs._Job(None, rng.randint(1, 500), rng.randint(0, 50_000), rng.randint(0, 10_000)) for _ in range(5000)]  # type: ignore[arg-type]
    t0 = dt.datetime.now()
    seq = rs._greedy(jobs)
    assert sorted(seq) == list(range(5000))
    assert (dt.datetime.now() - t0).total_seconds() < 2.0


# ───────────────────────────── temporary closures and new bookings ─────────────────────────────


def _lane(*closures: Closure, transit: int = 3) -> InboundLaneInfo:
    return InboundLaneInfo("L", "O", "UK1", "sea_air", transit, 100, 10, (), None, (10,) * 8, closures)


def test_open_departure_waits_for_temporary_closures() -> None:
    d = dt.date.fromisoformat
    assert _lane().open_departure(d("2026-10-01")) == d("2026-10-01")
    chained = _lane(Closure(d("2026-10-01"), d("2026-10-05")), Closure(d("2026-10-06"), d("2026-10-10")))
    assert chained.open_departure(d("2026-10-01")) == d("2026-10-11")
    # transit 2026-09-28 .. 10-01 touches the first closure -> wait for both
    assert chained.open_departure(d("2026-09-28")) == d("2026-10-11")
    assert chained.open_departure(d("2026-09-27")) == d("2026-09-27")  # arrives 09-30, before it
    assert _lane(Closure(d("2026-10-01"), None)).open_departure(d("2026-10-03")) is None
    assert _lane(Closure(None, None)).open_departure(d("2026-10-03")) is None


def test_expedite_departs_after_a_temporary_closure_of_its_origin() -> None:
    """PORT-A (origin of SEA-UK and SA-UK) is closed 10-05..10-14. PO-1#1 (trims +14 d) waits
    out the backlog by sea (W47); a sea-air split cannot leave before the port reopens and
    W41/W42 (overlapping the closure) have no capacity, so it books W43 and lands W45."""
    port = closure("port", "PORT-A", {"start": "2026-10-05", "end": "2026-10-14"})
    resp = optimize(request([{"code": "expedite_mode_shift"}], [TRIM_DELAY, port]))
    (d,) = resp.decisions
    assert d.lever == "expedite_mode_shift" and (d.book_week, d.arrive_week) == ("2026-W43", "2026-W45")
    assert resp.kpis_after.units_at_risk < resp.kpis_before.units_at_risk
    # An open-ended closure still rules the lane out.
    port["window"] = {"start": "2026-10-05"}
    assert optimize(request([{"code": "expedite_mode_shift"}], [TRIM_DELAY, port])).actions == []


def test_alt_source_waits_for_a_temporary_closure_of_its_lane() -> None:
    # NS-UK (the near-shore lane) is closed 10-10..10-20: the buy leaves 10-21, eta 10-25.
    ns = closure("lane", "NS-UK", {"start": "2026-10-10", "end": "2026-10-20"})
    resp = optimize(request([{"code": "resource_alt_supplier"}], [TRIM_DELAY, ns]))
    (po,) = [a for a in resp.actions if a.op == "po.create"]
    assert po.payload.eta_dc.isoformat() == "2026-10-25"  # type: ignore[union-attr]
    ns["window"] = {"start": "2026-10-10"}
    resp = optimize(request([{"code": "resource_alt_supplier"}], [TRIM_DELAY, ns]))
    assert resp.actions == [] and any("NS-UK of F-N is closed" in w for w in resp.warnings)


# ───────────────────────────── compliance_block window ─────────────────────────────


@pytest.mark.parametrize(
    ("window", "blocked"),
    [
        (None, {"F-N"}),
        ({"start": "2026-09-01", "end": "2026-10-04"}, set()),  # ended before as_of
        ({"start": "2027-01-04"}, set()),  # starts after the horizon
        ({"start": "2026-09-01", "end": "2026-10-05"}, {"F-N"}),
    ],
)
def test_compliance_block_window(window: dict[str, Any] | None, blocked: set[str]) -> None:
    p: dict[str, Any] = {"op": "compliance_block", "target": {"kinds": ["factory"], "ids": ["F-N"]}, "prov": "fact"}
    p["reason"] = "audit"
    if window is not None:
        p["window"] = window
    scn = scn_of([p])
    assert scn.blocked == blocked
    assert bool(blocked) or any("outside [as_of, horizon end]" in w for w in scn.warnings)


# ───────────────────────────── prioritize_allocation data guard ─────────────────────────────


def test_allocation_naming_a_cluster_of_another_dc_is_left_unchanged() -> None:
    from .test_levers_m3 import allocation_problem

    raw = allocation_problem()
    data = raw["data"]
    data["dcs"].append({"code": "E"})
    data["clusters"].append({"code": "E-A", "dc_code": "E"})
    data["allocations"].append(
        {
            "code": "AL-X",
            "sku": "S",
            "dc_code": "D",
            "cluster_code": "E-A",
            "ship_week": "2026-W41",
            "qty": 5,
            "version": 1,
        }
    )
    resp = optimize(raw)
    assert [a.target_ref for a in resp.actions] == ["AL-A", "AL-B"]  # as without the foreign row
    assert any("name a cluster of another DC" in w and "AL-X" in w for w in resp.warnings)


def test_max_changes_keeps_whole_cells_only() -> None:
    """Moving units between two clusters takes both set_qty ops: with max_changes=1 a lone
    half of the move (A down without B up) is never emitted."""
    from .test_levers_m3 import allocation_problem

    raw = allocation_problem()
    raw["plan_spec"]["levers"][0]["params"] = {"max_changes": 1}
    resp = optimize(raw)
    assert resp.actions == [] and any("keeps 0 of 2" in w for w in resp.warnings)
    raw["plan_spec"]["levers"][0]["params"] = {"max_changes": 2}
    assert [a.target_ref for a in optimize(raw).actions] == ["AL-A", "AL-B"]
