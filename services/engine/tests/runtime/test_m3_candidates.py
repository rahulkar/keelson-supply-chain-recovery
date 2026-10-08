"""Candidate plans from the AgentSpec and the connector capability check (``80-m3-deterministic-core.md`` §4).

Includes the CI check that every default lever's change-set ops are supported by Merxa — both by
its op handlers (``services/merxa-api/.../services/ops*.py``) and by what its MCP
``add_change_set_items`` tool publishes (the ``op`` description the runtime parses).
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

import pytest
from fakes.core import FakeCore
from keelson_contracts.changeset import M1_OPS
from keelson_contracts.optimizer import LEVER_OPS, LEVERS, ExpediteModeShiftLever, PlanSpec
from pydantic import ValidationError

from keelson_engine.runtime.candidates import (
    CHANGE_SET_TOOLS,
    ConnectorCapabilities,
    build_candidates,
    capability_check,
    connector_capabilities,
    lever_specs,
    ops_from_schema,
)
from keelson_engine.runtime.deps import ToolInfo
from keelson_engine.runtime.spec import ALL_LEVERS, AgentSpec, CandidateSpec, PlanningDefaults

REPO = Path(__file__).resolve().parents[4]
MERXA_SRC = REPO / "services" / "merxa-api" / "src" / "merxa_api"
ALL_OPS = frozenset(op for ops in LEVER_OPS.values() for op in ops)


def caps(ops: frozenset[str] = ALL_OPS, missing: tuple[str, ...] = ()) -> ConnectorCapabilities:
    return ConnectorCapabilities("merxa", "Merxa", missing, ops, "config")


# ─────────────────────────────── candidates ───────────────────────────────


def test_default_candidates_a_to_d_with_all_levers() -> None:
    out = build_candidates(PlanningDefaults(), caps())
    assert [(c.id, c.title, c.levers) for c in out] == [
        ("opt-A", "Rebalance between DCs", ("reallocate_inventory",)),
        ("opt-B", "Rebalance + targeted sea-air", ("reallocate_inventory", "expedite_mode_shift")),
        ("opt-C", "Near-shore top-up + resequence", ("resource_alt_supplier", "resequence_production")),
        ("opt-D", "Hybrid", ALL_LEVERS),
    ]
    assert not any(c.guided_only for c in out)
    d = out[-1]
    assert [lv.code for lv in d.plan_spec.levers] == list(ALL_LEVERS)
    assert set(d.ops) == ALL_OPS
    assert isinstance(out[1].plan_spec.levers[1], ExpediteModeShiftLever)
    # the plan specs are valid optimizer requests (one model, each lever once)
    for c in out:
        PlanSpec.model_validate(c.plan_spec.model_dump(mode="json"))


def test_candidates_follow_enabled_levers_and_drop_duplicates() -> None:
    only_rebalance = build_candidates(PlanningDefaults(enabled_levers=("reallocate_inventory",)), caps())
    assert [c.id for c in only_rebalance] == ["opt-A"]  # B = A, C empty, D = A
    two = build_candidates(PlanningDefaults(enabled_levers=("expedite_mode_shift", "reallocate_inventory")), caps())
    assert [(c.id, c.levers) for c in two] == [
        ("opt-A", ("reallocate_inventory",)),
        ("opt-B", ("reallocate_inventory", "expedite_mode_shift")),
    ]
    assert build_candidates(PlanningDefaults(enabled_levers=()), caps()) == []
    custom = PlanningDefaults(candidates=(
        CandidateSpec(id="alloc", title="Re-split allocations", levers=("prioritize_allocation",)),
        CandidateSpec(id="all", title="Everything", all_enabled=True),
    ))
    assert [(c.id, len(c.levers)) for c in build_candidates(custom, caps())] == [("alloc", 1), ("all", 5)]


def test_candidate_spec_validation() -> None:
    with pytest.raises(ValidationError):
        CandidateSpec(id="do-nothing", title="x")
    with pytest.raises(ValidationError):
        PlanningDefaults(candidates=(CandidateSpec(id="a", title="x"), CandidateSpec(id="a", title="y")))
    with pytest.raises(ValidationError):
        PlanningDefaults(enabled_levers=("reallocate_inventory", "reallocate_inventory"))
    with pytest.raises(ValidationError):
        PlanningDefaults(lever_params={"teleport": {}})
    with pytest.raises(ValidationError):
        PlanningDefaults(lever_params={"prioritize_allocation": {"max_changes": 0}})
    spec = AgentSpec.from_row("t", "v2", {"planning": {"enabled_levers": ["reallocate_inventory"],
                                                      "lever_params": {"expedite_mode_shift": {"max_units_total": 5}}}})
    assert spec.planning.enabled_levers == ("reallocate_inventory",)


def test_lever_params_from_spec() -> None:
    planning = PlanningDefaults(rebalance_max_units_total=900, rebalance_allowed_lanes=("DE1>NL1",),
                                lever_params={"expedite_mode_shift": {"max_premium_cents": 5000},
                                              "reallocate_inventory": {"max_units_total": 700}})
    specs = {s["code"]: s["params"] for s in lever_specs(ALL_LEVERS, planning)}
    assert specs["reallocate_inventory"] == {"max_units_total": 700, "allowed_lanes": ["DE1>NL1"]}
    assert specs["expedite_mode_shift"] == {"max_premium_cents": 5000}
    assert specs["prioritize_allocation"] == {}  # explicit lever_params replace the default
    assert {s["code"]: s["params"] for s in lever_specs(ALL_LEVERS, PlanningDefaults())}["prioritize_allocation"] == {
        "max_changes": 12}


# ─────────────────────────────── capability check ───────────────────────────────


def test_capability_check_marks_guided_only_with_reason() -> None:
    m1 = caps(frozenset(M1_OPS))
    assert capability_check(["reallocate_inventory"], m1) == (False, None)
    # M5 (95 §2): partly supported levers stay applicable; their other ops become guide steps
    assert capability_check(["reallocate_inventory", "expedite_mode_shift"], m1) == (False, None)
    to_only = caps(frozenset({"transfer_order.create"}))
    guided, reason = capability_check(["expedite_mode_shift"], to_only)
    assert guided and "capacity_booking.create" in reason and "po_line.split" in reason and "Merxa" in reason
    guided, reason = capability_check(["reallocate_inventory"], caps(missing=("apply_change_set",)))
    assert guided and "apply_change_set" in reason
    assert capability_check([], caps(missing=CHANGE_SET_TOOLS)) == (False, None)
    out = build_candidates(PlanningDefaults(), to_only)
    assert [(c.id, c.guided_only) for c in out] == [("opt-A", False), ("opt-B", False), ("opt-C", True), ("opt-D", False)]
    assert out[1].guide_ops == ("capacity_booking.create", "po_line.split")
    assert all(c.guided_reason for c in out if c.guided_only)


def _tool(name: str, schema: dict[str, Any] | None = None) -> ToolInfo:
    return ToolInfo(name=name, description="", input_schema=schema or {"type": "object"}, output_schema=None,
                    access="write", status="approved")


ITEM_SCHEMA = {
    "type": "object",
    "properties": {"items": {"type": "array", "items": {"$ref": "#/$defs/ChangeSetItemIn"}}},
    "$defs": {"ChangeSetItemIn": {"type": "object", "properties": {"op": {
        "type": "string",
        "description": "transfer_order.create | po_line.split | capacity_booking.create | revert.po_create"}}}},
}


async def test_connector_capabilities_sources() -> None:
    core = FakeCore()
    deps = core.deps()
    core.connectors.tools = [_tool(n) for n in CHANGE_SET_TOOLS]
    plain = await connector_capabilities(deps, tenant="t", connector="merxa", vendor="Merxa")
    assert plain.ops_source == "default" and plain.supported_ops == frozenset(M1_OPS) and plain.can_apply
    core.connectors.tools = [_tool(n) for n in CHANGE_SET_TOOLS if n != "add_change_set_items"] + [
        _tool("add_change_set_items", ITEM_SCHEMA)]
    from_schema = await connector_capabilities(deps, tenant="t", connector="merxa", vendor="Merxa")
    assert from_schema.ops_source == "tool_schema"
    assert from_schema.supported_ops == {"transfer_order.create", "po_line.split", "capacity_booking.create"}
    core.connectors.merxa_config = {"supported_ops": ["transfer_order.create"]}
    configured = await connector_capabilities(deps, tenant="t", connector="merxa", vendor="Merxa")
    assert configured.ops_source == "config" and configured.supported_ops == {"transfer_order.create"}
    core.connectors.tools = [_tool("create_change_set")]
    missing = await connector_capabilities(deps, tenant="t", connector="merxa", vendor="Merxa")
    assert missing.missing_tools == ("add_change_set_items", "validate_change_set", "apply_change_set")


def test_ops_from_schema_shapes() -> None:
    assert ops_from_schema({"properties": {"op": {"enum": ["a.b", "revert.x", "c.d_e"]}}}) == {"a.b", "c.d_e"}
    assert ops_from_schema({"anyOf": [{"properties": {"op": {"const": "x.y"}}}]}) == {"x.y"}
    assert ops_from_schema({"properties": {"op": {"type": "string"}}}) == set()
    assert ops_from_schema({"properties": {"other": {"description": "a.b"}}}) == set()


# ─────────────────────────────── CI: levers ⊆ Merxa ops ───────────────────────────────


def _merxa_handler_ops() -> set[str]:
    ops: set[str] = set()
    for name in ("ops.py", "ops_m2.py"):
        tree = ast.parse((MERXA_SRC / "services" / name).read_text(encoding="utf-8"))
        for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)):
            for stmt in cls.body:
                if (isinstance(stmt, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "op" for t in stmt.targets)
                        and isinstance(stmt.value, ast.Constant) and isinstance(stmt.value.value, str)):
                    ops.add(stmt.value.value)
    return {op for op in ops if op and not op.startswith("revert.")}


def _merxa_tool_op_description() -> str:
    tree = ast.parse((MERXA_SRC / "mcp" / "models.py").read_text(encoding="utf-8"))
    cls = next(n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ChangeSetItemIn")
    field = next(s for s in cls.body if isinstance(s, ast.AnnAssign) and getattr(s.target, "id", "") == "op")
    assert isinstance(field.value, ast.Call)
    kw = next(k for k in field.value.keywords if k.arg == "description")
    return str(ast.literal_eval(kw.value))


@pytest.mark.skipif(not MERXA_SRC.is_dir(), reason="services/merxa-api source not available")
def test_ci_every_default_lever_op_is_supported_by_merxa() -> None:
    handler_ops = _merxa_handler_ops()
    published = ops_from_schema({"properties": {"op": {"description": _merxa_tool_op_description()}}})
    assert set(LEVERS) == set(ALL_LEVERS) == set(PlanningDefaults().enabled_levers)
    for code in PlanningDefaults().enabled_levers:
        ops = set(LEVER_OPS[code])
        assert ops <= handler_ops, f"{code}: Merxa has no handler for {sorted(ops - handler_ops)}"
        assert ops <= published, f"{code}: Merxa's add_change_set_items does not publish {sorted(ops - published)}"
    # what the runtime check concludes from Merxa's tool: nothing guided
    merxa = ConnectorCapabilities("merxa", "Merxa", (), frozenset(published), "tool_schema")
    assert not any(c.guided_only for c in build_candidates(PlanningDefaults(), merxa))


# ───────── strategist brief: levers actually used (M3 integration regression) ─────────


def _opt(oid: str, levers: list[str], actions: list[dict[str, Any]], net: int) -> dict[str, Any]:
    return {"id": oid, "title": oid, "levers": levers, "actions": actions, "fallback": False,
            "kpi_delta": {"net_benefit_cents": net, "units_recovered": 10}, "kpis_after": {}, "kpis_before": {}}


def test_recommender_brief_describes_options_by_the_levers_they_use() -> None:
    """On the full profile "Hybrid" (all levers) ended up with exactly the sea-air plan of B, but
    the brief listed all five configured levers and the narrative credited it with near-shoring.
    The brief now lists the levers whose ops the actions contain, keeps the configured ones as
    ``levers_considered`` and flags an option that makes the same changes as an earlier one."""
    import json

    from keelson_engine.runtime.nodes.planning import _recommender_brief, levers_used

    split = {"op": "po_line.split", "target_ref": "PO-1#1", "payload": {"qty": 5}}
    move = {"op": "transfer_order.create", "payload": {"lines": [{"sku": "S", "qty": 3}]}}
    options = [
        _opt("opt-A", ["reallocate_inventory"], [move], 100),
        _opt("opt-B", ["reallocate_inventory", "expedite_mode_shift"], [move, split], 200),
        _opt("opt-D", list(ALL_LEVERS), [move, split], 200),
        _opt("do-nothing", [], [], 0),
    ]
    assert levers_used(options[2]) == ["reallocate_inventory", "expedite_mode_shift"]
    assert levers_used(options[3]) == []

    class _Scn:
        event_type, description, mode = "supply_delay", "trim delay", "actual"

    brief = {o["id"]: o for o in json.loads(_recommender_brief(_Scn(), options, []))["options"]}
    # plain lever names, never codes (the Strategist quoted "reallocate_inventory" in a narrative)
    assert brief["opt-D"]["levers"] == ["Rebalance inventory between DCs", "Expedite by sea-air / air"]
    assert len(brief["opt-D"]["levers_considered"]) == len(ALL_LEVERS)
    assert not any("_" in t for o in brief.values() for t in [*o["levers"], *o.get("levers_considered", [])])
    # token diet: levers_considered only when it differs from the levers actually used
    assert "levers_considered" not in brief["opt-A"] and "levers_considered" not in brief["do-nothing"]
    assert "exactly the same changes as opt-B" in brief["opt-D"]["assessment"]
    assert "same changes" not in brief["opt-B"]["assessment"]
    assert "same changes" not in brief["do-nothing"]["assessment"]
