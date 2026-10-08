"""Independent ER review of the M5 runtime: regression tests for the defects fixed in review.

* the policy origin is never relaxed by a node input (``chat`` — the only auto-approvable origin —
  comes from the run envelope alone);
* ``approval_check`` auto-approves only on the gate's own ``auto`` decision (never a rollback) and
  a human decision must answer the approval request this commit waits for;
* rollback: each request gets its own attempt (interrupt id / approval ref / idempotency key), an
  in-doubt rollback that ends in a conflict restores the pre-rollback status (it stays undoable);
* bundles: a later member's conflict still reconciles the earlier members that were applied;
* re-plan: the same signal is never re-planned twice; a delta never cancels a booking an earlier
  delta already cancelled, nor withdraws a split an applied later version already withdrew;
* guide: an operator guide classified ``untrusted_text`` is never shown verbatim.
"""

from __future__ import annotations

import dataclasses
import types as pytypes
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fakes.harness import Harness
from fakes.m5 import (
    ADA,
    ALL_M5_OPS,
    BOOKING,
    MARCO,
    PRIYA,
    SIGNALS,
    TO,
    action_row,
    approve_row,
    decider,
    last_interrupt,
    option,
    seed_plan,
)
from fakes.merxa_stub import MerxaStub
from google.adk.events.event import Event
from keelson_contracts.runs import Actor, ResumePayload

from keelson_engine.runtime.context import RunContext, bind_run
from keelson_engine.runtime.nodes import commit_nodes
from keelson_engine.runtime.remediation import BOOKING_CANCEL_OP, delta_actions
from keelson_engine.runtime.spec import AgentSpec

from test_m5_flows import TO_STUB, applied_by_marco, bundle_setup, texts

TENANT = "demo-retail"
LANE = "CGP-UK1-SA"


@pytest.fixture
async def m5(tmp_path: Path) -> AsyncIterator[Harness]:
    h = await Harness(tmp=tmp_path, merxa=MerxaStub(supported_ops=ALL_M5_OPS)).setup()
    try:
        yield h
    finally:
        await h.close()


def run_ctx(h: Harness, *, kind: str = "action", payload: dict[str, Any] | None = None,
            resume: ResumePayload | None = None, actor: dict[str, Any] = PRIYA) -> RunContext:
    return RunContext(
        tenant=TENANT, case_id=h.case_id, run_id="run-review", kind=kind, actor=Actor.model_validate(actor),
        payload=dict(payload or {}), spec=AgentSpec(tenant=TENANT), deps=h.core.deps(),
        invocation_id="e-run-review", resume=resume,
    )


async def drive(node: Any, state: dict[str, Any], node_input: Any) -> list[Any]:
    ctx = pytypes.SimpleNamespace(state=state)
    return [item async for item in node(ctx, node_input)]


def final(outputs: list[Any]) -> tuple[Any, str | None]:
    last = outputs[-1]
    if isinstance(last, Event):
        return last.output, last.actions.route
    return last, None


# ─────────────────────────────── policy origin ───────────────────────────────


async def test_policy_origin_is_never_relaxed_by_a_node_input(m5: Harness) -> None:
    signal_run = run_ctx(m5, kind="signal", payload={"signal_id": "7"})
    assert commit_nodes.effective_origin("chat", signal_run) == "webhook"
    assert commit_nodes.effective_origin(None, signal_run) == "webhook"
    sneaky = run_ctx(m5, kind="action", payload={"type": "apply", "signal_id": "7"})
    assert commit_nodes.effective_origin("chat", sneaky) == "webhook"
    chat = run_ctx(m5, kind="action", payload={"type": "apply"})
    assert commit_nodes.effective_origin("chat", chat) == "chat"
    assert commit_nodes.effective_origin("webhook", chat) == "webhook"  # stricter is fine
    assert commit_nodes.effective_origin("something-else", chat) == "chat"


# ─────────────────────────────── approval_check ───────────────────────────────


async def test_auto_route_needs_the_gates_own_auto_decision(m5: Harness) -> None:
    h = m5
    base = {commit_nodes.BUNDLE_KEY: {"id": "cs-1", "change_set_ids": ["cs-1"]}}
    with bind_run(run_ctx(h)):
        # the gate decided "human": an auto flag on the input never approves
        state = {**base, commit_nodes.APPROVAL_KEY: {"outcome": "human", "approved_by": None}}
        out, route = final(await drive(commit_nodes.approval_check, state, {"decision": "approve", "auto": True}))
        assert route == "REFUSED" and out["reason"] == "refused"
        # nor without any recorded decision
        out, route = final(await drive(commit_nodes.approval_check, dict(base), {"decision": "approve", "auto": True}))
        assert route == "REFUSED"
        # a rollback is never auto-approved, whatever the state says
        state = {**base, commit_nodes.COMMIT_MODE_KEY: "rollback",
                 commit_nodes.APPROVAL_KEY: {"outcome": "auto", "approved_by": "policy:x@p-1"}}
        out, route = final(await drive(commit_nodes.approval_check, state, {"decision": "approve", "auto": True}))
        assert route == "REFUSED"
        # the gate's own auto decision does
        state = {**base, commit_nodes.APPROVAL_KEY: {"outcome": "auto", "approved_by": "policy:auto-small-transfer@p-1"}}
        out, route = final(await drive(commit_nodes.approval_check, state, {"decision": "approve", "auto": True}))
        assert route == "APPROVED" and out["approved_by"] == "policy"


