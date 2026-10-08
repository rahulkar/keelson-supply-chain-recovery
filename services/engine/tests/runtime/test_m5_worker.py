"""M5 worker wiring: the Guide auto-tick loop runs beside the claim loop while a checklist store
exists (and not without one); execute's crash-retry recognises the M5 action payloads."""

from __future__ import annotations

import asyncio
from typing import Any

from fakes.core import FakeCore

from keelson_engine.runtime.execute import _original_kind
from keelson_engine.runtime.settings import WorkerSettings
from keelson_engine.worker import Worker

FAST = WorkerSettings(
    heartbeat_interval_s=0.02, cancel_poll_interval_s=0.02, reaper_interval_s=0.02, idle_poll_interval_s=0.02,
    shutdown_grace_s=0.5, snapshot_backoff_initial_s=0.01, snapshot_backoff_max_s=0.02, guide_tick_interval_s=0.02,
    scn_sync_enabled=False,
)


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def test_guide_loop_ticks_while_a_checklist_store_exists() -> None:
    core = FakeCore()
    worker = Worker(deps=core.deps(), settings=FAST, refresh_snapshot=False)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: worker.guide_ticks >= 3)
    worker.request_stop()
    await asyncio.wait_for(task, 5)


async def test_guide_loop_prefers_the_platform_poll() -> None:
    core = FakeCore()
    polls: list[int] = []

    async def poll() -> int:
        polls.append(1)
        return 0

    core.checklists.poll = poll  # type: ignore[union-attr]  # core.checklists.poll_checklists via the adapter
    worker = Worker(deps=core.deps(), settings=FAST, refresh_snapshot=False)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: len(polls) >= 2)
    worker.request_stop()
    await asyncio.wait_for(task, 5)


def test_checklists_adapter_maps_the_platform_module() -> None:
    import types

    from keelson_engine.runtime.deps import _ChecklistsAdapter

    calls: list[dict[str, Any]] = []
    mod = types.SimpleNamespace(
        create_checklist=lambda tenant, case_id, connector, steps, *, checklist_id=None, plan_version=None,
        option_id=None, block_id=None, meta=None: calls.append({"block_id": block_id, "id": checklist_id}),
        poll_checklists=lambda: [types.SimpleNamespace(verified=[("c", 1), ("c", 2)])],
    )
    adapter = _ChecklistsAdapter(mod)
    asyncio.run(adapter.create_checklist(tenant="t", case_id="case-1", checklist_id="CL-1", connector="merxa",
                                         plan_version=1, option_id="opt-A", steps=[{}], meta={}))
    from keelson_contracts.common import stable_block_id

    assert calls == [{"block_id": stable_block_id("case-1", "remediation_checklist", "CL-1"), "id": "CL-1"}]
    assert asyncio.run(adapter.poll()) == 2  # type: ignore[attr-defined]


async def test_no_guide_loop_without_a_checklist_store() -> None:
    core = FakeCore()
    core.checklists = None
    worker = Worker(deps=core.deps(), settings=FAST, refresh_snapshot=False)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.15)
    worker.request_stop()
    await asyncio.wait_for(task, 5)
    assert worker.guide_ticks == 0


def test_crash_retry_recognises_m5_actions() -> None:
    assert _original_kind("crash_retry", {"type": "rollback", "change_set_id": "cs-1"}) == "action"
    assert _original_kind("crash_retry", {"type": "checklist_step", "checklist_id": "CL-1", "seq": 1,
                                          "status": "done"}) == "action"
    assert _original_kind("crash_retry", {"type": "apply", "option_id": "opt-A", "plan_version": 1}) == "action"
    assert _original_kind("crash_retry", {"signal_id": "42"}) == "signal"
    assert _original_kind("crash_retry", {"text": "hi"}) == "chat"
