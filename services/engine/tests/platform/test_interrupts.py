"""Interrupts: quorum, supersede, stale hash/plan rejection, resume run insertion."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from e1_env import TENANT_A, TENANT_B
from keelson_contracts.runs import ResumePayload
from keelson_engine.core import interrupts as itr
from keelson_engine.core import repo, runs
from keelson_engine.core.db import tenant_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRIYA = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}
MARCO = {"sub": "sub-marco", "username": "marco", "name": "Marco Rossi", "roles": ["planner", "approver"]}
ANNA = {"sub": "sub-anna", "username": "anna", "name": "Anna Approver", "roles": ["approver"]}
LENA = {"sub": "sub-lena", "username": "lena", "name": "Lena Vogel", "roles": ["compliance"]}
H1 = "1" * 64
H2 = "2" * 64


async def _paused_run() -> tuple[str, str]:
    case_id = await repo.create_case("interrupts", created_by=PRIYA, tenant=TENANT_A)
    run_id = await runs.enqueue_run(tenant=TENANT_A, case_id=case_id, kind="chat", payload={"text": "x"},
                                    actor=PRIYA, idempotency_key=None)
    return case_id, run_id


async def _approval(case_id: str, run_id: str, *, count: int = 1, exclude_initiator: bool = False,
                    suffix: str = "1") -> str:
    iid = f"approval-cs-{case_id}-{suffix}"
    await itr.create_interrupt(
        tenant=TENANT_A, case_id=case_id, run_id=run_id, kind="approval", interrupt_id=iid,
        payload={"message": "Approve 3 transfers", "connector": "merxa"},
        quorum_spec={"roles": ["approver"], "count": count, "exclude_initiator": exclude_initiator},
        plan_version=2, change_set_hash=H1, invocation_id="inv-1",
    )
    return iid


async def test_create_is_idempotent_and_listed(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    await _approval(case_id, run_id)  # crash-retried node re-creates: no error, no duplicate
    open_ = await itr.list_open_interrupts(TENANT_A, case_id)
    assert [i["id"] for i in open_] == [iid]
    assert open_[0]["message"] == "Approve 3 transfers"
    assert open_[0]["response_schema"]["properties"]["decision"]["enum"] == ["approve", "reject", "revise"]
    assert open_[0]["session_id"] == run_id and open_[0]["initiator_sub"] == PRIYA["sub"]
    assert await itr.list_open_interrupts(TENANT_B, case_id) == []
    with pytest.raises(ValueError):
        await itr.create_interrupt(tenant=TENANT_A, case_id=case_id, run_id=run_id, kind="approval",
                                   interrupt_id="approach-x", payload={}, quorum_spec={}, plan_version=1,
                                   change_set_hash=H1, invocation_id="i")


async def test_single_approver_resolves_and_enqueues_resume(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    out = await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve",
                                       plan_version=2, change_set_hash=H1)
    assert out.status == "resolved" and out.run_id
    resume = await runs.get_run(TENANT_A, out.run_id)
    assert resume is not None and resume.kind == "resume" and resume.status == "queued"
    assert resume.session_id == run_id and resume.invocation_id == "inv-1"
    payload = ResumePayload.model_validate(resume.payload)
    assert payload.decision == "approve" and payload.change_set_hash == H1 and payload.plan_version == 2
    assert [d.sub for d in payload.decided_by] == ["sub-marco"] and payload.decided_by[0].role == "approver"
    intr = await itr.get_interrupt(TENANT_A, iid)
    assert intr is not None and intr["status"] == "RESOLVED"
    async with tenant_session(TENANT_A) as s:
        rr = (await s.execute(text("SELECT run_id, decision FROM resume_request WHERE interrupt_id = :i"),
                              {"i": iid})).one()
    assert tuple(rr) == (out.run_id, "approve")
    # an identical retry of the resolving request is acknowledged with the same run
    again = await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve",
                                         plan_version=2, change_set_hash=H1)
    assert again == out
    # anyone else now gets "closed"
    with pytest.raises(itr.InterruptClosed):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=ANNA, decision="approve",
                                  plan_version=2, change_set_hash=H1)


async def test_quorum_of_two(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id, count=2)
    assert await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve",
                                     plan_version=2, change_set_hash=H1) == "recorded"
    # same person twice does not count twice
    assert await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve",
                                     plan_version=2, change_set_hash=H1) == "recorded"
    out = await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid, decider=ANNA, decision="approve",
                                       plan_version=2, change_set_hash=H1)
    assert out.status == "resolved"
    resume = await runs.get_run(TENANT_A, out.run_id or "")
    assert resume is not None
    assert [d["sub"] for d in resume.payload["decided_by"]] == ["sub-marco", "sub-anna"]


async def test_reject_resolves_immediately(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id, count=2)
    out = await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid, decider=ANNA, decision="reject")
    assert out.status == "resolved"
    resume = await runs.get_run(TENANT_A, out.run_id or "")
    assert resume is not None and resume.payload["decision"] == "reject"


async def test_stale_hash_or_plan_version_rejected(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id)
    with pytest.raises(itr.StaleDecision):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve",
                                  plan_version=2, change_set_hash=H2)
    with pytest.raises(itr.StaleDecision):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve",
                                  plan_version=3, change_set_hash=H1)
    with pytest.raises(itr.InvalidDecision):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve")
    with pytest.raises(itr.InvalidDecision):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="confirm")
    intr = await itr.get_interrupt(TENANT_A, iid)
    assert intr is not None and intr["status"] == "PENDING"


async def test_eligibility_and_initiator_exclusion(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id, exclude_initiator=True)
    with pytest.raises(itr.NotEligible):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=LENA, decision="approve",
                                  plan_version=2, change_set_hash=H1)
    initiator_as_approver = {**PRIYA, "roles": ["planner", "approver"]}
    with pytest.raises(itr.NotEligible):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=initiator_as_approver,
                                  decision="approve", plan_version=2, change_set_hash=H1)
    assert await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve",
                                     plan_version=2, change_set_hash=H1) == "resolved"


async def test_supersede_blocks_decisions(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = await _approval(case_id, run_id, suffix="a")
    iid2 = await _approval(case_id, run_id, suffix="b")
    assert await itr.supersede_open(TENANT_A, case_id, "plan v3") == 2
    assert await itr.supersede_open(TENANT_A, case_id, "plan v3") == 0
    with pytest.raises(itr.InterruptClosed) as exc:
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=MARCO, decision="approve",
                                  plan_version=2, change_set_hash=H1)
    assert exc.value.status == "superseded"
    intr = await itr.get_interrupt(TENANT_A, iid2)
    assert intr is not None and intr["superseded_reason"] == "plan v3"


async def test_approach_edit_and_cross_tenant(platform: None) -> None:
    case_id, run_id = await _paused_run()
    iid = f"approach-{case_id}-1"
    await itr.create_interrupt(tenant=TENANT_A, case_id=case_id, run_id=run_id, kind="approach", interrupt_id=iid,
                               payload={}, quorum_spec={}, plan_version=None, change_set_hash=None,
                               invocation_id="inv-a")
    with pytest.raises(itr.InterruptNotFound):
        await itr.record_decision(tenant=TENANT_B, interrupt_id=iid, decider=PRIYA, decision="confirm")
    with pytest.raises(itr.InvalidDecision):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=PRIYA, decision="edit")
    out = await itr.record_decision_ex(tenant=TENANT_A, interrupt_id=iid, decider=PRIYA, decision="edit",
                                       edits="Only the women's styles")
    assert out.status == "resolved"
    resume = await runs.get_run(TENANT_A, out.run_id or "")
    assert resume is not None and resume.payload["edits"] == "Only the women's styles"
    assert resume.payload["plan_version"] is None


async def test_typed_edits_and_removed_checks_resume(platform: None) -> None:
    """M4 typed Edit through ``record_decision_ex``: typed edits (models or their JSON form) and
    ``removed_checks`` reach the resume payload; they are refused with any other decision."""
    from keelson_contracts.runs import ApproachEdit

    case_id, run_id = await _paused_run()
    iid = f"approach-{case_id}-1"
    await itr.create_interrupt(tenant=TENANT_A, case_id=case_id, run_id=run_id, kind="approach", interrupt_id=iid,
                               payload={}, quorum_spec={}, plan_version=None, change_set_hash=None,
                               invocation_id="inv-t")
    with pytest.raises(itr.InvalidDecision):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=PRIYA, decision="confirm",
                                  edits=[ApproachEdit(key="delay_days", value_int=3)])
    with pytest.raises(itr.InvalidDecision):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=PRIYA, decision="cancel",
                                  removed_checks=["t1"])
    with pytest.raises(itr.InvalidDecision):
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=PRIYA, decision="edit", edits=[])
    with pytest.raises(itr.InvalidDecision):  # duplicate key: ResumePayload refuses, nothing stored
        await itr.record_decision(tenant=TENANT_A, interrupt_id=iid, decider=PRIYA, decision="edit",
                                  edits=[{"key": "delay_days", "value_int": 3}, {"key": "delay_days", "value_int": 4}])
    out = await itr.record_decision_ex(
        tenant=TENANT_A, interrupt_id=iid, decider=PRIYA, decision="edit",
        edits=[ApproachEdit(key="delay_days", value_int=12), {"key": "scope", "value_ids": ["SUEZ"]}],
        removed_checks=["t3"],
    )
    assert out.status == "resolved"
    resume_run = await runs.get_run(TENANT_A, out.run_id or "")
    assert resume_run is not None
    resume = ResumePayload.model_validate(resume_run.payload)
    assert resume.edits == [ApproachEdit(key="delay_days", value_int=12), ApproachEdit(key="scope", value_ids=["SUEZ"])]
    assert resume.removed_checks == ["t3"]
