"""M5 targeted re-planning (95 §4): capacity bookings are ProblemData state, a lane capacity
cut below what is booked makes the plan choose which bookings fly (by margin / urgency)
and which are cancelled (their PO lines go back to their sea arrival), compensating
levers act around that choice, and only changes are emitted (delta change set).

The story (UK1, horizon W41-W48, as_of Mon 2026-10-05): sea-air lane SA-UK (1000 units
per week) carries three bookings departing W42 and arriving W44 - CB-0001 200 CHEAP-M
(margin 300 c) on PO-4#2, CB-0002 200 TOP-M (margin 2000 c) on PO-1#2, CB-0003 100 TOP-M
on PO-1#3. Without them both SKUs run short W44-W46 until the sea lines arrive W47. DE1
holds spare CHEAP-M (DC lane DE1>UK1, 4 days, 60 c/unit). A cut to 30 % leaves 300."""

from __future__ import annotations

import copy
import datetime as dt
import json
import random
import time
from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient
from keelson_contracts.changeset import ChangeSet, GenericOpItem
from keelson_contracts.optimizer import OptimizeRequest, ResequenceProductionParams
from pydantic import ValidationError

from keelson_opt.canonical import model_hash
from keelson_opt.isoweeks import iso_week_label
from keelson_opt.levers import resequence as rs
from keelson_opt.plan import EtaMove, LineSequence, base_lines
from keelson_opt.scn import build_scenario
from keelson_opt.service import run_impact, run_optimize
from keelson_opt.wire_m5 import (
    ActionM5,
    ImpactRequestM5,
    OptimizeRequestM5,
    OptimizeResponseM5,
    ProblemDataM5,
)

from .network import network_problem

WEEKS = [f"2026-W{w}" for w in range(41, 49)]
SEA_ETA = "2026-11-16"  # W47
SA_ETA = "2026-10-26"  # W44 (departs Mon 2026-10-12, W42, 14 days)
LEVERS = [{"code": "reallocate_inventory"}, {"code": "expedite_mode_shift"}]


def _pol(ref: str, sku: str, qty: int, eta: str, mode: str, lane: str) -> dict[str, Any]:
    return {
        "ref": ref,
        "po_code": ref.split("#")[0],
        "supplier_code": "SUP-1",
        "factory_code": "F-1",
        "dest_dc": "UK1",
        "sku": sku,
        "qty": qty,
        "eta": eta,
        "ex_factory": "2026-10-11",
        "mode": mode,
        "lane_code": lane,
    }


def bookings(**qty: int) -> list[dict[str, Any]]:
    q = {"CB-0001": 200, "CB-0002": 200, "CB-0003": 100, **qty}
    return [
        {"code": "CB-0001", "lane_code": "SA-UK", "iso_week": "2026-W42", "qty_units": q["CB-0001"], "version": 3,
         "lines": [{"po_line_ref": "PO-4#2", "qty": q["CB-0001"], "revert_eta": SEA_ETA}]},
        {"code": "CB-0002", "lane_code": "SA-UK", "iso_week": "2026-W42", "qty_units": q["CB-0002"], "version": 1,
         "lines": [{"po_line_ref": "PO-1#2", "qty": q["CB-0002"], "revert_eta": SEA_ETA}]},
        {"code": "CB-0003", "lane_code": "SA-UK", "iso_week": "2026-W42", "qty_units": q["CB-0003"], "version": 1,
         "lines": [{"po_line_ref": "PO-1#3", "qty": q["CB-0003"], "revert_eta": SEA_ETA}]},
    ]  # fmt: skip


