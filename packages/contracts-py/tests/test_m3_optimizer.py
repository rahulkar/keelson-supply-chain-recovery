"""keelson_contracts.optimizer M3 additions (docs/contracts/80-m3-deterministic-core.md §2-§3):
ProblemData v2, impact graph / TTS, the five v1 levers, scorecard, typed decisions and
actions with atomic groups. Backward compatibility: M1 payloads dump (and hash) unchanged."""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest
from keelson_contracts import changeset as cs
from keelson_contracts import optimizer as opt
from keelson_contracts.common import sha256_hex
from pydantic import ValidationError
from test_optimizer_models import CONTRACT_PERTURBATIONS, PLAN_SPEC, kpis, problem

# ───────────────────────────── fixtures ─────────────────────────────

V2: dict[str, Any] = {
    "locations": [
        {"code": "UK1", "kind": "dc", "name": "Daventry", "lat_e6": 52_257_000, "lon_e6": -1_163_000},
        {"code": "UK1-COLD-A", "kind": "cluster", "name": "UK1 cold A", "lat_e6": 54_000_000,
         "lon_e6": -2_000_000, "parent": "UK1"},
        {"code": "BDCGP", "kind": "port", "name": "Chittagong", "lat_e6": 22_330_000, "lon_e6": 91_800_000},
        {"code": "F-BD-01", "kind": "factory", "name": "Gazipur", "lat_e6": 24_000_000, "lon_e6": 90_400_000,
         "parent": "TRIM-BD-01"},
    ],
    "po_lines": [
        {"ref": "PO-26-0101#1", "po_code": "PO-26-0101", "supplier_code": "SUP-BD-01", "factory_code": "F-BD-01",
         "dest_dc": "UK1", "sku": "PUF-W-BLK-M", "qty": 3000, "eta": "2026-11-02", "ex_factory": "2026-10-01",
         "mode": "sea", "lane_code": "BDCGP-GBFXT-CAPE"},
        {"ref": "PO-26-0101#2", "po_code": "PO-26-0101", "supplier_code": "SUP-BD-01", "factory_code": "F-BD-01",
         "dest_dc": "UK1", "sku": "PUF-W-BLK-M", "qty": 500, "eta": "2026-11-09", "ex_factory": "2026-10-08",
         "mode": "sea"},
    ],
    "bom": [
        {"style_code": "PUF-W-BLK", "component_code": "ZIP-5", "supplier_code": "TRIM-BD-01", "factory_code": None},
        {"style_code": "PUF-W-BLK", "component_code": "WAD-200", "supplier_code": "TRIM-BD-01",
         "factory_code": "F-BD-01"},
    ],
    "inbound_lanes": [
        {"code": "BDCGP-GBFXT-CAPE", "origin": "BDCGP", "dest_dc": "UK1", "mode": "sea", "transit_days": 38,
         "cost_per_unit_cents": 42, "weekly_capacity_units": 60000, "via": ["CAPE"]},
        {"code": "BDCGP-GBFXT-SUEZ", "origin": "BDCGP", "dest_dc": "UK1", "mode": "sea", "transit_days": 28,
         "cost_per_unit_cents": 45, "weekly_capacity_units": 60000, "via": ["SUEZ"],
         "alt_lane_code": "BDCGP-GBFXT-CAPE"},
        {"code": "BDCGP-AEJEA-UK1", "origin": "BDCGP", "dest_dc": "UK1", "mode": "sea_air", "transit_days": 14,
         "cost_per_unit_cents": 310, "weekly_capacity_units": 4000, "via": ["AEJEA"]},
        {"code": "MATNG-UK1", "origin": "MATNG", "dest_dc": "UK1", "mode": "road", "transit_days": 5,
         "cost_per_unit_cents": 120, "weekly_capacity_units": 8000},
    ],
    "alt_sources": [
        {"supplier_code": "SUP-MA-01", "factory_code": "F-MA-01", "style_codes": ["PUF-W-BLK"],
         "lead_time_days": 21, "unit_cost_cents": 1150, "moq_units": 600, "weekly_capacity_units": 2500,
         "approved": True, "compliance_ok": True, "lane_code": "MATNG-UK1"},
    ],
    "production": [
        {"line_code": "F-BD-01-L1", "factory_code": "F-BD-01", "daily_capacity_units": 900,
         "orders": [
             {"code": "MO-1", "po_line_ref": "PO-26-0101#1", "sku": "PUF-W-BLK-M", "qty": 3000,
              "planned_start": "2026-10-06", "planned_end": "2026-10-09", "seq": 1, "movable": False},
             {"code": "MO-2", "po_line_ref": "PO-26-0101#2", "sku": "PUF-W-BLK-M", "qty": 500,
              "planned_start": "2026-10-10", "planned_end": "2026-10-10", "seq": 2},
         ]},
    ],
    "allocations": [
        {"code": "AL-0042", "sku": "PUF-W-BLK-M", "dc_code": "UK1", "cluster_code": "UK1-COLD-A",
         "ship_week": "2026-W44", "qty": 120, "protected": True, "version": 3},
    ],
}

