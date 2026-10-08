"""action:apply → dry-run → approval pause → resume → execute → reconcile (+ crash-retry,
auto-approval, what-if refusal, transient apply retry)."""

from __future__ import annotations

import pytest
from fakes.core import FakeCrypto, SimulatedCrash
from fakes.harness import MARCO, PRIYA, Harness
from fakes.scripts import (
    chat_flow_script,
    intake_disruption,
    investigator_turns,
    m4_review_turns,
    recommendation,
    structurer_turn,
)
from keelson_contracts.blocks import parse_block
from keelson_contracts.changeset import change_set_hash, idempotency_key

from keelson_engine.runtime.spec import PolicyConfig

@pytest.fixture(autouse=True)
def m1_auto_rule(harness: Harness) -> None:
    """These tests exercise the apply mechanics under the M1 auto rule (auto only when an approver
    starts a small transfer); the M5 default (a planner may) is covered by ``test_m5_policy.py``."""
    harness.spec_overrides["policy"] = PolicyConfig(auto_initiator_role="approver")


DECIDER_MARCO = {"sub": MARCO["sub"], "username": MARCO["username"], "name": MARCO["name"], "role": "approver",
                 "roles": MARCO["roles"]}


async def plan_ready(h: Harness, *, hypothetical: bool = False) -> None:
    """A case with plan v1 (delay stated → grounded → approach auto-confirmed in one run)."""
    if hypothetical:
        h.script.add("intake", intake_disruption(delay_days=7, hypothetical=True))
        m4_review_turns(h.script)
        h.script.add("inventory_demand_investigator", *investigator_turns())
        h.script.add("inventory_demand_structurer", structurer_turn(h.core.repo))
        h.script.add("recommend", recommendation())
    else:
        h.script = chat_flow_script(h.core.repo, delay_days=7)
    row = h.row("chat", {"text": "Trims from Meghna are 7 days late"})
    assert await h.run(row) == "succeeded"
    assert not h.core.interrupts.created, "grounded scenario must auto-confirm"


def apply_payload() -> dict[str, object]:
    return {"type": "apply", "option_id": "opt-A", "plan_version": 1}


async def approve(h: Harness, action_row: object, *, cache: object = None) -> object:
    created = h.core.interrupts.created[-1]
    resume = h.resume_row(
        action_row, "approve", [DECIDER_MARCO],
        plan_version=created["plan_version"], change_set_hash=created["change_set_hash"],
    )
    return resume


