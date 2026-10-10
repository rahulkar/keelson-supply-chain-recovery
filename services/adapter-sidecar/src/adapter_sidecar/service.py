"""Wiring: one :class:`Sidecar` per process (state store, backend, write lock, services).

Start-up (:meth:`Sidecar.start`) is fail-fast: migrations, the backend (staging tables, the CSV
directory lock, in-doubt applies settled), then the database-dependent config checks (every
entity SQL and view returns exactly its columns, probed with ``LIMIT 0``) and a writable
``SNAPSHOT_DIR``. ``/readyz`` reports ready only after all of them passed.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection

from adapter_sidecar import idempotency, journal
from adapter_sidecar.backends import build_backend, run_statements
from adapter_sidecar.changesets import ChangeSetService
from adapter_sidecar.config import VIEW_HEADER_COLUMNS, VIEW_LINE_COLUMNS, SidecarConfig, load_config
from adapter_sidecar.db import begin_write, create_engine, iso, quote_ident, utcnow
from adapter_sidecar.errors import ConfigError, Problem, SourceDataError
from adapter_sidecar.logs import get_logger
from adapter_sidecar.receipts import ReceiptVerifier
from adapter_sidecar.settings import Settings, origin_of
from adapter_sidecar.snapshots import SnapshotService, entity_columns_problem
from adapter_sidecar.state import ensure_sqlite_dir, migrate

log = get_logger(__name__)


class Sidecar:
    def __init__(self, settings: Settings, cfg: SidecarConfig | None = None, *, http_client: Any = None,
                 backend_client: Any = None) -> None:
        self.settings = settings
        self.cfg = cfg or load_config(settings.config_path)
        ensure_sqlite_dir(settings.state_db_url)
        try:
            self.state = create_engine(settings.state_db_url)
        except ValueError as exc:
            raise ConfigError(f"STATE_DB_URL: {exc}") from None
        if self.state.dialect.name != "sqlite":
            raise ConfigError("STATE_DB_URL must be a sqlite+aiosqlite URL (a Postgres state store is deferred)")
        if self.cfg.backend.kind == "rest":
            from adapter_sidecar.backends.rest import RestBackend

            self.backend = RestBackend(self.cfg, settings, self.state, client=backend_client)
        else:
            self.backend = build_backend(self.cfg, settings, self.state)
        self.lock = asyncio.Lock()
        self.ready = False
        self.problems: list[str] = []
        self.receipts = ReceiptVerifier(
            audience=settings.mcp_resource, issuers=settings.trusted_receipt_issuers,
            ext_iss=self.cfg.receipts.ext_iss or origin_of(settings.mcp_resource), rules=self.cfg.receipts,
            jwks_url=settings.keelson_jwks_url, jwks_ttl_s=settings.receipt_jwks_ttl_s,
            leeway_s=settings.receipt_leeway_s, http_client=http_client,
        )
        self.snapshots = SnapshotService(cfg=self.cfg, settings=settings, state=self.state, backend=self.backend,
                                         lock=self.lock, sync_sources=self.sync_sources)
        self.changesets = ChangeSetService(cfg=self.cfg, settings=settings, state=self.state, backend=self.backend,
                                           lock=self.lock, receipts=self.receipts, sync_sources=self.sync_sources)

    # ───────────────────────────── lifecycle ─────────────────────────────

    async def start(self) -> None:
        await migrate(self.state)
        if self.settings.demo_mode and self.cfg.backend.kind == "csv":
            self._seed_demo_data()
        await self.backend.start()
        self.problems = await self.check_database()
        self.settings.snapshot_dir.mkdir(parents=True, exist_ok=True)
        probe = self.settings.snapshot_dir / ".write-probe"
        try:
            probe.write_bytes(b"ok")
            probe.unlink()
        except OSError as exc:
            self.problems.append(f"SNAPSHOT_DIR {self.settings.snapshot_dir} is not writable ({exc.strerror})")
        if self.problems:
            raise ConfigError("; ".join(self.problems))
        self.ready = True
        log.info("adapter sidecar ready", extra={"system": self.cfg.system.name, "backend": self.cfg.backend.kind,
                                                 "entities": sorted(self.cfg.entities), "ops": sorted(self.cfg.ops),
                                                 "writable": self.cfg.writable})

    async def close(self) -> None:
        self.ready = False
        await self.backend.close()
        await self.state.dispose()

    def _seed_demo_data(self) -> None:
        """DEMO_MODE: copy the golden files into an empty ``DATA_DIR`` on first start."""
        data, golden = self.settings.data_dir, self.settings.demo_golden_dir
        if data is None or golden is None:
            return
        data.mkdir(parents=True, exist_ok=True)
        if any(data.glob("*.csv")):
            return
        for table in self.cfg.tables.values():
            src = golden / (table.file or "")
            if src.is_file():
                shutil.copyfile(src, data / src.name)
        log.info("demo data seeded from the golden files", extra={"dir": str(golden)})

    async def check_database(self) -> list[str]:
        """DB-dependent config checks: each entity SQL / view returns exactly its columns (``LIMIT 0``)."""
        problems: list[str] = []
        try:
            await self.backend.probe()
        except Exception as exc:  # reported as a readiness problem
            return [f"backend unreachable: {exc}"]
        async with self.backend.read() as conn:
            for entity, spec in self.cfg.entities.items():
                try:
                    cols = await _probe_columns(conn, spec.sql)
                except Exception as exc:
                    problems.append(f"entities.{entity}: the SQL fails ({_db_error(exc)})")
                    continue
                if (p := entity_columns_problem(entity, cols)) is not None:
                    problems.append(p)
            view = self.cfg.views.transfer_orders
            if view is not None:
                for label, sql, want in (("header_sql", view.header_sql, VIEW_HEADER_COLUMNS),
                                         ("lines_sql", view.lines_sql, VIEW_LINE_COLUMNS)):
                    try:
                        cols = await _probe_columns(conn, sql)
                    except Exception as exc:
                        problems.append(f"views.transfer_orders.{label}: the SQL fails ({_db_error(exc)})")
                        continue
                    if sorted(cols) != sorted(want):
                        problems.append(f"views.transfer_orders.{label} must return {list(want)}, got {cols}")
            if self.backend.kind == "sql":
                for name, table in self.cfg.tables.items():
                    try:
                        await conn.execute(text(
                            f"SELECT {', '.join(quote_ident(conn, c) for c in table.column_types)} "
                            f"FROM {quote_ident(conn, name)} WHERE 1 = 0"))
                    except Exception as exc:
                        problems.append(f"tables.{name}: not readable with the configured columns ({_db_error(exc)})")
        return problems

    # ───────────────────────────── change feed ─────────────────────────────

    async def sync_sources(self, *, force: bool = False) -> list[str]:
        """Refresh the backend; journal external drift (caller holds :attr:`lock`)."""
        changed = await self.backend.refresh(force=force)
        if changed:
            async with begin_write(self.state) as conn:
                await run_statements(conn, journal.drift_statements(self.cfg, changed, at=iso(utcnow()) or ""))
            log.warning("source changed outside the sidecar", extra={"tables": changed})
        return changed

    async def changes_since(self, watermark: str) -> dict[str, Any]:
        journal.parse_watermark(watermark)
        async with self.lock:
            try:
                await self.sync_sources()
            except SourceDataError as exc:
                raise Problem(503, "source_invalid", str(exc)) from None
        async with self.state.connect() as conn:
            return await journal.changes_since(conn, watermark)

    # ───────────────────────────── housekeeping ─────────────────────────────

    async def housekeeping(self) -> None:
        removed = await self.snapshots.cleanup()
        if removed:
            log.info("removed expired scn snapshots", extra={"count": removed})
        purged = await idempotency.purge_expired(self.state, self.settings.idempotency_retention_h)
        if purged:
            log.info("purged idempotency records", extra={"count": purged})
        async with begin_write(self.state) as conn:
            pruned = await journal.prune(conn, self.cfg.changes.retention)
        if pruned:
            log.info("pruned change journal", extra={"count": pruned})

    # ───────────────────────────── admin / demo ─────────────────────────────

    async def reset_demo(self) -> dict[str, Any]:
        """Restore the golden CSV files, forget change sets / snapshots / idempotency records, and
        journal a ``reset`` (Keelson resyncs). The audit log is append-only and stays; change-set
        codes keep counting so they never repeat."""
        if self.cfg.backend.kind != "csv":
            raise Problem(409, "not_supported", "demo reset restores CSV golden files only")
        golden = self.settings.demo_golden_dir
        if golden is None or not golden.is_dir():
            raise Problem(409, "not_configured", "DEMO_GOLDEN_DIR is not a directory")
        from adapter_sidecar.backends.csvdir import CsvBackend

        assert isinstance(self.backend, CsvBackend)
        async with self.lock:
            await self.backend.restore(golden)
            async with begin_write(self.state) as conn:
                for stmt in ("DELETE FROM change_set_effect", "DELETE FROM change_set_item",
                             "UPDATE change_set SET rollback_of = NULL", "DELETE FROM change_set",
                             "DELETE FROM idempotency_record", "DELETE FROM used_receipt", "DELETE FROM apply_journal",
                             "DELETE FROM sequence WHERE name LIKE 'code:%'"):
                    await conn.execute(text(stmt))
                await self.backend.forget_fingerprints(conn)
                await run_statements(conn, journal.drift_statements(self.cfg, [], at=iso(utcnow()) or "", kind="reset",
                                                                    detail="demo reset"))
            await self.backend.refresh(force=True)
            await self.snapshots.drop_all()
            async with self.state.connect() as conn:
                wm = await journal.high_water(conn)
        log.info("demo reset", extra={"watermark": wm})
        return {"status": "reset", "watermark": journal.format_watermark(wm)}

    async def stats(self) -> dict[str, Any]:
        tables: dict[str, int] = {}
        entities: dict[str, int] = {}
        async with self.backend.read() as conn:
            for name in self.cfg.tables:
                count = await conn.execute(text(f"SELECT count(*) FROM {quote_ident(conn, name)}"))
                tables[name] = int(count.scalar_one())
            for entity, spec in self.cfg.entities.items():
                entities[entity] = int((await conn.execute(text(f"SELECT count(*) FROM ({spec.sql}) e"))).scalar_one())
        async with self.state.connect() as conn:
            wm = await journal.high_water(conn)
            change_sets = int((await conn.execute(text("SELECT count(*) FROM change_set"))).scalar_one())
        return {"system": self.cfg.system.name, "backend": self.cfg.backend.kind, "tables": tables,
                "entities": entities, "fingerprints": await self.backend.fingerprints(),
                "watermark": journal.format_watermark(wm), "change_sets": change_sets}

    async def rows(self, table: str, filters: dict[str, str]) -> dict[str, Any]:
        spec = self.cfg.tables.get(table)
        if spec is None:
            raise Problem(404, "not_found", f"unknown table {table!r}")
        types = spec.column_types
        unknown = sorted(set(filters) - set(types))
        if unknown:
            raise Problem(400, "invalid_parameter", f"{unknown} are not columns of {table}")
        from adapter_sidecar.backends.staging import to_json_value

        async with self.backend.read() as conn:
            where = " AND ".join(f"CAST({quote_ident(conn, c)} AS TEXT) = :f{i}" for i, c in enumerate(filters))
            sql = (f"SELECT {', '.join(quote_ident(conn, c) for c in types)} FROM {quote_ident(conn, table)}"
                   + (f" WHERE {where}" if where else "") + " LIMIT 1000")
            res = (await conn.execute(text(sql), {f"f{i}": v for i, v in enumerate(filters.values())})).all()
        cols = list(types)
        return {"table": table, "items": [{c: to_json_value(types[c], v) for c, v in zip(cols, r, strict=True)}
                                          for r in res]}


async def _probe_columns(conn: AsyncConnection, sql: str) -> list[str]:
    res = await conn.execute(text(f"SELECT * FROM ({sql}) probe LIMIT 0"))
    return list(res.keys())


def _db_error(exc: Exception) -> str:
    orig = getattr(exc, "orig", None)
    return str(orig or exc).splitlines()[0][:200]
