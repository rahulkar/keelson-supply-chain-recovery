"""The four M3 levers, each alone, on small hand-made problems: expedite_mode_shift,
resource_alt_supplier (incl. MOQ repair, capacity, compliance, atomic groups),
prioritize_allocation, resequence_production (WSPT / enumeration / CP-SAT paths)."""

from __future__ import annotations

import copy
import itertools
import random
from collections import defaultdict
from typing import Any

import pytest
from keelson_contracts.changeset import ChangeSet
from keelson_contracts.optimizer import OptimizeRequest, OptimizeResponse

from keelson_opt.levers import resequence as rs
from keelson_opt.service import run_optimize

from .network import TRIM_DELAY, network_problem, request


def optimize(raw: dict[str, Any]) -> OptimizeResponse:
    return run_optimize(OptimizeRequest.model_validate(raw))


def change_set(resp: OptimizeResponse) -> ChangeSet:
    """Every plan must compile into a typed change set (identity mapping, as the engine does)."""
    return ChangeSet.model_validate(
        {
            "id": "cs-1",
            "tenant": "demo-retail",
            "connector": "merxa",
            "revision": 1,
            "items": [a.change_set_item() for a in resp.actions],
        }
    )


def assert_groups_contiguous_and_one_op_per_po_line(resp: OptimizeResponse) -> None:
    seen: list[str | None] = []
    for a in resp.actions:
        if a.group is not None and seen and seen[-1] != a.group:
            assert a.group not in seen, f"group {a.group} is not contiguous"
        seen.append(a.group)
    refs = [a.target_ref for a in resp.actions if a.op.startswith("po_line.")]
    assert len(refs) == len(set(refs)), "Merxa accepts one op per PO line per change set"


# ───────────────────────────── expedite_mode_shift ─────────────────────────────


def test_expedite_alone_splits_the_delayed_line_onto_sea_air() -> None:
    resp = optimize(request([{"code": "expedite_mode_shift"}]))
    assert resp.status == "OPTIMAL"
    assert (resp.kpis_before.units_at_risk, resp.kpis_after.units_at_risk) == (200, 0)
    (action,) = resp.actions
    assert action.op == "po_line.split" and action.target_ref == "PO-1#1" and action.group is None
    assert action.payload.model_dump() == {"mode": "sea_air", "qty": 200, "lane_code": "SA-UK"}
    (d,) = resp.decisions
    # Readiness 2026-09-25 + 14 days trims delay; Merxa departs the next day (Sat W41) -> +14 d.
    assert (d.book_week, d.arrive_week) == ("2026-W41", "2026-W43")
    assert d.premium_cents == 200 * (150 - 20)
    sc = resp.scorecard
    assert sc is not None
    assert sc.cost_breakdown.expedite_premium == sc.incremental_cost_cents == 26000
    assert sc.net_benefit_cents == resp.kpi_delta.net_benefit_cents == 200 * 2000 - 26000
    assert sc.changes == 1 and sc.lost_units == 0 and sc.fill_rate_bp == 10000
    assert any("expected_version=1" in w for w in resp.warnings)  # PoLine has no version yet
    change_set(resp)


def test_expedite_respects_weekly_capacity_and_reports_its_shadow_price() -> None:
    data = network_problem()
    data["inbound_lanes"][2]["weekly_capacity_units"] = 120
    raw = request([{"code": "expedite_mode_shift"}])
    raw["data"] = data
    resp = optimize(raw)
    assert [a.payload.qty for a in resp.actions] == [120]
    names = {b.name: b.shadow_price_cents for b in resp.explain.binding_constraints}
    # one more unit of sea-air capacity saves a lost TOP unit (margin 2000) minus the premium
    assert names["expedite lane SA-UK cap W41"] == 2000 - 130
    # capacity_scale on the lane (x50 %) halves it again
    raw["perturbations"].append(
        {"op": "capacity_scale", "target": {"kinds": ["lane"], "ids": ["SA-UK"]}, "factor_bp": 5000, "prov": "fact"}
    )
    assert [a.payload.qty for a in optimize(raw).actions] == [60]


