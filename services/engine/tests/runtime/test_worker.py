"""Worker loop: claim → run_case with heartbeat + cancel watcher, reaper, snapshot refresh with
back-off, Valkey wake-up, graceful stop."""

from __future__ import annotations

import asyncio
from typing import Any

from fakes.core import FakeCore, RunRow
from fakes.harness import PRIYA, Harness
from fakes.scripts import intake_kind

from keelson_engine.runtime.context import AbortController
from keelson_engine.runtime.settings import WorkerSettings
from keelson_engine.worker import Worker, valkey_waker

FAST = WorkerSettings(
    heartbeat_interval_s=0.02, cancel_poll_interval_s=0.02, reaper_interval_s=0.02, idle_poll_interval_s=0.02,
    shutdown_grace_s=0.5, snapshot_backoff_initial_s=0.01, snapshot_backoff_max_s=0.02,
)


def row(run_id: str) -> RunRow:
    return RunRow(id=run_id, tenant="demo-retail", case_id="case-1", kind="chat", payload={"text": "hi"}, actor=dict(PRIYA))


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def test_worker_runs_queue_reaps_and_refreshes_snapshot() -> None:
    core = FakeCore()
    core.runs.queue = [row("r1"), row("r2")]
    core.connectors.refresh_failures = 2
    seen: list[str] = []

    async def fake_run_case(run: Any, *, controller: AbortController, deps: Any) -> str:
        seen.append(run.id)
        await asyncio.sleep(0.06)  # long enough for a few heartbeats
        await deps.runs.finish_run(run.id, "succeeded")
        return "succeeded"

    worker = Worker(deps=core.deps(), settings=FAST, run_case_fn=fake_run_case)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: len(worker.completed) == 2 and worker.snapshot_ready.is_set())
    worker.request_stop()
    await asyncio.wait_for(task, 5)
    assert seen == ["r1", "r2"] and [s for _, s in worker.completed] == ["succeeded", "succeeded"]
    assert core.runs.heartbeats >= 2 and core.runs.reaped >= 1
    assert core.connectors.refreshes == 1  # succeeded after two back-off retries


async def test_cancel_watcher_and_lost_lease_abort_the_run() -> None:
    core = FakeCore()
    core.runs.queue = [row("cancel-me"), row("lose-me")]
    core.runs.cancel.add("cancel-me")
    core.runs.lost_lease.add("lose-me")
    reasons: dict[str, str | None] = {}

    async def fake_run_case(run: Any, *, controller: AbortController, deps: Any) -> str:
        await asyncio.wait_for(controller.signal.wait(), 5)
        reasons[run.id] = controller.reason
        return "cancelled" if controller.reason == "cancelled" else "abandoned"

    worker = Worker(deps=core.deps(), settings=FAST, run_case_fn=fake_run_case, refresh_snapshot=False)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: len(worker.completed) == 2)
    worker.request_stop()
    await asyncio.wait_for(task, 5)
    assert reasons == {"cancel-me": "cancelled", "lose-me": "lease_lost"}


async def test_graceful_stop_abandons_long_runs_after_grace() -> None:
    core = FakeCore()
    core.runs.queue = [row("slow")]
    started = asyncio.Event()

    async def fake_run_case(run: Any, *, controller: AbortController, deps: Any) -> str:
        started.set()
        await controller.signal.wait()
        return "abandoned" if controller.reason == "shutdown" else "cancelled"

    settings = WorkerSettings(**{**FAST.model_dump(), "shutdown_grace_s": 0.05, "heartbeat_interval_s": 5})
    worker = Worker(deps=core.deps(), settings=settings, run_case_fn=fake_run_case, refresh_snapshot=False)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.wait_for(started.wait(), 5)
    worker.request_stop()
    await asyncio.wait_for(task, 5)
    assert worker.completed == [("slow", "abandoned")]
    assert core.runs.finished == []  # left for the reaper (crash_retry), not finished


async def test_run_case_crash_is_recorded_as_failed() -> None:
    core = FakeCore()
    core.runs.queue = [row("boom")]

    async def fake_run_case(run: Any, *, controller: AbortController, deps: Any) -> str:
        raise RuntimeError("kaboom")

    worker = Worker(deps=core.deps(), settings=FAST, run_case_fn=fake_run_case, refresh_snapshot=False)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: worker.completed)
    worker.request_stop()
    await asyncio.wait_for(task, 5)
    assert core.runs.finished[-1]["status"] == "failed" and "kaboom" in core.runs.finished[-1]["error"]


async def test_valkey_waker_reads_new_entries() -> None:
    class FakeRedis:
        def __init__(self) -> None:
            self.calls: list[Any] = []
            self.responses: list[Any] = [[("keelson:runs:wake", [("1-0", {"run_id": "r"})])], []]

        async def xread(self, streams: dict[str, str], count: int, block: int) -> Any:
            self.calls.append(dict(streams))
            return self.responses.pop(0) if self.responses else []

    client = FakeRedis()
    wait = valkey_waker(client)
    assert await wait(0.01) is True
    assert await wait(0.01) is False
    assert client.calls == [{"keelson:runs:wake": "$"}, {"keelson:runs:wake": "1-0"}]


async def test_worker_drives_real_run_case(harness: Harness) -> None:
    """End-to-end: the worker claims a queued chat run and the runtime finishes it."""
    from keelson_engine.runtime import runner_cache

    h = harness
    h.script.add("intake", intake_kind("SMALLTALK"))
    runner_cache.set_runner_cache(h.cache)
    try:
        run = h.row("chat", {"text": "hello"})
        h.core.runs.queue = [run]
        worker = Worker(deps=h.core.deps(), settings=FAST, refresh_snapshot=False)
        task = asyncio.create_task(worker.run_forever())
        # The first real ADK run of a process pays cold imports + the SQLite session schema
        # (seconds on a loaded Docker host): generous bound, the run itself is instant.
        await _until(lambda: worker.completed, timeout=60.0)
        worker.request_stop()
        await asyncio.wait_for(task, 10)
    finally:
        runner_cache.set_runner_cache(None)
    assert worker.completed == [(run.id, "succeeded")]
    assert h.core.runs.status_of(run.id) == "succeeded"
    assert h.core.runs.invocations[run.id] == f"e-{run.id}"
