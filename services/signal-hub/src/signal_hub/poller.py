"""``python -m signal_hub.poller`` — feed adapters on schedules (``100`` §1, §9).

* Long-running (compose ``signal-hub-poller``): one **leader** at a time via a session-level
  Postgres advisory lock held on a dedicated connection; followers wait and retry. The leader polls
  every enabled source whose ``next_poll_at`` is due (GDACS 10 min, USGS 5 min, RSS 15 min; backoff on
  failure) and sweeps expiries every minute. Outbound HTTP goes only through the egress proxy.
* ``--once --adapter gdacs|usgs|rss|nhc|copernicus_ems|ofac|gdelt|meteoalarm|extraction|sweep``
  (Cloud Run Jobs triggered by Cloud Scheduler; the feed adapters come from
  :data:`signal_hub.sources.ADAPTERS`): one run, then exit (0 = ok, 1 = the run failed); a paused
  source (every tenant disabled it, or an opt-in source nobody enabled) is skipped with exit 0. ``extraction`` retries pending news extraction
  (needs LiteLLM: run it where the gateway is reachable; it reads the provisioned extraction key
  unless ``LITELLM_SIGNALS_KEY`` is set, :mod:`signal_hub.llm_key`).
* Tenant subscriptions (``110`` §5) shape the schedule: the cadence is the fastest one a subscribed
  tenant asked for (within limits), and a source every registered tenant disabled is not polled.

The poller itself never calls an LLM except in ``--adapter extraction``: RSS items are stored
``pending_extraction`` and the API (on the ``ai`` network) extracts them.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import signal
import sys
from typing import Any, Final

import asyncpg

from signal_hub import sources
from signal_hub.config import Settings, load_settings
from signal_hub.db import create_pool
from signal_hub.feeds import FeedRunner
from signal_hub.gazetteer import load_gazetteer
from signal_hub.httpfetch import Fetcher, build_feed_client, build_internal_client
from signal_hub.llm_key import resolve_llm_key
from signal_hub.logs import configure_logging, get_logger
from signal_hub.pipeline import Pipeline
from signal_hub.quarantine import LiteLlmClient
from signal_hub.timeutil import utc_now

__all__ = ["LEADER_LOCK", "main", "run_loop", "run_once"]

log = get_logger("signal_hub.poller")
LEADER_LOCK: Final = 0x5348_504F_4C4C  # "SHPOLL"
ADAPTERS: Final = (*sources.ADAPTERS, "extraction", "sweep")


async def _adapter_lock(conn: Any, adapter: str) -> bool:
    return bool(await conn.fetchval("SELECT pg_try_advisory_lock(hashtextextended($1, 0))", f"poll:{adapter}"))


async def _adapter_unlock(conn: Any, adapter: str) -> None:
    await conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", f"poll:{adapter}")


async def run_once(settings: Settings, adapter: str, *, connector: str | None = None) -> int:
    if adapter not in ADAPTERS:
        raise SystemExit(f"unknown adapter {adapter!r} (one of {', '.join(ADAPTERS)})")
    pool = await create_pool(settings.database_url, max_size=3)
    feed_client = build_feed_client(settings)
    internal = build_internal_client(settings)
    try:
        gaz = load_gazetteer(settings.geo_dir)
        llm = None
        if adapter == "extraction":
            settings = await resolve_llm_key(settings, pool)
            llm = LiteLlmClient(internal, base_url=settings.litellm_base_url, api_key=settings.litellm_key,
                                extract_alias=settings.extract_alias, guard_alias=settings.guard_alias,
                                timeout_s=settings.llm_timeout_s)
        pipeline = Pipeline(pool, gaz, llm, extraction_max_attempts=settings.extraction_max_attempts)
        runner = FeedRunner(settings, pipeline, gaz,
                            Fetcher(feed_client, min_interval_s=settings.min_interval_s, max_bytes=settings.http_max_bytes))
        async with pool.acquire() as lock_conn:
            if not await _adapter_lock(lock_conn, adapter):
                log.info("another run of this adapter is active; skipping", extra={"adapter": adapter})
                return 0
            try:
                now = utc_now()
                if adapter == "extraction":
                    report = await pipeline.process_pending(now=now, limit=settings.extraction_batch)
                    log.info("extraction run", extra=report.as_dict())
                    return 0
                if adapter == "sweep":
                    changes = await pipeline.sweep(now)
                    for tenant in await pipeline.tenants():
                        changes += await pipeline.sweep(now, tenant=tenant)
                    log.info("sweep run", extra={"alert_changes": len(changes)})
                    return 0
                await runner.refresh_schedule()
                results = []
                for _adapter, cid, _cadence in runner.connectors(include_disabled_adapters=True):
                    if _adapter != adapter or connector not in (None, cid):
                        continue
                    if cid in runner.paused:
                        log.info("source paused: no tenant subscribes to it", extra={"connector": cid})
                        continue
                    results += await runner.poll(adapter, now=now, connector_id=cid)
                for r in results:
                    log.info("poll", extra=r.summary())
                return 1 if any(r.status == "error" for r in results) else 0
            finally:
                await _adapter_unlock(lock_conn, adapter)
    finally:
        await feed_client.aclose()
        await internal.aclose()
        await pool.close()


async def run_loop(settings: Settings, stop: asyncio.Event) -> None:
    pool = await create_pool(settings.database_url, max_size=3)
    feed_client = build_feed_client(settings)
    try:
        gaz = load_gazetteer(settings.geo_dir)
        pipeline = Pipeline(pool, gaz, None, extraction_max_attempts=settings.extraction_max_attempts)
        runner = FeedRunner(settings, pipeline, gaz,
                            Fetcher(feed_client, min_interval_s=settings.min_interval_s, max_bytes=settings.http_max_bytes))
        while not stop.is_set():
            leader: asyncpg.Connection | None = None
            try:
                leader = await asyncpg.connect(settings.database_url)
                if not await leader.fetchval("SELECT pg_try_advisory_lock($1)", LEADER_LOCK):
                    log.info("not the leader; waiting")
                    await leader.close()
                    leader = None
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=15)
                    continue
                log.info("poller leader", extra={"connectors": [c for _, c, _ in runner.connectors()]})
                last_sweep = 0.0
                loop = asyncio.get_running_loop()
                while not stop.is_set():
                    await leader.fetchval("SELECT 1")  # lock is session-scoped: the connection must stay alive
                    now = utc_now()
                    await runner.refresh_schedule()
                    for adapter, cid, cadence in runner.connectors():
                        if stop.is_set():
                            break
                        if cid in runner.paused:
                            continue
                        if await runner.due(cid, now, cadence):
                            results = await runner.poll(adapter, now=utc_now(),
                                                        connector_id=cid if adapter in sources.MULTI_CONNECTOR else None)
                            for r in results:
                                log.info("poll", extra=r.summary())
                    if loop.time() - last_sweep >= 60:
                        await pipeline.sweep(utc_now())
                        for tenant in await pipeline.tenants():
                            await pipeline.sweep(utc_now(), tenant=tenant)
                        last_sweep = loop.time()
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=10)
            except Exception as exc:  # never let one bad poll/sweep end the leader loop (CancelledError passes)
                log.warning("poller loop error; retrying", extra={"error": f"{type(exc).__name__}: {exc}"[:300]})
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=10)
            finally:
                if leader is not None and not leader.is_closed():
                    with contextlib.suppress(Exception):
                        await leader.close()
    finally:
        await feed_client.aclose()
        await pool.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="signal_hub.poller")
    parser.add_argument("--once", action="store_true", help="run one adapter once and exit")
    parser.add_argument("--adapter", choices=ADAPTERS)
    parser.add_argument("--connector", help="rss / meteoalarm: only this source (connector id, e.g. meteoalarm.netherlands)")
    args = parser.parse_args(argv)
    settings = load_settings()
    configure_logging(settings.log_level)
    if args.once:
        if not args.adapter:
            parser.error("--once needs --adapter")
        sys.exit(asyncio.run(run_once(settings, args.adapter, connector=args.connector)))

    async def _main() -> None:
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, stop.set)
        await run_loop(settings, stop)

    asyncio.run(_main())


if __name__ == "__main__":
    main()
