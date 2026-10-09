"""Tier B: ``scn.snapshot_open`` / ``scn.snapshot_export`` and the signed part downloads
(contract 122 §4.1–4.2; the Merxa shapes of ``22-merxa-m2.md`` §4).

``open_snapshot`` refreshes the sources (drift is journalled first), then — one build at a time —
reads the change-feed watermark ``j:<seq>`` and runs every mapped entity's SQL inside one
consistent read transaction (for a SQL backend the watermark is read just *before* its snapshot,
so a change it cannot see is reported again later rather than lost). Rows are converted and
validated (:mod:`adapter_sidecar.scn_export`), ``source_id`` must be unique per entity, and parts of
at most ``snapshot.part_rows`` rows are written to ``SNAPSHOT_DIR/<id>/<entity>/part-NNNN.parquet``
(zstd, read-only, sha256). **Every** ``scn/1.0`` entity is in the manifest — an unmapped one as a
single empty part — because Keelson's sync requires all 15.

``export_urls`` returns HMAC-SHA256 signed URLs on ``PUBLIC_URL``
(``/v1/snapshots/<id>/<entity>/part-NNNN.parquet?exp=<unix>&sig=<hex>``, ``sig`` over
``"<id>/<entity>/<file>:<exp>"``); the download route needs no auth header. ``PUBLIC_URL`` must be
the origin of the MCP URL registered in Keelson, which refuses parts on any other host.
Snapshots expire after ``snapshot.ttl_s``; :meth:`SnapshotService.cleanup` deletes them.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import shutil
import time
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from adapter_sidecar import journal
from adapter_sidecar.backends import Backend
from adapter_sidecar.config import SidecarConfig
from adapter_sidecar.db import begin_write, iso, jdump, jload
from adapter_sidecar.errors import Problem, SourceDataError, not_found
from adapter_sidecar.logs import get_logger
from adapter_sidecar.scn_export import RowError, convert_row, write_part
from adapter_sidecar.settings import Settings
from keelson_contracts.scn import CANONICAL_MODEL, SCN_COLUMNS, SCN_ENTITIES

log = get_logger(__name__)

PART_RE = re.compile(r"^part-(\d{4})\.parquet$")
SIG_RE = re.compile(r"^[0-9a-f]{64}$")
ID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
MEDIA_TYPE = "application/vnd.apache.parquet"


def entity_columns_problem(entity: str, columns: list[str]) -> str | None:
    want = list(SCN_COLUMNS[entity])
    if sorted(columns) != sorted(want) or len(columns) != len(set(columns)):
        missing = [c for c in want if c not in columns]
        extra = [c for c in columns if c not in want]
        return (f"entities.{entity}: the SQL must return exactly the scn/1.0 columns "
                f"(missing {missing}, unknown {extra})")
    return None


class SnapshotService:
    def __init__(self, *, cfg: SidecarConfig, settings: Settings, state: AsyncEngine, backend: Backend,
                 lock: asyncio.Lock, sync_sources: Callable[..., Awaitable[list[str]]]) -> None:
        self.cfg = cfg
        self.settings = settings
        self.state = state
        self.backend = backend
        self.lock = lock
        self.sync_sources = sync_sources
        self.root = settings.snapshot_dir
        self.ttl_s = settings.snapshot_ttl_s or cfg.snapshot.ttl_s
        secret = settings.snapshot_secret
        if not secret:
            log.warning("SIDECAR_SNAPSHOT_SECRET is not set; signed snapshot URLs are valid for this process only")
            secret = secrets.token_urlsafe(32)
        self._secret = secret.encode("utf-8")
        self._build_slot = asyncio.Semaphore(1)

    # ───────────── open ─────────────

    async def open_snapshot(self, entities: Any, *, created_by: str | None = None) -> dict[str, Any]:
        if not isinstance(entities, list) or not entities or not all(isinstance(e, str) for e in entities):
            raise Problem(400, "invalid_parameter", f"entities must be a non-empty list of {list(SCN_ENTITIES)}")
        unknown = sorted(set(entities) - set(SCN_ENTITIES))
        if unknown:
            raise Problem(400, "unknown_entity", f"unknown scn/1.0 entities {unknown}; known: {list(SCN_ENTITIES)}")
        wanted = [e for e in SCN_ENTITIES if e in set(entities)]
        async with self.lock:
            try:
                await self.sync_sources(force=True)
            except SourceDataError as exc:
                raise Problem(503, "source_invalid", str(exc)) from None
        async with self._build_slot:
            return await self._build(wanted, created_by)

    async def _build(self, wanted: list[str], created_by: str | None) -> dict[str, Any]:
        snap_id = str(uuid.uuid7())
        base = self.root / snap_id
        started = time.perf_counter()
        now = datetime.now(UTC)
        expires = now + timedelta(seconds=self.ttl_s)
        crawled = self.backend.consistency == "crawled"
        rng = self.backend.as_of_range if crawled else None
        as_of = rng[1] if rng is not None else now
        manifest: dict[str, Any] = {}
        try:
            if self.backend.shared_state:
                async with self.backend.read() as conn:
                    watermark = await journal.high_water(conn)
                    for entity in wanted:
                        manifest[entity] = await self._entity(conn, base, entity)
            else:
                async with self.state.connect() as s:
                    watermark = await journal.high_water(s)
                async with self.backend.read() as conn:
                    for entity in wanted:
                        manifest[entity] = await self._entity(conn, base, entity)
            consistency = "crawled" if crawled else "consistent"
            async with begin_write(self.state) as conn:
                await conn.execute(
                    text("INSERT INTO snapshot (id, created_at, expires_at, as_of, watermark, consistency, as_of_from, "
                         "as_of_to, manifest, created_by) VALUES (:i, :c, :e, :a, :w, :k, :f, :t, :m, :by)"),
                    {"i": snap_id, "c": iso(now), "e": iso(expires), "a": iso(as_of),
                     "w": journal.format_watermark(watermark), "k": consistency,
                     "f": iso(rng[0]) if rng else None, "t": iso(rng[1]) if rng else None,
                     "m": jdump({"canonical_model": CANONICAL_MODEL, "entities": manifest}), "by": created_by},
                )
        except OSError as exc:
            shutil.rmtree(base, ignore_errors=True)
            log.error("snapshot storage not writable", extra={"dir": str(self.root), "error": str(exc)})
            raise Problem(503, "snapshot_storage_unavailable", "snapshot storage is not writable") from exc
        except RowError as exc:
            shutil.rmtree(base, ignore_errors=True)
            raise Problem(422, "invalid_source_data", str(exc), extra={"reason": exc.entity}) from None
        except BaseException:
            shutil.rmtree(base, ignore_errors=True)
            raise
        log.info("scn snapshot written", extra={"snapshot": snap_id, "entities": len(wanted),
                                                "rows": sum(m["rows"] for m in manifest.values()),
                                                "ms": round((time.perf_counter() - started) * 1000)})
        return {
            "snapshot_id": snap_id,
            "as_of": iso(as_of),
            "watermark": journal.format_watermark(watermark),
            "canonical_model": CANONICAL_MODEL,
            "expires_at": iso(expires),
            "consistency": consistency,
            "as_of_range": {"from": iso(rng[0]), "to": iso(rng[1])} if rng else None,
            "entities": {e: {"rows": m["rows"], "parts": len(m["parts"]), "sha256": [p["sha256"] for p in m["parts"]]}
                         for e, m in manifest.items()},
        }

    async def _entity(self, conn: AsyncConnection, base: Path, entity: str) -> dict[str, Any]:
        part_rows = self.cfg.snapshot.part_rows
        spec = self.cfg.entities.get(entity)
        parts: list[dict[str, Any]] = []
        if spec is None:
            info = await asyncio.to_thread(write_part, base / entity / "part-0001.parquet", entity, [])
            return {"rows": 0, "parts": [{"file": "part-0001.parquet", **info}]}
        result = await conn.stream(text(spec.sql))
        columns = list(result.keys())
        if (problem := entity_columns_problem(entity, columns)) is not None:
            await result.close()
            raise Problem(500, "invalid_entity_sql", problem)
        seen: set[str] = set()
        n = 0
        batch: list[dict[str, Any]] = []

        async def flush() -> None:
            nonlocal n, batch
            n += 1
            name = f"part-{n:04d}.parquet"
            info = await asyncio.to_thread(write_part, base / entity / name, entity, batch)
            parts.append({"file": name, **info})
            batch = []

        async for row in result:
            converted = convert_row(entity, columns, tuple(row))
            sid = converted["source_id"]
            if sid in seen:
                await result.close()
                raise RowError(entity, sid, "duplicate source_id")
            seen.add(sid)
            batch.append(converted)
            if len(batch) >= part_rows:
                await flush()
        if batch or n == 0:
            await flush()
        return {"rows": sum(p["rows"] for p in parts), "parts": parts}

    async def _load(self, snapshot_id: str) -> Any:
        if not ID_RE.match(str(snapshot_id)):
            raise not_found("snapshot", str(snapshot_id))
        async with self.state.connect() as conn:
            row = (await conn.execute(text("SELECT id, expires_at, manifest FROM snapshot WHERE id = :i"),
                                      {"i": snapshot_id})).first()
        if row is None or _ts(row.expires_at) <= datetime.now(UTC):
            raise not_found("snapshot", str(snapshot_id))
        return row

    # ───────────── export ─────────────

    async def export_urls(self, snapshot_id: str, entity: str) -> dict[str, Any]:
        row = await self._load(snapshot_id)
        entry = ((jload(row.manifest) or {}).get("entities") or {}).get(entity)
        if entry is None:
            raise Problem(404, "not_found", f"entity {entity!r} is not part of snapshot {snapshot_id}")
        exp = int(_ts(row.expires_at).timestamp())
        base = self.settings.public_url.rstrip("/")
        parts = []
        for p in entry["parts"]:
            path = f"{row.id}/{entity}/{p['file']}"
            parts.append({"uri": f"{base}/v1/snapshots/{path}?exp={exp}&sig={self.sign(path, exp)}",
                          "sha256": p["sha256"], "rows": p["rows"], "bytes": p["bytes"]})
        return {"snapshot_id": row.id, "entity": entity, "canonical_model": CANONICAL_MODEL, "rows": entry["rows"],
                "expires_at": row.expires_at, "parts": parts}

    def sign(self, path: str, exp: int) -> str:
        return hmac.new(self._secret, f"{path}:{exp}".encode(), hashlib.sha256).hexdigest()

    def resolve_download(self, snapshot_id: str, entity: str, file: str, exp: str | None, sig: str | None) -> Path:
        """Check the signature / expiry of a download URL; returns the file path."""
        if not ID_RE.match(snapshot_id) or entity not in SCN_ENTITIES or not PART_RE.match(file):
            raise Problem(404, "not_found", "no such snapshot file")
        if not exp or not sig or not exp.isascii() or not exp.isdigit() or len(exp) > 12:
            raise Problem(403, "invalid_signature", "exp and sig are required")
        if not SIG_RE.match(sig):  # also keeps non-ASCII out of compare_digest
            raise Problem(403, "invalid_signature", "signature does not match")
        path = f"{snapshot_id}/{entity}/{file}"
        if not hmac.compare_digest(self.sign(path, int(exp)), sig):
            raise Problem(403, "invalid_signature", "signature does not match")
        if int(exp) < int(time.time()):
            raise Problem(403, "url_expired", "this download URL has expired")
        full = self.root / snapshot_id / entity / file
        if not full.is_file():
            raise Problem(404, "not_found", "no such snapshot file")
        return full

    # ───────────── housekeeping ─────────────

    async def cleanup(self) -> int:
        now = iso(datetime.now(UTC))
        async with begin_write(self.state) as conn:
            gone = [str(r[0]) for r in (await conn.execute(text("SELECT id FROM snapshot WHERE expires_at <= :n"),
                                                           {"n": now})).all()]
            await conn.execute(text("DELETE FROM snapshot WHERE expires_at <= :n"), {"n": now})
            live = {str(r[0]) for r in (await conn.execute(text("SELECT id FROM snapshot"))).all()}
        removed = 0
        if self.root.is_dir():
            cutoff = time.time() - self.ttl_s
            for d in self.root.iterdir():
                if not d.is_dir():
                    continue
                orphan = d.name not in live and d.stat().st_mtime < cutoff
                if d.name in gone or orphan:
                    _rmtree_readonly(d)
                    removed += 1
        return removed

    async def drop_all(self) -> None:
        """Demo reset: forget every snapshot (files and rows)."""
        async with begin_write(self.state) as conn:
            await conn.execute(text("DELETE FROM snapshot"))
        if self.root.is_dir():
            for d in self.root.iterdir():
                if d.is_dir():
                    _rmtree_readonly(d)


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def _rmtree_readonly(path: Path) -> None:
    """Parts are written 0444; their directories stay writable, so rmtree works on Linux."""
    shutil.rmtree(path, ignore_errors=True)
