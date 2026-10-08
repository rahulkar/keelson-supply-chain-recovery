"""Compile every v1 op into a change set, atomic groups, allocation-group expansion, size
partitioning, the M3 approval rule and plan-diff rows (``80-m3-deterministic-core.md`` §3–§4)."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import pytest
from keelson_contracts.changeset import change_set_hash
from keelson_contracts.common import Provenance
from keelson_contracts.optimizer import Action, OptimizeResponse
from keelson_contracts.runs import Actor
from keelson_contracts.scenario import Assumption, DisruptionScenario
from pydantic import ValidationError

from keelson_engine.runtime.commit import (
    GroupTooLargeError,
    compile_changeset,
    expand_actions,
    partition_groups,
    split_allocation,
    stored_items,
)
from keelson_engine.runtime.nodes.planning import plan_diff_props
from keelson_engine.runtime.policy import HUMAN_ONLY_OPS, decide_approval
from keelson_engine.runtime.problem_scn import GROUP_ALLOC_PREFIX
from keelson_engine.runtime.spec import PolicyConfig

from test_m3_visuals import problem

GROUP = f"{GROUP_ALLOC_PREFIX}UK1|UK1-COLD-A|PUF-01-S|2026-W42|planned"


def m3_actions() -> list[dict[str, Any]]:
    """One action per v1 op, shaped like the optimizer emits them (groups contiguous)."""
    raw = [
        {"op": "transfer_order.create", "payload": {"from_dc": "DE1", "to_dc": "UK1", "ship_date": "2026-10-12",
                                                    "lines": [{"sku": "PUF-01-S", "qty": 120}]}},
        {"op": "po_line.split", "target_ref": "PO-1#1", "expected_version": 3, "group": "exp-1",
         "payload": {"mode": "sea_air", "qty": 200, "lane_code": "CGP-UK1-SA"}},
        {"op": "capacity_booking.create", "group": "exp-1",
         "payload": {"lane_code": "CGP-UK1-SA", "iso_week": "2026-W42", "qty_units": 200}},
        {"op": "po.create", "group": "alt-1",
         "payload": {"supplier_code": "CMT-TR-01", "factory_code": "F-TR-01", "dest_dc": "DE1", "eta_dc": "2026-10-31",
                     "lines": [{"sku": "PUF-01-S", "qty": 300}]}},
        {"op": "po_line.change_qty", "target_ref": "PO-1#1", "expected_version": 3, "group": "alt-1",
         "payload": {"qty": 300}},
        {"op": "po.create", "group": "alt-2",
         "payload": {"supplier_code": "CMT-TR-01", "factory_code": "F-TR-01", "dest_dc": "DE1", "eta_dc": "2026-10-31",
                     "lines": [{"sku": "KNT-01-M", "qty": 400}]}},
        {"op": "po_line.cancel", "target_ref": "PO-2#1", "expected_version": 1, "group": "alt-2", "payload": {}},
        {"op": "allocation.set_qty", "target_ref": "AL-0000007", "expected_version": 2, "payload": {"qty": 4}},
        {"op": "allocation.set_qty", "target_ref": GROUP, "expected_version": 2, "payload": {"qty": 10}},
        {"op": "production_order.resequence", "target_ref": "FL-BD01-A", "group": "rsq-1",
         "payload": {"line_code": "FL-BD01-A", "order_codes": ["MO-3", "MO-2"]}},
        {"op": "po_line.reschedule", "target_ref": "PO-1#1", "expected_version": 3, "group": "rsq-1",
         "payload": {"eta_dc": "2026-11-06"}},
    ]
    out = []
    for seq, a in enumerate(raw, start=1):
        out.append({"seq": seq, "target_ref": None, "expected_version": None, **a})
    return out


@dataclass(frozen=True)
class Member:
    code: str
    store_code: str
    qty: int
    version: int


MEMBERS = [Member("AL-1", "S1", 6, 1), Member("AL-2", "S2", 3, 4), Member("AL-3", "S3", 3, 2)]


async def expander(code: str) -> Sequence[Member]:
    assert code == GROUP
    return MEMBERS


def is_group(code: str) -> bool:
    return code.startswith(GROUP_ALLOC_PREFIX)


def test_optimizer_actions_are_contract_valid() -> None:
    for a in m3_actions():
        if a["target_ref"] != GROUP:
            Action.model_validate(a)
    resp = {"status": "OPTIMAL", "plan_spec_id": "opt-D", "kpis_before": KPIS, "kpis_after": KPIS,
            "kpi_delta": {"units_recovered": 0, "margin_recovered_cents": 0, "transfer_cost_cents": 0,
                          "net_benefit_cents": 0, "transfers": 1},
            "decisions": [], "explain": {"binding_constraints": []},
            "solver": {"name": "GLOP", "ortools": "9.15.6755", "params": {}, "seed": 7, "inputs_hash": "a" * 64,
                       "solve_ms": 1},
            "actions": [a for a in m3_actions() if a["target_ref"] != GROUP]}
    for i, a in enumerate(resp["actions"], start=1):
        a["seq"] = i
    OptimizeResponse.model_validate(resp)
    split = [dict(a) for a in resp["actions"]]
    split[1], split[3] = split[3], split[1]
    for i, a in enumerate(split, start=1):
        a["seq"] = i
    with pytest.raises(ValidationError, match="contiguous"):
        OptimizeResponse.model_validate({**resp, "actions": split})


KPIS = {"units_at_risk": 0, "revenue_at_risk_cents": 0, "margin_at_risk_cents": 0, "shortage_cluster_weeks": 0,
        "first_breach": []}


async def test_compile_every_v1_op_with_groups_and_expansion() -> None:
    actions = await expand_actions(m3_actions(), expander=expander, is_group=is_group)
    ops = [a["op"] for a in actions]
    # the group allocation became per-store items whose quantity changes (AL-1 6→5, AL-2 3→3 unchanged, AL-3 3→2)
    alloc = [a for a in actions if a["op"] == "allocation.set_qty"]
    assert [(a["target_ref"], a["expected_version"], a["payload"]["qty"], a.get("group")) for a in alloc] == [
        ("AL-0000007", 2, 4, None), ("AL-1", 1, 5, f"alloc:{GROUP}"), ("AL-3", 2, 2, f"alloc:{GROUP}")]
    assert [a["seq"] for a in actions] == list(range(1, len(actions) + 1))
    cs = compile_changeset(tenant="demo-retail", change_set_id="cs-1", connector="merxa", actions=actions)
    assert [i.op for i in cs.items] == ops
    assert set(ops) == {"transfer_order.create", "po_line.split", "capacity_booking.create", "po.create",
                        "po_line.change_qty", "po_line.cancel", "allocation.set_qty", "production_order.resequence",
                        "po_line.reschedule"}
    typed = {i.op: type(i).__name__ for i in cs.items}
    assert typed["po_line.split"] == "PoLineSplitItem"
    assert typed["production_order.resequence"] == "ProductionOrderResequenceItem"
    assert "GenericOpItem" not in typed.values()
    # the hash covers only the five canonical keys (groups never enter it) and equals Merxa's
    wire = [{k: a[k] for k in ("seq", "op", "target_ref", "expected_version", "payload")} for a in actions]
    assert cs.hash == change_set_hash(wire)
    stored = stored_items(cs, actions)
    assert [s.get("group") for s in stored] == [a.get("group") for a in actions]
    again = compile_changeset(tenant="demo-retail", change_set_id="cs-1", connector="merxa", actions=stored)
    assert again.hash == cs.hash  # reloaded rows (with groups) hash identically
    # atomic groups stay contiguous and in one change set
    chunks = partition_groups(stored, 500)
    assert len(chunks) == 1 and chunks[0] == stored


async def test_expansion_errors() -> None:
    group_only = [a for a in m3_actions() if a["target_ref"] == GROUP]
    with pytest.raises(LookupError):
        await expand_actions(group_only, expander=None, is_group=is_group)

    async def none(code: str) -> list[Member]:
        return []

    with pytest.raises(LookupError):
        await expand_actions(group_only, expander=none, is_group=is_group)

    async def overlapping(code: str) -> list[Member]:
        return [Member("AL-0000007", "S9", 9, 1)]

    with pytest.raises(ValueError, match="more than one item"):
        await expand_actions(m3_actions(), expander=overlapping, is_group=is_group)

    async def unchanged(code: str) -> list[Member]:
        return [Member("AL-9", "S9", 10, 1)]

    assert await expand_actions(group_only, expander=unchanged, is_group=is_group) == []


def test_split_allocation_is_proportional_and_exact() -> None:
    assert [(m.code, q) for m, q in split_allocation(9, MEMBERS)] == [("AL-1", 5), ("AL-2", 2), ("AL-3", 2)]
    assert sum(q for _, q in split_allocation(1001, MEMBERS)) == 1001
    zeros = [Member("B", "S", 0, 1), Member("A", "S", 0, 1)]
    assert [(m.code, q) for m, q in split_allocation(3, zeros)] == [("A", 2), ("B", 1)]
    assert split_allocation(5, []) == []


def test_partition_never_splits_a_group() -> None:
    items = [{"seq": i, "group": g} for i, g in enumerate([None, "a", "a", "a", None, "b", "b", None, None], start=1)]
    chunks = partition_groups(items, 4)
    assert [[i["seq"] for i in c] for c in chunks] == [[1, 2, 3, 4], [5, 6, 7, 8], [9]]
    assert all(len(c) <= 4 for c in chunks)
    chunks = partition_groups(items, 3)
    assert [[i["seq"] for i in c] for c in chunks] == [[1], [2, 3, 4], [5, 6, 7], [8, 9]]
    with pytest.raises(GroupTooLargeError):
        partition_groups(items, 2)
    assert partition_groups([], 5) == []


def _scenario() -> DisruptionScenario:
    return DisruptionScenario(
        id="s", tenant="demo-retail", mode="actual", event_family="SUP", event_type="t", description="d",
        perturbations=[], matched=[],
        assumptions=[Assumption(key="delay_days", label="Delay", value_text="7 days", prov=Provenance.DECISION)],
        config_version="v",
    )


APPROVER = Actor(sub="s", username="marco", name="Marco", roles=["planner", "approver"])


@pytest.mark.parametrize("op", sorted(HUMAN_ONLY_OPS))
async def test_expedite_nearshore_resequence_always_need_a_human(op: str) -> None:
    action = next(a for a in m3_actions() if a["op"] == op)
    cs = compile_changeset(tenant="demo-retail", change_set_id="cs", connector="merxa",
                           actions=[{**action, "seq": 1}])
    decision = decide_approval(change_set=cs, scenario=_scenario(), case_mode="actual", initiator=APPROVER,
                               config=PolicyConfig(auto_max_lines=1000, auto_max_units=1_000_000))
    assert decision.outcome == "human" and decision.approved_by is None
    assert HUMAN_ONLY_OPS[op] in decision.reason and "approver" in decision.reason


async def test_small_transfer_and_allocation_still_auto() -> None:
    actions = [a for a in m3_actions() if a["op"] == "transfer_order.create" or a["target_ref"] == "AL-0000007"]
    cs = compile_changeset(tenant="demo-retail", change_set_id="cs", connector="merxa",
                           actions=[{**a, "seq": i} for i, a in enumerate(actions, start=1)])
    decision = decide_approval(change_set=cs, scenario=_scenario(), case_mode="actual", initiator=APPROVER,
                               config=PolicyConfig())
    assert decision.outcome == "auto"


async def test_plan_diff_rows_for_every_op() -> None:
    actions = await expand_actions(m3_actions(), expander=expander, is_group=is_group)
    props = plan_diff_props(plan_version=3, actions=actions, vendor="Merxa", problem=problem())
    rows = {r.op: r for r in reversed(props.rows)}
    assert len(props.rows) == len(actions) and [r.seq for r in props.rows] == list(range(1, len(actions) + 1))
    assert rows["po_line.split"].before_text == "600 units by sea, ETA 2026-11-09"
    assert rows["po_line.split"].after_text == "200 units on lane CGP-UK1-SA" and rows["po_line.split"].qty == 200
    assert "applied together (exp-1)" in rows["po_line.split"].target_label
    assert rows["capacity_booking.create"].qty == 0 and "2026-W42" in rows["capacity_booking.create"].target_label
    assert rows["po.create"].before_text == "No purchase order" and "F-TR-01" in rows["po.create"].target_label
    assert (rows["po_line.change_qty"].before_text, rows["po_line.change_qty"].after_text) == ("600 units open", "300 units")
    assert rows["po_line.change_qty"].qty == 300
    assert rows["po_line.cancel"].after_text == "Cancelled" and rows["po_line.cancel"].qty == 400
    assert rows["po_line.reschedule"].before_text == "ETA 2026-11-09" and rows["po_line.reschedule"].after_text == "ETA 2026-11-06"
    assert rows["production_order.resequence"].after_text == "MO-3 → MO-2"
    assert rows["allocation.set_qty"].after_text == "4 units"
    # value: transfer 120 + new POs (300 + 400) + reduced/cancelled PO lines (300 + 400) at unit cost
    assert props.totals.value_cents == 120 * 1300 + 300 * 1300 + 400 * 600 + 300 * 1300 + 400 * 600