def replan_data(*, sa_capacity: int = 1000, booking_list: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    data = network_problem()
    data["skus"].append({"code": "CHEAP-M", "style_code": "CHEAP", "retail_price_cents": 1000, "unit_cost_cents": 700})
    data["demand"] += [
        {"cluster_code": cl, "sku": "CHEAP-M", "iso_week": w, "units": u}
        for cl, u in (("UK1-A", 30), ("UK1-B", 20))
        for w in WEEKS
    ]
    data["cluster_inventory"] += [
        {"cluster_code": "UK1-A", "sku": "CHEAP-M", "on_hand": 30},
        {"cluster_code": "UK1-B", "sku": "CHEAP-M", "on_hand": 20},
    ]
    data["inventory"] += [
        {"location_type": "dc", "location_code": "UK1", "sku": "CHEAP-M", "on_hand": 100},
        {"location_type": "dc", "location_code": "DE1", "sku": "CHEAP-M", "on_hand": 400},
    ]
    data["lanes"] = [
        {"from_dc": "DE1", "to_dc": "UK1", "transit_days": 4, "cost_per_unit_cents": 60, "max_units_wk": 5000}
    ]
    data["receipts"] = [
        {"ref": "PO-1#1", "dc_code": "UK1", "sku": "TOP-M", "qty": 200, "eta": SEA_ETA},
        {"ref": "PO-1#2", "dc_code": "UK1", "sku": "TOP-M", "qty": 200, "eta": SA_ETA},
        {"ref": "PO-1#3", "dc_code": "UK1", "sku": "TOP-M", "qty": 100, "eta": SA_ETA},
        {"ref": "PO-4#1", "dc_code": "UK1", "sku": "CHEAP-M", "qty": 50, "eta": SEA_ETA},
        {"ref": "PO-4#2", "dc_code": "UK1", "sku": "CHEAP-M", "qty": 200, "eta": SA_ETA},
        {"ref": "PO-2#1", "dc_code": "DE1", "sku": "KNIT-M", "qty": 150, "eta": "2026-10-26"},
    ]
    data["po_lines"] = [
        _pol("PO-1#1", "TOP-M", 200, SEA_ETA, "sea", "SEA-UK"),
        _pol("PO-1#2", "TOP-M", 200, SA_ETA, "sea_air", "SA-UK"),
        _pol("PO-1#3", "TOP-M", 100, SA_ETA, "sea_air", "SA-UK"),
        _pol("PO-4#1", "CHEAP-M", 50, SEA_ETA, "sea", "SEA-UK"),
        _pol("PO-4#2", "CHEAP-M", 200, SA_ETA, "sea_air", "SA-UK"),
        data["po_lines"][1],  # PO-2#1 (DE1 knit, sea)
    ]
    data["inbound_lanes"][2]["weekly_capacity_units"] = sa_capacity
    data["capacity_bookings"] = bookings() if booking_list is None else booking_list
    return data


def cut(factor_bp: int, lane: str = "SA-UK") -> dict[str, Any]:
    return {
        "op": "capacity_scale",
        "target": {"kinds": ["lane"], "ids": [lane]},
        "factor_bp": factor_bp,
        "prov": "fact",
    }


def request(
    levers: list[dict[str, Any]] | None = None,
    perturbations: list[dict[str, Any]] | None = None,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "data": replan_data() if data is None else data,
        "perturbations": [cut(3000)] if perturbations is None else perturbations,
        "plan_spec": {
            "id": "opt-replan",
            "title": "re-plan",
            "levers": copy.deepcopy(LEVERS if levers is None else levers),
        },
        "solver": {"time_limit_s": 5, "seed": 3},
    }


def optimize(raw: dict[str, Any]) -> OptimizeResponseM5:
    return run_optimize(OptimizeRequestM5.model_validate(raw))


def ops(resp: OptimizeResponseM5) -> list[tuple[str, str | None, str | None]]:
    return [(a.op, a.target_ref, a.group) for a in resp.actions]


def change_set(resp: OptimizeResponseM5) -> ChangeSet:
    return ChangeSet.model_validate(
        {"id": "cs-1", "tenant": "demo-retail", "connector": "merxa", "revision": 1,
         "items": [a.change_set_item() for a in resp.actions]}
    )  # fmt: skip


# ───────────────────────────── the capacity cut ─────────────────────────────


def test_capacity_cut_cancels_the_lowest_value_booking_and_compensates_with_transfers() -> None:
    resp = optimize(request())
    assert resp.status == "OPTIMAL"
    # 300 units left on SA-UK W42: both TOP bookings (2000 c margin, 300 units) fly; the CHEAP
    # booking (300 c margin) is cancelled and its line goes back to the sea arrival (W47).
    cancels = [a for a in resp.actions if a.op == "capacity_booking.cancel"]
    assert [(a.target_ref, a.expected_version, a.payload.model_dump()) for a in cancels] == [
        ("CB-0001", 3, {"booking_code": "CB-0001"})
    ]
    (back,) = [a for a in resp.actions if a.op == "po_line.reschedule"]
    assert back.target_ref == "PO-4#2" and back.payload.model_dump(mode="json") == {"eta_dc": SEA_ETA}
    assert cancels[0].group == back.group == "rebook-1"
    # Compensating lever: CHEAP-M from DE1 covers W44-W46 (150 units), cheaper than the lost margin.
    transfers = [a for a in resp.actions if a.op == "transfer_order.create"]
    assert transfers and {(a.payload.from_dc, a.payload.to_dc) for a in transfers} == {("DE1", "UK1")}
    assert sum(ln.qty for a in transfers for ln in a.payload.lines if ln.sku == "CHEAP-M") == 150
    # order: transfers first (M1 order), then the booking group
    assert [a.op for a in resp.actions] == ["transfer_order.create"] * len(transfers) + [
        "capacity_booking.cancel",
        "po_line.reschedule",
    ]
    # Baseline = first-come-first-served: CB-0001 and CB-0003 fly, CB-0002 (200 TOP) rolls.
    assert resp.kpis_before.units_at_risk == 200
    assert resp.kpis_before.margin_at_risk_cents == 200 * 2000
    assert resp.kpis_after.units_at_risk == 0
    assert resp.kpi_delta.net_benefit_cents == 200 * 2000 - 150 * 60
    assert resp.solver.params["bookings_decidable"] == 3 and resp.solver.params["bookings_cancelled"] == 1
    assert any("CB-0002" in w and "earliest bookings" in w for w in resp.warnings)
    sc = resp.scorecard
    assert sc is not None and sc.changes == len(resp.actions) and sc.incremental_cost_cents == 150 * 60
    # compiles: the cancel is a generic op, the reschedule typed; one op per PO line
    cs = change_set(resp)
    assert isinstance(cs.items[len(transfers)], GenericOpItem)
    refs = [a.target_ref for a in resp.actions if a.op.startswith("po_line.")]
    assert len(refs) == len(set(refs))


def test_delta_only_bookings_that_fly_and_their_lines_are_never_reemitted() -> None:
    every = [
        {"code": "reallocate_inventory"},
        # even when the lever is pointed at the booked lines explicitly
        {"code": "expedite_mode_shift", "params": {"po_line_refs": ["PO-1#1", "PO-1#2", "PO-1#3", "PO-4#2"]}},
        {"code": "resource_alt_supplier", "params": {"style_codes": ["TOP", "CHEAP"]}},
        {"code": "prioritize_allocation"},
    ]
    resp = optimize(request(every))
    targets = {a.target_ref for a in resp.actions}
    assert not targets & {"CB-0002", "CB-0003", "PO-1#2", "PO-1#3"}
    assert not [a for a in resp.actions if a.op == "po_line.split" and a.target_ref in {"PO-1#2", "PO-1#3", "PO-4#2"}]
    assert [a.target_ref for a in resp.actions if a.op == "capacity_booking.cancel"] == ["CB-0001"]
    refs = [a.target_ref for a in resp.actions if a.op.startswith("po_line.")]
    assert len(refs) == len(set(refs))
    change_set(resp)


def test_no_change_when_the_capacity_still_suffices() -> None:
    for factor in (5000, 10000):  # 500 = exactly what is booked; 1000 = untouched
        resp = optimize(request(perturbations=[cut(factor)]))
        assert resp.actions == [] and resp.decisions == []
        assert resp.kpis_before.units_at_risk == resp.kpis_after.units_at_risk == 0
        assert "bookings_decidable" not in resp.solver.params
        assert not [w for w in resp.warnings if "capacity_bookings" in w]


def test_without_levers_the_response_is_the_first_come_first_served_baseline() -> None:
    resp = optimize(request([]))
    assert resp.actions == [] and resp.solver.name == "none"
    assert resp.kpis_before == resp.kpis_after and resp.kpis_before.units_at_risk == 200
    imp = run_impact(ImpactRequestM5.model_validate({"data": replan_data(), "perturbations": [cut(3000)]}))
    assert imp.kpis == resp.kpis_before
    assert imp.baseline_kpis.units_at_risk == 0  # without the cut every booking flies
    assert any("capacity_bookings" in w for w in imp.warnings)


def test_do_nothing_fallback_still_cancels_what_no_longer_fits() -> None:
    # Only CHEAP-M bookings, all the same value: the first-come-first-served choice is
    # already optimal, and no lever can help (no DC lane), so the plan is the fallback - it
    # still cancels the booking that does not fit (the capacity cut leaves no other plan).
    data = replan_data(sa_capacity=1000)
    data["lanes"] = []
    data["capacity_bookings"] = [
        {"code": "CB-0001", "lane_code": "SA-UK", "iso_week": "2026-W42", "qty_units": 200,
         "lines": [{"po_line_ref": "PO-4#2", "qty": 200, "revert_eta": SEA_ETA}]},
        {"code": "CB-0002", "lane_code": "SA-UK", "iso_week": "2026-W42", "qty_units": 200,
         "lines": [{"po_line_ref": "PO-1#2", "qty": 200, "revert_eta": SEA_ETA}]},
    ]  # fmt: skip
    resp = optimize(request([{"code": "reallocate_inventory"}], [cut(2500)], data))
    # 250 left: one booking flies. TOP (CB-0002) is worth more than CHEAP (CB-0001), so the
    # plan keeps CB-0002 although first-come-first-served would keep CB-0001.
    assert [a.target_ref for a in resp.actions if a.op == "capacity_booking.cancel"] == ["CB-0001"]
    assert resp.kpis_after.margin_at_risk_cents < resp.kpis_before.margin_at_risk_cents
    # Swap the values: now first-come-first-served is optimal -> fallback, same cancel set.
    data2 = copy.deepcopy(data)
    data2["capacity_bookings"][0]["lines"][0]["po_line_ref"] = "PO-1#2"
    data2["capacity_bookings"][1]["lines"][0]["po_line_ref"] = "PO-4#2"
    resp2 = optimize(request([{"code": "reallocate_inventory"}], [cut(2500)], data2))
    assert [(a.op, a.target_ref) for a in resp2.actions] == [
        ("capacity_booking.cancel", "CB-0002"),
        ("po_line.reschedule", "PO-4#2"),
    ]
    assert resp2.solver.params.get("plan_fallback") == "do_nothing"
    assert resp2.kpis_after == resp2.kpis_before
    assert any("still cancels" in w for w in resp2.warnings)
    assert any("expected_version=1" in w and "capacity_bookings" in w for w in resp2.warnings)


def test_bookings_draw_lane_capacity_from_new_splits() -> None:
    # No cut: SA-UK 550/week, 500 booked in W42 -> a new split departing W42 gets 50 only.
    data = replan_data(sa_capacity=550)
    data["receipts"][0]["qty"] = data["po_lines"][0]["qty"] = 600  # PO-1#1 (sea) has 400 spare
    data["demand"] += [{"cluster_code": "UK1-A", "sku": "TOP-M", "iso_week": w, "units": 100} for w in WEEKS[3:6]]
    raw = request([{"code": "expedite_mode_shift", "params": {"po_line_refs": ["PO-1#1"]}}], [], data)
    resp = optimize(raw)
    (split,) = [a for a in resp.actions if a.op == "po_line.split"]
    assert split.target_ref == "PO-1#1" and split.payload.qty == 50
    assert not [a for a in resp.actions if a.op == "capacity_booking.cancel"]
    names = {b.name for b in resp.explain.binding_constraints}
    assert "expedite lane SA-UK cap W42" in names
    # Without the bookings (M3 view) the same split would take the whole 300-unit shortfall.
    raw_m3 = copy.deepcopy(raw)
    del raw_m3["data"]["capacity_bookings"]
    resp_m3 = optimize(raw_m3)
    assert [a.payload.qty for a in resp_m3.actions if a.op == "po_line.split"] == [300]


def test_departed_bookings_are_never_questioned() -> None:
    data = replan_data()
    for b in data["capacity_bookings"]:
        b["iso_week"] = "2026-W40"  # departed before the as_of week
    resp = optimize(request(data=data, perturbations=[cut(0)]))
    assert not [a for a in resp.actions if a.op == "capacity_booking.cancel"]
    assert "bookings_decidable" not in resp.solver.params


def test_revert_eta_is_derived_from_the_lanes_sea_alternative() -> None:
    data = replan_data()
    data["inbound_lanes"][2]["alt_lane_code"] = "SEA-UK"  # sea, 31 days
    for b in data["capacity_bookings"]:
        for ln in b["lines"]:
            ln.pop("revert_eta")
    resp = optimize(request(data=data))
    (back,) = [a for a in resp.actions if a.op == "po_line.reschedule"]
    # Merxa departure rule max(as_of, ex_factory) + 1 = 2026-10-12, + 31 days transit
    assert (back.target_ref, back.payload.model_dump(mode="json")) == ("PO-4#2", {"eta_dc": "2026-11-12"})


def test_cancelled_line_without_a_sea_route_is_lost_and_reported() -> None:
    data = replan_data()
    for b in data["capacity_bookings"]:
        for ln in b["lines"]:
            ln.pop("revert_eta")  # SA-UK has no alt lane
    scn = build_scenario(ProblemDataM5.model_validate(data), [cut(3000)])
    assert scn.decidable == ("CB-0001", "CB-0002", "CB-0003")
    assert scn.baseline_kept == frozenset({"CB-0001", "CB-0003"})
    (rolled,) = [rl for rl in scn.receipt_lines if rl.ref == "PO-1#2"]
    assert rolled.lost and rolled.qty == 200
    resp = optimize(request(data=data))
    assert [a.target_ref for a in resp.actions if a.op == "capacity_booking.cancel"] == ["CB-0001"]
    assert not [a for a in resp.actions if a.op == "po_line.reschedule"]
    assert any("no sea route" in w for w in resp.warnings)


def test_partial_line_booking_without_sea_route_loses_only_the_booked_units() -> None:
    data = replan_data()
    data["capacity_bookings"][0]["qty_units"] = 120
    data["capacity_bookings"][0]["lines"][0]["qty"] = 120  # 80 of PO-4#2 are not on the booking
    data["capacity_bookings"][0]["lines"][0].pop("revert_eta")  # SA-UK has no sea alternative
    scn = build_scenario(ProblemDataM5.model_validate(data), [cut(1000)])  # 100 left: nothing fits but CB-0003
    assert scn.baseline_kept == frozenset({"CB-0003"})
    by_ref = sorted((rl.ref, rl.qty, rl.week, rl.lost) for rl in scn.receipt_lines if rl.ref == "PO-4#2")
    assert by_ref == [("PO-4#2", 80, 3, False), ("PO-4#2", 120, 0, True)]


def test_determinism_and_input_order_independence() -> None:
    a = optimize(request()).model_dump(mode="json")
    raw = request()
    rnd = random.Random(7)
    for key in ("capacity_bookings", "po_lines", "receipts", "demand", "inventory"):
        rnd.shuffle(raw["data"][key])
    b = optimize(raw).model_dump(mode="json")
    for d in (a, b):
        d["solver"].pop("solve_ms")
        d["solver"].pop("inputs_hash")
    assert a == b


def test_response_round_trips_and_shared_models_reject_the_new_op() -> None:
    resp = optimize(request())
    wire = resp.model_dump(mode="json")
    assert OptimizeResponseM5.model_validate(wire) == resp
    cancel = next(a for a in wire["actions"] if a["op"] == "capacity_booking.cancel")
    assert set(cancel) == {"seq", "op", "target_ref", "expected_version", "payload", "group"}
    with pytest.raises(ValidationError):
        ActionM5.model_validate({**cancel, "target_ref": "CB-9999"})
    with pytest.raises(ValidationError):
        ActionM5.model_validate({**cancel, "payload": {}})


def test_inputs_hash_of_pre_m5_requests_is_unchanged(small_raw: dict[str, Any]) -> None:
    assert model_hash(OptimizeRequestM5.model_validate(small_raw)) == model_hash(
        OptimizeRequest.model_validate(small_raw)
    )
    raw = request()
    assert "capacity_bookings" in OptimizeRequestM5.model_validate(raw).model_dump(mode="json")["data"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda d: d["capacity_bookings"][0].update(lane_code="NOPE"), "unknown inbound lane"),
        (lambda d: d["capacity_bookings"][0]["lines"][0].update(po_line_ref="PO-9#1"), "unknown po_line"),
        (lambda d: d["capacity_bookings"][1]["lines"][0].update(po_line_ref="PO-4#2"), "already carried"),
        (lambda d: d["capacity_bookings"][0]["lines"][0].update(qty=201), "more than qty_units"),
        (lambda d: d["capacity_bookings"][1].update(code="CB-0001"), "duplicate capacity_booking"),
        (lambda d: d["capacity_bookings"][0].update(qty_units=1.5), "integer"),
    ],
)
def test_booking_validation(mutate: Callable[[dict[str, Any]], None], message: str) -> None:
    data = replan_data()
    mutate(data)
    with pytest.raises(ValidationError, match=message):
        ProblemDataM5.model_validate(data)