def test_expedite_split_stays_below_the_open_quantity_and_skips_closed_lanes() -> None:
    data = network_problem()
    data["po_lines"][0]["qty"] = data["receipts"][0]["qty"] = 150
    # 40 days late: PO-1#1 would arrive after the horizon; a split lands in W47.
    late = {**TRIM_DELAY, "days": 40}
    raw = request([{"code": "expedite_mode_shift"}], [late])
    raw["data"] = data
    (a,) = optimize(raw).actions
    assert a.payload.qty == 149  # Merxa: split qty < open qty
    # The sea-air hub is closed: no candidate lane, nothing to do.
    raw["perturbations"].append(
        {"op": "capacity_scale", "target": {"kinds": ["port"], "ids": ["HUB"]}, "factor_bp": 0, "prov": "fact"}
    )
    resp = optimize(raw)
    assert resp.actions == [] and resp.decisions == []


def test_expedite_params_filter_modes_lanes_refs_and_budget() -> None:
    def run(params: dict[str, Any]) -> OptimizeResponse:
        return optimize(request([{"code": "expedite_mode_shift", "params": params}]))

    assert run({"modes": ["air"]}).actions == []  # no air lane in the data
    assert run({"allowed_lanes": ["NS-UK"]}).actions == []
    assert any("NOPE" in w for w in run({"allowed_lanes": ["NOPE"]}).warnings)
    assert run({"po_line_refs": ["PO-2#1"]}).actions == []
    resp = run({"max_premium_cents": 130 * 50})
    assert [a.payload.qty for a in resp.actions] == [50]
    assert resp.scorecard is not None and resp.scorecard.cost_breakdown.expedite_premium <= 130 * 50
    assert [a.payload.qty for a in run({"max_units_total": 70}).actions] == [70]


# ───────────────────────────── resource_alt_supplier ─────────────────────────────


def test_alt_supplier_alone_is_one_atomic_group() -> None:
    resp = optimize(request([{"code": "resource_alt_supplier"}]))
    assert resp.kpis_after.units_at_risk == 0
    po, chg = resp.actions
    assert (po.op, chg.op) == ("po.create", "po_line.change_qty") and po.group == chg.group == "alt-1"
    assert po.payload.model_dump(mode="json") == {
        "supplier_code": "SUP-N",
        "factory_code": "F-N",
        "dest_dc": "UK1",
        "eta_dc": "2026-10-16",  # as_of + 7 d lead time + 4 d road
        "lines": [{"sku": "TOP-M", "qty": 200}],
    }
    assert (chg.target_ref, chg.payload.qty) == ("PO-1#1", 300)  # reduced by the same quantity
    (d,) = resp.decisions
    assert d.premium_cents == 200 * (1300 + 40 - 1000 - 20)
    assert resp.scorecard is not None and resp.scorecard.cost_breakdown.nearshore_premium == d.premium_cents
    assert_groups_contiguous_and_one_op_per_po_line(resp)
    change_set(resp)


def test_moq_repair_tops_up_or_drops_deterministically() -> None:
    def run(moq: int, **src: Any) -> OptimizeResponse:
        data = network_problem()
        data["alt_sources"][0].update(moq_units=moq, **src)
        raw = request([{"code": "resource_alt_supplier"}])
        raw["data"] = data
        return optimize(raw)

    up = run(300)  # 200 needed: topping up to the MOQ beats losing 200 units
    (po, chg) = up.actions
    assert po.payload.total_qty == 300 and chg.payload.qty == 200
    assert up.kpis_after.units_at_risk == 0
    dropped = run(600)  # the line has only 500 open units: the MOQ cannot be met
    assert dropped.actions == [] and dropped.kpis_after == dropped.kpis_before
    for resp in (up, run(250), run(200)):
        for a in resp.actions:
            if a.op == "po.create":
                assert a.payload.total_qty >= 200


