"""Runs queue: enqueue/idempotency, claim with per-case lease, heartbeat, reaper, cancel."""

from __future__ import annotations

import asyncio

import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_env import TENANT_A
from keelson_contracts.runs import RUNS_WAKE_STREAM
from keelson_engine.core import repo, runs
from keelson_engine.core.db import system_session
from keelson_engine.core.valkey import get_valkey

pytestmark = pytest.mark.asyncio(loop_scope="session")

ACTOR = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}


@pytest_asyncio.fixture(loop_scope="session")
async def clean_queue(platform: None) -> None:
    """Other tests leave queued runs behind; claim tests need an empty queue."""
    async with system_session() as s:
        await s.execute(text("UPDATE run SET status = 'cancelled' WHERE status IN ('queued', 'running')"))


async def _case() -> str:
    return await repo.create_case("runs", created_by=ACTOR, tenant=TENANT_A)


async def _chat(case_id: str, text_: str = "hello", key: str | None = None) -> str:
    return await runs.enqueue_run(tenant=TENANT_A, case_id=case_id, kind="chat", payload={"text": text_},
                                  actor=ACTOR, idempotency_key=key, user_message=text_)


async def test_enqueue_dedupes_on_idempotency_key(platform: None) -> None:
    case_id = await _case()
    r1 = await _chat(case_id, "hi", "k-1")
    r2 = await _chat(case_id, "hi", "k-1")
    assert r1 == r2
    assert len(await repo.list_messages(case_id, tenant=TENANT_A)) == 1
    with pytest.raises(runs.IdempotencyConflict):
        await _chat(case_id, "different text", "k-1")
    r3 = await _chat(case_id, "hi", "k-2")
    assert r3 != r1
    row = await runs.get_run(TENANT_A, r1)
    assert row is not None and row.status == "queued" and row.session_id == r1 and row.kind == "chat"
    assert row.request().payload.text == "hi"  # type: ignore[union-attr]


async def test_concurrent_enqueue_same_key_creates_one_run(platform: None) -> None:
    case_id = await _case()
    ids = await asyncio.gather(*[_chat(case_id, "same", "k-race") for _ in range(5)])
    assert len(set(ids)) == 1


async def test_enqueue_validates_payload(platform: None) -> None:
    case_id = await _case()
    with pytest.raises(ValueError):
        await runs.enqueue_run(tenant=TENANT_A, case_id=case_id, kind="action", payload={"type": "explode"},
                               actor=ACTOR, idempotency_key=None)


async def test_enqueue_wakes_workers(platform: None) -> None:
    case_id = await _case()
    before = await get_valkey().xlen(RUNS_WAKE_STREAM)
    await _chat(case_id)
    assert await get_valkey().xlen(RUNS_WAKE_STREAM) == before + 1


async def test_claim_fifo_and_one_running_run_per_case(clean_queue: None) -> None:
    c1, c2 = await _case(), await _case()
    a1 = await _chat(c1, "a1")
    a2 = await _chat(c1, "a2")
    b1 = await _chat(c2, "b1")
    first = await runs.claim_next_run("w1")
    second = await runs.claim_next_run("w2")
    third = await runs.claim_next_run("w3")
    assert first is not None and first.id == a1 and first.attempt == 1 and first.lease_owner == "w1"
    assert second is not None and second.id == b1  # a2 must wait for a1 (same case)
    assert third is None
    await runs.finish_run(a1, "succeeded")
    nxt = await runs.claim_next_run("w3")
    assert nxt is not None and nxt.id == a2


async def test_concurrent_claims_never_double_start_a_case(clean_queue: None) -> None:
    case_ids = [await _case() for _ in range(3)]
    for cid in case_ids:
        for i in range(3):
            await _chat(cid, f"m{i}")
    claimed = await asyncio.gather(*[runs.claim_next_run(f"w{i}") for i in range(8)])
    got = [c for c in claimed if c is not None]
    assert len(got) == 3
    assert sorted(c.case_id for c in got) == sorted(case_ids)


async def test_heartbeat_requires_lease_owner(clean_queue: None) -> None:
    case_id = await _case()
    rid = await _chat(case_id)
    run = await runs.claim_next_run("w1", lease_seconds=30)
    assert run is not None and run.id == rid
    assert await runs.heartbeat(rid, "w1") is True
    assert await runs.heartbeat(rid, "intruder") is False
    await runs.finish_run(rid, "paused", invocation_id="inv-1")
    assert await runs.heartbeat(rid, "w1") is False
    row = await runs.get_run(TENANT_A, rid)
    assert row is not None and row.status == "paused" and row.invocation_id == "inv-1"


