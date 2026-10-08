"""Demo pack replay (``100`` §7): deterministic, tenant-scoped, labelled ``simulated``.

A pack is ``<SCENARIOS_DIR>/<pack>/pack.yaml`` (:class:`~keelson_contracts.signals.ScenarioPack`)
plus fixture files next to it, served at ``/demo-feeds/<pack>/<file>`` so the **real adapters**
fetch and parse them (the base URL is overridden per adapter while a pack runs).

Step payloads (``ScenarioPackStep.payload``) per adapter:

* ``gdacs`` / ``usgs``: ``{fixture: "<file>.geojson"}`` — GDACS event list (may embed footprints) /
  USGS summary GeoJSON.
* ``rss``: ``{fixture: "<file>.xml", connector_id?: "rss.<slug>", source_name?: str,
  extraction?: NewsExtraction, extractions?: {<guid|link|title>: NewsExtraction}}`` — pre-baked
  extractions (``extracted_by: prebaked``) so no LLM is needed; items without one go through the
  normal quarantine (``pending_extraction`` when the LLM is unavailable).
* ``merxa_inject``: ``{event: {type, id, timestamp?, data}} | {fixture: "<file>.json"}`` (+ optional
  ``extraction`` for the free text) — handled like a forwarded Merxa webhook.
* ``chat_hint``: ``{text: str}`` — a presenter hint, logged only.

SimClock: ``sim_now = start_at + offset_s (+ speed × wall seconds since start when speed > 0)``.
``advance`` moves the offset (``seconds``, ``to_step``, or by default to the next step) and applies
every due step in order. All ids derive from content (``uuid5``) and every timestamp from the sim
clock, so a reset + replay reproduces the same signals, events and alerts.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

import asyncpg
import yaml
from pydantic import ValidationError

from keelson_contracts.signals import PACK_ID_PATTERN, ScenarioPack, ScenarioPackStep, pack_step_id
from signal_hub.adapters.connector import ConnectorEvent, parse_connector_event
from signal_hub.adapters.gdacs import parse_gdacs
from signal_hub.adapters.rss import parse_feed
from signal_hub.adapters.usgs import parse_usgs
from signal_hub.db import tx
from signal_hub.gazetteer import Gazetteer
from signal_hub.httpfetch import Fetcher
from signal_hub.logs import get_logger
from signal_hub.models import NewsItemDraft, SourceContext
from signal_hub.pipeline import CLUSTER_LOCK, IngestReport, Pipeline

__all__ = ["FIXTURE_TYPES", "PackError", "Replay", "fixture_path", "list_packs", "load_pack"]

log = get_logger(__name__)
FIXTURE_TYPES: Final[dict[str, str]] = {
    ".geojson": "application/geo+json",
    ".json": "application/json",
    ".xml": "application/rss+xml",
    ".rss": "application/rss+xml",
    ".atom": "application/atom+xml",
    ".txt": "text/plain; charset=utf-8",
}
_PACK_RE = re.compile(PACK_ID_PATTERN)


class PackError(ValueError):
    pass


def _pack_dir(scenarios_dir: Path, pack_id: str) -> Path:
    if not _PACK_RE.match(pack_id or ""):
        raise PackError("invalid pack id")
    return scenarios_dir / pack_id


def load_pack(scenarios_dir: Path, pack_id: str) -> ScenarioPack:
    path = _pack_dir(scenarios_dir, pack_id) / "pack.yaml"
    if not path.is_file():
        raise PackError(f"pack {pack_id!r} not found")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        pack = ScenarioPack.model_validate(data)
    except (yaml.YAMLError, ValidationError) as exc:
        raise PackError(f"pack {pack_id!r} is invalid: {exc}") from exc
    if pack.id != pack_id:
        raise PackError(f"pack {pack_id!r}: id in pack.yaml is {pack.id!r}")
    return pack


def list_packs(scenarios_dir: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not scenarios_dir.is_dir():
        return out
    for entry in sorted(scenarios_dir.iterdir()):
        if not entry.is_dir() or not (entry / "pack.yaml").is_file():
            continue
        try:
            pack = load_pack(scenarios_dir, entry.name)
        except PackError as exc:
            out.append({"id": entry.name, "error": str(exc)[:300]})
            continue
        out.append({"id": pack.id, "title": pack.title, "description": pack.description,
                    "start_at": pack.start_at.isoformat(), "steps": len(pack.steps)})
    return out


def fixture_path(scenarios_dir: Path, pack_id: str, rel: str) -> Path:
    """The fixture file for ``/demo-feeds/<pack>/<rel>`` (no traversal, known types only)."""
    base = _pack_dir(scenarios_dir, pack_id).resolve()
    if not rel or rel.startswith(("/", "\\")) or ".." in Path(rel).parts or "\\" in rel:
        raise PackError("invalid fixture path")
    target = (base / rel).resolve()
    if base not in target.parents or target.suffix.lower() not in FIXTURE_TYPES or not target.is_file():
        raise PackError("fixture not found")
    return target


@dataclass(slots=True)
class RunState:
    tenant: str
    pack_id: str
    sim_start: dt.datetime
    started_wall_at: dt.datetime
    speed: float
    offset_s: int
    last_step_index: int
    status: str
    log: list[dict[str, Any]]

    def sim_now(self, wall_now: dt.datetime) -> dt.datetime:
        elapsed = 0.0
        if self.speed > 0:
            elapsed = max(0.0, (wall_now - self.started_wall_at).total_seconds()) * self.speed
        return self.sim_start + dt.timedelta(seconds=self.offset_s + int(elapsed))

    def as_dict(self, pack: ScenarioPack, wall_now: dt.datetime) -> dict[str, Any]:
        now = self.sim_now(wall_now)
        nxt = self.last_step_index + 1
        return {
            "tenant": self.tenant, "pack_id": self.pack_id, "status": self.status, "speed": self.speed,
            "sim_start": self.sim_start.isoformat(), "sim_now": now.isoformat(),
            "elapsed_s": int((now - self.sim_start).total_seconds()),
            "steps_total": len(pack.steps), "steps_applied": self.last_step_index + 1,
            "next_step": ({"index": nxt, "t_offset_s": pack.steps[nxt].t_offset_s, "adapter": pack.steps[nxt].adapter,
                           "note": pack.steps[nxt].note} if nxt < len(pack.steps) else None),
            "log": self.log,
        }


def _run_from_row(row: asyncpg.Record) -> RunState:
    return RunState(row["tenant"], row["pack_id"], row["sim_start"], row["started_wall_at"], float(row["speed"]),
                    int(row["offset_s"]), int(row["last_step_index"]), row["status"], list(row["log"] or []))


class Replay:
    def __init__(self, scenarios_dir: Path, demo_feeds_base_url: str, pipeline: Pipeline, gaz: Gazetteer,
                 fetcher: Fetcher) -> None:
        self.scenarios_dir = scenarios_dir
        self.base_url = demo_feeds_base_url.rstrip("/")
        self.pipeline = pipeline
        self.pool: asyncpg.Pool = pipeline.pool
        self.gaz = gaz
        self.fetcher = fetcher

    # ───────────── state ─────────────
    async def _load(self, conn: asyncpg.Connection, tenant: str, pack_id: str, *, lock: bool = False) -> RunState | None:
        row = await conn.fetchrow(
            f"SELECT * FROM demo_run WHERE tenant = $1 AND pack_id = $2{' FOR UPDATE' if lock else ''}", tenant, pack_id
        )
        return _run_from_row(row) if row is not None else None

    async def _save(self, conn: asyncpg.Connection, run: RunState) -> None:
        await conn.execute(
            """INSERT INTO demo_run (tenant, pack_id, sim_start, started_wall_at, speed, offset_s, last_step_index,
                                     status, log, updated_at)
               VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, now())
               ON CONFLICT (tenant, pack_id) DO UPDATE SET sim_start = EXCLUDED.sim_start,
                 started_wall_at = EXCLUDED.started_wall_at, speed = EXCLUDED.speed, offset_s = EXCLUDED.offset_s,
                 last_step_index = EXCLUDED.last_step_index, status = EXCLUDED.status, log = EXCLUDED.log,
                 updated_at = now()""",
            run.tenant, run.pack_id, run.sim_start, run.started_wall_at, run.speed, run.offset_s,
            run.last_step_index, run.status, run.log,
        )

    async def state(self, tenant: str, pack_id: str, *, wall_now: dt.datetime) -> dict[str, Any]:
        pack = load_pack(self.scenarios_dir, pack_id)
        async with tx(self.pool, tenant) as conn:
            run = await self._load(conn, tenant, pack_id)
        if run is None:
            return {"tenant": tenant, "pack_id": pack_id, "status": "idle", "steps_total": len(pack.steps),
                    "steps_applied": 0, "title": pack.title}
        return {**run.as_dict(pack, wall_now), "title": pack.title}

    # ───────────── commands ─────────────
    async def reset(self, tenant: str, pack_id: str) -> dict[str, Any]:
        load_pack(self.scenarios_dir, pack_id)
        return await self._reset_data(tenant, pack_id)

    async def reset_all(self, tenant: str) -> dict[str, Any]:
        """Every pack of this tenant: the packs on disk plus any pack id still present in its data
        (e.g. a pack removed since it ran). ``{tenant, packs, counts}`` (summed deletions)."""
        packs = {p["id"] for p in list_packs(self.scenarios_dir) if "error" not in p}
        async with tx(self.pool, tenant) as conn:
            for table in ("demo_run", "event", "signal", "news_item"):
                packs |= {r["pack_id"] for r in await conn.fetch(
                    f"SELECT DISTINCT pack_id FROM {table} WHERE tenant = $1 AND pack_id IS NOT NULL", tenant)}
        counts: dict[str, int] = {"packs": 0}
        for pack_id in sorted(p for p in packs if _PACK_RE.match(p)):
            result = await self._reset_data(tenant, pack_id)
            counts["packs"] += 1
            for key, value in result["deleted"].items():
                counts[key] = counts.get(key, 0) + int(value)
        for key, value in (await self._reset_tenant_data(tenant)).items():
            counts[key] = counts.get(key, 0) + int(value)
        return {"tenant": tenant, "packs": sorted(packs), "counts": counts}

    async def _reset_tenant_data(self, tenant: str) -> dict[str, int]:
        """The rest of the tenant's hub state (the demo reset puts the connected system back to its
        seed, so what its old webhooks said no longer holds): connector-event signals, events and
        free-text items, plus every remaining alert (also on global feed events), its change rows
        and the tenant's exposures. Global feed data, the tenant network and alert rules stay; the
        engine's post-reset scn resync pushes the network again, which re-evaluates the open global
        events (an alert that still applies is raised afresh)."""
        async with tx(self.pool, tenant) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock($1)", CLUSTER_LOCK)

            async def delete(sql: str) -> int:
                return int(await conn.fetchval(f"WITH d AS ({sql} RETURNING 1) SELECT count(*) FROM d", tenant))

            counts = {
                "alert_changes": await delete("DELETE FROM alert_change WHERE tenant = $1"),
                "alerts": await delete("DELETE FROM alert WHERE tenant = $1"),
                "exposures": await delete("DELETE FROM exposure WHERE tenant = $1"),
                "events": await delete("DELETE FROM event WHERE tenant = $1 AND pack_id IS NULL"),
                "signals": await delete("DELETE FROM signal WHERE tenant = $1 AND pack_id IS NULL"),
                "news_items": await delete("DELETE FROM news_item WHERE tenant = $1 AND pack_id IS NULL"),
            }
        log.info("demo tenant data reset", extra={"tenant": tenant, **counts})
        return counts

    async def _reset_data(self, tenant: str, pack_id: str) -> dict[str, Any]:
        async with tx(self.pool, tenant) as conn:
            await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", f"demo:{tenant}:{pack_id}")
            counts = {
                "alert_changes": await conn.fetchval(
                    """WITH d AS (DELETE FROM alert_change WHERE tenant = $1 AND alert_id IN
                       (SELECT a.id FROM alert a JOIN event e ON e.id = a.event_id WHERE e.tenant = $1 AND e.pack_id = $2)
                       RETURNING 1) SELECT count(*) FROM d""", tenant, pack_id),
                "events": await conn.fetchval(
                    "WITH d AS (DELETE FROM event WHERE tenant = $1 AND pack_id = $2 RETURNING 1) SELECT count(*) FROM d",
                    tenant, pack_id),
                "signals": await conn.fetchval(
                    "WITH d AS (DELETE FROM signal WHERE tenant = $1 AND pack_id = $2 RETURNING 1) SELECT count(*) FROM d",
                    tenant, pack_id),
                "news_items": await conn.fetchval(
                    "WITH d AS (DELETE FROM news_item WHERE tenant = $1 AND pack_id = $2 RETURNING 1) SELECT count(*) FROM d",
                    tenant, pack_id),
            }
            await conn.execute("DELETE FROM demo_run WHERE tenant = $1 AND pack_id = $2", tenant, pack_id)
        log.info("demo pack reset", extra={"tenant": tenant, "pack": pack_id, **counts})
        return {"tenant": tenant, "pack_id": pack_id, "status": "idle", "deleted": counts}

    async def start(self, tenant: str, pack_id: str, *, wall_now: dt.datetime, speed: float = 0.0) -> dict[str, Any]:
        pack = load_pack(self.scenarios_dir, pack_id)
        await self.reset(tenant, pack_id)
        await self.pipeline.ensure_tenant(tenant)
        run = RunState(tenant, pack_id, pack.start_at, wall_now, max(0.0, speed), 0, -1, "running", [])
        async with tx(self.pool, tenant) as conn:
            await self._save(conn, run)
        return await self._apply_due(tenant, pack, wall_now=wall_now)

    async def advance(self, tenant: str, pack_id: str, *, wall_now: dt.datetime, seconds: int | None = None,
                      to_step: int | None = None) -> dict[str, Any]:
        pack = load_pack(self.scenarios_dir, pack_id)
        async with tx(self.pool, tenant) as conn:
            run = await self._load(conn, tenant, pack_id, lock=True)
            if run is None:
                raise PackError(f"pack {pack_id!r} is not running (start it first)")
            elapsed = int((run.sim_now(wall_now) - run.sim_start).total_seconds())
            if seconds is not None:
                run.offset_s += max(0, int(seconds))
            else:
                target = run.last_step_index + 1 if to_step is None else to_step
                if 0 <= target < len(pack.steps):
                    run.offset_s += max(0, pack.steps[target].t_offset_s - elapsed)
            await self._save(conn, run)
        return await self._apply_due(tenant, pack, wall_now=wall_now)

    async def tick(self, *, wall_now: dt.datetime) -> int:
        """Auto-advance runs with ``speed > 0`` (every registered tenant). Returns steps applied."""
        applied = 0
        for tenant in await self.pipeline.tenants():
            async with tx(self.pool, tenant) as conn:
                rows = await conn.fetch(
                    "SELECT pack_id FROM demo_run WHERE tenant = $1 AND status = 'running' AND speed > 0", tenant)
            for r in rows:
                try:
                    pack = load_pack(self.scenarios_dir, r["pack_id"])
                    state = await self._apply_due(tenant, pack, wall_now=wall_now)
                    applied += len(state.get("applied_now", []))
                except PackError as exc:
                    log.warning("demo tick skipped", extra={"tenant": tenant, "pack": r["pack_id"], "error": str(exc)})
        return applied

    # ───────────── step application ─────────────
    async def _apply_due(self, tenant: str, pack: ScenarioPack, *, wall_now: dt.datetime) -> dict[str, Any]:
        applied_now: list[dict[str, Any]] = []
        async with self.pool.acquire() as lock_conn:
            key = f"demo-apply:{tenant}:{pack.id}"
            await lock_conn.execute("SELECT pg_advisory_lock(hashtextextended($1, 0))", key)
            try:
                async with tx(self.pool, tenant) as conn:
                    run = await self._load(conn, tenant, pack.id)
                if run is None:
                    raise PackError(f"pack {pack.id!r} is not running")
                sim_now = run.sim_now(wall_now)
                for index, step in pack.due_steps(sim_now, after_index=run.last_step_index):
                    at = pack.start_at + dt.timedelta(seconds=step.t_offset_s)
                    entry = await self._apply_step(tenant, pack, index, step, at)
                    run.last_step_index = index
                    run.log.append(entry)
                    applied_now.append(entry)
                    async with tx(self.pool, tenant) as conn:
                        await self._save(conn, run)
                await self.pipeline.sweep(sim_now, tenant=tenant, pack_id=pack.id)
                if run.last_step_index >= len(pack.steps) - 1 and run.status != "finished":
                    run.status = "finished"
                    async with tx(self.pool, tenant) as conn:
                        await self._save(conn, run)
            finally:
                await lock_conn.execute("SELECT pg_advisory_unlock(hashtextextended($1, 0))", key)
        return {**run.as_dict(pack, wall_now), "applied_now": applied_now, "title": pack.title}

    async def _fixture(self, pack: ScenarioPack, name: Any) -> bytes:
        if not isinstance(name, str) or not name:
            raise PackError("step payload needs a 'fixture' file name")
        fixture_path(self.scenarios_dir, pack.id, name)  # validates before fetching
        result = await self.fetcher.get(f"demo:{pack.id}", f"{self.base_url}/{pack.id}/{name}", throttle=False)
        return result.body

    async def _apply_step(self, tenant: str, pack: ScenarioPack, index: int, step: ScenarioPackStep,
                          at: dt.datetime) -> dict[str, Any]:
        ctx = SourceContext(retrieved_at=at, tenant=tenant, pack_id=pack.id)
        payload = step.payload
        report = IngestReport()
        if step.adapter == "gdacs":
            doc = json.loads(await self._fixture(pack, payload.get("fixture")))
            report = await self.pipeline.ingest(parse_gdacs(doc, ctx, self.gaz), now=at)
        elif step.adapter == "usgs":
            doc = json.loads(await self._fixture(pack, payload.get("fixture")))
            report = await self.pipeline.ingest(parse_usgs(doc, ctx, self.gaz), now=at)
        elif step.adapter == "rss":
            body = await self._fixture(pack, payload.get("fixture"))
            items = parse_feed(body, ctx, connector_id=str(payload.get("connector_id") or "rss.demo"),
                               source_name=str(payload.get("source_name") or "Demo Freight Daily"))
            report = await self._news(items, tenant, payload, at)
        elif step.adapter == "merxa_inject":
            raw = payload.get("event")
            if raw is None:
                raw = json.loads(await self._fixture(pack, payload.get("fixture")))
            event = ConnectorEvent.model_validate(raw)
            drafts, texts = parse_connector_event(event, ctx)
            report = await self.pipeline.ingest(drafts, now=at)
            report.merge(await self._news(texts, tenant, payload, at))
        # chat_hint: presenter hint only (nothing ingested)
        return {
            "index": index, "step_id": pack_step_id(pack.id, index), "adapter": step.adapter,
            "t_offset_s": step.t_offset_s, "at": at.isoformat(), "note": step.note,
            "hint": payload.get("text") if step.adapter == "chat_hint" else None,
            "result": report.as_dict(),
        }

    async def _news(self, items: list[NewsItemDraft], tenant: str, payload: dict[str, Any], at: dt.datetime) -> IngestReport:
        raw_by_key = payload.get("extractions")
        by_key: dict[str, Any] = raw_by_key if isinstance(raw_by_key, dict) else {}
        raw_default = payload.get("extraction")
        default = raw_default if isinstance(raw_default, dict) else None
        for item in items:
            native = str(item.context.get("native_id") or "")
            prebaked = by_key.get(native) or by_key.get(item.url or "") or by_key.get(item.title) or default
            if isinstance(prebaked, dict):
                item.context["prebaked"] = prebaked
        ids = await self.pipeline.queue_news(items, now=at)
        report = IngestReport(news_queued=len(ids))
        if ids:
            report.merge(await self.pipeline.process_news(ids, tenant=tenant, now=at))
        return report