def test_alt_supplier_capacity_and_whole_line_cancel() -> None:
    data = network_problem()
    data["alt_sources"][0].update(moq_units=0, weekly_capacity_units=120)
    closures = [
        {"op": "capacity_scale", "target": {"kinds": ["chokepoint"], "ids": [c]}, "factor_bp": 0, "prov": "fact"}
        for c in ("CAPE", "SUEZ")
    ]
    raw = request([{"code": "resource_alt_supplier"}], closures)
    raw["data"] = data
    resp = optimize(raw)
    per_week: dict[str, int] = defaultdict(int)
    for d in resp.decisions:
        per_week[d.eta.isoformat()] += d.qty
    assert per_week and max(per_week.values()) <= 120
    ops = [a.op for a in resp.actions]
    assert ops.count("po_line.cancel") == 1 or ops.count("po_line.change_qty") == 1
    group = {a.group for a in resp.actions}
    assert len(group) == 1  # every buy replaces PO-1#1: one atomic group
    assert ops[-1] in ("po_line.cancel", "po_line.change_qty") and set(ops[:-1]) == {"po.create"}
    assert_groups_contiguous_and_one_op_per_po_line(resp)
    change_set(resp)


@pytest.mark.parametrize(
    "change",
    [
        {"approved": False},
        {"compliance_ok": False},
    ],
)
def test_unapproved_or_non_compliant_sources_are_never_used(change: dict[str, Any]) -> None:
    data = network_problem()
    data["alt_sources"][0].update(change)
    raw = request([{"code": "resource_alt_supplier"}])
    raw["data"] = data
    assert optimize(raw).actions == []


def test_compliance_block_and_allowed_factories() -> None:
    block = {
        "op": "compliance_block",
        "target": {"kinds": ["supplier"], "ids": ["SUP-N"]},
        "reason": "audit",
        "prov": "fact",
    }
    assert optimize(request([{"code": "resource_alt_supplier"}], [TRIM_DELAY, block])).actions == []
    raw = request([{"code": "resource_alt_supplier", "params": {"allowed_factories": ["F-X"]}}])
    assert optimize(raw).actions == []
    raw = request([{"code": "resource_alt_supplier", "params": {"style_codes": ["KNIT"]}}])
    assert optimize(raw).actions == []  # F-N does not make KNIT


def test_expedite_and_alt_never_touch_the_same_po_line() -> None:
    resp = optimize(request([{"code": "expedite_mode_shift"}, {"code": "resource_alt_supplier"}]))
    assert resp.kpis_after.units_at_risk == 0
    assert_groups_contiguous_and_one_op_per_po_line(resp)
    assert [a.op for a in resp.actions] == ["po_line.split"]  # cheaper than buying near-shore


# ───────────────────────────── prioritize_allocation ─────────────────────────────


