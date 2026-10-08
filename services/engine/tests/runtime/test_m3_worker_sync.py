"""Worker snapshot-sync scheduling (``80-m3-deterministic-core.md`` §1) and the core adapter
for the platform's ``core.scn_sync`` functions.

Startup: ``sync_connector`` for every syncable connector only after the tool snapshot. Then the
5-minute tier-C ``poll_changes``, the nightly ``request_sync`` and ``due_syncs()`` every interval
(one sync per connector per round). A failing or hanging sync never stops the loop, and syncs
never block case runs.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import Any

import pytest
from fakes.core import FakeCore, FakeScn, RunRow
from fakes.harness import PRIYA

from keelson_engine.runtime.context import AbortController
from keelson_engine.runtime.errors import RuntimeConfigError
from keelson_engine.runtime.scn_store import CoreScnAdapter, DueSync, SnapshotRef
from keelson_engine.runtime.settings import WorkerSettings
from keelson_engine.worker import Worker

FAST = WorkerSettings(
    heartbeat_interval_s=0.02, cancel_poll_interval_s=0.02, reaper_interval_s=0.02, idle_poll_interval_s=0.02,
    shutdown_grace_s=0.5, snapshot_backoff_initial_s=0.01, snapshot_backoff_max_s=0.02, scn_due_interval_s=0.02,
    scn_sync_timeout_s=2.0, scn_poll_interval_s=3600.0, scn_nightly_hour_utc=23,
)
NOON = dt.datetime(2026, 10, 6, 12, 0, tzinfo=dt.UTC)


async def _until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.01)


async def _stop(worker: Worker, task: asyncio.Task[None]) -> None:
    worker.request_stop()
    await asyncio.wait_for(task, 5)


class OrderedScn(FakeScn):
    """Records how many tool-snapshot refreshes had succeeded when each sync started."""

    def __init__(self, core: FakeCore, **kw: Any) -> None:
        super().__init__(**kw)
        self.core = core
        self.refreshes_at_sync: list[int] = []

    async def sync_connector(self, *, tenant: str, connector: str, reason: str) -> Any:
        self.refreshes_at_sync.append(self.core.connectors.refreshes)
        return await super().sync_connector(tenant=tenant, connector=connector, reason=reason)


async def test_startup_sync_runs_after_the_tool_snapshot_then_due_syncs() -> None:
    core = FakeCore()
    core.connectors.refresh_failures = 2
    scn = OrderedScn(core, targets=[("demo-retail", "merxa"), ("northwind-test", "lite")])
    scn.due = [
        [],  # round 1: only the start-up requests
        [DueSync("demo-retail", "merxa", "webhook"), DueSync("demo-retail", "merxa", "poll")],
        [],
        [DueSync("northwind-test", "lite", "retry")],
    ]
    core.scn = scn
    worker = Worker(clock=lambda: NOON, deps=core.deps(), settings=FAST)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: len(scn.syncs) == 4 and scn.due_calls >= 4)
    await _stop(worker, task)
    # start-up: request_sync for every syncable connector, synced by the very next due round
    assert scn.requests == [("demo-retail", "merxa", "startup"), ("northwind-test", "lite", "startup")]
    assert scn.syncs == [
        ("demo-retail", "merxa", "startup"), ("northwind-test", "lite", "startup"),
        ("demo-retail", "merxa", "webhook"),  # one sync per connector per round (the poll is folded in)
        ("northwind-test", "lite", "retry"),
    ]
    assert scn.refreshes_at_sync == [1, 1, 1, 1]  # never before the tool snapshot succeeded
    assert [o for *_, o in worker.syncs] == ["ok"] * 4


async def test_failed_and_hung_syncs_never_stop_the_loop() -> None:
    core = FakeCore()
    core.scn.targets = [("a", "merxa"), ("b", "merxa"), ("c", "merxa")]
    core.scn.sync_errors[("a", "merxa")] = RuntimeError("download failed")
    core.scn.due = [[], [DueSync("a", "merxa", "poll")]]
    worker = Worker(clock=lambda: NOON, deps=core.deps(), settings=FAST.model_copy(update={"scn_sync_timeout_s": 0.05}),
                    refresh_snapshot=False)
    gate = asyncio.Event()
    original = core.scn.sync_connector

    async def sync(*, tenant: str, connector: str, reason: str) -> Any:
        if tenant == "b":
            await gate.wait()  # never released: hits the timeout
        return await original(tenant=tenant, connector=connector, reason=reason)

    core.scn.sync_connector = sync  # type: ignore[method-assign]
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: len(worker.syncs) == 4)
    await _stop(worker, task)
    assert worker.syncs == [("a", "merxa", "startup", "failed"), ("b", "merxa", "startup", "timeout"),
                            ("c", "merxa", "startup", "ok"), ("a", "merxa", "poll", "failed")]
    assert core.scn.due_calls >= 1


async def test_a_long_sync_never_blocks_case_runs() -> None:
    core = FakeCore()
    core.scn.targets = [("demo-retail", "merxa")]
    core.scn.sync_gate = asyncio.Event()  # the startup sync hangs until the end of the test
    core.runs.queue = [RunRow(id=f"r{i}", tenant="demo-retail", case_id=f"c{i}", kind="chat", payload={"text": "hi"},
                              actor=dict(PRIYA)) for i in range(3)]

    async def fake_run_case(run: Any, *, controller: AbortController, deps: Any) -> str:
        await asyncio.sleep(0.02)
        await deps.runs.finish_run(run.id, "succeeded")
        return "succeeded"

    worker = Worker(clock=lambda: NOON, deps=core.deps(), settings=FAST.model_copy(update={"scn_sync_timeout_s": 30.0}),
                    run_case_fn=fake_run_case)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: len(worker.completed) == 3)
    assert core.scn.syncs == [("demo-retail", "merxa", "startup")] and worker.syncs == []  # still syncing
    await _stop(worker, task)  # stopping cancels the in-flight sync (the platform keeps the old snapshot)
    assert [s for _, s in worker.completed] == ["succeeded"] * 3


async def test_change_feed_poll_marks_stale_then_due_sync() -> None:
    core = FakeCore()
    core.scn.targets = [("a", "merxa"), ("b", "lite")]
    core.scn.poll_errors[("b", "lite")] = RuntimeError("no approved scn.changes_since")
    core.scn.changed = {("a", "merxa")}
    worker = Worker(deps=core.deps(), settings=FAST.model_copy(update={"scn_poll_interval_s": 0.05}),
                    refresh_snapshot=False, clock=lambda: NOON)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: ("a", "merxa", "poll", "ok") in worker.syncs and len(worker.polls) >= 4)
    await _stop(worker, task)
    assert worker.syncs[:2] == [("a", "merxa", "startup", "ok"), ("b", "lite", "startup", "ok")]
    assert worker.polls[:2] == [("a", "merxa", "changed"), ("b", "lite", "error")]
    assert ("a", "merxa", "unchanged") in worker.polls  # later polls see no change
    assert worker.syncs.count(("a", "merxa", "poll", "ok")) == 1


class HangingPollScn(FakeScn):
    """``poll_changes`` of one connector never returns (a hung connector)."""

    def __init__(self, hang: tuple[str, str], **kw: Any) -> None:
        super().__init__(**kw)
        self.hang = hang

    async def poll_changes(self, *, tenant: str, connector: str) -> bool:
        if (tenant, connector) == self.hang:
            self.polls.append((tenant, connector))
            await asyncio.Event().wait()
        return await super().poll_changes(tenant=tenant, connector=connector)


async def test_a_hung_change_feed_poll_never_stalls_due_syncs() -> None:
    """Review regression: the tier-C poll is a connector call; a hung connector is cut off after
    ``scn_poll_timeout_s`` and the other connectors are still polled and synced."""
    core = FakeCore()
    core.scn = HangingPollScn(("a", "merxa"), targets=[("a", "merxa"), ("b", "lite")])
    core.scn.changed = {("b", "lite")}
    settings = FAST.model_copy(update={"scn_poll_interval_s": 0.05, "scn_poll_timeout_s": 0.1})
    worker = Worker(deps=core.deps(), settings=settings, refresh_snapshot=False, clock=lambda: NOON)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: ("b", "lite", "poll", "ok") in worker.syncs, timeout=5.0)
    await _stop(worker, task)
    assert worker.polls[:2] == [("a", "merxa", "error"), ("b", "lite", "changed")]


async def test_nightly_resync_is_requested_once_a_day() -> None:
    core = FakeCore()
    core.scn.targets = [("a", "merxa")]
    now = [dt.datetime(2026, 10, 6, 1, 59, tzinfo=dt.UTC)]
    worker = Worker(deps=core.deps(), settings=FAST.model_copy(update={"scn_nightly_hour_utc": 2}),
                    refresh_snapshot=False, clock=lambda: now[0])
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: core.scn.due_calls >= 2)
    nightly = lambda: [r for r in core.scn.requests if r[2] == "nightly"]  # noqa: E731
    assert nightly() == []  # started before the nightly hour
    now[0] = dt.datetime(2026, 10, 6, 2, 0, tzinfo=dt.UTC)
    await _until(lambda: ("a", "merxa", "nightly", "ok") in worker.syncs)
    now[0] = dt.datetime(2026, 10, 6, 23, 0, tzinfo=dt.UTC)
    calls = core.scn.due_calls
    await _until(lambda: core.scn.due_calls >= calls + 3)
    assert nightly() == [("a", "merxa", "nightly")]  # once per day
    now[0] = dt.datetime(2026, 10, 7, 2, 30, tzinfo=dt.UTC)
    await _until(lambda: len(nightly()) == 2)
    await _stop(worker, task)
    late = Worker(deps=core.deps(), settings=FAST.model_copy(update={"scn_nightly_hour_utc": 2}),
                  refresh_snapshot=False, clock=lambda: NOON)  # starts after the hour: start-up sync counts
    task = asyncio.create_task(late.run_forever())
    await _until(lambda: core.scn.due_calls >= calls + 10)
    await _stop(late, task)
    assert len(nightly()) == 2


async def test_start_up_without_request_sync_syncs_directly_and_polls_only_tier_c() -> None:
    from keelson_engine.runtime.deps import ConnectorInfo

    core = FakeCore()
    core.scn.targets = [("a", "merxa"), ("a", "lite")]

    async def no_request(*, tenant: str, connector: str, reason: str) -> None:
        raise RuntimeConfigError("core.scn_sync.request_sync is not available")

    core.scn.request_sync = no_request  # type: ignore[method-assign]
    tiers = {"merxa": ("A", "B", "C", "W"), "lite": ("A", "B")}

    async def get_connector(*, tenant: str, slug: str) -> ConnectorInfo:
        return ConnectorInfo(slug=slug, name=slug, url="", issuer="", lanes=(), tiers=tiers[slug])

    core.connectors.get_connector = get_connector  # type: ignore[method-assign]
    worker = Worker(deps=core.deps(), settings=FAST.model_copy(update={"scn_poll_interval_s": 0.03}),
                    refresh_snapshot=False, clock=lambda: NOON)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: len(core.scn.polls) >= 2)
    await _stop(worker, task)
    assert worker.syncs[:2] == [("a", "merxa", "startup", "ok"), ("a", "lite", "startup", "ok")]
    assert set(core.scn.polls) == {("a", "merxa")}  # no change feed on "lite": never polled


async def test_sync_disabled_or_no_store() -> None:
    core = FakeCore()
    core.scn.targets = [("demo-retail", "merxa")]
    worker = Worker(clock=lambda: NOON, deps=core.deps(), settings=FAST.model_copy(update={"scn_sync_enabled": False}))
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: worker.snapshot_ready.is_set())
    await asyncio.sleep(0.05)
    await _stop(worker, task)
    assert core.scn.syncs == [] and core.scn.due_calls == 0
    deps = core.deps()
    deps.scn = None
    worker = Worker(clock=lambda: NOON, deps=deps, settings=FAST)
    task = asyncio.create_task(worker.run_forever())
    await _until(lambda: worker.snapshot_ready.is_set())
    await _stop(worker, task)
    assert worker.syncs == []


async def test_stop_while_waiting_for_the_tool_snapshot() -> None:
    core = FakeCore()
    core.connectors.refresh_failures = 10_000
    core.scn.targets = [("demo-retail", "merxa")]
    worker = Worker(clock=lambda: NOON, deps=core.deps(), settings=FAST)
    task = asyncio.create_task(worker.run_forever())
    await asyncio.sleep(0.1)
    await _stop(worker, task)
    assert core.scn.syncs == []


# ─────────────────────────────── core adapter ───────────────────────────────


class FakeScnSync:
    """Stands in for ``keelson_engine.core.scn_sync`` with the documented signatures."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.status = "ready"

    async def current_snapshot(self, tenant: str, connector: str) -> dict[str, Any] | None:
        self.calls.append(("current", tenant, connector))
        return {"id": "11111111-1111-1111-1111-111111111111", "tenant": tenant, "connector": connector,
                "as_of": dt.datetime(2026, 10, 5, 6, 0, tzinfo=dt.UTC), "watermark": "0/16B3F28",
                "status": self.status, "row_counts": {"po_line": 1080}}

    async def pin_snapshot(self, tenant: str, snapshot_id: str, plan_id: int) -> None:
        self.calls.append(("pin", tenant, snapshot_id, plan_id))

    async def unpin_plan(self, tenant: str, plan_id: int) -> int:
        self.calls.append(("unpin", tenant, plan_id))
        return 1

    async def mark_stale(self, tenant: str, connector: str, *, reason: str) -> None:
        self.calls.append(("stale", tenant, connector, reason))

    async def sync_connector(self, tenant: str, connector: str, *, reason: str) -> dict[str, Any]:
        self.calls.append(("sync", tenant, connector, reason))
        return {"id": "x"}

    async def due_syncs(self, limit: int = 20) -> list[Any]:
        return [SimpleNamespace(tenant="demo-retail", connector="merxa", reason=None, stale_since=None),
                {"tenant": "t2", "connector": "c2", "reason": "webhook"}, ("t3", "c3", "retry")]

    async def list_sync_connectors(self) -> list[tuple[str, str]]:
        return [("demo-retail", "merxa"), ("t2", "c2")]

    async def poll_changes(self, tenant: str, connector: str) -> bool:
        self.calls.append(("poll", tenant, connector))
        return True

    async def request_sync(self, tenant: str, connector: str, reason: str = "admin") -> dict[str, Any]:
        self.calls.append(("request", tenant, connector, reason))
        return {}