def test_http_optimize_and_impact_accept_bookings(client: TestClient, auth: Callable[..., dict[str, str]]) -> None:
    r = client.post("/v1/optimize", json=request(), headers=auth())
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert [a["op"] for a in body["actions"]][-2:] == ["capacity_booking.cancel", "po_line.reschedule"]
    assert body == optimize(request()).model_dump(mode="json") | {"solver": body["solver"]}
    bad = request()
    bad["data"]["capacity_bookings"][0]["lane_code"] = "NOPE"
    r = client.post("/v1/optimize", json=bad, headers=auth())
    assert r.status_code == 422 and "unknown inbound lane" in r.text
    imp = {"data": replan_data(), "perturbations": [cut(3000)], "include_graph": True}
    r = client.post("/v1/impact", json=imp, headers=auth())
    assert r.status_code == 200 and r.json()["kpis"]["units_at_risk"] == 200
    # the rolled booking's PO is a disrupted path in the graph; its DC's TTS reflects the late units
    nodes = {n["id"]: n for n in r.json()["graph"]["nodes"]}
    assert nodes["po:PO-1"]["status"] != "ok" and nodes["dc:UK1"]["status"] == "impacted"


def test_resequence_never_moves_a_line_under_re_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    data = replan_data()
    data["production"] = [{"line_code": "L1", "factory_code": "F-1", "daily_capacity_units": 100, "orders": []}]
    scn = build_scenario(ProblemDataM5.model_validate(data), [cut(3000)])
    move = EtaMove("PO-4#2", dt.date(2026, 10, 26), dt.date(2026, 10, 19), 2, False)
    seq = LineSequence("L1", "F-1", ("A", "B"), ("B", "A"), ("PO-4#2",))
    monkeypatch.setattr(rs, "_line", lambda *_a, **_k: (seq, {"PO-4#2": move}))
    params = ResequenceProductionParams()
    free = rs.plan_resequence(scn, base_lines(scn), params, seed=0, w_lost_bp=10000)
    assert not any("carried by a capacity booking" in w for w in free.warnings)
    out = rs.plan_resequence(scn, base_lines(scn), params, seed=0, w_lost_bp=10000, pinned=frozenset({"PO-4#2"}))
    assert out.moves == {} and out.sequences == []
    assert any("carried by a capacity booking" in w for w in out.warnings)


