"""M5 quorum decisions (``95`` §1): four-eyes counted by distinct people and roles, initiator
exclusion, same user twice, rejects with a reason, the executor (approver role) first in
``decided_by`` whatever the order, the API's quorum progress and the executor-only Merxa
pre-flight, and the live approval card."""

from __future__ import annotations

from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_browser import Browser
from e1_env import TENANT_A
from e1_idp import FakeRealm
from keelson_contracts.blocks import parse_block
from keelson_contracts.policy import QuorumView
from keelson_contracts.runs import ResumePayload
from keelson_engine.core import connectors, interrupts as itr
from keelson_engine.core import repo, runs
from keelson_engine.core.db import tenant_session
from keelson_engine.core.tokens import get_token_broker
from test_tokens import MERXA_SPEC, _link

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRIYA = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}
MARCO = {"sub": "sub-marco", "username": "marco", "name": "Marco Rossi", "roles": ["planner", "approver"]}
ANNA = {"sub": "sub-anna", "username": "anna", "name": "Anna Approver", "roles": ["approver"]}
LENA = {"sub": "sub-lena", "username": "lena", "name": "Lena Vogel", "roles": ["compliance"]}
DUAL = {"sub": "sub-dual", "username": "dual", "name": "Dana Dual", "roles": ["approver", "compliance"]}
H1 = "1" * 64
FOUR_EYES = {"roles": ["approver", "compliance"], "count": 2, "exclude_initiator": True}


async def _paused_run(initiator: dict[str, Any] = PRIYA) -> tuple[str, str]:
    case_id = await repo.create_case("quorum", created_by=initiator, tenant=TENANT_A)
    run_id = await runs.enqueue_run(tenant=TENANT_A, case_id=case_id, kind="chat", payload={"text": "x"},
                                    actor=initiator, idempotency_key=None)
    return case_id, run_id


async def _approval(case_id: str, run_id: str, quorum: dict[str, Any] = FOUR_EYES, suffix: str = "1") -> str:
    iid = f"approval-cs-{case_id}-{suffix}"
    await itr.create_interrupt(
        tenant=TENANT_A, case_id=case_id, run_id=run_id, kind="approval", interrupt_id=iid,
        payload={"message": "Approve 2 PO changes", "connector": "merxa"}, quorum_spec=quorum,
        plan_version=2, change_set_hash=H1, invocation_id="inv-q",
    )
    return iid


async def _approve(iid: str, who: dict[str, Any]) -> itr.DecisionOutcome:
    return await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid, decider=who, decision="approve",
                                        plan_version=2, change_set_hash=H1)


async def _resume(out: itr.DecisionOutcome) -> ResumePayload:
    run = await runs.get_run(TENANT_A, out.run_id or "")
    assert run is not None and run.kind == "resume"
    return ResumePayload.model_validate(run.payload)


async def _decision_rows(iid: str) -> list[dict[str, Any]]:
    async with tenant_session(TENANT_A) as s:
        res = await s.execute(text("SELECT approver_sub, role, roles, decision, reason FROM approval_decision "
                                   "WHERE interrupt_id = :i ORDER BY id"), {"i": iid})
        return [dict(m) for m in res.mappings().all()]


# ───────────────────────────── core permutations ─────────────────────────────