async def test_core_adapter_binds_the_platform_functions() -> None:
    adapter = CoreScnAdapter()
    fake = FakeScnSync()
    adapter._m = fake
    ref = await adapter.current_snapshot(tenant="demo-retail", connector="merxa")
    assert ref == SnapshotRef(id="11111111-1111-1111-1111-111111111111", connector="merxa", as_of=dt.date(2026, 10, 5),
                              watermark="0/16B3F28", status="ready", row_counts={"po_line": 1080})
    fake.status = "loading"
    assert await adapter.current_snapshot(tenant="demo-retail", connector="merxa") is None
    await adapter.pin_snapshot(tenant="demo-retail", snapshot_id="s1", plan_id=42)
    await adapter.mark_stale(tenant="demo-retail", connector="merxa", reason="change_set.applied")
    await adapter.sync_connector(tenant="demo-retail", connector="merxa", reason="startup")
    assert fake.calls[-3:] == [("pin", "demo-retail", "s1", 42), ("stale", "demo-retail", "merxa", "change_set.applied"),
                               ("sync", "demo-retail", "merxa", "startup")]
    assert await adapter.due_syncs() == [DueSync("demo-retail", "merxa", "stale"), DueSync("t2", "c2", "webhook"),
                                         DueSync("t3", "c3", "retry")]
    assert await adapter.startup_targets() == [("demo-retail", "merxa"), ("t2", "c2")]
    assert await adapter.poll_changes(tenant="demo-retail", connector="merxa") is True
    await adapter.request_sync(tenant="demo-retail", connector="merxa", reason="nightly")
    assert fake.calls[-2:] == [("poll", "demo-retail", "merxa"), ("request", "demo-retail", "merxa", "nightly")]
    await adapter.unpin_plan(tenant="demo-retail", plan_id=41)
    assert fake.calls[-1] == ("unpin", "demo-retail", 41)


async def test_core_adapter_degrades_without_the_platform_side() -> None:
    adapter = CoreScnAdapter()
    adapter._m = None
    assert await adapter.current_snapshot(tenant="t", connector="merxa") is None
    await adapter.pin_snapshot(tenant="t", snapshot_id="s", plan_id=1)
    await adapter.unpin_plan(tenant="t", plan_id=1)
    await adapter.mark_stale(tenant="t", connector="merxa", reason="x")
    assert await adapter.due_syncs() == [] and await adapter.startup_targets() == []
    assert await adapter.poll_changes(tenant="t", connector="merxa") is False
    with pytest.raises(RuntimeConfigError):
        await adapter.sync_connector(tenant="t", connector="merxa", reason="startup")
    with pytest.raises(RuntimeConfigError):
        await adapter.request_sync(tenant="t", connector="merxa", reason="nightly")
    adapter._m = SimpleNamespace()  # an older platform build: functions missing one by one
    assert await adapter.current_snapshot(tenant="t", connector="merxa") is None