@pytest.mark.perf
def test_s4_full_profile_replan_under_budget(capsys: pytest.CaptureFixture[str]) -> None:
    """The Merxa seed books every in-production BD purchase order on its sea-air lane; the
    Act 3 inject cuts CGP-UK1-SA to 30 %: all five levers + the booking decision < 5 s."""
    from .fixtures.full_problem import BD_TRIMS_DELAY, OPTIONS, full_request

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
    t0 = time.perf_counter()
    req = OptimizeRequestM5.model_validate(raw)
    resp = run_optimize(req)
    elapsed = time.perf_counter() - t0
    assert resp.status == "OPTIMAL"
    assert resp.solver.params["bookings_decidable"] >= 2 and resp.solver.params["bookings_cancelled"] >= 1
    cancels = [a.target_ref for a in resp.actions if a.op == "capacity_booking.cancel"]
    assert cancels and all(a.group for a in resp.actions if a.op == "capacity_booking.cancel")
    booked_refs = {ln["po_line_ref"] for b in data["capacity_bookings"] for ln in b["lines"]}
    assert not [a for a in resp.actions if a.op != "po_line.reschedule" and a.target_ref in booked_refs]
    refs = [a.target_ref for a in resp.actions if a.op.startswith("po_line.")]
    assert len(refs) == len(set(refs))
    change_set(resp)
    with capsys.disabled():
        print(
            f"\n[S4] full profile re-plan (CGP-UK1-SA 30 %), all levers: total={elapsed:.2f}s "
            f"(solver {resp.solver.name}, {resp.solver.solve_ms} ms) cancels={len(cancels)} "
            f"actions={len(resp.actions)} body={len(json.dumps(raw)) / 1e6:.1f} MB"
        )
    assert elapsed < 5.0


