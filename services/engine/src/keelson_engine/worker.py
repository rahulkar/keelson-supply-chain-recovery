"""engine-worker: claims queued runs and drives them through the ADK runtime.

Loop: ``claim_next_run`` (``FOR UPDATE SKIP LOCKED`` + per-case lease) → ``run_case`` with a
heartbeat task (lease renewal; a lost lease aborts the run without finishing it) and a cancel
watcher (``POST /api/runs/{id}/cancel`` → ``abort_signal``). A reaper re-queues expired leases as
``crash_retry``. New runs wake the loop through the Valkey stream ``keelson:runs:wake`` (with a
polling fallback). ``SIGTERM``: stop claiming, give in-flight runs a grace period, then abandon
them to the reaper. On start-up the Merxa tool snapshot is refreshed with back-off until it
succeeds (it needs engine-api's JWKS for the client assertion).

Canonical snapshots (``80-m3-deterministic-core.md`` §1, platform ``core/scn_sync.py``): after
the tool snapshot, a separate sync task requests a sync of every syncable connector
(``list_sync_connectors`` → ``request_sync(…, "startup")``; without ``request_sync`` it calls
``sync_connector`` directly) and runs the due loop at once, so ``sync_connector`` runs at start-up.
Then, every ``scn_due_interval_s``: the tier-C change-feed poll (``poll_changes`` for connectors
with tier ``C``, every ``scn_poll_interval_s`` = 5 min, each bounded by ``scn_poll_timeout_s``; it
marks a connector stale on any change),
the nightly resync (``request_sync(…, "nightly")`` once a day from ``scn_nightly_hour_utc``) and
``due_syncs()`` — debounced stale marks (webhooks, polls), failure retries, start-up / admin /
nightly requests — synced one at a time (``sync_connector(t, c, reason=d.reason)``). The task never shares a run slot and
the claim loop never waits for it, so a sync never blocks case runs; a failed or timed-out sync
is logged and the platform keeps the previous snapshot current.

Snapshot GC (M5 polish): a separate task deletes the snapshots that syncs and demo resets only
*mark* for GC — ``scn.collect_garbage`` (optional port method; platform
``core.scn_sync.collect_garbage``) in budgeted passes of ``scn_gc_batches_per_tick`` statements of
``scn_gc_batch_rows`` rows, each its own short transaction with ``scn_gc_pause_s`` between batches;
while work is left the next pass follows right away, otherwise every ``scn_gc_interval_s``. It
never shares a run slot and never blocks the sync loop.

M7 (``110`` §4): tier-A connectors with a read mapping and **no** tier-B snapshot tools are synced
by crawling (the platform's ``sync_connector`` runs ``core/scn_crawl.py`` for them and loads a
``crawled`` snapshot) on the same schedule — start-up request, due list (stale marks after a per-op
apply, admin "Sync now", retries) and the nightly resync. They have no change feed, so instead of
the tier-C poll the worker requests a re-crawl every ``scn_crawl_interval_s`` (``0`` = off). The
crawl targets (:attr:`Worker.crawl_targets`: sync targets whose detected tiers lack ``B``) are
re-read with every poll round. A connector with tier B is always snapshot-synced, never crawled.

M5 (``95`` §2): a guide task verifies open remediation checklists (read-back of the expected
entities) on new webhook events of their connector and every ``guide_poll_interval_s`` (60 s);
start-up registers the CEL policy evaluator with the platform's policy port.

Run with ``python -m keelson_engine.worker``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import os
import signal
import socket
import uuid
from collections import deque
from collections.abc import Awaitable, Callable
from typing import Any

import structlog
from keelson_contracts.runs import RUNS_WAKE_STREAM

from .runtime.context import AbortController
from .runtime.deps import RuntimeDeps, field_of, get_deps
from .runtime.errors import RuntimeConfigError
from .runtime.execute import fail_run, run_case
from .runtime.settings import WorkerSettings

__all__ = ["Worker", "main"]

log = structlog.get_logger("keelson_engine.worker")

GC_PASSES_KEPT = 256
"""How many snapshot GC pass results :attr:`Worker.gc_passes` keeps (diagnostics, tests)."""

RunCaseFn = Callable[..., Awaitable[str]]
WakeWaiter = Callable[[float], Awaitable[bool]]


class Worker:
    def __init__(
        self,
        *,
        deps: RuntimeDeps | None = None,
        settings: WorkerSettings | None = None,
        worker_id: str | None = None,
        run_case_fn: RunCaseFn = run_case,
        wake: WakeWaiter | None = None,
        lease_seconds: int = 60,
        refresh_snapshot: bool = True,
        sync_snapshots: bool | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        verify_guides: bool | None = None,
        collect_garbage: bool | None = None,
    ) -> None:
        self._deps = deps
        self.settings = settings or WorkerSettings()
        self.worker_id = worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        self._run_case = run_case_fn
        self._wake = wake
        self._lease_seconds = lease_seconds
        self._refresh_snapshot = refresh_snapshot
        self._stop = asyncio.Event()
        self._inflight: dict[str, tuple[asyncio.Task[str], AbortController]] = {}
        self._slots = asyncio.Semaphore(max(1, self.settings.concurrency))
        self.snapshot_ready = asyncio.Event()
        self.completed: list[tuple[str, str]] = []
        self._sync_snapshots = self.settings.scn_sync_enabled if sync_snapshots is None else sync_snapshots
        self.syncs: list[tuple[str, str, str, str]] = []
        """``(tenant, connector, reason, outcome)`` of every sync attempt (``ok | failed | timeout``)."""
        self.polls: list[tuple[str, str, str]] = []
        """``(tenant, connector, outcome)`` of every change-feed poll (``changed | unchanged | error``)."""
        self._clock = clock or (lambda: dt.datetime.now(dt.UTC))
        self._verify_guides = self.settings.guide_verify_enabled if verify_guides is None else verify_guides
        self.guide_ticks = 0
        """Completed guide verification ticks (M5 auto-tick loop)."""
        self._collect_garbage = self.settings.scn_gc_enabled if collect_garbage is None else collect_garbage
        self.crawl_targets: set[tuple[str, str]] = set()
        """M7: ``(tenant, connector)`` synced by crawling (tier A + read mapping, no tier B)."""
        self.crawl_requests: list[tuple[str, str]] = []
        """M7: periodic re-crawl requests made (diagnostics, tests)."""
        self.gc_passes: deque[Any] = deque(maxlen=GC_PASSES_KEPT)
        """The results of the latest snapshot GC passes (``GcProgress``-like; ``None`` for a failed
        pass). Bounded: the loop runs for the worker's lifetime."""

    @property
    def deps(self) -> RuntimeDeps:
        return self._deps or get_deps()

    def request_stop(self) -> None:
        if not self._stop.is_set():
            log.info("worker stopping", worker_id=self.worker_id)
        self._stop.set()

    # ───────────────────────────── background loops ─────────────────────────────

    async def _reaper_loop(self) -> None:
        while not self._stop.is_set():
            try:
                requeued = await self.deps.runs.reap_expired(self.settings.max_attempts)
                if requeued:
                    log.warning("reaped expired run leases", runs=requeued)
            except Exception as exc:  # loop boundary: keep reaping after transient DB errors
                log.warning("reaper failed", error=str(exc))
            await self._sleep(self.settings.reaper_interval_s)

    async def _snapshot_loop(self) -> None:
        delay = self.settings.snapshot_backoff_initial_s
        while not self._stop.is_set():
            try:
                await self.deps.connectors.refresh_tool_snapshots()
            except Exception as exc:  # retried with back-off until engine-api JWKS is reachable
                log.warning("tool snapshot refresh failed; retrying", error=str(exc), retry_in_s=delay)
                await self._sleep(delay)
                delay = min(delay * 2, self.settings.snapshot_backoff_max_s)
                continue
            log.info("tool snapshot refreshed")
            self.snapshot_ready.set()
            return

    async def _guide_loop(self) -> None:
        """M5 Guide auto-tick (``95`` §2): while checklists are open, read the expected entities back
        on every new webhook event of their connector and at least every 60 s — the platform's
        ``core.checklists.poll_checklists`` (``ChecklistsPort.poll``) when it provides one, else the
        runtime's ``guide.GuideVerifier`` (``guide_poll_interval_s``). Never shares a run slot; a
        failure only logs."""
        from .runtime.guide import GuideVerifier  # noqa: PLC0415

        platform_poll = getattr(self.deps.checklists, "poll", None)
        verifier = GuideVerifier(self.deps, poll_interval_s=self.settings.guide_poll_interval_s)
        tick = platform_poll if callable(platform_poll) else verifier.tick
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(tick(), timeout=self.settings.guide_verify_timeout_s)
            except Exception as exc:  # loop boundary: the next tick retries
                log.warning("guide verification tick failed", error=type(exc).__name__)
            self.guide_ticks += 1
            await self._sleep(self.settings.guide_tick_interval_s)

    async def _gc_loop(self) -> None:
        """Background snapshot GC: budgeted ``scn.collect_garbage`` passes until nothing marked is
        left, then idle for ``scn_gc_interval_s``. A failing pass only logs (the marks stay, the
        next pass continues where the deletes stopped)."""
        collect = getattr(self.deps.scn, "collect_garbage", None)
        if not callable(collect):
            return
        st = self.settings
        while not self._stop.is_set():
            progress: Any = None
            try:
                progress = await collect(batch_rows=st.scn_gc_batch_rows, max_batches=st.scn_gc_batches_per_tick,
                                         pause_s=st.scn_gc_pause_s)
            except Exception as exc:  # loop boundary: transient DB error, try again later
                log.warning("snapshot gc pass failed", error=type(exc).__name__)
            self.gc_passes.append(progress)
            # work left AND this pass got somewhere: go on right away. A pass that deleted nothing
            # (every marked snapshot is being collected by another worker, or refused as unsafe)
            # waits the normal interval instead of polling the database every pause_s.
            busy = (progress is not None and int(getattr(progress, "pending", 0) or 0) > 0
                    and int(getattr(progress, "batches", 1) or 0) > 0)
            await self._sleep(st.scn_gc_pause_s if busy else st.scn_gc_interval_s)

    async def _sync_one(self, tenant: str, connector: str, reason: str) -> None:
        scn = self.deps.scn
        outcome = "ok"
        crawl = (tenant, connector) in self.crawl_targets
        try:
            # one entry point: the platform crawls tier-A connectors and exports tier-B ones
            await asyncio.wait_for(
                scn.sync_connector(tenant=tenant, connector=connector, reason=reason),
                timeout=self.settings.scn_sync_timeout_s,
            )
        except TimeoutError:
            outcome = "timeout"
            log.warning("snapshot sync timed out; previous snapshot stays current", tenant=tenant,
                        connector=connector, reason=reason)
        except Exception as exc:  # loop boundary: one failing connector never stops the others
            outcome = "failed"
            log.warning("snapshot sync failed; previous snapshot stays current", tenant=tenant, connector=connector,
                        reason=reason, error=type(exc).__name__)
        else:
            log.info("snapshot synced", tenant=tenant, connector=connector, reason=reason,
                     kind="crawl" if crawl else "snapshot")
        self.syncs.append((tenant, connector, reason, outcome))

    async def _refresh_crawl_targets(self, targets: list[tuple[str, str]]) -> set[tuple[str, str]]:
        """M7: the sync targets that are crawled — detected tier A without tier B (a connector whose
        tiers are unknown is left to the platform and never re-crawled on the interval)."""
        found: set[tuple[str, str]] = set()
        for tenant, connector in targets:
            try:
                info = await self.deps.connectors.get_connector(tenant=tenant, slug=connector)
            except Exception:  # registry unavailable: keep what we knew
                if (tenant, connector) in self.crawl_targets:
                    found.add((tenant, connector))
                continue
            tiers = tuple(getattr(info, "tiers", ()) or ())
            if "A" in tiers and "B" not in tiers:
                found.add((tenant, connector))
        self.crawl_targets = found
        return found

    async def _request_crawls(self) -> None:
        """M7: tier A has no change feed — ask for a re-crawl of every crawl target (debounced by
        the platform's due list; a running crawl keeps the request due until it finishes)."""
        for tenant, connector in sorted(self.crawl_targets):
            if self._stop.is_set():
                return
            try:
                await self.deps.scn.request_sync(tenant=tenant, connector=connector, reason="crawl_interval")
                self.crawl_requests.append((tenant, connector))
            except Exception as exc:  # loop boundary
                log.warning("re-crawl request failed", tenant=tenant, connector=connector, error=type(exc).__name__)

    async def _sync_targets(self) -> list[tuple[str, str]]:
        try:
            targets = list(await self.deps.scn.startup_targets())
        except Exception as exc:  # loop boundary: try again next round
            log.warning("could not list syncable connectors", error=type(exc).__name__)
            targets = []
        await self._refresh_crawl_targets(targets)
        return targets

    async def _has_change_feed(self, tenant: str, connector: str) -> bool:
        """Tier C detected (unknown tiers: try — ``poll_changes`` refuses connectors without one)."""
        try:
            info = await self.deps.connectors.get_connector(tenant=tenant, slug=connector)
        except Exception:  # registry unavailable: let poll_changes decide
            return True
        tiers = tuple(getattr(info, "tiers", ()) or ())
        return not tiers or "C" in tiers

    async def _poll_changes(self) -> None:
        for tenant, connector in await self._sync_targets():
            if self._stop.is_set():
                return
            if (tenant, connector) in self.crawl_targets:
                continue  # tier A: no change feed to poll (re-crawled on its own interval)
            if not await self._has_change_feed(tenant, connector):
                continue
            try:
                changed = await asyncio.wait_for(
                    self.deps.scn.poll_changes(tenant=tenant, connector=connector),
                    timeout=self.settings.scn_poll_timeout_s,
                )
            except Exception as exc:  # no change feed (tier B only), the call failed or hung: next poll retries
                log.debug("change-feed poll skipped", tenant=tenant, connector=connector, error=type(exc).__name__)
                self.polls.append((tenant, connector, "error"))
                continue
            self.polls.append((tenant, connector, "changed" if changed else "unchanged"))

    async def _request_nightly(self) -> None:
        for tenant, connector in await self._sync_targets():
            try:
                await self.deps.scn.request_sync(tenant=tenant, connector=connector, reason="nightly")
            except Exception as exc:  # loop boundary
                log.warning("nightly sync request failed", tenant=tenant, connector=connector,
                            error=type(exc).__name__)

    async def _sync_loop(self) -> None:
        scn = self.deps.scn
        if scn is None:
            return
        if self._refresh_snapshot:  # scn.* tools must be in the tool snapshot first
            stop = asyncio.create_task(self._stop.wait())
            ready = asyncio.create_task(self.snapshot_ready.wait())
            try:
                await asyncio.wait({stop, ready}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (stop, ready):
                    task.cancel()
                await asyncio.gather(stop, ready, return_exceptions=True)
        if self._stop.is_set():
            return
        for tenant, connector in await self._sync_targets():
            if self._stop.is_set():
                return
            try:
                await scn.request_sync(tenant=tenant, connector=connector, reason="startup")
            except RuntimeConfigError:  # an older platform without request_sync: sync right away
                await self._sync_one(tenant, connector, "startup")
            except Exception as exc:  # e.g. the connector cannot be synced (any more)
                log.warning("start-up sync request failed", tenant=tenant, connector=connector,
                            error=type(exc).__name__)
        loop = asyncio.get_running_loop()
        next_poll = loop.time() + self.settings.scn_poll_interval_s
        crawl_every = self.settings.scn_crawl_interval_s
        next_crawl = loop.time() + crawl_every if crawl_every > 0 else None
        started = self._clock()
        # the start-up sync stands in for tonight's run when the worker starts after the hour
        nightly_done = started.date() if started.hour >= self.settings.scn_nightly_hour_utc else None
        while not self._stop.is_set():
            if loop.time() >= next_poll:
                next_poll = loop.time() + self.settings.scn_poll_interval_s
                await self._poll_changes()
            if next_crawl is not None and loop.time() >= next_crawl:
                next_crawl = loop.time() + crawl_every
                await self._request_crawls()
            now = self._clock()
            if now.hour >= self.settings.scn_nightly_hour_utc and nightly_done != now.date():
                nightly_done = now.date()
                await self._request_nightly()
            try:
                due = await scn.due_syncs()
            except Exception as exc:  # transient DB error: ask again next tick
                log.warning("due_syncs failed", error=type(exc).__name__)
                due = []
            seen: set[tuple[str, str]] = set()
            for item in due:
                key = (item.tenant, item.connector)
                if key in seen or self._stop.is_set():
                    continue
                seen.add(key)
                await self._sync_one(item.tenant, item.connector, item.reason)
            await self._sleep(self.settings.scn_due_interval_s)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stop.wait(), timeout=seconds)

    async def _wait_for_work(self) -> None:
        timeout = self.settings.idle_poll_interval_s
        if self._wake is None:
            await self._sleep(timeout)
            return
        waiter = asyncio.create_task(self._wake(timeout))
        stopper = asyncio.create_task(self._stop.wait())
        try:
            await asyncio.wait({waiter, stopper}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (waiter, stopper):
                task.cancel()
            await asyncio.gather(waiter, stopper, return_exceptions=True)

    # ───────────────────────────────── one run ─────────────────────────────────

    async def _heartbeat(self, run_id: str, controller: AbortController) -> None:
        while True:
            await asyncio.sleep(self.settings.heartbeat_interval_s)
            try:
                alive = await self.deps.runs.heartbeat(run_id, self.worker_id, self._lease_seconds)
            except Exception as exc:  # transient DB error: keep the run, try again next tick
                log.warning("heartbeat failed", run_id=run_id, error=str(exc))
                continue
            if not alive:
                log.error("run lease lost; abandoning run", run_id=run_id)
                controller.abort("lease_lost", "lease lost")
                return

    async def _cancel_watch(self, run_id: str, controller: AbortController) -> None:
        while not controller.signal.is_set():
            await asyncio.sleep(self.settings.cancel_poll_interval_s)
            try:
                if await self.deps.runs.is_cancel_requested(run_id):
                    log.info("run cancel requested", run_id=run_id)
                    controller.abort("cancelled", "cancelled by user")
                    return
            except Exception as exc:  # transient DB error
                log.warning("cancel check failed", run_id=run_id, error=str(exc))

    async def process(self, run: Any) -> str:
        run_id = str(field_of(run, "id"))
        controller = AbortController()
        helpers = [
            asyncio.create_task(self._heartbeat(run_id, controller)),
            asyncio.create_task(self._cancel_watch(run_id, controller)),
        ]
        task = asyncio.create_task(self._run_case(run, controller=controller, deps=self.deps))
        self._inflight[run_id] = (task, controller)
        try:
            status = await task
        except Exception as exc:  # run boundary: run_case normally records failures itself
            try:
                # Generic RUN_ERROR + error id on the case stream (only if the run was still
                # ours to fail); the exception itself goes to the log under that id.
                await fail_run(
                    self.deps, str(field_of(run, "tenant")), str(field_of(run, "case_id")), run_id,
                    code="INTERNAL_ERROR", detail="run crashed", exc=exc, worker_id=self.worker_id,
                    emit_first=False,
                )
            except Exception as finish_exc:
                log.error("could not record run failure", run_id=run_id, error=type(finish_exc).__name__)
            status = "failed"
        finally:
            for helper in helpers:
                helper.cancel()
            await asyncio.gather(*helpers, return_exceptions=True)
            self._inflight.pop(run_id, None)
        log.info("run done", run_id=run_id, status=status)
        self.completed.append((run_id, status))
        return status

    # ──────────────────────────────── main loop ────────────────────────────────

    async def _acquire_slot(self) -> bool:
        """Wait for a free run slot, or for a stop request (False)."""
        acquire = asyncio.create_task(self._slots.acquire())
        stop = asyncio.create_task(self._stop.wait())
        await asyncio.wait({acquire, stop}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        if not acquire.done():
            acquire.cancel()
        await asyncio.gather(acquire, stop, return_exceptions=True)
        got = acquire.done() and not acquire.cancelled() and acquire.exception() is None
        if got and self._stop.is_set():
            self._slots.release()
            return False
        return got

    async def _claim_loop(self) -> None:
        running: set[asyncio.Task[str]] = set()
        while not self._stop.is_set():
            if not await self._acquire_slot():
                break
            try:
                run = await self.deps.runs.claim_next_run(self.worker_id, self._lease_seconds)
            except Exception as exc:  # DB hiccup: back off and retry
                self._slots.release()
                log.warning("claim failed", error=str(exc))
                await self._sleep(self.settings.idle_poll_interval_s)
                continue
            if run is None:
                self._slots.release()
                await self._wait_for_work()
                continue
            task = asyncio.create_task(self.process(run))
            running.add(task)
            task.add_done_callback(lambda t: (running.discard(t), self._slots.release()))
        await self._drain(running)

    async def _drain(self, running: set[asyncio.Task[str]]) -> None:
        if not running:
            return
        log.info("waiting for in-flight runs", count=len(running), grace_s=self.settings.shutdown_grace_s)
        done, pending = await asyncio.wait(running, timeout=self.settings.shutdown_grace_s)
        if pending:
            for _, controller in list(self._inflight.values()):
                controller.abort("shutdown", "worker shutting down")
            await asyncio.wait(pending, timeout=10)

    async def run_forever(self) -> None:
        background = [asyncio.create_task(self._reaper_loop())]
        if self._refresh_snapshot:
            background.append(asyncio.create_task(self._snapshot_loop()))
        if self._sync_snapshots:
            background.append(asyncio.create_task(self._sync_loop()))
        if self._collect_garbage and self.deps.scn is not None:
            background.append(asyncio.create_task(self._gc_loop()))
        if self._verify_guides and self.deps.checklists is not None:
            background.append(asyncio.create_task(self._guide_loop()))
        try:
            await self._claim_loop()
        finally:
            self._stop.set()
            for task in background:
                task.cancel()
            await asyncio.gather(*background, return_exceptions=True)


def valkey_waker(client: Any, stream: str = RUNS_WAKE_STREAM) -> WakeWaiter:
    """``XREAD BLOCK`` on the wake stream (new entries only); ``False`` on timeout/error."""
    last_id = "$"

    async def wait(timeout_s: float) -> bool:
        nonlocal last_id
        try:
            resp = await client.xread({stream: last_id}, count=10, block=int(timeout_s * 1000))
        except Exception as exc:  # Valkey down: fall back to the poll interval
            log.warning("valkey wake read failed", error=str(exc))
            await asyncio.sleep(timeout_s)
            return False
        if not resp:
            return False
        for _key, entries in resp:
            if entries:
                last_id = entries[-1][0]
        return True

    return wait


def check_startup_data() -> None:
    """M4: fail fast on platform data the workflow compiles from — the disruption taxonomy and
    the chokepoint catalogue (``runtime.nodes.taxonomy.check_taxonomy``, framing-owned; skipped
    while that module does not exist)."""
    import importlib  # noqa: PLC0415

    try:
        taxonomy = importlib.import_module("keelson_engine.runtime.nodes.taxonomy")
    except ModuleNotFoundError as exc:
        if exc.name != "keelson_engine.runtime.nodes.taxonomy":
            raise
        log.warning("taxonomy module not available; start-up data check skipped")
        return
    check = getattr(taxonomy, "check_taxonomy", None)
    if callable(check):
        check()


async def main() -> None:
    from .config import get_settings  # noqa: PLC0415
    from .core.db import init_db  # noqa: PLC0415 - E1 platform
    from .core.logs import configure_logging  # noqa: PLC0415
    from .core.valkey import get_valkey  # noqa: PLC0415

    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    check_startup_data()
    await init_db()
    from .runtime.policy import register_policy_evaluator  # noqa: PLC0415

    register_policy_evaluator()  # CEL evaluator for the platform's policy port (admin test bench)
    worker = Worker(wake=valkey_waker(get_valkey()), lease_seconds=settings.run_lease_seconds)
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.request_stop)
    log.info("worker started", worker_id=worker.worker_id)
    try:
        await worker.run_forever()
    finally:
        from .runtime.runner_cache import get_runner_cache  # noqa: PLC0415

        await get_runner_cache().close()
        log.info("worker stopped", worker_id=worker.worker_id)


if __name__ == "__main__":
    asyncio.run(main())
