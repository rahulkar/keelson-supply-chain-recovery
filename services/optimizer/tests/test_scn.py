"""Scenario normalisation and perturbation semantics on a tiny hand-made problem."""

from __future__ import annotations

from typing import Any

import pytest
from keelson_contracts.optimizer import ImpactRequest, ProblemData

from keelson_opt.scn import build_scenario, round_bp


def tiny(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "tenant": "t1",
        "as_of": "2026-10-05",
        "horizon": {"start_week": "2026-W41", "weeks": 4},
        "dcs": [{"code": "A"}, {"code": "B"}],
        "clusters": [{"code": "A-1", "dc_code": "A"}, {"code": "B-1", "dc_code": "B"}],
        "skus": [{"code": "S1", "style_code": "ST", "retail_price_cents": 1000, "unit_cost_cents": 400}],
        "inventory": [
            {
                "location_type": "dc",
                "location_code": "A",
                "sku": "S1",
                "on_hand": 100,
                "allocated": 30,
                "reserved": 5,
                "in_transit": 7,
            },
            {"location_type": "store", "location_code": "STORE-9", "sku": "S1", "on_hand": 3},
        ],
        "receipts": [
            {"ref": "PO-1#1", "dc_code": "A", "sku": "S1", "qty": 50, "eta": "2026-10-16"},
            {"ref": "PO-2#1", "dc_code": "B", "sku": "S1", "qty": 60, "eta": "2026-10-01"},
            {"ref": "PO-3#1", "dc_code": "B", "sku": "S1", "qty": 70, "eta": "2026-10-30"},
        ],
        "demand": [{"cluster_code": "A-1", "sku": "S1", "iso_week": f"2026-W{w}", "units": 15} for w in range(41, 45)]
        + [{"cluster_code": "A-1", "sku": "S1", "iso_week": "2026-W50", "units": 99}],
        "lanes": [{"from_dc": "A", "to_dc": "B", "transit_days": 8, "cost_per_unit_cents": 10, "max_units_wk": 1000}],
    }
    data.update(over)
    return data


def scn_for(data: dict[str, Any], perturbations: list[dict[str, Any]] | None = None):  # type: ignore[no-untyped-def]
    req = ImpactRequest(data=data, perturbations=perturbations or [])
    return build_scenario(req.data, req.perturbations)


def test_reserved_stock_is_not_double_counted_after_a_committed_transfer() -> None:
    """Merxa applying transfer_order.create(A->B, 40) sets reserved += 40 at A and
    in_transit += 40 at B. Re-planning from that state must see the 40 units once (at B),
    not at both DCs."""
    data = tiny(
        inventory=[
            {"location_type": "dc", "location_code": "A", "sku": "S1", "on_hand": 100, "reserved": 40},
            {"location_type": "dc", "location_code": "B", "sku": "S1", "on_hand": 0, "in_transit": 40},
        ],
        receipts=[],
    )
    scn = scn_for(data)
    assert scn.dc_supply[("A", "S1")] == 60
    assert scn.dc_supply[("B", "S1")] == 40
    assert scn.dc_atp[("A", "S1")] == 60
    assert sum(scn.dc_supply.values()) == 100  # network total == physical units


def test_baseline_buckets_and_stock() -> None:
    scn = scn_for(tiny())
    assert scn.weeks == ["2026-W41", "2026-W42", "2026-W43", "2026-W44"]
    assert scn.dc_supply[("A", "S1")] == 102  # on_hand - reserved + in_transit
    assert scn.dc_atp[("A", "S1")] == 65  # on_hand - allocated - reserved
    assert scn.receipts[("A", "S1")] == [0, 50, 0, 0]
    assert scn.receipts[("B", "S1")] == [60, 0, 0, 70]  # overdue receipt lands in week 0
    assert scn.demand[("A-1", "S1")] == [15, 15, 15, 15]
    assert scn.lanes[0].lag_weeks == 2  # ceil(8 / 7)
    assert scn.first_ship_week == 0
    assert any("store-level" in w for w in scn.warnings)
    assert any("outside the horizon" in w for w in scn.warnings)