async def _expire(run_id: str) -> None:
    async with system_session() as s:
        await s.execute(text("UPDATE run SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
                        {"id": run_id})


async def test_reaper_requeues_as_crash_retry_keeping_invocation(clean_queue: None) -> None:
    case_id = await _case()
    rid = await _chat(case_id)
    run = await runs.claim_next_run("w-dead")
    assert run is not None
    await runs.set_run_invocation(rid, "inv-xyz")
    await _expire(rid)
    assert await runs.reap_expired(max_attempts=3) == [rid]
    retry = await runs.claim_next_run("w-new")
    assert retry is not None and retry.id == rid
    assert retry.kind == "crash_retry" and retry.origin_kind == "chat"
    assert retry.invocation_id == "inv-xyz" and retry.session_id == rid and retry.attempt == 2
    assert retry.request().kind == "chat"
    # the dead worker can no longer heartbeat
    assert await runs.heartbeat(rid, "w-dead") is False


async def test_reaper_gives_up_after_max_attempts(clean_queue: None) -> None:
    case_id = await _case()
    rid = await _chat(case_id)
    for attempt in range(1, 4):
        run = await runs.claim_next_run(f"w{attempt}")
        assert run is not None and run.attempt == attempt
        await _expire(rid)
        requeued = await runs.reap_expired(max_attempts=3)
        assert requeued == ([rid] if attempt < 3 else [])
    row = await runs.get_run(TENANT_A, rid)
    assert row is not None and row.status == "failed" and "lease expired" in (row.error or "")
    async with system_session() as s:
        types = [r[0] for r in (await s.execute(
            text("SELECT type FROM run_event WHERE run_id = :r ORDER BY id"), {"r": rid})).all()]
    assert types == ["RUN_ERROR"]


async def test_cancel_queued_and_running(clean_queue: None) -> None:
    case_id = await _case()
    running = await _chat(case_id, "first")
    queued = await _chat(case_id, "second")
    claimed = await runs.claim_next_run("w1")
    assert claimed is not None and claimed.id == running
    assert await runs.request_cancel(TENANT_A, queued) == ("queued", "cancelled")
    assert await runs.request_cancel(TENANT_A, running) == ("running", "running")
    assert await runs.is_cancel_requested(running) is True
    assert await runs.is_cancel_requested(queued) is True
    assert await runs.request_cancel(TENANT_A, queued) == ("cancelled", "cancelled")
    await runs.finish_run(running, "cancelled", error="cancelled by user")
    assert await runs.claim_next_run("w2") is None  # cancelled runs are never claimed
    with pytest.raises(runs.RunNotFound):
        await runs.request_cancel(TENANT_A, "no-such-run")


async def test_finish_run_validates_status(platform: None) -> None:
    with pytest.raises(ValueError):
        await runs.finish_run("x", "running")  # type: ignore[arg-type]


async def test_finish_run_never_overwrites_final_state_or_foreign_lease(clean_queue: None) -> None:
    """Regression: a zombie worker (lease reaped) must not clear another worker's lease or turn
    a reaper-failed run into 'succeeded'."""
    case_id = await _case()
    rid = await _chat(case_id)
    assert (await runs.claim_next_run("w-zombie")) is not None
    await _expire(rid)
    assert await runs.reap_expired(max_attempts=3) == [rid]
    retry = await runs.claim_next_run("w-live")
    assert retry is not None and retry.id == rid
    # the zombie reports completion with its worker id: ignored, w-live keeps the lease
    assert await runs.finish_run(rid, "succeeded", worker_id="w-zombie") is False
    row = await runs.get_run(TENANT_A, rid)
    assert row is not None and row.status == "running" and row.lease_owner == "w-live"
    assert await runs.heartbeat(rid, "w-live") is True
    # the owner finishes; a later (late) finish of any kind changes nothing
    assert await runs.finish_run(rid, "paused", invocation_id="inv-1", worker_id="w-live") is True
    assert await runs.finish_run(rid, "failed", error="late") is False
    row = await runs.get_run(TENANT_A, rid)
    assert row is not None and row.status == "paused" and row.error is None and row.invocation_id == "inv-1"