async def test_four_eyes_compliance_first_executor_is_the_approver(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    first = await _approve(iid, LENA)
    assert first.status == "recorded" and first.run_id is None
    assert first.quorum is not None and first.quorum.remaining == 1 and first.quorum.missing_roles == ["approver"]
    assert [(a.sub, a.role) for a in first.quorum.approvals] == [("sub-lena", "compliance")]
    out = await _approve(iid, MARCO)
    assert out.status == "resolved" and out.quorum is not None and out.quorum.met
    resume = await _resume(out)
    # the approver executes (his delegated token), whatever the order; Lena is the co-approver
    assert [(d.sub, d.role, d.username) for d in resume.decided_by] == [
        ("sub-marco", "approver", "marco"), ("sub-lena", "compliance", "lena")]
    rows = await _decision_rows(iid)
    assert [(r["approver_sub"], r["role"], r["roles"]) for r in rows] == [
        ("sub-lena", "compliance", ["compliance"]), ("sub-marco", "approver", ["planner", "approver"])]
    intr = await itr.get_interrupt(TENANT_A, iid)
    assert intr is not None and intr["status"] == "RESOLVED"


async def test_four_eyes_two_approvers_never_complete(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    assert (await _approve(iid, MARCO)).status == "recorded"
    with pytest.raises(itr.NotEligible) as exc:
        await _approve(iid, ANNA)  # a second approver cannot stand in for compliance
    assert exc.value.details["missing_roles"] == ["compliance"]
    assert [r["approver_sub"] for r in await _decision_rows(iid)] == ["sub-marco"]  # nothing stored for Anna
    out = await _approve(iid, LENA)
    assert out.status == "resolved"
    assert [d.sub for d in (await _resume(out)).decided_by] == ["sub-marco", "sub-lena"]


async def test_same_user_twice_counts_once(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    one = await _approve(iid, MARCO)
    two = await _approve(iid, MARCO)  # retry / double click: acknowledged, not counted again
    assert one.status == two.status == "recorded"
    assert two.quorum is not None and two.quorum.remaining == 1 and len(two.quorum.approvals) == 1
    # a person holding BOTH roles is still one person: never a quorum on their own
    case2, run2 = await _paused_run()
    iid2 = await _approval(case2, run2)
    out = await _approve(iid2, DUAL)
    assert out.status == "recorded" and out.quorum is not None and out.quorum.remaining == 1
    assert (await _approve(iid2, DUAL)).status == "recorded"
    with pytest.raises(itr.InterruptClosed):  # changing one's mind after approving is not allowed
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid2, decider=DUAL, decision="reject")


async def test_dual_role_first_then_single_role_approver_completes(platform: None) -> None:
    """Maximum matching: Dana (approver + compliance) approved first; Anna (approver only) then
    completes it — Dana counts as compliance, Anna executes."""
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    first = await _approve(iid, DUAL)
    assert first.quorum is not None and first.quorum.approvals[0].role == "approver"
    out = await _approve(iid, ANNA)
    assert out.status == "resolved" and out.quorum is not None
    assert {a.sub: a.role for a in out.quorum.approvals} == {"sub-dual": "compliance", "sub-anna": "approver"}
    assert [(d.sub, d.role) for d in (await _resume(out)).decided_by] == [
        ("sub-anna", "approver"), ("sub-dual", "compliance")]


async def test_initiator_is_excluded(platform: None) -> None:
    case_id, run_id = await _paused_run(initiator=MARCO)  # Marco started the run
    iid = await _approval(case_id, run_id)
    with pytest.raises(itr.NotEligible):
        await _approve(iid, MARCO)
    with pytest.raises(itr.NotEligible):  # nor may he reject his own change set
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="reject")
    assert (await _approve(iid, LENA)).status == "recorded"
    out = await _approve(iid, ANNA)
    assert out.status == "resolved"
    assert [d.sub for d in (await _resume(out)).decided_by] == ["sub-anna", "sub-lena"]
    # without exclude_initiator the initiator may approve
    case2, run2 = await _paused_run(initiator=MARCO)
    iid2 = await _approval(case2, run2, quorum={"roles": ["approver"], "count": 1, "exclude_initiator": False})
    assert (await _approve(iid2, MARCO)).status == "resolved"


async def test_required_reject_rejects_with_reason(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    assert (await _approve(iid, MARCO)).status == "recorded"
    with pytest.raises(itr.NotEligible):  # a planner holds no required role
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=PRIYA, decision="reject",
                                  reason="no")
    with pytest.raises(itr.InvalidDecision):  # a reason is only for reject/revise
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=LENA, decision="approve",
                                  plan_version=2, change_set_hash=H1, reason="fine")
    out = await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid, decider=LENA, decision="reject",
                                       reason="Supplier not on the approved list")
    assert out.status == "resolved"
    resume = await _resume(out)
    assert resume.decision == "reject" and resume.reason == "Supplier not on the approved list"
    assert [(d.sub, d.role) for d in resume.decided_by] == [("sub-lena", "compliance")]
    assert (await _decision_rows(iid))[-1]["reason"] == "Supplier not on the approved list"


