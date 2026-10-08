"""Runtime settings: the engine ``Settings`` (E1, ``keelson_engine.config``) plus a few
worker knobs with sensible defaults (env-overridable)."""

from __future__ import annotations

from typing import Annotated

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

__all__ = ["WorkerSettings", "engine_settings"]


def engine_settings():  # type: ignore[no-untyped-def]
    """The process-wide engine settings (``keelson_engine.config.get_settings``)."""
    from keelson_engine.config import get_settings  # noqa: PLC0415 - keep import light

    return get_settings()


class WorkerSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="WORKER_", extra="ignore")

    heartbeat_interval_s: float = 15.0
    cancel_poll_interval_s: float = 2.0
    reaper_interval_s: float = 15.0
    idle_poll_interval_s: float = 5.0
    max_attempts: int = 3
    shutdown_grace_s: float = 25.0
    snapshot_backoff_initial_s: float = 2.0
    snapshot_backoff_max_s: float = 60.0
    concurrency: int = 1
    runner_cache_size: int = 8
    # canonical snapshot sync (80-m3-deterministic-core.md §1); runs beside the claim loop
    scn_sync_enabled: bool = True
    scn_due_interval_s: float = 5.0
    """How often ``due_syncs()`` is asked what is due (debounced webhooks / polls, retries, requests)."""
    scn_poll_interval_s: float = 300.0
    """Tier-C ``scn.changes_since`` poll cadence (``poll_changes`` per syncable connector)."""
    scn_nightly_hour_utc: Annotated[int, Field(ge=0, le=23)] = 2
    """UTC hour from which the daily full resync is requested."""
    scn_poll_timeout_s: float = 60.0
    """Upper bound of one connector's change-feed poll (a hung connector never stalls the due loop)."""
    scn_sync_timeout_s: float = 600.0
    """Upper bound of one ``sync_connector`` call (the platform keeps the previous snapshot on failure)."""
    scn_crawl_interval_s: float = 3600.0
    """M7: re-crawl interval of tier-A connectors (no change feed); ``0`` = only stale marks / nightly."""
    # background GC of snapshots marked by syncs / demo resets (core.scn_sync.collect_garbage)
    scn_gc_enabled: bool = True
    scn_gc_interval_s: float = 10.0
    """How often the GC task looks for marked snapshots when it has nothing left to do."""
    scn_gc_batch_rows: Annotated[int, Field(ge=1)] = 20_000
    """Rows per delete statement (one short transaction) and entity table."""
    scn_gc_batches_per_tick: Annotated[int, Field(ge=1)] = 25
    """Delete statements per GC call; with work left the next call follows after ``scn_gc_pause_s``."""
    scn_gc_pause_s: float = 0.05
    """Pause between two batches (yields the database to syncs, readers and runs)."""
    # M5 Guide auto-tick (95 §2)
    guide_verify_enabled: bool = True
    guide_tick_interval_s: float = 5.0
    """How often open checklists are looked at (a new webhook event triggers a read-back)."""
    guide_poll_interval_s: float = 60.0
    """Read-back poll cadence per (tenant, connector) while a checklist is open."""
    guide_verify_timeout_s: float = 120.0
    """Upper bound of one verification tick."""
