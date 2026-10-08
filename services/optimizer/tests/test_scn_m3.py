"""M3 perturbation resolution (80 §2): supplier / component via BOM, factory, port /
chokepoint / inbound-lane closures (wait, catalogued re-route, lost), partial capacity,
compliance blocks."""

from __future__ import annotations

import datetime as dt
from typing import Any

from keelson_contracts.optimizer import ImpactRequest

from keelson_opt.scn import Scenario, build_scenario

from .network import network_problem


def scn_of(perturbations: list[dict[str, Any]], **over: Any) -> Scenario:
    req = ImpactRequest(data=network_problem(**over), perturbations=perturbations)
    return build_scenario(req.data, req.perturbations)


def line(scn: Scenario, ref: str):  # type: ignore[no-untyped-def]
    (rl,) = [rl for rl in scn.receipt_lines if rl.ref == ref]
    return rl


def shift(kind: str, ident: str, days: int = 7) -> dict[str, Any]:
    return {"op": "lead_time_shift", "target": {"kinds": [kind], "ids": [ident]}, "days": days, "prov": "fact"}


def closure(kind: str, ident: str, window: dict[str, Any] | None = None, factor: int = 0) -> dict[str, Any]:
    p: dict[str, Any] = {
        "op": "capacity_scale",
        "target": {"kinds": [kind], "ids": [ident]},
        "factor_bp": factor,
        "prov": "fact",
    }
    if window is not None:
        p["window"] = window
    return p


def test_supplier_resolves_through_bom_at_the_factory() -> None:
    scn = scn_of([shift("supplier", "TRIM-1")])
    assert line(scn, "PO-1#1").prod_delay == 7  # CMP-Z from TRIM-1 is used at F-1
    assert line(scn, "PO-2#1").prod_delay == 0
    assert scn.receipts[("UK1", "TOP-M")][4] == 500  # W44 -> W45
    assert scn.disrupted == {"supplier": {"TRIM-1"}}
    # Same style made at another factory: TRIM-1's BOM row is F-1 only, so no match ...
    pl = network_problem()["po_lines"]
    pl[0]["factory_code"] = "F-9"
    scn = scn_of([shift("supplier", "TRIM-1")], po_lines=pl)
    assert line(scn, "PO-1#1").prod_delay == 0
    assert any("matched no receipts" in w for w in scn.warnings)
    # ... while TRIM-2's BOM row has factory null = every factory.
    scn = scn_of([shift("supplier", "TRIM-2")], po_lines=pl)
    assert line(scn, "PO-1#1").prod_delay == 7


def test_component_direct_supplier_and_factory_kinds() -> None:
    assert line(scn_of([shift("component", "CMP-L", 3)]), "PO-1#1").prod_delay == 3
    assert line(scn_of([shift("component", "CMP-Y", 3)]), "PO-2#1").prod_delay == 3
    scn = scn_of([shift("supplier", "SUP-2", 4)])  # the PO line's own supplier
    assert (line(scn, "PO-1#1").prod_delay, line(scn, "PO-2#1").prod_delay) == (0, 4)
    scn = scn_of([shift("factory", "F-1", 5), shift("factory", "F-2", 2)])
    assert (line(scn, "PO-1#1").prod_delay, line(scn, "PO-2#1").prod_delay) == (5, 2)
    assert scn.disrupted == {"factory": {"F-1", "F-2"}}


def test_permanent_chokepoint_closure_reroutes_onto_the_catalogued_same_mode_lane() -> None:
    scn = scn_of([closure("chokepoint", "CAPE")])
    rl = line(scn, "PO-1#1")
    assert (rl.transport_delay, rl.lost) == (10, False)  # SEA-UK-2 (via SUEZ) is 10 days longer
    assert rl.eta_perturbed == dt.date(2026, 11, 5) and rl.week == 4  # W45
    assert scn.inbound_lanes["SEA-UK"].caps == (0,) * 8
    assert scn.inbound_lanes["SEA-UK-2"].caps == (10000,) * 8
    assert line(scn, "PO-2#1").transport_delay == 0  # SEA-DE does not pass the Cape
    assert scn.disrupted == {"chokepoint": {"CAPE"}}


