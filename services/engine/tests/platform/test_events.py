"""EventBus: persisted seq, replay ordering, live tail, live-only events, reasoning coalescing."""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from e1_env import TENANT_A, TENANT_B
from keelson_contracts.agui import (
    ScMetadata,
    Stage,
    reasoning_message_content,
    reasoning_message_end,
    reasoning_message_start,
    run_started,
    step_started,
)
from keelson_contracts.runs import case_stream_key
from keelson_engine.core import repo
from keelson_engine.core.events import EventBus
from keelson_engine.core.valkey import get_valkey

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _sc(case_id: str, tenant: str = TENANT_A, label: str = "x") -> ScMetadata:
    return ScMetadata(tenant_id=tenant, case_id=case_id, stage=Stage.UNDERSTANDING, prov="fact", label=label)


async def _case(tenant: str = TENANT_A) -> str:
    return await repo.create_case("events", created_by="u", tenant=tenant)


def _step(case_id: str, n: int) -> dict[str, Any]:
    return step_started(_sc(case_id, label=f"step {n}"), stage=Stage.UNDERSTANDING)


async def _collect(it: Any, n: int, timeout: float = 5.0) -> list[tuple[int, dict[str, Any]]]:
    out: list[tuple[int, dict[str, Any]]] = []

    async def run() -> None:
        async for item in it:
            out.append(item)
            if len(out) >= n:
                return

    await asyncio.wait_for(run(), timeout)
    return out


async def test_append_persists_with_monotonic_seq(platform: None) -> None:
    bus = EventBus()
    case_id = await _case()
    event = run_started(_sc(case_id), thread_id=case_id, run_id="run-1")
    seqs = [await bus.append(tenant=TENANT_A, case_id=case_id, run_id="run-1", event=event)]
    for i in range(3):
        seqs.append(await bus.append(tenant=TENANT_A, case_id=case_id, run_id="run-1", event=_step(case_id, i)))
    assert seqs == sorted(seqs) and len(set(seqs)) == 4 and seqs[0] > 0
    assert "seq" not in event["metadata"]["sc"] or event["metadata"]["sc"]["seq"] is None  # caller dict untouched
    replayed = [(s, e) async for s, e in bus.replay(tenant=TENANT_A, case_id=case_id, after=0)]
    assert [s for s, _ in replayed] == seqs
    for seq, ev in replayed:
        assert ev["metadata"]["sc"]["seq"] == seq
    assert await bus.last_seq(tenant=TENANT_A, case_id=case_id) == seqs[-1]
    # replay after a cursor
    tail = [s async for s, _ in bus.replay(tenant=TENANT_A, case_id=case_id, after=seqs[1])]
    assert tail == seqs[2:]


async def test_append_rejects_mismatched_or_invalid_events(platform: None) -> None:
    bus = EventBus()
    case_id = await _case()
    with pytest.raises(ValueError):
        await bus.append(tenant=TENANT_B, case_id=case_id, run_id=None, event=_step(case_id, 1))
    with pytest.raises(ValueError):
        await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event={"type": "RUN_STARTED"})
    bad = _step(case_id, 1)
    bad["bogus"] = 1
    with pytest.raises(ValueError):
        await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event=bad)


async def test_replay_is_tenant_scoped(platform: None) -> None:
    bus = EventBus()
    case_id = await _case()
    await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event=_step(case_id, 1))
    assert [x async for x in bus.replay(tenant=TENANT_B, case_id=case_id, after=0)] == []


async def test_replay_then_tail_orders_replayed_and_live(platform: None) -> None:
    bus = EventBus(block_ms=200)
    case_id = await _case()
    first = [await bus.append(tenant=TENANT_A, case_id=case_id, run_id="r", event=_step(case_id, i)) for i in range(3)]
    it = bus.replay_then_tail(tenant=TENANT_A, case_id=case_id, after=first[0])
    got: list[tuple[int, dict[str, Any]]] = []

    async def consume() -> None:
        async for item in it:
            got.append(item)
            if len(got) == 4:
                return

    task = asyncio.create_task(consume())
    await asyncio.sleep(0.3)
    later = [await bus.append(tenant=TENANT_A, case_id=case_id, run_id="r", event=_step(case_id, 10 + i))
             for i in range(2)]
    await asyncio.wait_for(task, 5)
    assert [s for s, _ in got] == first[1:] + later
    assert [e["metadata"]["sc"]["label"] for _, e in got] == ["step 1", "step 2", "step 10", "step 11"]