async def test_executor_check_runs_only_for_the_approver(platform: None) -> None:
    checked: list[dict[str, Any]] = []

    async def check(executor: dict[str, Any]) -> None:
        checked.append(executor)

    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid, decider=LENA, decision="approve",
                                 plan_version=2, change_set_hash=H1, executor_check=check)
    assert checked == []  # compliance never needs a link
    await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve",
                                 plan_version=2, change_set_hash=H1, executor_check=check)
    assert [(c["sub"], c["role"], c["is_decider"]) for c in checked] == [("sub-marco", "approver", True)]

    # the approver approved first: completing the quorum re-checks HIM, not the decider
    checked.clear()
    case2, run2 = await _paused_run()
    iid2 = await _approval(case2, run2)
    await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid2, decider=MARCO, decision="approve",
                                 plan_version=2, change_set_hash=H1, executor_check=check)

    class Refused(Exception):
        pass

    async def refuse(executor: dict[str, Any]) -> None:
        checked.append(executor)
        raise Refused(executor["sub"])

    with pytest.raises(Refused):
        await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid2, decider=LENA, decision="approve",
                                     plan_version=2, change_set_hash=H1, executor_check=refuse)
    assert [(c["sub"], c["is_decider"]) for c in checked] == [("sub-marco", True), ("sub-marco", False)]
    # refused → rolled back: Lena's approval is not stored and the interrupt stays open
    assert [r["approver_sub"] for r in await _decision_rows(iid2)] == ["sub-marco"]
    intr = await itr.get_interrupt(TENANT_A, iid2)
    assert intr is not None and intr["status"] == "PENDING"


async def test_legacy_rows_and_quorum_progress(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    await _approve(iid, LENA)
    async with tenant_session(TENANT_A) as s:  # a pre-M5 row: only the counted role, no roles list
        await s.execute(text("UPDATE approval_decision SET roles = '[]'::jsonb WHERE interrupt_id = :i"), {"i": iid})
    progress = await itr.quorum_progress(TENANT_A, [iid])
    view = progress[iid]
    assert isinstance(view, QuorumView) and view.remaining == 1 and view.approvals[0].role == "compliance"
    assert itr.quorum_requirement({"roles": ["approver"], "bogus": 1}).count == 1
    assert itr.quorum_requirement({"roles": [], "count": 0}).roles == ["approver"]  # unusable → default


# ───────────────────────────── API ─────────────────────────────


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any, platform: None) -> dict[str, Browser]:
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    return {n: await browser_factory(n) for n in ("priya", "marco", "lena")}


async def _unlink(sub: str) -> None:
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("DELETE FROM connector_identity_link WHERE keelson_sub = :k"), {"k": sub})
        await s.execute(text("DELETE FROM connector_credential WHERE principal_sub = :k"), {"k": sub})


def _card(case_id: str, iid: str) -> dict[str, Any]:
    return {
        "id": f"blk_appr_{case_id[-12:]}", "kind": "approval_card", "v": 1, "title": "Approval",
        "placement": "inline",
        "provenance": {"kind": "decision", "engine": "keelson-commit@0.1", "inputs_hash": None, "as_of": "2026-10-07"},
        "data_ref": None,
        "props": {"interrupt_id": iid, "change_set_id": "cs-1", "change_set_hash": H1, "plan_version": 2,
                  "summary": {"lines": 2, "units": 40, "value_cents": 120000}, "reason": "Four-eyes",
                  "required_roles": ["approver", "compliance"], "connect": {"status": "missing", "connect_url": None},
                  "buttons": ["approve", "reject", "revise"]},
    }