async def test_a_decision_for_another_approval_request_is_refused(m5: Harness) -> None:
    h = m5
    resume = ResumePayload(interrupt_id="approval-other-1", decision="approve", plan_version=1,
                           change_set_hash="a" * 64, decided_by=[{"sub": MARCO["sub"], "username": "marco",
                                                                  "name": "Marco Rossi", "role": "approver"}])
    state = {commit_nodes.BUNDLE_KEY: {"id": "cs-1", "change_set_ids": ["cs-1"]},
             commit_nodes.APPROVAL_KEY: {"outcome": "human", "interrupt_id": "approval-cs-1-1",
                                         "requirement": {"roles": ["approver"], "count": 1,
                                                         "exclude_initiator": False}}}
    with bind_run(run_ctx(h, kind="resume", resume=resume)):
        out, route = final(await drive(commit_nodes.approval_check, state,
                                       {"decision": "approve", "interrupt_id": "approval-other-1"}))
    assert route == "REFUSED" and "does not match the open approval request" in out["message"]
    assert h.merxa.apply_calls == 0


async def test_the_gate_records_the_interrupt_it_waits_for(m5: Harness) -> None:
    h = m5
    await seed_plan(h, [option("opt-A", [TO])])
    row = action_row(h, {"type": "apply", "option_id": "opt-A", "plan_version": 1}, ADA)
    assert await h.run(row) == "paused"
    intr = last_interrupt(h)
    assert await h.run(approve_row(h, row, [decider(MARCO, "approver")])) == "succeeded"
    assert h.merxa.apply_calls == 1  # the matching decision still applies
    assert intr["interrupt_id"].startswith("approval-")


# ─────────────────────────────── rollback ───────────────────────────────


async def test_every_rollback_request_gets_its_own_attempt(m5: Harness) -> None:
    h = m5
    cs = await applied_by_marco(h, [TO])
    first = action_row(h, {"type": "rollback", "change_set_id": cs["id"]}, PRIYA)
    assert await h.run(first) == "paused"
    assert last_interrupt(h)["interrupt_id"] == f"approval-rb-{cs['id']}-1"
    second = action_row(h, {"type": "rollback", "change_set_id": cs["id"]}, PRIYA)
    assert await h.run(second) == "paused"
    assert last_interrupt(h)["interrupt_id"] == f"approval-rb-{cs['id']}-2"
    assert await h.run(approve_row(h, second, [decider(MARCO, "approver")])) == "succeeded"
    [call] = [args for tool, args in h.merxa.calls if tool == "rollback_change_set"]
    assert call["approval_ref"] == commit_nodes.rollback_approval_id(
        commit_nodes.compile_items(tenant=TENANT, change_set_id=cs["id"], connector="merxa", actions=cs["items"]), 2)
    assert h.core.repo.change_sets[cs["id"]]["status"] == "rolled_back"


async def test_a_rollback_requested_again_after_a_rejection_gets_a_new_approval_request(m5: Harness) -> None:
    """The platform stores interrupts ``ON CONFLICT (id) DO NOTHING``: reusing the rejected request's
    id would leave the new Undo without any approval card."""
    h = m5
    cs = await applied_by_marco(h, [TO])
    first = action_row(h, {"type": "rollback", "change_set_id": cs["id"]}, PRIYA)
    assert await h.run(first) == "paused"
    assert await h.run(approve_row(h, first, [decider(MARCO, "approver")], decision="reject")) == "succeeded"
    assert h.merxa.rollback_calls == 0
    again = action_row(h, {"type": "rollback", "change_set_id": cs["id"]}, PRIYA)
    assert await h.run(again) == "paused"
    ids = [i["interrupt_id"] for i in h.core.interrupts.created if i["interrupt_id"].startswith("approval-rb-")]
    assert ids == [f"approval-rb-{cs['id']}-1", f"approval-rb-{cs['id']}-2"]
    assert await h.run(approve_row(h, again, [decider(MARCO, "approver")])) == "succeeded"
    assert h.merxa.rollback_calls == 1 and h.core.repo.change_sets[cs["id"]]["status"] == "rolled_back"