# ───────────────────────────── review regressions ─────────────────────────────


def _booked_load(resp: OptimizeResponseM5, booking_list: list[dict[str, Any]]) -> int:
    """Units the response leaves on SA-UK in W42: kept bookings + new splits departing W42."""
    cancelled = {a.target_ref for a in resp.actions if a.op == "capacity_booking.cancel"}
    kept = sum(b["qty_units"] for b in booking_list if b["code"] not in cancelled and b["iso_week"] == "2026-W42")
    split = sum(
        d.qty for d in resp.decisions if d.lever == "expedite_mode_shift" and d.lane_code == "SA-UK"
        and d.book_week == "2026-W42"
    )  # fmt: skip
    return kept + split


def test_baseline_booking_fallback_never_overbooks_the_lane(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review fix: when the optimised booking choice loses to first-come-first-served, the
    switch to the baseline choice must not keep splits that were planned against the
    capacity the optimised choice left free (that would book more than the cut lane holds)."""
    from keelson_opt import planner

    # A deliberately poor booking choice: keep only the cheap booking (200 of 300 units).
    monkeypatch.setattr(planner, "_decide_bookings", lambda *_a, **_k: (frozenset({"CB-0001"}), 0, []))
    data = replan_data()
    data["lanes"] = []
    raw = request([{"code": "expedite_mode_shift", "params": {"po_line_refs": ["PO-1#1"]}}], [cut(3000)], data)
    resp = optimize(raw)
    assert resp.status == "OPTIMAL"
    assert _booked_load(resp, data["capacity_bookings"]) <= 300
    change_set(resp)


def test_resequence_never_moves_any_booked_line(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review fix: every booked PO line is an already-applied decision, not only the lines of
    over-booked cells - a resequence must not move one (it would miss its booked departure)."""
    from keelson_opt import planner

    seen: list[frozenset[str]] = []
    real = planner.plan_resequence

    def spy(*a: Any, **k: Any) -> Any:
        seen.append(k["pinned"])
        return real(*a, **k)

    monkeypatch.setattr(planner, "plan_resequence", spy)
    data = replan_data()
    data["production"] = [{"line_code": "L1", "factory_code": "F-1", "daily_capacity_units": 100, "orders": []}]
    optimize(request([{"code": "resequence_production"}], [], data))  # no cut: nothing is decidable
    assert seen == [frozenset({"PO-1#2", "PO-1#3", "PO-4#2"})]


def test_partial_line_booking_cancel_matches_the_reschedule_action() -> None:
    """Review fix: ``po_line.reschedule`` moves the whole PO line in the target system, so the
    re-plan must simulate the whole line at its sea arrival (not only the booked units)."""
    data = replan_data()
    data["capacity_bookings"][0]["qty_units"] = 120
    data["capacity_bookings"][0]["lines"][0]["qty"] = 120
    resp = optimize(request(data=data, perturbations=[cut(1000)]))  # 100 left: only CB-0003 fits
    back = {a.target_ref: a.payload.model_dump(mode="json") for a in resp.actions if a.op == "po_line.reschedule"}
    assert back.get("PO-4#2") == {"eta_dc": SEA_ETA}
    scn = build_scenario(ProblemDataM5.model_validate(data), [cut(1000)])
    assert sorted((rl.ref, rl.qty, rl.week) for rl in scn.receipt_lines if rl.ref == "PO-4#2") == [("PO-4#2", 200, 6)]


@pytest.mark.parametrize(
    "levers",
    [[{"code": "prioritize_allocation"}], [{"code": "resource_alt_supplier", "params": {"style_codes": ["TOP"]}}]],
)
def test_booking_decision_without_transfer_or_expedite_levers(levers: list[dict[str, Any]]) -> None:
    resp = optimize(request(levers))
    assert resp.status == "OPTIMAL"
    assert [a.target_ref for a in resp.actions if a.op == "capacity_booking.cancel"] == ["CB-0001"]
    change_set(resp)