def allocation_problem(protected: bool = False) -> dict[str, Any]:
    """One DC, clusters A (protected minimum 10, no stock) and B; 20 units for 20 demand.
    Pro-rata on requests (A 20, B 10) sends A 13 (3 of them only rebuild its buffer) and B 7:
    3 sales lost. Selling first loses none."""
    return {
        "data": {
            "tenant": "demo-retail",
            "as_of": "2026-10-05",
            "horizon": {"start_week": "2026-W41", "weeks": 1},
            "dcs": [{"code": "D"}],
            "clusters": [{"code": "D-A", "dc_code": "D"}, {"code": "D-B", "dc_code": "D"}],
            "skus": [{"code": "S", "style_code": "ST", "retail_price_cents": 2000, "unit_cost_cents": 500}],
            "inventory": [{"location_type": "dc", "location_code": "D", "sku": "S", "on_hand": 20}],
            "demand": [
                {"cluster_code": "D-A", "sku": "S", "iso_week": "2026-W41", "units": 10},
                {"cluster_code": "D-B", "sku": "S", "iso_week": "2026-W41", "units": 10},
            ],
            "protected_min": [{"cluster_code": "D-A", "sku": "S", "units": 10}],
            "allocations": [
                {
                    "code": "AL-A",
                    "sku": "S",
                    "dc_code": "D",
                    "cluster_code": "D-A",
                    "ship_week": "2026-W41",
                    "qty": 13,
                    "protected": protected,
                    "version": 4,
                },
                {
                    "code": "AL-B",
                    "sku": "S",
                    "dc_code": "D",
                    "cluster_code": "D-B",
                    "ship_week": "2026-W41",
                    "qty": 7,
                    "version": 2,
                },
            ],
        },
        "perturbations": [],
        "plan_spec": {"id": "opt-p", "title": "prioritise", "levers": [{"code": "prioritize_allocation"}]},
        "solver": {"time_limit_s": 5, "seed": 0},
    }


def test_prioritize_allocation_sells_before_stocking() -> None:
    resp = optimize(allocation_problem())
    assert (resp.kpis_before.units_at_risk, resp.kpis_after.units_at_risk) == (3, 0)
    assert [(a.op, a.target_ref, a.expected_version, a.payload.qty) for a in resp.actions] == [
        ("allocation.set_qty", "AL-A", 4, 10),
        ("allocation.set_qty", "AL-B", 2, 10),
    ]
    assert [(d.allocation_code, d.qty_before, d.qty_after) for d in resp.decisions] == [
        ("AL-A", 13, 10),
        ("AL-B", 7, 10),
    ]
    assert resp.solver.name == "priority-split"
    change_set(resp)


def test_prioritize_never_cuts_a_protected_allocation_and_honours_max_changes() -> None:
    resp = optimize(allocation_problem(protected=True))
    assert resp.actions == [] and resp.kpis_after == resp.kpis_before  # A keeps 13: nothing to gain
    raw = allocation_problem()
    raw["plan_spec"]["levers"][0]["params"] = {"max_changes": 1}
    assert len(optimize(raw).actions) <= 1
    raw["plan_spec"]["levers"][0]["params"] = {"ship_weeks": ["2026-W42"]}
    resp = optimize(raw)
    assert resp.actions == [] and any("no allocations in scope" in w for w in resp.warnings)


# ───────────────────────────── resequence_production ─────────────────────────────