async def test_tail_falls_back_to_postgres_when_publish_is_lost(platform: None) -> None:
    """A persisted event whose XADD never happened still reaches subscribers (idle poll)."""
    bus = EventBus(block_ms=200)
    case_id = await _case()
    it = bus.replay_then_tail(tenant=TENANT_A, case_id=case_id, after=None)
    task = asyncio.create_task(_collect(it, 1))
    await asyncio.sleep(0.1)
    seq = await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event=_step(case_id, 1), live=False)
    got = await asyncio.wait_for(task, 5)
    assert got[0][0] == seq


async def test_live_only_events_have_no_seq_and_are_not_persisted(platform: None) -> None:
    bus = EventBus(block_ms=200)
    case_id = await _case()
    it = bus.replay_then_tail(tenant=TENANT_A, case_id=case_id, after=None)
    task = asyncio.create_task(_collect(it, 2))
    await asyncio.sleep(0.1)
    sc = _sc(case_id)
    zero = await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None,
                            event=reasoning_message_content(sc, message_id="m1", delta="thinking"), persist=False)
    seq = await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event=_step(case_id, 2))
    got = await asyncio.wait_for(task, 5)
    assert zero == 0
    assert got[0][0] == 0 and got[0][1]["type"] == "REASONING_MESSAGE_CONTENT"
    assert got[1][0] == seq
    assert [s async for s, _ in bus.replay(tenant=TENANT_A, case_id=case_id, after=0)] == [seq]


async def test_stream_is_trimmed_and_expires(platform: None) -> None:
    bus = EventBus(stream_maxlen=5, stream_ttl_s=100)
    case_id = await _case()
    for i in range(30):
        await bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event=_step(case_id, i))
    valkey = get_valkey()
    key = case_stream_key(case_id)
    assert await valkey.xlen(key) < 30
    assert 0 < await valkey.ttl(key) <= 100
    entries = await valkey.xrange(key, count=1)
    assert json.loads(entries[0][1]["data"])["type"] == "STEP_STARTED"


async def test_reasoning_coalescer_persists_aggregate_once(platform: None) -> None:
    bus = EventBus(block_ms=200)
    case_id = await _case()
    co = bus.coalescer()
    sc = _sc(case_id)
    await co.append(tenant=TENANT_A, case_id=case_id, run_id="r", event=reasoning_message_start(sc, message_id="m"))
    for part in ("Looking ", "at ", "POs"):
        assert await co.append(tenant=TENANT_A, case_id=case_id, run_id="r",
                               event=reasoning_message_content(sc, message_id="m", delta=part)) == 0
    await co.append(tenant=TENANT_A, case_id=case_id, run_id="r", event=reasoning_message_end(sc, message_id="m"))
    stored = [e async for _, e in bus.replay(tenant=TENANT_A, case_id=case_id, after=0)]
    assert [e["type"] for e in stored] == [
        "REASONING_MESSAGE_START", "REASONING_MESSAGE_CONTENT", "REASONING_MESSAGE_END",
    ]
    assert stored[1]["delta"] == "Looking at POs"
    # the aggregate is not pushed live (subscribers already saw the deltas)
    live = await get_valkey().xrange(case_stream_key(case_id))
    live_types = [json.loads(f["data"])["type"] for _, f in live]
    assert live_types.count("REASONING_MESSAGE_CONTENT") == 3


async def test_appends_of_one_case_are_serialised_until_commit(platform: None) -> None:
    """Regression: a writer holding the case stream lock (an uncommitted append) blocks the next
    append of that case, so a later seq can never commit before an earlier one."""
    from keelson_engine.core.db import tenant_session
    from keelson_engine.core.events import lock_case_stream

    bus = EventBus()
    case_id = await _case()
    other_case = await _case()
    async with tenant_session(TENANT_A) as s:
        await lock_case_stream(s, case_id)
        blocked = asyncio.create_task(
            bus.append(tenant=TENANT_A, case_id=case_id, run_id=None, event=_step(case_id, 1))
        )
        # another case is not affected by this case's lock
        free = await asyncio.wait_for(
            bus.append(tenant=TENANT_A, case_id=other_case, run_id=None, event=_step(other_case, 1)), 3
        )
        await asyncio.sleep(0.3)
        assert not blocked.done()
    seq = await asyncio.wait_for(blocked, 5)
    assert seq > free


async def test_concurrent_writers_never_skip_events_for_a_tailing_reader(platform: None) -> None:
    bus = EventBus(block_ms=200)
    case_id = await _case()
    n = 30
    task = asyncio.create_task(_collect(bus.replay_then_tail(tenant=TENANT_A, case_id=case_id, after=None), n, 15))
    await asyncio.sleep(0.1)
    written = await asyncio.gather(*[
        bus.append(tenant=TENANT_A, case_id=case_id, run_id=f"r{i % 2}", event=_step(case_id, i)) for i in range(n)
    ])
    got = await task
    assert [s for s, _ in got] == sorted(written)
