"""M5 polish regressions (runtime side): a lane event that reaches a plan through its transfer
pseudo-lane (``FROM>TO``) re-plans that lane; every workflow node has a user-facing stage (or is
an explicit stage-less control node)."""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fakes.harness import Harness
from fakes.m5 import ALL_M5_OPS, SIGNALS, option, seed_plan
from fakes.merxa_stub import MerxaStub
from keelson_contracts.agui import NODE_STAGES, STAGELESS_NODES, Stage, stage_for_node
from keelson_contracts.blocks import parse_block

from keelson_engine.runtime.llm import FakeModelFactory, FakeScript
from keelson_engine.runtime.nodes.replan import parse_lane_signal
from keelson_engine.runtime.spec import AgentSpec
from keelson_engine.runtime.workflow import WorkflowBindings, build_workflow

TO_STUB = {"op": "transfer_order.create", "target_ref": None, "expected_version": None,
           "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-12",
                       "lines": [{"sku": "PUF-W-BLK-M", "qty": 120}, {"sku": "PUF-W-BLK-S", "qty": 180}]}}


@pytest.fixture
async def m5(tmp_path: Path) -> AsyncIterator[Harness]:
    h = await Harness(tmp=tmp_path, merxa=MerxaStub(supported_ops=ALL_M5_OPS)).setup()
    try:
        yield h
    finally:
        await h.close()


def _blocks(h: Harness, run_id: str, kind: str) -> list[Any]:
    return [parse_block(e["content"]) for e in h.events(run_id)
            if e["type"] == "ACTIVITY_SNAPSHOT" and e["content"]["kind"] == kind]


# ───────────────────────────── lane aliases ─────────────────────────────


def _sig(data: dict[str, Any]) -> Any:
    return parse_lane_signal({"id": "s", "type": "lane.capacity_changed", "data": data}, today=dt.date(2026, 10, 7))


def test_lane_signal_aliases_and_resolution() -> None:
    sig = _sig({"lane_code": "DE1-NL1-RD", "factor_bp": 3000, "origin": "DE1", "dest": "NL1",
                "plan_lane_code": "DE1>NL1"})
    assert sig.lane_code == sig.event_lane_code == "DE1-NL1-RD" and sig.aliases == ("DE1>NL1",)
    assert sig.label == "DE1-NL1-RD"
    assert sig.resolve(lambda lane: lane == "DE1>NL1") and sig.lane_code == "DE1>NL1"
    assert sig.label == "DE1-NL1-RD (DE1 → NL1)"
    # the exact code wins when the plan uses it too
    sig = _sig({"lane_code": "DE1-NL1-RD", "factor_bp": 3000, "origin": "DE1", "dest": "NL1"})
    assert sig.resolve(lambda lane: True) and sig.lane_code == "DE1-NL1-RD"
    # no dependency: unchanged
    assert not sig.resolve(lambda lane: False) and sig.lane_code == "DE1-NL1-RD"
    # only well-formed typed endpoints become an alias
    for data in ({"origin": "DE1"}, {"origin": "DE1", "dest": "DE1"}, {"origin": "D E", "dest": "NL1"},
                 {"origin": "A>B", "dest": "NL1"}, {"plan_lane_code": "x" * 200}, {"plan_lane_code": 5}):
        assert _sig({"lane_code": "L-1", "factor_bp": 1, **data}).aliases == ()


async def test_signal_on_the_road_lane_replans_the_transfer_pseudo_lane(m5: Harness) -> None:
    """The router matched the plan by (origin, dest): the run re-plans ``DE1>NL1`` (the lane as the
    plan and the optimizer know it) and shows the connector's lane code to people."""
    h = m5
    await seed_plan(h, [option("opt-A", [TO_STUB])])
    signal_id = h.core.signals.add("lane.capacity_changed", {
        "lane_code": "DE1-NL1-RD", "factor_bp": 3000, "origin": "DE1", "dest": "NL1",
        "plan_lane_code": "DE1>NL1", "matched_by": ["lane_pair"]})
    row = h.row("signal", {"signal_id": signal_id}, SIGNALS)
    assert await h.run(row) == "succeeded"
    _, sc2 = h.core.repo.scenarios[h.case_id][-1]
    cut = [p for p in sc2.perturbations if p.op == "capacity_scale"]
    assert len(cut) == 1 and cut[0].target.ids == ["DE1>NL1"] and cut[0].factor_bp == 3000
    v2 = h.core.repo.plans[h.case_id][-1]
    assert v2["version"] == 2
    assert v2["kpis"]["replan"]["lane_code"] == "DE1>NL1"
    assert v2["kpis"]["replan"]["event_lane_code"] == "DE1-NL1-RD"
    [narr] = _blocks(h, row.id, "narrative")
    assert narr.props.banner.text.startswith("New information: road capacity on DE1-NL1-RD (DE1 → NL1) cut to 30%")


async def test_signal_with_only_the_pair_resolves_without_the_router_hint(m5: Harness) -> None:
    h = m5
    await seed_plan(h, [option("opt-A", [TO_STUB])])
    signal_id = h.core.signals.add("lane.capacity_changed", {
        "lane_code": "DE1-NL1-RD", "factor_bp": 0, "origin": "DE1", "dest": "NL1"})
    assert await h.run(h.row("signal", {"signal_id": signal_id}, SIGNALS)) == "succeeded"
    assert h.core.repo.plans[h.case_id][-1]["version"] == 2
    # the opposite direction is another lane: nothing changes
    other = h.core.signals.add("lane.capacity_changed", {
        "lane_code": "NL1-DE1-RD", "factor_bp": 0, "origin": "NL1", "dest": "DE1"})
    assert await h.run(h.row("signal", {"signal_id": other}, SIGNALS)) == "succeeded"
    assert h.core.repo.plans[h.case_id][-1]["version"] == 2


# ───────────────────────────── stages ─────────────────────────────


def test_every_workflow_node_maps_to_a_stage() -> None:
    wf = build_workflow(AgentSpec(tenant="demo-retail"), WorkflowBindings(models=FakeModelFactory(FakeScript())))
    names = {n.name for n in wf.graph.nodes if not n.name.startswith("__")}
    missing = sorted(n for n in names if stage_for_node(n) is None and n not in STAGELESS_NODES)
    assert not missing, f"workflow nodes without a stage: {missing}"
    assert not (set(NODE_STAGES) & STAGELESS_NODES)
    # the entries are real nodes (no stale names)
    assert set(NODE_STAGES) <= names and STAGELESS_NODES <= names


def test_m5_node_stages() -> None:
    assert stage_for_node("prepare_rollback") is Stage.PREPARING_CHANGES
    assert stage_for_node("prepare_replan") is Stage.BUILDING_OPTIONS
    assert stage_for_node("checklist_step") is Stage.APPLYING