async def test_api_four_eyes_progress_preflight_and_card(people: dict[str, Browser], merxa_realm: FakeRealm) -> None:
    priya, marco, lena = people["priya"], people["marco"], people["lena"]
    await _unlink("sub-marco")
    await _unlink("sub-lena")
    case_id = (await priya.post("/api/cases", json={})).json()["id"]
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "q1"})
    iid = await _approval(case_id, msg.json()["run_id"])
    card = _card(case_id, iid)
    parse_block(card)
    await repo.save_block(case_id, msg.json()["run_id"], card, tenant=TENANT_A)
    url = f"/api/interrupts/{iid}/decision"
    approve = {"decision": "approve", "plan_version": 2, "change_set_hash": H1}

    assert (await priya.post(url, json=approve)).status_code == 403  # planner: no quorum role
    # Lena (compliance) has no Merxa link and needs none
    r = await lena.post(url, json=approve)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["status"] == "recorded"
    assert body["quorum"]["remaining"] == 1 and body["quorum"]["missing_roles"] == ["approver"]
    assert [a["sub"] for a in body["quorum"]["approvals"]] == ["sub-lena"]
    # the case snapshot carries the progress, and the card was refreshed live
    snap = (await marco.get(f"/api/cases/{case_id}")).json()
    assert snap["open_interrupts"][0]["quorum"]["remaining"] == 1
    block = await repo.get_block(card["id"], tenant=TENANT_A)
    assert block is not None and block["envelope"]["props"]["quorum"]["approvals"][0]["name"] == "Lena Vogel"
    async with tenant_session(TENANT_A) as s:
        ev = (await s.execute(text("SELECT payload FROM run_event WHERE case_id = :c AND type = 'ACTIVITY_SNAPSHOT' "
                                   "ORDER BY id DESC LIMIT 1"), {"c": case_id})).scalar_one()
    assert ev["content"]["id"] == card["id"] and ev["metadata"]["sc"]["label"] == "1 of 2 approvals"

    # Marco executes: he must connect first
    r = await marco.post(url, json=approve)
    assert r.status_code == 412 and r.json()["code"] == "connect_required"
    await _link(get_token_broker(), merxa_realm, "sub-marco")
    r = await marco.post(url, json=approve)
    assert r.status_code == 202 and r.json()["status"] == "resumed", r.text
    assert r.json()["quorum"]["remaining"] == 0
    resume = await runs.get_run(TENANT_A, r.json()["run_id"])
    assert resume is not None
    assert [d["username"] for d in resume.payload["decided_by"]] == ["marco", "lena"]
    async with tenant_session(TENANT_A) as s:
        audit = (await s.execute(text("SELECT after FROM admin_audit WHERE target = :t ORDER BY id"),
                                 {"t": f"interrupt:{iid}"})).scalars().all()
    assert [a["outcome"] for a in audit] == ["recorded", "resolved"]


async def test_api_executor_must_still_be_linked_when_compliance_completes(
        people: dict[str, Browser], merxa_realm: FakeRealm) -> None:
    priya, marco, lena = people["priya"], people["marco"], people["lena"]
    await _link(get_token_broker(), merxa_realm, "sub-marco")
    case_id = (await priya.post("/api/cases", json={})).json()["id"]
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "q2"})
    iid = await _approval(case_id, msg.json()["run_id"])
    url = f"/api/interrupts/{iid}/decision"
    approve = {"decision": "approve", "plan_version": 2, "change_set_hash": H1}
    assert (await marco.post(url, json=approve)).json()["status"] == "recorded"
    await get_token_broker().revoke_user_link(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco")
    r = await lena.post(url, json=approve)
    assert r.status_code == 412 and r.json()["code"] == "executor_connect_required"
    assert r.json()["executor"]["username"] == "marco"
    intr = await itr.get_interrupt(TENANT_A, iid)
    assert intr is not None and intr["status"] == "PENDING"  # Lena can approve once Marco reconnects
    await _link(get_token_broker(), merxa_realm, "sub-marco")
    r = await lena.post(url, json=approve)
    assert r.status_code == 202 and r.json()["status"] == "resumed"


async def test_api_reject_reason(people: dict[str, Browser]) -> None:
    priya, lena = people["priya"], people["lena"]
    case_id = (await priya.post("/api/cases", json={})).json()["id"]
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "q3"})
    iid = await _approval(case_id, msg.json()["run_id"])
    card = _card(case_id, iid)
    await repo.save_block(case_id, msg.json()["run_id"], card, tenant=TENANT_A)
    url = f"/api/interrupts/{iid}/decision"
    r = await lena.post(url, json={"decision": "approve", "plan_version": 2, "change_set_hash": H1, "reason": "x"})
    assert r.status_code == 422
    r = await lena.post(url, json={"decision": "reject", "reason": "x" * 501})
    assert r.status_code == 422
    r = await lena.post(url, json={"decision": "reject", "reason": "Wrong supplier"})
    assert r.status_code == 202 and r.json()["status"] == "resumed"
    resume = await runs.get_run(TENANT_A, r.json()["run_id"])
    assert resume is not None and resume.payload["reason"] == "Wrong supplier"
    block = await repo.get_block(card["id"], tenant=TENANT_A)
    assert block is not None
    rejection = block["envelope"]["props"]["rejection"]
    assert rejection["name"] == "Lena Vogel" and rejection["reason"] == "Wrong supplier"
    assert rejection["role"] == "compliance" and "quorum" not in block["envelope"]["props"]
