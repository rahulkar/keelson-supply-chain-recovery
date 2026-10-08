"""Cases, messages, actions, decisions, cancel and block data through the API (tenant + role checks)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_browser import Browser
from e1_env import TENANT_A
from e1_idp import FakeRealm
from keelson_contracts.blocks import make_block
from keelson_contracts.common import new_block_id
from keelson_contracts.runs import ResumePayload
from keelson_engine.core import connectors, interrupts, repo, runs
from keelson_engine.core.db import tenant_session
from keelson_engine.core.tokens import get_token_broker
from test_tokens import MERXA_SPEC, _link

pytestmark = pytest.mark.asyncio(loop_scope="session")

H1 = "ab" * 32


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any) -> dict[str, Browser]:
    return {name: await browser_factory(name) for name in ("priya", "marco", "lena", "ava", "nora", "sam")}


async def _new_case(b: Browser, title: str | None = None) -> str:
    resp = await b.post("/api/cases", json={"title": title} if title else {})
    assert resp.status_code == 201, resp.text
    return str(resp.json()["id"])


async def test_case_crud_and_tenant_isolation(people: dict[str, Browser]) -> None:
    marco, nora = people["marco"], people["nora"]
    case_id = await _new_case(marco, "Trim delay")
    listed = (await marco.get("/api/cases", params={"limit": 200})).json()["items"]
    assert case_id in {c["id"] for c in listed}
    item = next(c for c in listed if c["id"] == case_id)
    assert set(item) == {"id", "title", "mode", "status", "updated_at", "last_message_preview",
                         "last_signal_at"}  # M5 (95 §4): additive, "Updated" badge
    assert item["last_signal_at"] is None
    detail = (await marco.get(f"/api/cases/{case_id}")).json()
    assert detail["case"]["title"] == "Trim delay" and detail["case"]["created_by"] == "sub-marco"
    assert detail["messages"] == [] and detail["blocks"] == [] and detail["open_interrupts"] == []
    assert detail["last_event_seq"] == 0
    # other tenant: invisible
    assert (await nora.get(f"/api/cases/{case_id}")).status_code == 404
    assert case_id not in {c["id"] for c in (await nora.get("/api/cases")).json()["items"]}
    resp = await nora.post(f"/api/cases/{case_id}/messages", json={"text": "hi"}, headers={"Idempotency-Key": "n1"})
    assert resp.status_code == 404
    assert (await marco.get("/api/cases/does-not-exist")).status_code == 404


async def test_role_checks(people: dict[str, Browser]) -> None:
    lena, ava, marco = people["lena"], people["ava"], people["marco"]
    case_id = await _new_case(marco)
    assert (await lena.get(f"/api/cases/{case_id}")).status_code == 200  # compliance reads
    assert (await lena.post("/api/cases", json={})).status_code == 403
    resp = await lena.post(f"/api/cases/{case_id}/messages", json={"text": "x"}, headers={"Idempotency-Key": "l1"})
    assert resp.status_code == 403
    assert (await ava.get("/api/cases")).status_code == 403  # platform-admin has no tenant data access


async def test_messages_enqueue_runs_idempotently(people: dict[str, Browser]) -> None:
    priya = people["priya"]
    case_id = await _new_case(priya)
    missing = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "Trims are 7 days late"})
    assert missing.status_code == 400 and missing.json()["code"] == "idempotency_key_required"
    r1 = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "Trims are 7 days late"},
                          headers={"Idempotency-Key": "key-1"})
    assert r1.status_code == 202
    run_id = r1.json()["run_id"]
    r2 = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "Trims are 7 days late"},
                          headers={"Idempotency-Key": "key-1"})
    assert r2.status_code == 202 and r2.json()["run_id"] == run_id
    r3 = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "something else"},
                          headers={"Idempotency-Key": "key-1"})
    assert r3.status_code == 422 and r3.json()["code"] == "idempotency_key_reused"
    assert (await priya.post(f"/api/cases/{case_id}/messages", json={"text": ""},
                             headers={"Idempotency-Key": "key-2"})).status_code == 422
    detail = (await priya.get(f"/api/cases/{case_id}")).json()
    assert [m["text"] for m in detail["messages"]] == ["Trims are 7 days late"]
    assert detail["messages"][0]["run_id"] == run_id
    assert detail["case"]["title"] == "Trims are 7 days late"
    run = await runs.get_run(TENANT_A, run_id)
    assert run is not None and run.kind == "chat" and run.actor["username"] == "priya"
    assert run.actor["roles"] == ["planner"] and run.payload == {"text": "Trims are 7 days late"}


async def test_actions_require_current_plan(people: dict[str, Browser]) -> None:
    marco = people["marco"]
    case_id = await _new_case(marco)
    body = {"type": "apply", "option_id": "opt-A", "plan_version": 1}
    no_plan = await marco.post(f"/api/cases/{case_id}/actions", json=body, headers={"Idempotency-Key": "a1"})
    assert no_plan.status_code == 409 and no_plan.json()["code"] == "stale_plan"
    await repo.save_plan(case_id, 1, [{"id": "opt-A", "title": "Rebalance"}], "opt-A", {}, tenant=TENANT_A)
    await repo.save_plan(case_id, 2, [{"id": "opt-A", "title": "Rebalance"}], "opt-A", {}, tenant=TENANT_A)
    stale = await marco.post(f"/api/cases/{case_id}/actions", json=body, headers={"Idempotency-Key": "a2"})
    assert stale.status_code == 409 and stale.json()["current_plan_version"] == 2
    unknown = await marco.post(f"/api/cases/{case_id}/actions", json={**body, "plan_version": 2, "option_id": "x"},
                               headers={"Idempotency-Key": "a3"})
    assert unknown.status_code == 422
    ok = await marco.post(f"/api/cases/{case_id}/actions", json={**body, "plan_version": 2},
                          headers={"Idempotency-Key": "a4"})
    assert ok.status_code == 202
    run = await runs.get_run(TENANT_A, ok.json()["run_id"])
    assert run is not None and run.kind == "action"
    assert run.payload == {"type": "apply", "option_id": "opt-A", "plan_version": 2}
    bad = await marco.post(f"/api/cases/{case_id}/actions", json={**body, "type": "nuke"},
                           headers={"Idempotency-Key": "a5"})
    assert bad.status_code == 422


async def test_confirm_assumptions_action_carries_keys(people: dict[str, Browser]) -> None:
    """M4 (contract 90 §5): ``confirm_assumptions`` is accepted with its keys; keys are required for it
    and refused on the other action types."""
    priya = people["priya"]
    case_id = await _new_case(priya)
    await repo.save_plan(case_id, 1, [{"id": "opt-A", "title": "Rebalance"}], "opt-A", {}, tenant=TENANT_A)
    body = {"type": "confirm_assumptions", "option_id": "opt-A", "plan_version": 1, "assumption_keys": ["delay_days"]}
    ok = await priya.post(f"/api/cases/{case_id}/actions", json=body, headers={"Idempotency-Key": "c1"})
    assert ok.status_code == 202, ok.text
    run = await runs.get_run(TENANT_A, ok.json()["run_id"])
    assert run is not None and run.kind == "action"
    assert run.payload == {"type": "confirm_assumptions", "option_id": "opt-A", "plan_version": 1,
                           "assumption_keys": ["delay_days"]}
    missing = await priya.post(f"/api/cases/{case_id}/actions", json={**body, "assumption_keys": []},
                               headers={"Idempotency-Key": "c2"})
    assert missing.status_code == 422 and missing.json()["code"] == "invalid_action"
    extra = await priya.post(f"/api/cases/{case_id}/actions", json={**body, "type": "apply"},
                             headers={"Idempotency-Key": "c3"})
    assert extra.status_code == 422 and extra.json()["code"] == "invalid_action"
    dup = await priya.post(f"/api/cases/{case_id}/actions",
                           json={**body, "assumption_keys": ["delay_days", "delay_days"]},
                           headers={"Idempotency-Key": "c4"})
    assert dup.status_code == 422
    promote = await priya.post(f"/api/cases/{case_id}/actions",
                               json={"type": "promote", "option_id": "opt-A", "plan_version": 1},
                               headers={"Idempotency-Key": "c5"})
    assert promote.status_code == 202
    nora = await people["nora"].post(f"/api/cases/{case_id}/actions", json=body, headers={"Idempotency-Key": "c6"})
    assert nora.status_code == 404


async def _approval_interrupt(case_id: str, run_id: str) -> str:
    iid = f"approval-cs-{case_id}-1"
    await interrupts.create_interrupt(
        tenant=TENANT_A, case_id=case_id, run_id=run_id, kind="approval", interrupt_id=iid,
        payload={"message": "Approve 2 transfers", "connector": "merxa"},
        quorum_spec={"roles": ["approver"], "count": 1}, plan_version=3, change_set_hash=H1, invocation_id="inv-9",
    )
    return iid


async def test_approval_decision_flow(people: dict[str, Browser], merxa_realm: FakeRealm) -> None:
    priya, marco = people["priya"], people["marco"]
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    case_id = await _new_case(priya)
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "g"})
    iid = await _approval_interrupt(case_id, msg.json()["run_id"])
    detail = (await marco.get(f"/api/cases/{case_id}")).json()
    assert detail["open_interrupts"][0]["id"] == iid
    assert detail["open_interrupts"][0]["response_schema"]["required"] == ["decision"]

    url = f"/api/interrupts/{iid}/decision"
    approve = {"decision": "approve", "plan_version": 3, "change_set_hash": H1}
    resp = await priya.post(url, json=approve)  # planner without approver role
    assert resp.status_code == 403
    resp = await marco.post(url, json=approve)  # no Merxa link yet
    assert resp.status_code == 412
    assert resp.json()["code"] == "connect_required"
    assert resp.json()["connect_url"] == (
        f"http://keelson.localhost/api/connectors/merxa/connect?return_to=%2Fcases%2F{case_id}"
    )
    await _link(get_token_broker(), merxa_realm, "sub-marco")
    resp = await marco.post(url, json={**approve, "change_set_hash": "cd" * 32})
    assert resp.status_code == 409 and resp.json()["code"] == "stale"
    resp = await marco.post(url, json={**approve, "plan_version": 2})
    assert resp.status_code == 409
    resp = await marco.post(url, json={"decision": "confirm"})
    assert resp.status_code == 422
    resp = await marco.post(url, json=approve)
    assert resp.status_code == 202 and resp.json()["status"] == "resumed"
    resume = await runs.get_run(TENANT_A, resp.json()["run_id"])
    assert resume is not None and resume.kind == "resume" and resume.invocation_id == "inv-9"
    assert resume.payload["decided_by"][0]["username"] == "marco"
    # superseded/closed now
    resp = await marco.post(url, json={"decision": "reject"})
    assert resp.status_code == 409
    assert (await people["nora"].post(url, json=approve)).status_code == 404


async def test_superseded_interrupt_is_409(people: dict[str, Browser]) -> None:
    priya = people["priya"]
    case_id = await _new_case(priya)
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "s"})
    iid = f"approach-{case_id}-1"
    await interrupts.create_interrupt(tenant=TENANT_A, case_id=case_id, run_id=msg.json()["run_id"], kind="approach",
                                      interrupt_id=iid, payload={}, quorum_spec={}, plan_version=None,
                                      change_set_hash=None, invocation_id="inv")
    await interrupts.supersede_open(TENANT_A, case_id, "new message")
    resp = await priya.post(f"/api/interrupts/{iid}/decision", json={"decision": "confirm"})
    assert resp.status_code == 409 and resp.json()["code"] == "superseded"


async def test_approach_confirm_by_planner(people: dict[str, Browser]) -> None:
    priya = people["priya"]
    case_id = await _new_case(priya)
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "c"})
    iid = f"approach-{case_id}-1"
    await interrupts.create_interrupt(tenant=TENANT_A, case_id=case_id, run_id=msg.json()["run_id"], kind="approach",
                                      interrupt_id=iid, payload={}, quorum_spec={}, plan_version=None,
                                      change_set_hash=None, invocation_id="inv")
    resp = await priya.post(f"/api/interrupts/{iid}/decision", json={"decision": "confirm"})
    assert resp.status_code == 202 and resp.json()["status"] == "resumed"


async def _approach_interrupt(b: Browser, key: str) -> tuple[str, str]:
    case_id = await _new_case(b)
    msg = await b.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": key})
    iid = f"approach-{case_id}-1"
    await interrupts.create_interrupt(tenant=TENANT_A, case_id=case_id, run_id=msg.json()["run_id"], kind="approach",
                                      interrupt_id=iid, payload={}, quorum_spec={}, plan_version=None,
                                      change_set_hash=None, invocation_id="inv")
    return case_id, iid


async def test_typed_approach_edit_reaches_the_resume_payload(people: dict[str, Browser]) -> None:
    """M4 typed Edit (contract ``ResumePayload``): ``edits: [{key, value_int|value_ids}]`` and
    ``removed_checks`` are accepted, validated and carried into the resume run unchanged."""
    priya = people["priya"]
    _case, iid = await _approach_interrupt(priya, "te1")
    body = {"decision": "edit",
            "edits": [{"key": "delay_days", "value_int": 10}, {"key": "remove_receipts", "value_ids": ["PO-1#1"]}],
            "removed_checks": ["t2"]}
    resp = await priya.post(f"/api/interrupts/{iid}/decision", json=body)
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "resumed"
    run = await runs.get_run(TENANT_A, resp.json()["run_id"])
    assert run is not None and run.kind == "resume"
    resume = ResumePayload.model_validate(run.payload)
    assert isinstance(resume.edits, list)
    assert [e.model_dump(exclude_none=True) for e in resume.edits] == body["edits"]
    assert resume.removed_checks == ["t2"] and resume.decision == "edit"
    async with tenant_session(TENANT_A) as s:
        stored = (await s.execute(text("SELECT edits FROM approval_decision WHERE interrupt_id = :i"),
                                  {"i": iid})).scalar_one()
    assert json.loads(stored) == {"edits": body["edits"], "removed_checks": ["t2"]}
    # removed checks alone are an edit, too
    _case, iid2 = await _approach_interrupt(priya, "te2")
    only = await priya.post(f"/api/interrupts/{iid2}/decision", json={"decision": "edit", "removed_checks": ["t1"]})
    assert only.status_code == 202, only.text
    run2 = await runs.get_run(TENANT_A, only.json()["run_id"])
    assert run2 is not None and run2.payload["removed_checks"] == ["t1"] and run2.payload["edits"] is None


@pytest.mark.parametrize(("body", "code"), [
    ({"decision": "confirm", "edits": [{"key": "delay_days", "value_int": 3}]}, "invalid_decision"),
    ({"decision": "what_if", "removed_checks": ["t1"]}, "invalid_decision"),
    ({"decision": "edit", "edits": [{"key": "delay_days", "value_int": 3},
                                    {"key": "delay_days", "value_int": 4}]}, "invalid_decision"),
    ({"decision": "edit", "removed_checks": ["t1", "t1"]}, "invalid_decision"),
    ({"decision": "edit", "edits": [{"key": "delay_days", "value_int": 3, "value_ids": ["x"]}]}, "invalid_request"),
    ({"decision": "edit", "edits": [{"key": "Delay", "value_int": 3}]}, "invalid_request"),
    ({"decision": "edit", "edits": [{"key": "scope", "value_ids": []}]}, "invalid_request"),
    ({"decision": "edit", "edits": []}, "invalid_decision"),
    ({"decision": "edit", "removed_checks": [f"t{i}" for i in range(51)]}, "invalid_request"),
])
async def test_invalid_typed_edits_are_refused(people: dict[str, Browser], body: dict[str, Any], code: str) -> None:
    priya = people["priya"]
    case_id, iid = await _approach_interrupt(priya, f"bad-{uuid.uuid4().hex}")
    resp = await priya.post(f"/api/interrupts/{iid}/decision", json=body)
    assert resp.status_code == 422 and resp.json()["code"] == code, resp.text
    detail = (await priya.get(f"/api/cases/{case_id}")).json()
    assert [i["id"] for i in detail["open_interrupts"]] == [iid]  # nothing recorded, still open


async def test_confirm_assumptions_takes_up_to_50_keys(people: dict[str, Browser]) -> None:
    """``ActionBody.assumption_keys`` matches the contract / web limit (``MAX_PAYLOAD_KEYS`` = 50)."""
    priya = people["priya"]
    case_id = await _new_case(priya)
    await repo.save_plan(case_id, 1, [{"id": "opt-A", "title": "Rebalance"}], "opt-A", {}, tenant=TENANT_A)
    body = {"type": "confirm_assumptions", "option_id": "opt-A", "plan_version": 1,
            "assumption_keys": [f"key_{i}" for i in range(50)]}
    ok = await priya.post(f"/api/cases/{case_id}/actions", json=body, headers={"Idempotency-Key": "k50"})
    assert ok.status_code == 202, ok.text
    run = await runs.get_run(TENANT_A, ok.json()["run_id"])
    assert run is not None and len(run.payload["assumption_keys"]) == 50
    over = await priya.post(f"/api/cases/{case_id}/actions",
                            json={**body, "assumption_keys": [f"key_{i}" for i in range(51)]},
                            headers={"Idempotency-Key": "k51"})
    assert over.status_code == 422


async def test_cancel_run(people: dict[str, Browser]) -> None:
    priya, nora = people["priya"], people["nora"]
    case_id = await _new_case(priya)
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "x"})
    run_id = msg.json()["run_id"]
    assert (await nora.post(f"/api/runs/{run_id}/cancel")).status_code == 404
    assert (await people["lena"].post(f"/api/runs/{run_id}/cancel")).status_code == 403
    resp = await priya.post(f"/api/runs/{run_id}/cancel")
    assert resp.status_code == 202 and resp.json()["status"] == "cancelled"
    async with tenant_session(TENANT_A) as s:
        ev = (await s.execute(text("SELECT payload FROM run_event WHERE run_id = :r"), {"r": run_id})).scalar_one()
    assert ev["type"] == "RUN_ERROR" and ev["code"] == "CANCELLED"
    assert (await priya.post("/api/runs/unknown/cancel")).status_code == 404


async def test_block_data(people: dict[str, Browser]) -> None:
    marco, nora = people["marco"], people["nora"]
    case_id = await _new_case(marco)
    bid = new_block_id()
    env = make_block("narrative", {"markdown": "Hello"}, block_id=bid, title="Summary",
                     provenance={"kind": "inference", "engine": "keelson-compose@0.1", "as_of": "2026-10-05"})
    envelope = env.model_dump(mode="json", by_alias=True)
    await repo.save_block(case_id, None, {**envelope, "data_ref": f"/api/blocks/{bid}/data"},
                          data={"rows": [1, 2, 3]}, tenant=TENANT_A)
    resp = await marco.get(f"/api/blocks/{bid}/data")
    assert resp.status_code == 200 and resp.json() == {"rows": [1, 2, 3]}
    assert (await nora.get(f"/api/blocks/{bid}/data")).status_code == 404
    assert (await marco.get("/api/blocks/not-a-block/data")).status_code == 404
    bid2 = new_block_id()
    await repo.save_block(case_id, None, {**envelope, "id": bid2}, tenant=TENANT_A)
    assert (await marco.get(f"/api/blocks/{bid2}/data")).status_code == 404
    detail = (await marco.get(f"/api/cases/{case_id}")).json()
    assert [b["id"] for b in detail["blocks"]] == [bid, bid2]


async def test_approve_must_match_current_plan_and_change_set(
    people: dict[str, Browser], merxa_realm: FakeRealm
) -> None:
    """Regression: approving is checked against the case's *current* plan version and change set
    row, not only the values the card was created with."""
    priya, marco = people["priya"], people["marco"]
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    await _link(get_token_broker(), merxa_realm, "sub-marco")
    case_id = await _new_case(priya)
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "cp"})
    await repo.save_plan(case_id, 3, [{"id": "opt-A", "title": "Rebalance"}], "opt-A", tenant=TENANT_A)
    items = [{"seq": 1, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
              "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-19", "lines": []}}]
    cs_id = await repo.create_change_set(case_id, 3, "opt-A", "merxa", items, H1, tenant=TENANT_A)
    iid = f"approval-{cs_id}-1"
    await interrupts.create_interrupt(
        tenant=TENANT_A, case_id=case_id, run_id=msg.json()["run_id"], kind="approval", interrupt_id=iid,
        payload={"message": "Approve 1 transfer", "connector": "merxa", "change_set_id": cs_id},
        quorum_spec={"roles": ["approver"], "count": 1}, plan_version=3, change_set_hash=H1, invocation_id="inv-c",
    )
    url = f"/api/interrupts/{iid}/decision"
    approve = {"decision": "approve", "plan_version": 3, "change_set_hash": H1}

    # the change set row was revised after the card was shown
    await repo.update_change_set(cs_id, hash="ef" * 32, tenant=TENANT_A)
    resp = await marco.post(url, json=approve)
    assert resp.status_code == 409 and resp.json()["code"] == "stale"
    await repo.update_change_set(cs_id, hash=H1, tenant=TENANT_A)

    # a newer plan exists although the open approval was not superseded
    await repo.save_plan(case_id, 4, [{"id": "opt-A", "title": "Rebalance v4"}], "opt-A", tenant=TENANT_A)
    resp = await marco.post(url, json=approve)
    assert resp.status_code == 409 and resp.json()["code"] == "stale"
    intr = await interrupts.get_interrupt(TENANT_A, iid)
    assert intr is not None and intr["status"] == "PENDING"
    async with tenant_session(TENANT_A) as s:
        assert (await s.execute(text("SELECT count(*) FROM approval_decision WHERE interrupt_id = :i"),
                                {"i": iid})).scalar_one() == 0
    # rejecting a stale card is still allowed (it only stops the plan)
    resp = await marco.post(url, json={"decision": "reject"})
    assert resp.status_code == 202 and resp.json()["status"] == "resumed"