async def test_rollback_of_another_cases_change_set_is_refused(m5: Harness) -> None:
    h = m5
    cs = await applied_by_marco(h, [TO])
    h.core.repo.change_sets[cs["id"]]["case_id"] = None  # a row that names no case never matches this one
    row = action_row(h, {"type": "rollback", "change_set_id": cs["id"]}, PRIYA)
    assert await h.run(row) == "succeeded" and "not part of this case" in texts(h)[-1]
    assert h.merxa.rollback_calls == 0


async def test_in_doubt_rollback_that_conflicts_stays_undoable(m5: Harness) -> None:
    """A crash after ``rolling_back`` was recorded: the retried execute reads back, calls Merxa with
    the same attempt; a conflict answer must restore the pre-rollback status (not leave the batch
    stuck in ``rolling_back``, which can never be undone again)."""
    h = m5
    cs = await applied_by_marco(h, [TO])
    row = h.core.repo.change_sets[cs["id"]]
    row["status"] = "rolling_back"
    row["result"] = {**row["result"], "rollback_attempts": 1, "rollback_from": "verified"}
    h.merxa.rollback_conflicts = [{"entity_ref": "TO-0001", "detail": "shipped after the apply"}]
    state = {
        commit_nodes.COMMIT_MODE_KEY: "rollback", commit_nodes.CHANGE_SET_KEY: cs["id"],
        commit_nodes.BUNDLE_KEY: {"id": cs["id"], "change_set_ids": [cs["id"]], "systems": {"merxa": "Merxa"}},
        commit_nodes.ROLLBACK_KEY: {"plan_version": 1, "attempt": 1},
        commit_nodes.INITIATOR_KEY: PRIYA,
        commit_nodes.APPROVAL_KEY: {"outcome": "human", "policy_version": "p-1",
                                    "executor": {"sub": MARCO["sub"], "username": "marco", "name": "Marco Rossi",
                                                 "role": "approver"}, "co_approved_by": []},
    }
    with bind_run(run_ctx(h)):
        outputs = await drive(commit_nodes.execute, state, {"change_set_id": cs["id"]})
    out, _ = final(outputs)
    assert out["status"] == "rollback_conflict"
    assert h.core.repo.change_sets[cs["id"]]["status"] == "verified"
    assert h.core.repo.change_sets[cs["id"]]["result"]["rollback_conflicts"][0]["entity_ref"] == "TO-0001"
    # and it can be undone later
    h.merxa.rollback_conflicts = []
    again = action_row(h, {"type": "rollback", "change_set_id": cs["id"]}, PRIYA)
    assert await h.run(again) == "paused"


# ─────────────────────────────── bundles ───────────────────────────────


async def test_bundle_member_conflict_still_reconciles_the_applied_members(m5: Harness) -> None:
    h = m5
    tms = await bundle_setup(h)
    tms.apply_conflicts = [{"entity_ref": "2026-W44", "detail": "capacity changed"}]
    row = action_row(h, {"type": "apply", "option_id": "opt-B", "plan_version": 1}, PRIYA)
    assert await h.run(row) == "paused"
    assert await h.run(approve_row(h, row, [decider(MARCO, "approver")])) == "succeeded"
    rows = sorted(h.core.repo.change_sets.values(), key=lambda c: c["id"])
    assert [r["status"] for r in rows] == ["verified", "conflict"]  # the Merxa member was read back
    message = texts(h)[-1]
    assert "Applied 1 changes in Merxa" in message and "Carrier TMS reported that some records changed" in message
    assert h.core.repo.plan_status.get((h.case_id, 1)) != "committed"


# ─────────────────────────────── re-plan ───────────────────────────────


async def test_the_same_signal_is_never_replanned_twice(m5: Harness) -> None:
    h = m5
    await applied_by_marco(h, [TO_STUB])
    h.optimizer.units_per_transfer = (200, 260)
    signal_id = h.core.signals.add("lane.capacity_changed", {"lane_code": "DE1>NL1", "factor_bp": 3000, "weeks": 3})
    assert await h.run(h.row("signal", {"signal_id": signal_id}, SIGNALS)) == "paused"
    plans, interrupts = len(h.core.repo.plans[h.case_id]), len(h.core.interrupts.created)
    scenarios = len(h.core.repo.scenarios[h.case_id])
    # duplicate delivery (or a retried run) of the same signal
    assert await h.run(h.row("signal", {"signal_id": signal_id}, SIGNALS)) == "succeeded"
    assert len(h.core.repo.plans[h.case_id]) == plans == 2
    assert len(h.core.interrupts.created) == interrupts
    assert len(h.core.repo.scenarios[h.case_id]) == scenarios