SCORECARD: dict[str, Any] = {
    "fill_rate_bp": 9650,
    "lost_units": 420,
    "lost_margin_cents": 840_000,
    "incremental_cost_cents": 183_000,
    "cost_breakdown": {"transfer": 63_000, "expedite_premium": 120_000, "nearshore_premium": 0},
    "net_benefit_cents": 1_517_000,
    "shortage_days": 12,
    "first_breach_shift_days": 7,
    "tts_gain_days": 5,
    "changes": 6,
}


def v2_problem(**over: Any) -> dict[str, Any]:
    data = problem(**copy.deepcopy(V2))
    data.update(over)
    return data


def transfer_action(seq: int = 1, **over: Any) -> dict[str, Any]:
    action = {"seq": seq, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
              "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-19",
                          "lines": [{"sku": "PUF-W-BLK-M", "qty": 400}]}}
    action.update(over)
    return action


ACTIONS_BY_OP: dict[str, dict[str, Any]] = {
    "transfer_order.create": transfer_action(),
    "po_line.split": {"seq": 1, "op": "po_line.split", "target_ref": "PO-26-0101#1", "expected_version": 4,
                      "payload": {"mode": "sea_air", "qty": 1200, "lane_code": "BDCGP-AEJEA-UK1"}},
    "capacity_booking.create": {"seq": 1, "op": "capacity_booking.create", "target_ref": None,
                                "expected_version": None,
                                "payload": {"lane_code": "BDCGP-AEJEA-UK1", "iso_week": "2026-W42",
                                            "qty_units": 1200}},
    "po.create": {"seq": 1, "op": "po.create", "target_ref": None, "expected_version": None,
                  "payload": {"supplier_code": "SUP-MA-01", "factory_code": "F-MA-01", "dest_dc": "UK1",
                              "eta_dc": "2026-11-02", "lines": [{"sku": "PUF-W-BLK-M", "qty": 600}]}},
    "po_line.change_qty": {"seq": 1, "op": "po_line.change_qty", "target_ref": "PO-26-0101#1",
                           "expected_version": 4, "payload": {"qty": 2400}},
    "po_line.cancel": {"seq": 1, "op": "po_line.cancel", "target_ref": "PO-26-0101#2", "expected_version": 2,
                       "payload": {}},
    "allocation.set_qty": {"seq": 1, "op": "allocation.set_qty", "target_ref": "AL-0042", "expected_version": 3,
                           "payload": {"qty": 80}},
    "production_order.resequence": {"seq": 1, "op": "production_order.resequence", "target_ref": "F-BD-01-L1",
                                    "expected_version": None,
                                    "payload": {"line_code": "F-BD-01-L1", "order_codes": ["MO-2", "MO-1"]}},
    "po_line.reschedule": {"seq": 1, "op": "po_line.reschedule", "target_ref": "PO-26-0101#2",
                           "expected_version": 2, "payload": {"eta_dc": "2026-11-05"}},
}


def response(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "status": "OPTIMAL",
        "plan_spec_id": "opt-D",
        "kpis_before": kpis(),
        "kpis_after": kpis(units_at_risk=0, revenue_at_risk_cents=0, margin_at_risk_cents=0,
                           shortage_cluster_weeks=0, first_breach=[]),
        "kpi_delta": {"units_recovered": 120, "margin_recovered_cents": 192000, "transfer_cost_cents": 14000,
                      "net_benefit_cents": 178000, "transfers": 1},
        "decisions": [],
        "actions": [],
        "explain": {"binding_constraints": []},
        "solver": {"name": "GLOP", "ortools": "9.15.6755", "params": {}, "seed": 7, "inputs_hash": "a" * 64,
                   "solve_ms": 12},
    }
    data.update(over)
    return data