def test_lead_time_shift_by_receipt_po_and_beyond_horizon() -> None:
    scn = scn_for(
        tiny(),
        [
            {
                "op": "lead_time_shift",
                "target": {"kinds": ["receipt"], "ids": ["PO-1#1"]},
                "days": 7,
                "prov": "decision",
            },
            {"op": "lead_time_shift", "target": {"kinds": ["po"], "ids": ["PO-3"]}, "days": 3, "prov": "fact"},
        ],
    )
    assert scn.receipts[("A", "S1")] == [0, 0, 50, 0]
    assert scn.receipts[("B", "S1")] == [60, 0, 0, 0]  # 2026-11-02 is after the horizon
    assert any("after the horizon end" in w for w in scn.warnings)


def test_negative_shift_and_window() -> None:
    perturbation = {
        "op": "lead_time_shift",
        "target": {"kinds": ["dc"], "ids": ["B"]},
        "days": -14,
        "prov": "assumption",
        "window": {"start": "2026-10-20", "end": None},
    }
    # Passed as a plain dict: the normaliser reads perturbations structurally, so this
    # works whichever perturbation union (generic with `window`, or local M1) is active.
    scn = build_scenario(ProblemData.model_validate(tiny()), [perturbation])
    # Only PO-3 (eta 2026-10-30, inside the window) moves; PO-2 stays in week 0.
    assert scn.receipts[("B", "S1")] == [60, 70, 0, 0]


def test_windowed_capacity_scale_only_hits_overlapping_weeks() -> None:
    perturbation = {
        "op": "capacity_scale",
        "target": {"kinds": ["lane"], "ids": ["A>B"]},
        "factor_bp": 0,
        "prov": "fact",
        "window": {"start": "2026-10-14", "end": "2026-10-20"},  # Wed W42 .. Tue W43
    }
    scn = build_scenario(ProblemData.model_validate(tiny()), [perturbation])
    assert scn.lanes[0].caps == (1000, 0, 0, 1000)


def test_demand_scale_weeks_rounding_and_targets() -> None:
    scn = scn_for(
        tiny(),
        [
            {
                "op": "demand_scale",
                "target": {"kinds": ["dc"], "ids": ["A"]},
                "factor_bp": 11800,
                "weeks": ["2026-W42", "2026-W43"],
                "prov": "assumption",
            },
            {
                "op": "demand_scale",
                "target": {"kinds": ["style"], "ids": ["NOPE"]},
                "factor_bp": 0,
                "prov": "assumption",
            },
        ],
    )
    assert scn.demand[("A-1", "S1")] == [15, round_bp(15, 11800), 18, 15]
    assert round_bp(15, 11800) == 18  # 17.7 -> 18 (half up)
    assert round_bp(5, 15000) == 8  # 7.5 -> 8
    assert any("demand_scale #2 matched no demand" in w for w in scn.warnings)


def test_capacity_scale_lane_and_unsupported_things() -> None:
    scn = scn_for(
        tiny(),
        [
            {
                "op": "capacity_scale",
                "target": {"kinds": ["lane", "dc"], "ids": ["A>B", "A"]},
                "factor_bp": 2500,
                "prov": "fact",
            },
            {"op": "cost_scale", "target": {"kinds": ["sku"], "ids": ["S1"]}, "factor_bp": 12000, "prov": "fact"},
        ],
    )
    assert scn.lanes[0].caps == (250, 250, 250, 250)
    assert any("['dc'] not supported" in w for w in scn.warnings)
    assert any("'cost_scale' is not supported" in w for w in scn.warnings)


def test_mid_week_as_of_ships_from_next_monday() -> None:
    scn = scn_for(tiny(as_of="2026-10-07"))
    assert scn.first_ship_week == 1


def test_referential_integrity_is_enforced() -> None:
    bad = tiny()
    bad["demand"].append({"cluster_code": "ZZ-9", "sku": "S1", "iso_week": "2026-W41", "units": 1})
    with pytest.raises(ValueError, match="unknown cluster ZZ-9"):
        ProblemData.model_validate(bad)