async def test_apply_human_approval_execute_reconcile(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    action = h.row("action", apply_payload(), PRIYA)
    assert await h.run(action) == "paused"

    [cs] = h.core.repo.change_sets.values()
    items = cs["items"]
    assert cs["hash"] == change_set_hash(items) and cs["status"] == "validated"
    assert cs["ext_code"] == "CS-0001" and cs["preview"]["preview_hash"] == cs["hash"]
    interrupt = h.core.interrupts.created[-1]
    assert interrupt["kind"] == "approval"
    assert interrupt["interrupt_id"] == f"approval-{cs['id']}-1"
    assert interrupt["change_set_hash"] == cs["hash"] and interrupt["plan_version"] == 1
    assert interrupt["quorum_spec"] == {"roles": ["approver"], "count": 1, "exclude_initiator": False}
    # preparer did the dry-run; no write ran with the reader or any user token yet
    writes = [c for c in h.mcp_calls if c.tool in ("create_change_set", "add_change_set_items", "validate_change_set")]
    assert writes and {c.principal for c in writes} == {"preparer"}
    assert h.merxa.apply_calls == 0
    events = h.events(action.id)
    cards = [parse_block(e["content"]) for e in events if e["type"] == "ACTIVITY_SNAPSHOT"]
    assert [c.kind for c in cards] == ["plan_diff", "approval_card"]
    assert cards[1].props.change_set_hash == cs["hash"] and cards[1].props.required_roles == ["approver"]
    assert events[-1]["outcome"]["interrupts"][0]["reason"] == "approval"

    resume = await approve(h, action)
    assert await h.run(resume) == "succeeded"

    assert h.merxa.apply_calls == 1 and h.merxa.apply_effects == 1
    [apply_call] = [args for tool, args in h.merxa.calls if tool == "apply_change_set"]
    assert apply_call["idempotency_key"] == idempotency_key("demo-retail", cs["id"], 1)
    claims = h.core.crypto.signed[-1]
    assert claims.approved_by == "user:marco" and claims.approver_name == "Marco Rossi"
    assert claims.keelson_sub == MARCO["sub"] and claims.ext_sub == f"merxa-{MARCO['sub']}"
    assert claims.initiated_by == "Priya Shah" and claims.change_set_hash == cs["hash"]
    assert claims.change_set_ref == h.core.repo.change_sets[cs["id"]]["ext_change_set_id"]
    assert claims.approval_id == apply_call["approval_ref"] == FakeCrypto.decode(apply_call["receipt"])["approval_id"]
    apply_principals = {c.principal for c in h.mcp_calls if c.tool == "apply_change_set"}
    assert apply_principals == {"user"}
    assert ("user", MARCO["sub"]) in h.core.tokens.issued

    row = h.core.repo.change_sets[cs["id"]]
    assert row["status"] == "verified" and row["result"]["verified"] == len(items)
    assert row["result"]["attribution_text"] == "Changed by Marco Rossi via Keelson (" + claims.approval_id + ")"
    assert [x["state"] for x in h.core.repo.ledger if x["state"] == "verified"] == ["verified"] * len(items)
    assert h.core.repo.plan_status[(h.case_id, 1)] == "committed"
    trackers = [parse_block(e["content"]) for e in h.events(resume.id) if e["type"] == "ACTIVITY_SNAPSHOT"]
    assert [t.props.status for t in trackers] == ["applying", "applied", "verified"]
    assert trackers[-1].props.ext_url == "http://merxa.localhost/change-batches/CS-0001"
    assert trackers[-1].props.verified == len(items)
    steps = [e["stepName"] for e in h.events(resume.id) if e["type"] == "STEP_STARTED"]
    assert steps == ["awaiting_approval", "applying", "verified"]
    assert "verified" in h.core.repo.messages[-1]["text"]


async def test_execute_crash_retry_applies_exactly_once(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    action = h.row("action", apply_payload(), PRIYA)
    assert await h.run(action) == "paused"
    resume = await approve(h, action)
    # the worker "dies" right after Merxa applied, before the engine recorded it
    h.core.repo.crash_on_update = lambda cs_id, fields: fields.get("status") == "applied"
    with pytest.raises(SimulatedCrash):
        await h.run(resume)
    assert h.merxa.apply_calls == 1
    [cs] = h.core.repo.change_sets.values()
    assert cs["status"] == "applying"  # in doubt

    # the reaper re-queues the same run as crash_retry; a fresh runner continues the invocation
    retry = h.row("crash_retry", resume.payload, resume.actor, run_id=resume.id,
                  session_id=resume.session_id, invocation_id=resume.invocation_id)
    retry.origin_kind = "resume"  # E1's RunRow keeps the original kind
    fresh = h.new_cache()
    try:
        assert await h.run(retry, cache=fresh) == "succeeded"
    finally:
        await fresh.close()
    assert h.merxa.apply_calls == 1, "execute must read back an in-doubt change set instead of re-applying"
    assert h.merxa.apply_effects == 1
    assert h.core.repo.change_sets[cs["id"]]["status"] == "verified"

    # and a further retry of the finished run is a no-op for Merxa
    again = h.row("crash_retry", resume.payload, resume.actor, run_id=resume.id,
                  session_id=resume.session_id, invocation_id=resume.invocation_id)
    await h.run(again)
    assert h.merxa.apply_calls == 1


async def test_auto_policy_apply_uses_autocommit(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    action = h.row("action", apply_payload(), MARCO)  # initiator holds the approver role
    assert await h.run(action) == "succeeded"
    assert not [i for i in h.core.interrupts.created if i["kind"] == "approval"]
    claims = h.core.crypto.signed[-1]
    assert claims.approved_by == "policy:auto-small-transfer@p-1" and claims.policy_version == "p-1"
    assert claims.approver_name is None and claims.keelson_sub == MARCO["sub"]
    assert claims.ext_sub == "svc-autocommit"
    assert {c.principal for c in h.mcp_calls if c.tool == "apply_change_set"} == {"autocommit"}
    assert h.merxa.apply_effects == 1
    [cs] = h.core.repo.change_sets.values()
    assert h.core.repo.change_sets[cs["id"]]["status"] == "verified"
    assert "auto-policy auto-small-transfer@p-1" in (h.core.repo.change_sets[cs["id"]]["result"]["attribution_text"] or "")


async def test_transient_apply_error_retries_with_same_key(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    h.merxa.fail_next_apply = ["receipt_jwks_unavailable: could not fetch the Keelson JWKS"]
    action = h.row("action", apply_payload(), MARCO)
    assert await h.run(action) == "succeeded"
    keys = [args["idempotency_key"] for tool, args in h.merxa.calls if tool == "apply_change_set"]
    assert len(keys) == 2 and len(set(keys)) == 1
    assert h.merxa.apply_effects == 1


async def test_reject_closes_without_applying(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    action = h.row("action", apply_payload(), PRIYA)
    assert await h.run(action) == "paused"
    resume = h.resume_row(action, "reject", [DECIDER_MARCO])
    assert await h.run(resume) == "succeeded"
    assert h.merxa.apply_calls == 0
    assert "rejected" in h.core.repo.messages[-1]["text"]


async def test_what_if_case_cannot_compile(harness: Harness) -> None:
    h = harness
    await plan_ready(h, hypothetical=True)
    assert h.core.repo.cases[h.case_id]["mode"] == "what_if"
    rec = parse_block(h.core.repo.blocks_of("recommended_option")[-1])
    apply = next(a for a in rec.props.actions if a.type == "apply")
    assert not apply.enabled and "what-if" in (apply.disabled_reason or "").lower()
    action = h.row("action", apply_payload(), MARCO)
    assert await h.run(action) == "succeeded"
    assert h.core.repo.change_sets == {}
    assert not [c for c in h.mcp_calls if c.tool in ("create_change_set", "apply_change_set")]
    assert "what-if" in h.core.repo.messages[-1]["text"]


async def test_stale_plan_version_is_refused(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    action = h.row("action", {"type": "apply", "option_id": "opt-A", "plan_version": 2}, MARCO)
    assert await h.run(action) == "succeeded"
    assert h.core.repo.change_sets == {}
    assert "plan has changed" in h.core.repo.messages[-1]["text"]


async def test_approver_without_merxa_link_fails_loudly(harness: Harness) -> None:
    h = harness
    await plan_ready(h)
    action = h.row("action", apply_payload(), PRIYA)
    assert await h.run(action) == "paused"
    h.core.tokens.unlinked.add(MARCO["sub"])
    resume = await approve(h, action)
    assert await h.run(resume) == "failed"
    assert h.merxa.apply_calls == 0
    assert h.events(resume.id)[-1]["type"] == "RUN_ERROR"