# ───────────────────────────── ProblemData v2 ─────────────────────────────


def test_problem_data_v2_round_trips() -> None:
    pd_ = opt.ProblemData.model_validate(v2_problem())
    assert pd_.locations[0].lat_e6 == 52_257_000
    assert pd_.po_lines[1].lane_code is None
    assert pd_.inbound_lanes[0].via == ["CAPE"] and pd_.inbound_lanes[3].via == []
    assert pd_.alt_sources[0].usable
    assert pd_.production[0].orders[0].movable is False and pd_.production[0].orders[1].movable is True
    assert pd_.allocations[0].version == 3
    again = opt.ProblemData.model_validate_json(pd_.model_dump_json())
    assert again == pd_


def test_m1_problem_dump_and_hash_unchanged() -> None:
    """M3 fields are omitted while empty, so an M1 payload (and its inputs_hash) is identical."""
    m1 = problem()
    dumped = opt.ProblemData.model_validate(m1).model_dump(mode="json")
    assert set(dumped) == set(m1)
    for v2_key in V2:
        assert v2_key not in dumped
    m1_full = copy.deepcopy(m1)
    m1_full["inventory"][0]["reserved"] = 0  # defaulted M1 fields of the example
    for dc in m1_full["dcs"]:
        dc.setdefault("outbound_cap_units_wk", None)
    assert dumped == m1_full
    assert sha256_hex(dumped) == sha256_hex(m1_full)
    req = opt.ImpactRequest.model_validate({"data": m1, "perturbations": CONTRACT_PERTURBATIONS})
    assert set(req.model_dump(mode="json")) == {"data", "perturbations"}  # include_graph omitted while false
    with_graph = opt.ImpactRequest.model_validate({"data": m1, "include_graph": True})
    assert with_graph.model_dump(mode="json")["include_graph"] is True
    oreq = opt.OptimizeRequest.model_validate({"data": m1, "plan_spec": PLAN_SPEC})
    assert set(oreq.model_dump(mode="json")["plan_spec"]["weights"]) == {"lost_margin", "transfer_cost"}


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("locations", 0, "lat_e6"), 52.257),  # coordinates are integers (degrees x 1e6)
        (("locations", 0, "lat_e6"), "52257000"),
        (("locations", 0, "lat_e6"), 90_000_001),
        (("locations", 0, "lon_e6"), -180_000_001),
        (("locations", 0, "kind"), "warehouse"),
        (("inbound_lanes", 0, "cost_per_unit_cents"), 42.0),  # money is integer cents
        (("alt_sources", 0, "unit_cost_cents"), 11.5),
        (("alt_sources", 0, "approved"), 1),  # strict booleans
        (("alt_sources", 0, "style_codes"), []),
        (("po_lines", 0, "qty"), -1),
        (("po_lines", 0, "mode"), "rail"),
        (("po_lines", 0, "ref"), "PO-26-0101"),
        (("po_lines", 0, "eta"), 1793577600),  # timestamps are not dates
        (("allocations", 0, "version"), 0),
        (("allocations", 0, "ship_week"), "2026-W54"),
        (("production", 0, "orders", 1, "planned_end"), "2026-10-01"),
    ],
)
def test_v2_strict_scalars(path: tuple[Any, ...], value: Any) -> None:
    data = v2_problem()
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    with pytest.raises(ValidationError):
        opt.ProblemData.model_validate(data)