def test_receipt_is_lost_when_the_alternative_is_closed_too() -> None:
    scn = scn_of([closure("chokepoint", "CAPE"), closure("chokepoint", "SUEZ")])
    assert line(scn, "PO-1#1").lost and line(scn, "PO-2#1").lost
    assert ("UK1", "TOP-M") not in scn.receipts
    assert any("650 receipt units are lost" in w for w in scn.warnings)


def test_a_faster_premium_alt_lane_is_never_used_automatically() -> None:
    lanes = network_problem()["inbound_lanes"]
    lanes[0]["alt_lane_code"] = "SA-UK"
    scn = scn_of([closure("chokepoint", "CAPE")], inbound_lanes=lanes)
    assert line(scn, "PO-1#1").lost  # sea-air is an expedite decision, not a re-route


def test_temporary_closure_delays_by_its_length_unless_rerouting_is_faster() -> None:
    # Transit of PO-1#1: 2026-09-25 .. 2026-10-26.
    scn = scn_of([closure("port", "PORT-UK", {"start": "2026-10-20", "end": "2026-10-24"})])
    assert line(scn, "PO-1#1").transport_delay == 5  # backlog of a 5-day closure < 10-day re-route
    scn = scn_of([closure("port", "PORT-UK", {"start": "2026-10-01", "end": "2026-10-20"})])
    assert line(scn, "PO-1#1").transport_delay == 20  # PORT-UK is on SEA-UK-2 too: only waiting helps
    scn = scn_of([closure("chokepoint", "CAPE", {"start": "2026-10-01", "end": "2026-10-20"})])
    assert line(scn, "PO-1#1").transport_delay == 10  # 20-day closure: re-route via SEA-UK-2 is faster
    scn = scn_of([closure("port", "PORT-UK", {"start": "2026-11-01", "end": "2026-11-05"})])
    assert line(scn, "PO-1#1").transport_delay == 0  # after the arrival: not affected
    open_ended = scn_of([closure("port", "PORT-B", {"start": "2026-10-01"})])
    assert line(open_ended, "PO-2#1").lost  # origin closed for good, no alternative


def test_lane_kind_matches_inbound_codes_and_partial_capacity_only_limits_new_bookings() -> None:
    scn = scn_of([closure("lane", "SEA-DE")])
    assert line(scn, "PO-2#1").lost
    scn = scn_of([closure("lane", "SA-UK", {"start": "2026-10-12", "end": "2026-10-18"}, factor=5000)])
    assert scn.inbound_lanes["SA-UK"].caps == (300, 150, 300, 300, 300, 300, 300, 300)
    assert not any(rl.affected for rl in scn.receipt_lines)


def test_compliance_block_and_unsupported_kinds() -> None:
    scn = scn_of(
        [
            {
                "op": "compliance_block",
                "target": {"kinds": ["factory"], "ids": ["F-N"]},
                "reason": "audit",
                "prov": "fact",
            },
            {"op": "lead_time_shift", "target": {"kinds": ["lane"], "ids": ["SEA-UK"]}, "days": 3, "prov": "fact"},
            {
                "op": "compliance_block",
                "target": {"kinds": ["supplier"], "ids": ["NOBODY"]},
                "reason": "x",
                "prov": "fact",
            },
        ]
    )
    assert scn.blocked == {"F-N"}
    assert any("['lane'] not supported" in w for w in scn.warnings)
    assert any("compliance_block #2 matched no supplier or factory" in w for w in scn.warnings)


def test_m1_data_without_po_lines_behaves_as_before() -> None:
    data = network_problem()
    for key in ("po_lines", "bom", "inbound_lanes", "alt_sources"):
        data.pop(key)
    req = ImpactRequest(data=data, perturbations=[shift("po", "PO-1", 7), shift("supplier", "TRIM-1")])
    scn = build_scenario(req.data, req.perturbations)
    assert line(scn, "PO-1#1").prod_delay == 7  # po kind still works
    assert any("lead_time_shift #2 matched no receipts" in w for w in scn.warnings)