async def test_a_replanned_but_uncompiled_delta_is_compiled_on_retry(m5: Harness) -> None:
    h = m5
    await applied_by_marco(h, [TO_STUB])
    h.optimizer.units_per_transfer = (200, 260)
    signal_id = h.core.signals.add("lane.capacity_changed", {"lane_code": "DE1>NL1", "factor_bp": 3000, "weeks": 3})
    assert await h.run(h.row("signal", {"signal_id": signal_id}, SIGNALS)) == "paused"
    # as if the run had crashed after saving plan v2 but before compiling the delta
    for cs_id in [c["id"] for c in h.core.repo.change_sets.values() if c["plan_version"] == 2]:
        del h.core.repo.change_sets[cs_id]
    before = len(h.core.interrupts.created)
    assert await h.run(h.row("signal", {"signal_id": signal_id}, SIGNALS)) == "paused"
    assert len(h.core.repo.plans[h.case_id]) == 2 and len(h.core.interrupts.created) == before + 1
    intr = last_interrupt(h)
    assert intr["plan_version"] == 2 and intr["quorum_spec"]["count"] == 1  # still a person, never auto


def booking(qty: int, *, ref: str | None, version: int, week: str = "2026-W44") -> dict[str, Any]:
    item = {**BOOKING, "payload": {"lane_code": LANE, "iso_week": week, "qty_units": qty}, "plan_version": version}
    if ref:
        item["created_ref"] = ref
    return item


def test_delta_never_cancels_a_booking_twice() -> None:
    applied = [
        booking(400, ref="BK-1", version=1),
        {"op": BOOKING_CANCEL_OP, "target_ref": "BK-1", "payload": {}, "plan_version": 2},
        booking(120, ref="BK-2", version=2),
    ]
    # a second cut on the same lane still leaves room for what is held now (120 ≤ 150)
    result = delta_actions([], applied, dirty_lane=LANE, lane_capacity=150)
    assert result.actions == [] and result.cancelled_bookings == 0
    # a deeper cut only touches the live re-booking
    result = delta_actions([], applied, dirty_lane=LANE, lane_capacity=60)
    assert [(a["op"], a["target_ref"]) for a in result.actions if a["op"] == BOOKING_CANCEL_OP] == [
        (BOOKING_CANCEL_OP, "BK-2")]


def test_delta_counts_fallback_booking_refs_per_cancel() -> None:
    applied = [
        booking(100, ref=None, version=1),
        booking(100, ref=None, version=1),
        {"op": BOOKING_CANCEL_OP, "target_ref": f"{LANE}@2026-W44", "payload": {}, "plan_version": 2},
    ]
    result = delta_actions([], applied, dirty_lane=LANE, lane_capacity=50)
    assert result.cancelled_bookings == 1  # one booking was released already, one is still held


def test_delta_never_withdraws_a_split_twice() -> None:
    split = {"op": "po_line.split", "target_ref": "PO-BD-0001#1", "plan_version": 1,
             "payload": {"lane_code": LANE, "mode": "sea_air", "qty": 300}}
    resched = {"op": "po_line.reschedule", "target_ref": "PO-BD-0001#1", "plan_version": 2,
               "payload": {"eta_dc": "2026-11-05"}}
    applied = [booking(300, ref="BK-1", version=1), split]
    first = delta_actions([], applied, dirty_lane=LANE, lane_capacity=0, po_etas={"PO-BD-0001#1": "2026-11-05"})
    assert first.rescheduled == 1
    # v2 cancelled BK-1 and withdrew the split; a later booking on the lane overflows a further cut
    later = [*applied, {"op": BOOKING_CANCEL_OP, "target_ref": "BK-1", "payload": {}, "plan_version": 2}, resched,
             booking(100, ref="BK-3", version=2)]
    again = delta_actions([], later, dirty_lane=LANE, lane_capacity=0, po_etas={"PO-BD-0001#1": "2026-11-05"})
    assert again.cancelled_bookings == 1 and again.rescheduled == 0
    assert [(a["op"], a["target_ref"]) for a in again.actions] == [(BOOKING_CANCEL_OP, "BK-3")]


# ─────────────────────────────── guide ───────────────────────────────


async def test_untrusted_operator_guide_is_never_shown_verbatim(m5: Harness) -> None:
    h = m5
    h.core.connectors.tools = [
        dataclasses.replace(t, content_trust="untrusted_text") if t.name == "get_operator_guide" else t
        for t in h.core.connectors.tools
    ]
    await seed_plan(h, [option("opt-A", [TO])])
    row = action_row(h, {"type": "guide", "option_id": "opt-A", "plan_version": 1}, PRIYA)
    assert await h.run(row) == "succeeded"
    assert h.merxa.guide_calls == 0
    [cl] = h.core.checklists.rows.values()  # type: ignore[union-attr]
    assert cl["meta"]["source"] == "generated" and cl["meta"]["prov"] == "inference"

