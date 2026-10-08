"""Ops hardening regressions (platform): runId on API-emitted run events, generic errors with an
error id, consistent case snapshots, Disconnect-Merxa (RFC 7009) and the viewer-based approval
pre-flight."""

from __future__ import annotations

import re
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_browser import Browser
from e1_env import PUBLIC, TENANT_A
from e1_idp import FakeRealm
from keelson_contracts.agui import ScMetadata, Stage, run_finished_interrupt
from keelson_engine.core import connectors, interrupts, repo, runs
from keelson_engine.core.db import system_session, tenant_session
from keelson_engine.core.events import get_event_bus
from keelson_engine.core.tokens import ConnectRequired, get_token_broker
from test_tokens import MERXA_SPEC, _link

pytestmark = pytest.mark.asyncio(loop_scope="session")

SECRET = "postgresql://keelson:hunter2@pg-core:5432/keelson"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
H1 = "ab" * 32


@pytest_asyncio.fixture(loop_scope="session")
async def merxa(platform: None) -> None:
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)


async def _unlink(sub: str) -> None:
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("DELETE FROM connector_identity_link WHERE keelson_sub = :k"), {"k": sub})
        await s.execute(text("DELETE FROM connector_credential WHERE principal_sub = :k"), {"k": sub})


async def _events(run_id: str) -> list[dict[str, Any]]:
    async with tenant_session(TENANT_A) as s:
        rows = (await s.execute(text("SELECT payload FROM run_event WHERE run_id = :r ORDER BY id"),
                                {"r": run_id})).all()
    return [dict(r[0]) for r in rows]


# ───────────────────────────── runId + generic errors ─────────────────────────────


async def test_api_cancel_run_error_names_its_run(browser_factory: Any) -> None:
    priya: Browser = await browser_factory("priya")
    case_id = (await priya.post("/api/cases", json={})).json()["id"]
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "rid"})
    run_id = msg.json()["run_id"]
    assert (await priya.post(f"/api/runs/{run_id}/cancel")).status_code == 202
    [event] = await _events(run_id)
    assert event["type"] == "RUN_ERROR" and event["code"] == "CANCELLED"
    assert event["metadata"]["sc"]["runId"] == run_id
    assert ScMetadata.model_validate(event["metadata"]["sc"]).run_id == run_id


async def test_reaper_give_up_is_generic_with_error_id(platform: None) -> None:
    async with system_session() as s:
        await s.execute(text("UPDATE run SET status = 'cancelled' WHERE status IN ('queued', 'running')"))
    actor = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}
    case_id = await repo.create_case("reaper", created_by=actor, tenant=TENANT_A)
    rid = await runs.enqueue_run(tenant=TENANT_A, case_id=case_id, kind="chat", payload={"text": "x"}, actor=actor,
                                 idempotency_key=None, user_message="x")
    for attempt in range(1, 4):
        assert await runs.claim_next_run(f"w{attempt}") is not None
        async with system_session() as s:
            await s.execute(text("UPDATE run SET lease_expires_at = now() - interval '1 second' WHERE id = :r"),
                            {"r": rid})
        await runs.reap_expired(max_attempts=3)
    [event] = await _events(rid)
    assert event["code"] == "CRASH_LIMIT" and event["metadata"]["sc"]["runId"] == rid
    error_id = event["metadata"]["errorId"]
    assert UUID_RE.fullmatch(error_id) and error_id in event["message"]
    assert "lease expired" not in event["message"] and "attempts" not in event["message"]


async def test_unhandled_api_exception_is_generic_500(platform: None, browser_factory: Any,
                                                      monkeypatch: pytest.MonkeyPatch) -> None:
    from keelson_engine.api.main import create_app

    marco: Browser = await browser_factory("marco")

    async def broken(*_: Any, **__: Any) -> Any:
        raise RuntimeError(f"could not connect to {SECRET}")

    monkeypatch.setattr(repo, "list_cases", broken)
    transport = httpx.ASGITransport(app=create_app(manage_resources=False), raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=PUBLIC) as client:
        resp = await client.get("/api/cases", headers={"Cookie": f"keelson_session={marco.cookie}"})
    assert resp.status_code == 500
    body = resp.json()
    assert body["code"] == "internal_error" and UUID_RE.fullmatch(body["error_id"])
    assert body["error_id"] in body["message"]
    assert "hunter2" not in resp.text and "RuntimeError" not in resp.text and "Traceback" not in resp.text


# ───────────────────────────── case snapshot ─────────────────────────────