def reseq_problem() -> dict[str, Any]:
    """Line L-1 (F-1, 100 units/day) makes 3 x 300 units: KNIT, KNIT, then TOP. UK1 runs out
    of TOP in W42 while TOP arrives Fri W43 (PO-X, made elsewhere, covers W45 on); producing
    TOP first saves 6 days (Sat W42) and the W42 sales."""
    data = network_problem()
    data["inventory"][0]["on_hand"] = 0
    data["inventory"][1]["on_hand"] = 2000
    data["receipts"] = [
        {"ref": "PO-T#1", "dc_code": "UK1", "sku": "TOP-M", "qty": 300, "eta": "2026-10-23"},
        {"ref": "PO-K1#1", "dc_code": "DE1", "sku": "KNIT-M", "qty": 300, "eta": "2026-10-17"},
        {"ref": "PO-K2#1", "dc_code": "DE1", "sku": "KNIT-M", "qty": 300, "eta": "2026-10-20"},
        {"ref": "PO-X#1", "dc_code": "UK1", "sku": "TOP-M", "qty": 400, "eta": "2026-11-02"},
    ]
    data["po_lines"] = [
        {
            "ref": r["ref"],
            "po_code": r["ref"].split("#")[0],
            "supplier_code": "SUP-1",
            "factory_code": "F-1",
            "dest_dc": r["dc_code"],
            "sku": r["sku"],
            "qty": 300,
            "eta": r["eta"],
            "ex_factory": "2026-10-13",
            "mode": "road",
            "lane_code": None,
        }
        for r in data["receipts"]
    ]
    data["po_lines"][-1]["factory_code"] = "F-2"  # not made on L-1
    data["production"] = [
        {
            "line_code": "L-1",
            "factory_code": "F-1",
            "daily_capacity_units": 100,
            "orders": [
                {
                    "code": "MO-1",
                    "po_line_ref": "PO-K1#1",
                    "sku": "KNIT-M",
                    "qty": 300,
                    "planned_start": "2026-10-05",
                    "planned_end": "2026-10-07",
                    "seq": 1,
                },
                {
                    "code": "MO-2",
                    "po_line_ref": "PO-K2#1",
                    "sku": "KNIT-M",
                    "qty": 300,
                    "planned_start": "2026-10-08",
                    "planned_end": "2026-10-10",
                    "seq": 2,
                },
                {
                    "code": "MO-3",
                    "po_line_ref": "PO-T#1",
                    "sku": "TOP-M",
                    "qty": 300,
                    "planned_start": "2026-10-11",
                    "planned_end": "2026-10-13",
                    "seq": 3,
                },
                {
                    "code": "MO-0",
                    "po_line_ref": "PO-T#1",
                    "sku": "TOP-M",
                    "qty": 1,
                    "planned_start": "2026-10-01",
                    "planned_end": "2026-10-01",
                    "seq": 0,
                    "movable": False,
                },
            ],
        },
    ]
    return {
        "data": data,
        "perturbations": [],
        "plan_spec": {"id": "opt-r", "title": "resequence", "levers": [{"code": "resequence_production"}]},
        "solver": {"time_limit_s": 5, "seed": 0},
    }


def test_resequence_produces_the_short_style_first() -> None:
    resp = optimize(reseq_problem())
    assert resp.kpis_after.units_at_risk < resp.kpis_before.units_at_risk
    reseq, *rescheds = resp.actions
    assert reseq.op == "production_order.resequence" and reseq.target_ref == "L-1"
    assert reseq.payload.order_codes == ["MO-3", "MO-1", "MO-2"]  # movable orders only
    assert {a.op for a in rescheds} == {"po_line.reschedule"}
    eta = {a.target_ref: a.payload.eta_dc.isoformat() for a in rescheds}
    assert eta == {"PO-T#1": "2026-10-17", "PO-K1#1": "2026-10-20", "PO-K2#1": "2026-10-23"}
    assert {a.group for a in resp.actions} == {"resequence-1"}
    (d,) = resp.decisions
    assert d.order_codes_before == ["MO-1", "MO-2", "MO-3"] and d.order_codes_after == ["MO-3", "MO-1", "MO-2"]
    assert {(c.po_line_ref, c.eta_before.isoformat(), c.eta_after.isoformat()) for c in d.eta_changes} == {
        ("PO-T#1", "2026-10-23", "2026-10-17"),
        ("PO-K1#1", "2026-10-17", "2026-10-20"),
        ("PO-K2#1", "2026-10-20", "2026-10-23"),
    }
    assert resp.solver.params["resequence_methods"] == "L-1:wspt"
    assert resp.scorecard is not None and resp.scorecard.incremental_cost_cents == 0
    change_set(resp)


def test_resequence_respects_late_materials_and_line_filters() -> None:
    raw = reseq_problem()
    # TOP's trims are 7 days late: MO-3 cannot start before 2026-10-18 -> nothing to gain.
    raw["perturbations"] = [
        {"op": "lead_time_shift", "target": {"kinds": ["po_line"], "ids": ["PO-T#1"]}, "days": 7, "prov": "fact"}
    ]
    resp = optimize(raw)
    assert resp.actions == []
    assert resp.solver.params.get("resequence_methods") in (None, "L-1:enumeration")
    raw = reseq_problem()
    raw["plan_spec"]["levers"][0]["params"] = {"line_codes": ["L-9"]}
    resp = optimize(raw)
    assert resp.actions == [] and any("unknown line_codes ['L-9']" in w for w in resp.warnings)
    raw["plan_spec"]["levers"][0]["params"] = {"max_orders_per_line": 2}
    resp = optimize(raw)
    assert resp.actions == [] and any("3 movable orders (> 2)" in w for w in resp.warnings)