def _mutate(path: tuple[Any, ...], value: Any) -> dict[str, Any]:
    data = v2_problem()
    node: Any = data
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    return data


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (_mutate(("po_lines", 0, "dest_dc"), "XX1"), "unknown dest_dc XX1"),
        (_mutate(("po_lines", 0, "sku"), "NOPE"), "unknown sku NOPE"),
        (_mutate(("po_lines", 0, "lane_code"), "NO-LANE"), "unknown inbound lane NO-LANE"),
        (_mutate(("po_lines", 0, "po_code"), "PO-OTHER"), "does not belong to po_code"),
        (_mutate(("inbound_lanes", 0, "dest_dc"), "XX1"), "unknown dest_dc XX1"),
        (_mutate(("inbound_lanes", 1, "alt_lane_code"), "NOPE"), "unknown alt_lane_code NOPE"),
        (_mutate(("alt_sources", 0, "lane_code"), "NOPE"), "unknown inbound lane NOPE"),
        (_mutate(("allocations", 0, "cluster_code"), "NOPE"), "unknown cluster NOPE"),
        (_mutate(("production", 0, "orders", 1, "sku"), "NOPE"), "unknown sku NOPE"),
        (_mutate(("production", 0, "orders", 1, "seq"), 1), "duplicate seq"),
        (_mutate(("po_lines", 1, "ref"), "PO-26-0101#1"), "duplicate po_line codes"),
        (_mutate(("inbound_lanes", 1, "code"), "BDCGP-GBFXT-CAPE"), "duplicate inbound_lane codes"),
        (v2_problem(allocations=[V2["allocations"][0], V2["allocations"][0]]), "duplicate allocation codes"),
        (v2_problem(locations=[V2["locations"][0], V2["locations"][0]]), "duplicate location"),
    ],
)
def test_v2_referential_integrity(data: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        opt.ProblemData.model_validate(data)


def test_same_code_different_location_kinds_allowed() -> None:
    locs = [*V2["locations"], {"code": "UK1", "kind": "cluster", "name": "x", "lat_e6": 0, "lon_e6": 0}]
    assert len(opt.ProblemData.model_validate(v2_problem(locations=locs)).locations) == 5


# ───────────────────────────── perturbation targets ─────────────────────────────


def test_extended_target_kinds() -> None:
    assert {"supplier", "component", "factory", "port", "chokepoint", "lane", "po_line", "receipt", "cluster",
            "dc", "style"} <= opt.TARGET_KINDS
    assert {"supplier", "component", "factory"} <= opt.TARGET_KINDS_BY_OP["lead_time_shift"]
    assert {"port", "chokepoint", "lane"} == opt.TARGET_KINDS_BY_OP["capacity_scale"]
    assert all(kinds <= opt.TARGET_KINDS for kinds in opt.TARGET_KINDS_BY_OP.values())
    req = opt.ImpactRequest.model_validate({
        "data": v2_problem(),
        "perturbations": [
            {"op": "lead_time_shift", "target": {"kinds": ["supplier"], "ids": ["TRIM-BD-01"]}, "days": 7,
             "prov": "decision"},
            {"op": "lead_time_shift", "target": {"kinds": ["component", "factory"], "ids": ["ZIP-5", "F-BD-01"]},
             "days": 3, "prov": "assumption"},
            {"op": "capacity_scale", "target": {"kinds": ["chokepoint"], "ids": ["SUEZ"]}, "factor_bp": 0,
             "prov": "fact"},
            {"op": "capacity_scale", "target": {"kinds": ["port"], "ids": ["VNSGN"]}, "factor_bp": 0,
             "prov": "fact", "window": {"start": "2026-10-10", "end": "2026-10-19"}},
        ],
    })
    assert [p.target.kinds[0] for p in req.perturbations] == ["supplier", "component", "chokepoint", "port"]


# ───────────────────────────── impact graph / TTS ─────────────────────────────


def _node(i: int, **over: Any) -> dict[str, Any]:
    node = {"id": f"n{i}", "kind": "cluster", "label": f"Node {i}", "status": "ok", "units_at_risk": 0}
    node.update(over)
    return node


def impact_response(**over: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "kpis": kpis(),
        "by_dc": [],
        "timeline": [],
        "baseline_kpis": kpis(),
        "provenance": {"engine": "keelson-opt", "version": "0.2.0", "inputs_hash": "0" * 64,
                       "computed_at": "2026-10-05T09:00:00Z"},
    }
    data.update(over)
    return data


def test_impact_response_graph_tts_bottlenecks() -> None:
    graph = {
        "nodes": [_node(1, kind="supplier", status="disrupted"),
                  _node(2, kind="dc", status="at_risk", units_at_risk=3200, first_breach_week="2026-W45")],
        "edges": [{"source": "n1", "target": "n2", "kind": "supplies"}],
    }
    resp = opt.ImpactResponse.model_validate(impact_response(
        graph=graph,
        tts=[{"dc_code": "UK1", "cluster_code": None, "tts_days": 9, "ttr_days": 21, "verdict": "loss"},
             {"dc_code": "DE1", "tts_days": 30, "verdict": "ok"}],
        bottlenecks=[{"name": "lane BDCGP-AEJEA-UK1 cap W43", "shadow_price_cents": 310}],
    ))
    assert resp.graph is not None and resp.graph.nodes[1].first_breach_week == "2026-W45"
    assert resp.graph.nodes[0].first_breach_week is None
    assert resp.tts[1].ttr_days is None and resp.bottlenecks[0].shadow_price_cents == 310
    assert opt.Bottleneck is opt.BindingConstraint


def test_m1_impact_response_dump_unchanged() -> None:
    dumped = opt.ImpactResponse.model_validate(impact_response()).model_dump(mode="json")
    assert set(dumped) == {"kpis", "by_dc", "timeline", "baseline_kpis", "provenance", "warnings"}


def test_graph_node_cap() -> None:
    ok = {"nodes": [_node(i) for i in range(opt.MAX_GRAPH_NODES)], "edges": []}
    assert len(opt.ImpactGraph.model_validate(ok).nodes) == 300
    with pytest.raises(ValidationError, match="300"):
        opt.ImpactGraph.model_validate({"nodes": [_node(i) for i in range(301)], "edges": []})


@pytest.mark.parametrize(
    ("graph", "message"),
    [
        ({"nodes": [_node(1), _node(1)]}, "duplicate graph node ids"),
        ({"nodes": [_node(1)], "edges": [{"source": "n1", "target": "n9", "kind": "ships"}]}, "unknown nodes"),
        ({"nodes": [_node(1)], "edges": [{"source": "n1", "target": "n1", "kind": "ships"}]}, "self-loops"),
        ({"nodes": [_node(1), _node(2)], "edges": [{"source": "n1", "target": "n2", "kind": "ships"}] * 2},
         "duplicate graph edges"),
        ({"nodes": [_node(1, status="fine")]}, "status"),
        ({"nodes": [_node(1, units_at_risk=1.5)]}, "units_at_risk"),
        ({"nodes": [_node(1)], "edges": [{"source": "n1", "target": "n1", "kind": "teleports"}]}, "kind"),
    ],
)
def test_graph_rejects(graph: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        opt.ImpactGraph.model_validate(graph)


def test_tts_verdict_and_ints() -> None:
    with pytest.raises(ValidationError):
        opt.TtsRow.model_validate({"dc_code": "UK1", "tts_days": 9, "verdict": "panic"})
    with pytest.raises(ValidationError):
        opt.TtsRow.model_validate({"dc_code": "UK1", "tts_days": 9.5, "verdict": "ok"})


# ───────────────────────────── levers ─────────────────────────────

ALL_LEVERS = [
    {"code": "reallocate_inventory", "params": {"max_units_total": 20000, "allowed_lanes": ["DE1>NL1"]}},
    {"code": "expedite_mode_shift", "params": {"modes": ["sea_air", "air"], "allowed_lanes": ["BDCGP-AEJEA-UK1"],
                                               "po_line_refs": ["PO-26-0101#1"], "max_premium_cents": 500000}},
    {"code": "resource_alt_supplier", "params": {"allowed_factories": ["F-MA-01"], "max_units_total": 5000}},
    {"code": "prioritize_allocation", "params": {"dc_codes": ["UK1"], "ship_weeks": ["2026-W44"],
                                                 "max_changes": 50}},
    {"code": "resequence_production", "params": {"line_codes": ["F-BD-01-L1"], "max_orders_per_line": 8}},
]


def test_all_levers_compose_in_one_plan() -> None:
    spec = opt.PlanSpec.model_validate({"id": "opt-D", "title": "Hybrid", "levers": ALL_LEVERS})
    assert [type(lv).__name__ for lv in spec.levers] == [
        "ReallocateInventoryLever", "ExpediteModeShiftLever", "ResourceAltSupplierLever",
        "PrioritizeAllocationLever", "ResequenceProductionLever",
    ]
    assert spec.levers[1].params.modes == ["sea_air", "air"]  # type: ignore[union-attr]
    assert opt.PlanSpec.model_validate_json(spec.model_dump_json()) == spec


@pytest.mark.parametrize("code", opt.LEVER_CODES)
def test_levers_default_params(code: str) -> None:
    spec = opt.PlanSpec.model_validate({"id": "x", "title": "x", "levers": [{"code": code}]})
    assert spec.levers[0].code == code
    if code == "expedite_mode_shift":
        assert spec.levers[0].params.modes == ["sea_air"]  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "bad",
    [
        {"code": "expedite_mode_shift", "params": {"modes": ["ship"]}},
        {"code": "expedite_mode_shift", "params": {"modes": []}},
        {"code": "expedite_mode_shift", "params": {"modes": ["air", "air"]}},
        {"code": "expedite_mode_shift", "params": {"po_line_refs": ["PO-1"]}},
        {"code": "expedite_mode_shift", "params": {"max_premium_cents": 10.5}},
        {"code": "resource_alt_supplier", "params": {"max_units_total": -1}},
        {"code": "resource_alt_supplier", "params": {"allowed_factories": ["F", "F"]}},
        {"code": "prioritize_allocation", "params": {"respect_protected": "yes"}},
        {"code": "prioritize_allocation", "params": {"max_changes": 0}},
        {"code": "resequence_production", "params": {"line_codes": ["L1"], "surprise": 1}},
        {"code": "reallocate_inventory", "params": {"modes": ["air"]}},  # params of another lever
    ],
)
def test_bad_lever_params(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        opt.PlanSpec.model_validate({"id": "x", "title": "x", "levers": [bad]})


def test_lever_catalogue_and_params_schema() -> None:
    catalogue = opt.lever_catalogue()
    assert [info.code for info in catalogue] == list(opt.LEVER_CODES) == [
        "reallocate_inventory", "expedite_mode_shift", "resource_alt_supplier", "prioritize_allocation",
        "resequence_production",
    ]
    assert opt.LEVER_OPS == {
        "reallocate_inventory": ("transfer_order.create",),
        "expedite_mode_shift": ("po_line.split", "capacity_booking.create"),
        "resource_alt_supplier": ("po.create", "po_line.change_qty", "po_line.cancel"),
        "prioritize_allocation": ("allocation.set_qty",),
        "resequence_production": ("production_order.resequence", "po_line.reschedule"),
    }
    for info in catalogue:
        assert info.params_schema == opt.lever_params_schema(info.code)
        assert info.params_schema["type"] == "object" and info.params_schema["additionalProperties"] is False
        assert "$ref" not in json.dumps(info.params_schema)  # self-contained (no nested models)
        assert set(info.ops) <= set(opt.ActionOp.__args__) <= set(cs.TYPED_OPS)  # type: ignore[attr-defined]
    # M1 compatibility: the reallocate schema is exactly what the optimizer published in M1.
    assert catalogue[0].params_schema == opt.ReallocateInventoryParams.model_json_schema()


def test_every_lever_param_default_is_valid() -> None:
    for d in opt.LEVERS.values():
        d.params_model.model_validate({})


# ───────────────────────────── actions ─────────────────────────────


@pytest.mark.parametrize("op", sorted(ACTIONS_BY_OP))
def test_action_per_op(op: str) -> None:
    raw = ACTIONS_BY_OP[op]
    action = opt.Action.model_validate(raw)
    assert type(action.payload).__name__ == {
        "transfer_order.create": "TransferOrderCreatePayload",
        "po_line.split": "PoLineSplitPayload",
        "capacity_booking.create": "CapacityBookingCreatePayload",
        "po.create": "PoCreatePayload",
        "po_line.change_qty": "PoLineChangeQtyPayload",
        "po_line.cancel": "PoLineCancelPayload",
        "allocation.set_qty": "AllocationSetQtyPayload",
        "production_order.resequence": "ProductionOrderResequencePayload",
        "po_line.reschedule": "PoLineReschedulePayload",
    }[op]
    item = action.change_set_item()
    assert item == raw  # identical wire dict: the change-set hash is unchanged
    assert cs.change_set_hash([item]) == cs.change_set_hash([raw])
    assert action.model_dump(mode="json") == raw  # group omitted while None
    compiled = cs.CHANGE_SET_ITEM_ADAPTER.validate_python(item)
    assert compiled.op == op and not isinstance(compiled, cs.GenericOpItem)
    assert opt.Action.model_validate_json(action.model_dump_json()) == action


def test_same_shaped_payloads_dispatch_by_op() -> None:
    """{qty} is both allocation.set_qty and po_line.change_qty: the op decides."""
    a = opt.Action.model_validate(ACTIONS_BY_OP["allocation.set_qty"])
    b = opt.Action.model_validate(ACTIONS_BY_OP["po_line.change_qty"])
    assert isinstance(a.payload, cs.AllocationSetQtyPayload)
    assert isinstance(b.payload, cs.PoLineChangeQtyPayload)
    with pytest.raises(ValidationError, match="must be"):
        opt.Action(seq=1, op="allocation.set_qty", target_ref="AL-1", expected_version=1,
                   payload=cs.PoLineChangeQtyPayload(qty=1))


@pytest.mark.parametrize(
    "bad",
    [
        {**ACTIONS_BY_OP["po_line.split"], "target_ref": None},  # needs a PO line
        {**ACTIONS_BY_OP["po_line.split"], "expected_version": None},
        {**ACTIONS_BY_OP["po_line.split"], "payload": {"mode": "sea", "qty": 1, "lane_code": "L"}},
        {**ACTIONS_BY_OP["po_line.split"], "payload": {"mode": "sea_air", "qty": 0, "lane_code": "L"}},
        {**ACTIONS_BY_OP["capacity_booking.create"], "target_ref": "X"},
        {**ACTIONS_BY_OP["capacity_booking.create"],
         "payload": {"lane_code": "L", "iso_week": "2026-W54", "qty_units": 1}},
        {**ACTIONS_BY_OP["po.create"], "expected_version": 1},
        {**ACTIONS_BY_OP["po.create"], "payload": {**ACTIONS_BY_OP["po.create"]["payload"], "lines": []}},
        {**ACTIONS_BY_OP["po.create"], "payload": {**ACTIONS_BY_OP["po.create"]["payload"], "rush": True}},
        {**ACTIONS_BY_OP["po_line.cancel"], "payload": {"reason": "x"}},
        {**ACTIONS_BY_OP["production_order.resequence"], "target_ref": "OTHER-LINE"},
        {**ACTIONS_BY_OP["allocation.set_qty"], "expected_version": 0},
        {**ACTIONS_BY_OP["allocation.set_qty"], "payload": {"qty": 1.0}},
        {**ACTIONS_BY_OP["po_line.reschedule"], "payload": {"eta_dc": "2026-11-05T00:00:00Z"}},
        {**ACTIONS_BY_OP["po_line.reschedule"], "op": "po_line.teleport"},
        {**ACTIONS_BY_OP["po_line.cancel"], "group": ""},
    ],
)
def test_action_rejects(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        opt.Action.model_validate(bad)


def _alt_supplier_group(start: int) -> list[dict[str, Any]]:
    return [
        {**ACTIONS_BY_OP["po.create"], "seq": start, "group": "alt-1"},
        {**ACTIONS_BY_OP["po_line.change_qty"], "seq": start + 1, "group": "alt-1"},
    ]


def test_atomic_groups() -> None:
    resp = opt.OptimizeResponse.model_validate(
        response(actions=[transfer_action(1), *_alt_supplier_group(2)], scorecard=SCORECARD)
    )
    assert [a.group for a in resp.actions] == [None, "alt-1", "alt-1"]
    dumped = resp.model_dump(mode="json")
    assert "group" not in dumped["actions"][0] and dumped["actions"][1]["group"] == "alt-1"
    items = [a.change_set_item() for a in resp.actions]
    assert all("group" not in i for i in items)
    change_set = cs.ChangeSet(id="cs_1", tenant="demo-retail", connector="merxa", revision=1,
                              items=[cs.CHANGE_SET_ITEM_ADAPTER.validate_python(i) for i in items])
    assert change_set.hash == cs.change_set_hash(items)
    assert change_set.ops == {"transfer_order.create", "po.create", "po_line.change_qty"}


def test_groups_must_be_contiguous_and_seq_ordered() -> None:
    group = _alt_supplier_group(1)
    split = [group[0], transfer_action(2), {**group[1], "seq": 3}]
    with pytest.raises(ValidationError, match="contiguous"):
        opt.OptimizeResponse.model_validate(response(actions=split))
    with pytest.raises(ValidationError, match="seq 1..n"):
        opt.OptimizeResponse.model_validate(response(actions=[transfer_action(2)]))
    with pytest.raises(ValidationError, match="seq 1..n"):
        opt.OptimizeResponse.model_validate(response(actions=[transfer_action(2), transfer_action(1)]))


def test_m1_optimize_response_dump_unchanged() -> None:
    resp = opt.OptimizeResponse.model_validate(response(actions=[transfer_action(1)], decisions=[
        {"lever": "reallocate_inventory", "from_dc": "DE1", "to_dc": "NL1", "sku": "PUF-W-BLK-M", "qty": 400,
         "ship_week": "2026-W43", "arrive_week": "2026-W44"}]))
    dumped = resp.model_dump(mode="json")
    assert "scorecard" not in dumped
    assert dumped["actions"][0] == transfer_action(1)
    assert isinstance(resp.decisions[0], opt.Decision) and opt.ReallocateDecision is opt.Decision


# ───────────────────────────── decisions / scorecard ─────────────────────────────

DECISIONS = [
    {"lever": "reallocate_inventory", "from_dc": "DE1", "to_dc": "NL1", "sku": "PUF-W-BLK-M", "qty": 400,
     "ship_week": "2026-W43", "arrive_week": "2026-W44"},
    {"lever": "expedite_mode_shift", "po_line_ref": "PO-26-0101#1", "sku": "PUF-W-BLK-M", "dest_dc": "UK1",
     "lane_code": "BDCGP-AEJEA-UK1", "mode": "sea_air", "qty": 1200, "book_week": "2026-W42",
     "arrive_week": "2026-W44", "premium_cents": 120000},
    {"lever": "resource_alt_supplier", "supplier_code": "SUP-MA-01", "factory_code": "F-MA-01", "dest_dc": "UK1",
     "sku": "PUF-W-BLK-M", "qty": 600, "eta": "2026-11-02", "replaces_po_line_ref": "PO-26-0101#1",
     "premium_cents": 150000},
    {"lever": "prioritize_allocation", "allocation_code": "AL-0042", "sku": "PUF-W-BLK-M", "dc_code": "UK1",
     "cluster_code": "UK1-COLD-A", "ship_week": "2026-W44", "qty_before": 120, "qty_after": 80},
    {"lever": "resequence_production", "line_code": "F-BD-01-L1", "factory_code": "F-BD-01",
     "order_codes_before": ["MO-1", "MO-2"], "order_codes_after": ["MO-2", "MO-1"],
     "eta_changes": [{"po_line_ref": "PO-26-0101#2", "eta_before": "2026-11-09", "eta_after": "2026-11-05"}]},
]


def test_decisions_per_lever() -> None:
    resp = opt.OptimizeResponse.model_validate(response(decisions=DECISIONS, scorecard=SCORECARD))
    assert [type(d).__name__ for d in resp.decisions] == [
        "Decision", "ExpediteDecision", "AltSupplierDecision", "AllocationDecision", "ResequenceDecision",
    ]
    assert resp.model_dump(mode="json")["decisions"] == [
        {**d, "eta_changes": d.get("eta_changes", [])} if d["lever"] == "resequence_production" else d
        for d in DECISIONS
    ]


@pytest.mark.parametrize(
    "bad",
    [
        {**DECISIONS[1], "lever": "teleport"},
        {**DECISIONS[1], "qty": 0},
        {**DECISIONS[1], "mode": "sea"},
        {**DECISIONS[2], "premium_cents": 1.5},
        {**DECISIONS[2], "replaces_po_line_ref": "PO"},
        {**DECISIONS[4], "order_codes_after": ["MO-2", "MO-3"]},
        {**DECISIONS[4], "order_codes_after": ["MO-2", "MO-2"]},
    ],
)
def test_bad_decisions(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        opt.OptimizeResponse.model_validate(response(decisions=[bad]))


def test_scorecard() -> None:
    sc = opt.Scorecard.model_validate(SCORECARD)
    assert sc.cost_breakdown.expedite_premium == 120_000
    assert set(opt.SCORECARD_METRICS) == set(SCORECARD) - {"cost_breakdown"}
    for key, value in [("incremental_cost_cents", 1), ("fill_rate_bp", 10_001), ("lost_units", -1),
                       ("lost_margin_cents", 1.5), ("net_benefit_cents", "1"), ("changes", True)]:
        with pytest.raises(ValidationError):
            opt.Scorecard.model_validate({**SCORECARD, key: value})
    with pytest.raises(ValidationError, match="sums to"):
        opt.Scorecard.model_validate({**SCORECARD, "cost_breakdown": {**SCORECARD["cost_breakdown"], "transfer": 0}})
    negative = {**SCORECARD, "net_benefit_cents": -5, "first_breach_shift_days": -2, "tts_gain_days": -1}
    assert opt.Scorecard.model_validate(negative).first_breach_shift_days == -2
