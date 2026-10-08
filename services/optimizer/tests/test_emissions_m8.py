"""Incremental transport CO₂e (126 §3): hand-computed integer math per component, signs
(transfer +, expedite +, near-shore can be -, a cancelled sea-air booking -), rounding, coverage,
input-order independence, and no change at all for requests without ``emissions``."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from keelson_contracts.optimizer import OptimizeRequest, OptimizeResponse

from keelson_opt.service import run_optimize
from keelson_opt.wire_m5 import OptimizeRequestM5

from .fixtures.full_problem import BD_TRIMS_DELAY, OPTIONS, full_request
from .fixtures.m8_inputs import DC_LANE_KM, EMISSIONS, FACTORS, LEGS_KM, UNIT_WEIGHTS_G, m8_request
from .test_replan_m5 import request as replan_request

F = {f["mode"]: f["g_co2e_per_tkm"] for f in FACTORS}


def _intensity(lane: str) -> int:
    return sum(km * F[mode] for mode, km in LEGS_KM[lane])


def _kg(num: int) -> int:
    return (2 * num + 10**9) // (2 * 10**9)


def _weight(sku: str) -> int:
    return UNIT_WEIGHTS_G[sku[:3]]


def _solve(option: str, **kw: Any) -> OptimizeResponse:
    raw = m8_request(OPTIONS[option], plan_id=f"opt-{option}", perturbations=[BD_TRIMS_DELAY], **kw)
    return run_optimize(OptimizeRequest.model_validate(raw))


@pytest.fixture(scope="module")
def solved() -> dict[str, OptimizeResponse]:
    return {o: _solve(o) for o in "ABC"}


def test_transfers_add_road_emissions(solved: dict[str, OptimizeResponse]) -> None:
    resp = solved["A"]
    num = sum(
        d.qty * _weight(d.sku) * DC_LANE_KM[f"{d.from_dc}>{d.to_dc}"] * F["road"]
        for d in resp.decisions
        if d.lever == "reallocate_inventory"
    )
    sc = resp.scorecard
    assert sc is not None and sc.co2e_breakdown is not None
    assert sc.co2e_breakdown.transfer_kg == _kg(num) > 0
    assert sc.incremental_co2e_kg == sc.co2e_breakdown.transfer_kg
    assert resp.emissions is not None and resp.emissions.coverage_bp == 10_000 and resp.emissions.uncovered == []


def test_expedite_adds_the_air_leg(solved: dict[str, OptimizeResponse]) -> None:
    resp = solved["B"]
    lanes = {pl["ref"]: pl["lane_code"] for pl in m8_request([])["data"]["po_lines"]}
    num = sum(
        d.qty * _weight(d.sku) * (_intensity(d.lane_code) - _intensity(lanes[d.po_line_ref]))
        for d in resp.decisions
        if d.lever == "expedite_mode_shift"
    )
    sc = resp.scorecard
    assert sc is not None and sc.co2e_breakdown is not None
    assert sc.co2e_breakdown.expedite_kg == _kg(num) > 0
    assert sc.incremental_co2e_kg == sc.co2e_breakdown.transfer_kg + sc.co2e_breakdown.expedite_kg
    assert sc.incremental_co2e_kg > solved["A"].scorecard.incremental_co2e_kg  # type: ignore[union-attr,operator]


def test_nearshore_road_can_beat_deep_sea(solved: dict[str, OptimizeResponse]) -> None:
    resp = solved["C"]
    data = m8_request([])["data"]
    lanes = {pl["ref"]: pl["lane_code"] for pl in data["po_lines"]}
    alt_lane = {(s["factory_code"], s["lane_code"].split("-")[1]): s["lane_code"] for s in data["alt_sources"]}
    num = 0
    for d in resp.decisions:
        if d.lever != "resource_alt_supplier":
            continue
        lane = alt_lane[(d.factory_code, d.dest_dc)]
        num += d.qty * _weight(d.sku) * (_intensity(lane) - _intensity(lanes[d.replaces_po_line_ref]))
    sc = resp.scorecard
    assert sc is not None and sc.co2e_breakdown is not None
    assert num < 0 and sc.co2e_breakdown.nearshore_kg == _kg(num) < 0


def test_do_nothing_has_zero_incremental_co2e() -> None:
    raw = m8_request([], plan_id="do-nothing", perturbations=[BD_TRIMS_DELAY])
    resp = run_optimize(OptimizeRequest.model_validate(raw))
    sc = resp.scorecard
    assert sc is not None and sc.incremental_co2e_kg == 0
    assert sc.co2e_breakdown is not None and sc.co2e_breakdown.model_dump() == {
        "transfer_kg": 0, "expedite_kg": 0, "nearshore_kg": 0, "rebook_kg": 0}
    expected = sum(
        pl["qty"] * _weight(pl["sku"]) * _intensity(pl["lane_code"]) for pl in raw["data"]["po_lines"] if pl["lane_code"]
    )
    assert resp.emissions is not None and resp.emissions.baseline_inbound_kg == _kg(expected)


def test_cancelled_sea_air_booking_saves_co2e() -> None:
    raw = replan_request()
    sa = next(il for il in raw["data"]["inbound_lanes"] if il["code"] == "SA-UK")
    sa["alt_lane_code"] = "SEA-UK"
    legs = {"SEA-UK": [("sea", 20000), ("road", 100)], "SEA-UK-2": [("sea", 15000), ("road", 100)],
            "SA-UK": [("sea", 5000), ("air", 5500)], "SEA-DE": [("sea", 15500), ("road", 300)],
            "NS-UK": [("road", 2300)]}
    for il in raw["data"]["inbound_lanes"]:
        il["legs"] = [{"mode": m, "distance_km": km} for m, km in legs[il["code"]]]
    for lane in raw["data"]["lanes"]:
        lane["distance_km"] = 1100
    weights = {"TOP-M": 300, "KNIT-M": 500, "CHEAP-M": 900}
    for sku in raw["data"]["skus"]:
        sku["unit_weight_g"] = weights[sku["code"]]
    raw["emissions"] = copy.deepcopy(EMISSIONS)
    resp = run_optimize(OptimizeRequestM5.model_validate(raw))
    cancelled = [a.target_ref for a in resp.actions if a.op == "capacity_booking.cancel"]
    assert cancelled == ["CB-0001"]
    sc = resp.scorecard
    assert sc is not None and sc.co2e_breakdown is not None
    delta = (5000 * F["sea"] + 5500 * F["air"]) - (20000 * F["sea"] + 100 * F["road"])
    # The first-come-first-served baseline flies CB-0001 (200 CHEAP-M) and CB-0003; the plan flies
    # CB-0002 (200 TOP-M) and CB-0003 instead: CHEAP-M goes back to sea (-), TOP-M flies (+).
    assert sc.co2e_breakdown.rebook_kg == _kg((-200 * 900 + 200 * 300) * delta) < 0


def test_rounded_components_sum_to_the_total_and_input_order_is_irrelevant() -> None:
    raw = m8_request(OPTIONS["D"], plan_id="opt-D", perturbations=[BD_TRIMS_DELAY])
    shuffled = copy.deepcopy(raw)
    for key in ("skus", "lanes", "inbound_lanes", "po_lines", "receipts", "demand"):
        shuffled["data"][key] = list(reversed(shuffled["data"][key]))
    shuffled["emissions"]["factors"] = list(reversed(shuffled["emissions"]["factors"]))
    a = run_optimize(OptimizeRequest.model_validate(raw))
    b = run_optimize(OptimizeRequest.model_validate(shuffled))
    assert a.scorecard is not None and b.scorecard is not None
    assert a.scorecard.co2e_breakdown == b.scorecard.co2e_breakdown
    assert a.emissions == b.emissions
    c = a.scorecard.co2e_breakdown
    assert c is not None
    assert c.transfer_kg + c.expedite_kg + c.nearshore_kg + c.rebook_kg == a.scorecard.incremental_co2e_kg


def test_coverage_reports_missing_lanes_and_weights() -> None:
    raw = m8_request(OPTIONS["B"], plan_id="opt-B", perturbations=[BD_TRIMS_DELAY])
    for il in raw["data"]["inbound_lanes"]:
        if il["code"] == "CGP-UK1-SA":
            il["legs"] = []
    for sku in raw["data"]["skus"]:
        if sku["code"].startswith("PUF-W-BLK"):
            sku.pop("unit_weight_g")
    resp = run_optimize(OptimizeRequest.model_validate(raw))
    assert resp.emissions is not None
    assert 0 < resp.emissions.coverage_bp < 10_000
    assert "lane:CGP-UK1-SA" in resp.emissions.uncovered
    assert any(u.startswith("sku:PUF-W-BLK") for u in resp.emissions.uncovered)
    assert len(resp.emissions.uncovered) <= 20


def test_factor_set_without_a_mode_leaves_those_lanes_uncovered() -> None:
    raw = m8_request(OPTIONS["B"], plan_id="opt-B", perturbations=[BD_TRIMS_DELAY])
    raw["emissions"]["factors"] = [f for f in raw["emissions"]["factors"] if f["mode"] != "air"]
    resp = run_optimize(OptimizeRequest.model_validate(raw))
    assert resp.emissions is not None and resp.emissions.coverage_bp < 10_000
    assert resp.scorecard is not None and resp.scorecard.co2e_breakdown is not None
    assert resp.scorecard.co2e_breakdown.expedite_kg == 0


def test_requests_without_m8_blocks_are_unchanged(small_raw: dict[str, Any]) -> None:
    """No ``emissions`` / ``robustness``: nothing new in the request or response dumps (the
    small and full goldens, ``test_optimize`` / ``test_full_options``, stay byte-identical)."""
    req = OptimizeRequest.model_validate(full_request(OPTIONS["B"], plan_id="opt-B"))
    dumped = json.dumps(req.model_dump(mode="json"))
    for key in ("emissions", "robustness", "unit_weight_g", "distance_km", "legs"):
        assert f'"{key}"' not in dumped
    resp = run_optimize(req).model_dump(mode="json")
    assert "emissions" not in resp and "robustness" not in resp
    for key in ("incremental_co2e_kg", "co2e_breakdown", "bands"):
        assert key not in resp["scorecard"]
    small = OptimizeRequest.model_validate(small_raw).model_dump(mode="json")
    assert "emissions" not in small and "robustness" not in small


def test_emissions_spec_validation() -> None:
    raw = m8_request(OPTIONS["A"])
    raw["emissions"]["factors"].append({"mode": "sea", "g_co2e_per_tkm": 10})
    with pytest.raises(ValueError, match="duplicate modes"):
        OptimizeRequest.model_validate(raw)
    raw = m8_request(OPTIONS["A"])
    raw["emissions"]["factors"][0]["g_co2e_per_tkm"] = 1.5
    with pytest.raises(ValueError):
        OptimizeRequest.model_validate(raw)