async def test_case_snapshot_is_consistent_with_its_cursor(browser_factory: Any,
                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: an interrupt (and its RUN_FINISHED) committed while GET /api/cases/{id} runs is
    either in the snapshot, or after ``last_event_seq`` (so the SSE replay delivers it) — never
    missing from both."""
    priya: Browser = await browser_factory("priya")
    case_id = (await priya.post("/api/cases", json={})).json()["id"]
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "snap"})
    run_id = msg.json()["run_id"]
    iid = f"approach-{case_id}-1"
    written: dict[str, int] = {}
    original = interrupts.open_interrupts_in

    async def racing_read(session: Any, cid: str) -> list[dict[str, Any]]:
        # the worker pauses the run between the snapshot's first read and this one
        await interrupts.create_interrupt(
            tenant=TENANT_A, case_id=case_id, run_id=run_id, kind="approach", interrupt_id=iid,
            payload={"message": "Confirm?"}, quorum_spec={}, plan_version=None, change_set_hash=None,
            invocation_id="inv",
        )
        sc = ScMetadata(tenant_id=TENANT_A, case_id=case_id, run_id=run_id, stage=Stage.UNDERSTANDING,
                        prov="decision", label="Waiting")
        event = run_finished_interrupt(sc, thread_id=case_id, run_id=run_id, interrupts=[
            {"id": iid, "reason": "approach", "message": "Confirm?", "response_schema": {"type": "object"}}])
        written["seq"] = await get_event_bus().append(tenant=TENANT_A, case_id=case_id, run_id=run_id, event=event)
        return await original(session, cid)

    monkeypatch.setattr(interrupts, "open_interrupts_in", racing_read)
    detail = (await priya.get(f"/api/cases/{case_id}")).json()
    monkeypatch.setattr(interrupts, "open_interrupts_in", original)
    in_snapshot = iid in [i["id"] for i in detail["open_interrupts"]]
    assert in_snapshot or detail["last_event_seq"] < written["seq"]
    # REPEATABLE READ: the whole response is one snapshot taken before the concurrent commit
    assert not in_snapshot and detail["last_event_seq"] < written["seq"]
    again = (await priya.get(f"/api/cases/{case_id}")).json()
    assert iid in [i["id"] for i in again["open_interrupts"]] and again["last_event_seq"] >= written["seq"]


# ───────────────────────────── Disconnect Merxa ─────────────────────────────


async def test_disconnect_revokes_at_idp_and_locally(merxa: None, browser_factory: Any,
                                                     merxa_realm: FakeRealm) -> None:
    marco: Browser = await browser_factory("marco")
    await _unlink("sub-marco")
    broker = get_token_broker()
    await _link(broker, merxa_realm, "sub-marco")
    async with tenant_session(TENANT_A) as s:
        assert (await s.execute(text("SELECT count(*) FROM connector_credential WHERE principal_sub = 'sub-marco' "
                                     "AND refresh_token_enc IS NOT NULL"))).scalar_one() == 1
    live_tokens = set(merxa_realm.refresh_tokens)
    assert (await marco.get("/api/connectors/merxa/link")).json()["apply_ready"] is True

    # CSRF/Origin are required like any mutation
    no_csrf = await marco.client.delete("/api/connectors/merxa/link",
                                        headers={"Cookie": f"keelson_session={marco.cookie}"})
    assert no_csrf.status_code in (401, 403)

    resp = await marco.delete("/api/connectors/merxa/link")
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"status": "missing", "revoked": True, "idp_revocation": "revoked"}
    [call] = [c for c in merxa_realm.calls if c["grant_type"] == "revoke"][-1:]
    form = call["form"]
    assert form["client_id"] == "keelson-connect" and form["token_type_hint"] == "refresh_token"
    assert form["client_assertion_type"].endswith("jwt-bearer") and form["client_assertion"]
    assert form["token"] in live_tokens and form["token"] in merxa_realm.revoked_refresh

    link = (await marco.get("/api/connectors/merxa/link")).json()
    assert link == {"status": "missing", "ext_username": None, "roles": [], "scopes": [], "apply_ready": False}
    async with tenant_session(TENANT_A) as s:
        cred = (await s.execute(text("SELECT refresh_token_enc, access_token_enc FROM connector_credential "
                                     "WHERE principal_sub = 'sub-marco'"))).one()
        status = (await s.execute(text("SELECT status FROM connector_identity_link "
                                       "WHERE keelson_sub = 'sub-marco'"))).scalar_one()
        audit = (await s.execute(text("SELECT count(*) FROM admin_audit WHERE action = 'connector.unlink' "
                                      "AND actor_sub = 'sub-marco'"))).scalar_one()
    assert cred == (None, None) and status == "revoked" and audit >= 1
    with pytest.raises(ConnectRequired):
        await broker.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco")

    # idempotent; nothing left to revoke at the IdP
    again = await marco.delete("/api/connectors/merxa/link")
    assert again.json() == {"status": "missing", "revoked": False, "idp_revocation": "not_needed"}

    # re-connecting makes the link active again
    await _link(broker, merxa_realm, "sub-marco")
    assert (await marco.get("/api/connectors/merxa/link")).json()["status"] == "active"


async def test_disconnect_still_revokes_locally_when_idp_fails(merxa: None, browser_factory: Any,
                                                               merxa_realm: FakeRealm) -> None:
    sam: Browser = await browser_factory("sam")
    await _unlink("sub-sam")
    await _link(get_token_broker(), merxa_realm, "sub-sam", username="priya.shah")
    merxa_realm.revocation_status = 503
    try:
        resp = await sam.delete("/api/connectors/merxa/link")
    finally:
        merxa_realm.revocation_status = 200
    assert resp.status_code == 200 and resp.json()["idp_revocation"] == "failed" and resp.json()["revoked"] is True
    assert (await sam.get("/api/connectors/merxa/link")).json()["status"] == "missing"
    nora: Browser = await browser_factory("nora")
    assert (await nora.delete("/api/connectors/merxa/link")).status_code == 404  # other tenant: no connector


# ───────────────────────────── approval pre-flight is per viewer ─────────────────────────────


async def test_approval_preflight_uses_the_deciders_link(merxa: None, browser_factory: Any,
                                                         merxa_realm: FakeRealm) -> None:
    """The approval card's connect.status describes the initiator at card creation; the decision
    API (412) and GET …/link (apply_ready) answer for the person deciding."""
    priya: Browser = await browser_factory("priya")
    marco: Browser = await browser_factory("marco")
    await _unlink("sub-priya")
    await _unlink("sub-marco")
    broker = get_token_broker()
    await _link(broker, merxa_realm, "sub-priya", username="priya.shah")  # initiator linked
    case_id = (await priya.post("/api/cases", json={})).json()["id"]
    msg = await priya.post(f"/api/cases/{case_id}/messages", json={"text": "go"}, headers={"Idempotency-Key": "pf"})
    iid = f"approval-cs-{case_id}-1"
    await interrupts.create_interrupt(
        tenant=TENANT_A, case_id=case_id, run_id=msg.json()["run_id"], kind="approval", interrupt_id=iid,
        payload={"message": "Approve", "connector": "merxa", "connect": {"status": "active"}},
        quorum_spec={"roles": ["approver"], "count": 1}, plan_version=3, change_set_hash=H1, invocation_id="inv-pf",
    )
    assert (await priya.get("/api/connectors/merxa/link")).json()["apply_ready"] is True
    viewer = (await marco.get("/api/connectors/merxa/link")).json()
    assert viewer["status"] == "missing" and viewer["apply_ready"] is False
    approve = {"decision": "approve", "plan_version": 3, "change_set_hash": H1}
    resp = await marco.post(f"/api/interrupts/{iid}/decision", json=approve)
    assert resp.status_code == 412 and resp.json()["code"] == "connect_required"
    assert resp.json()["link_status"] == "missing"

    # active link without changes:apply → still not ready, still 412 (same rule)
    await _link(broker, merxa_realm, "sub-marco")
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE connector_identity_link SET scopes = '[\"openid\"]'::jsonb "
                             "WHERE keelson_sub = 'sub-marco'"))
    viewer = (await marco.get("/api/connectors/merxa/link")).json()
    assert viewer["status"] == "active" and viewer["apply_ready"] is False
    assert (await marco.post(f"/api/interrupts/{iid}/decision", json=approve)).status_code == 412

    await _link(broker, merxa_realm, "sub-marco")
    assert (await marco.get("/api/connectors/merxa/link")).json()["apply_ready"] is True
    resp = await marco.post(f"/api/interrupts/{iid}/decision", json=approve)
    assert resp.status_code == 202 and resp.json()["status"] == "resumed"


async def test_refresh_tools_failure_is_generic(merxa: None, browser_factory: Any) -> None:
    sam: Browser = await browser_factory("sam")

    async def lister(url: str, bearer: str) -> list[dict[str, Any]]:
        raise RuntimeError(f"connect to {SECRET} failed")

    connectors.set_tool_lister(lister)
    try:
        resp = await sam.post("/api/connectors/merxa/refresh-tools")
    finally:
        connectors.set_tool_lister(None)
    assert resp.status_code == 502 and resp.json()["code"] == "connector_unreachable"
    assert UUID_RE.fullmatch(resp.json()["error_id"]) and "hunter2" not in resp.text