def _jobs(n: int, seed: int, releases: bool) -> list[rs._Job]:
    rng = random.Random(seed)
    return [
        rs._Job(None, rng.randint(1, 9), rng.randint(0, 12) if releases else 0, rng.randint(0, 50))  # type: ignore[arg-type]
        for _ in range(n)
    ]


def _brute(jobs: list[rs._Job]) -> int:
    return min(rs._objective(jobs, p) for p in itertools.permutations(range(len(jobs))))


@pytest.mark.parametrize("seed", range(8))
def test_wspt_is_exact_without_releases_and_enumeration_with_them(seed: int) -> None:
    jobs = _jobs(6, seed, releases=False)
    assert rs._objective(jobs, rs._wspt(jobs)) == _brute(jobs)
    jobs = _jobs(6, seed, releases=True)
    assert rs._objective(jobs, rs._enumerate(jobs)) == _brute(jobs)


def test_cpsat_path_is_deterministic_and_never_worse_than_greedy() -> None:
    jobs = _jobs(14, 99, releases=True)
    a, name = rs._cpsat(jobs, seed=7)
    b, _ = rs._cpsat(copy.deepcopy(jobs), seed=7)
    assert a == b and sorted(a) == list(range(14))
    assert name in ("cpsat_optimal", "cpsat_feasible", "greedy")
    assert rs._objective(jobs, a) <= rs._objective(jobs, rs._greedy(jobs))


def test_resequence_uses_cpsat_for_mid_size_lines_with_release_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    raw = reseq_problem()
    line = raw["data"]["production"][0]
    for n in range(4, 11):  # 7 more small orders (no PO line in the data: weight 0)
        line["orders"].append(
            {
                "code": f"MO-{n}",
                "po_line_ref": f"PO-Z{n}#1",
                "sku": "KNIT-M",
                "qty": 50,
                "planned_start": "2026-10-14",
                "planned_end": "2026-10-14",
                "seq": n,
            }
        )
    # KNIT trims for PO-K1#1 are 2 days late: release dates -> CP-SAT (10 movable orders)
    raw["perturbations"] = [
        {"op": "lead_time_shift", "target": {"kinds": ["po_line"], "ids": ["PO-K1#1"]}, "days": 2, "prov": "fact"}
    ]
    resp = optimize(raw)
    assert resp.solver.params["resequence_methods"] in ("L-1:cpsat_optimal", "L-1:cpsat_feasible")
    assert resp.solver.params["cpsat_num_workers"] == 1 and resp.solver.name == "CP-SAT"
    (reseq,) = [a for a in resp.actions if a.op == "production_order.resequence"]
    assert reseq.payload.order_codes[0] == "MO-3"
    assert resp.kpis_after.units_at_risk < resp.kpis_before.units_at_risk
    again = optimize(raw).model_dump(mode="json")
    first = resp.model_dump(mode="json")
    again["solver"].pop("solve_ms"), first["solver"].pop("solve_ms")
    assert again == first


@pytest.mark.parametrize(("env", "workers"), [(None, 1), ("1", 1), ("4", 4), ("0", 1), ("x", 1)])
def test_cpsat_workers_from_env(monkeypatch: pytest.MonkeyPatch, env: str | None, workers: int) -> None:
    if env is None:
        monkeypatch.delenv("CPSAT_NUM_WORKERS", raising=False)
    else:
        monkeypatch.setenv("CPSAT_NUM_WORKERS", env)
    assert rs.cpsat_workers() == workers
