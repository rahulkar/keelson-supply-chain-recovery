"""SQL backend: any SQLAlchemy async URL (``postgresql+asyncpg`` or ``sqlite+aiosqlite``), named by
``backend.sql.url_env``.

* Snapshots read in one ``REPEATABLE READ, READ ONLY`` transaction (SQLite: one WAL snapshot).
* An apply is **one** backend transaction: the write primitives (rows locked ``FOR UPDATE`` on
  Postgres) plus an exactly-once marker row ``{change_set_id, purpose, preview_hash, applied_at}``
  in ``apply_marker_table``. Before the backend commits, the state rows the apply produces are
  written to ``apply_journal``; after the commit they are replayed into the state store and the
  journal row is deleted. A crash in between is settled at start-up by looking for the marker:
  present → the state rows are written; absent → the apply never happened.
* External drift: tables with ``fingerprint_sql`` are re-fingerprinted by ``scn.changes_since``
  and ``scn.snapshot_open``; the sidecar's own applies update the fingerprints right after their
  commit (best effort: a foreign write in that instant is not reported).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from adapter_sidecar.backends import Backend, Stmt, WriteTxn, journal_plan, run_statements
from adapter_sidecar.config import SqlBackend as SqlOpts
from adapter_sidecar.db import begin_write, create_engine, is_sqlite, iso, jsonable, quote_ident, utcnow
from adapter_sidecar.errors import ConfigError
from adapter_sidecar.logs import get_logger

log = get_logger(__name__)


class SqlWriteTxn(WriteTxn):
    def __init__(self, backend: SqlBackend, conn: AsyncConnection, trans: AsyncTransaction) -> None:
        self.backend = backend
        self.conn = conn
        self.trans = trans
        self.touched: set[str] = set()

    async def rollback(self) -> None:
        if self.trans.is_active:
            await self.trans.rollback()

    async def commit(self, *, change_set_id: str, purpose: str, statements: Sequence[Stmt],
                     marker: dict[str, Any]) -> None:
        b = self.backend
        marker_id = str(marker["id"])
        await self.conn.execute(
            text(f"INSERT INTO {quote_ident(self.conn, b.opts.apply_marker_table)} "
                 "(change_set_id, purpose, preview_hash, applied_at) VALUES (:c, :p, :h, :t)"),
            {"c": marker_id, "p": purpose, "h": marker.get("preview_hash"), "t": iso(utcnow())},
        )
        async with begin_write(b.state) as s:
            await s.execute(
                text("INSERT INTO apply_journal (change_set_id, purpose, plan, created_at) VALUES (:c, :p, :j, :t)"),
                {"c": change_set_id, "p": purpose, "j": journal_plan(statements, marker=marker_id), "t": iso(utcnow())},
            )
        try:
            await self.trans.commit()
        except BaseException:
            await self.rollback()
            # A failed COMMIT may still have committed (connection lost after the server's
            # commit): the marker decides. Unknown (marker lookup fails too) → the journal row
            # stays and start-up recovery settles it.
            if not await b.marker_exists(marker_id):
                async with begin_write(b.state) as s:
                    await s.execute(text("DELETE FROM apply_journal WHERE change_set_id = :c"), {"c": change_set_id})
                raise
            log.warning("backend COMMIT reported an error but the apply marker exists; settling",
                        extra={"change_set_id": change_set_id})
        async with begin_write(b.state) as s:
            await run_statements(s, statements)
            await s.execute(text("DELETE FROM apply_journal WHERE change_set_id = :c"), {"c": change_set_id})
        try:
            await b.store_own_fingerprints(sorted(self.touched))
        except Exception:  # noqa: BLE001 - drift bookkeeping never fails a committed apply
            log.warning("could not refresh table fingerprints after an apply", exc_info=True)


class SqlBackend(Backend):
    kind = "sql"
    shared_state = False

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        opts = self.cfg.backend.sql
        assert opts is not None
        self.opts: SqlOpts = opts
        url = self.settings.secret(opts.url_env)
        if not url:
            raise ConfigError(f"{opts.url_env} must hold the SQL backend URL")
        try:
            self._engine = create_engine(url)
        except ValueError as exc:
            raise ConfigError(f"{opts.url_env}: {exc}") from None

    @property
    def engine(self) -> AsyncEngine:
        return self._engine

    @property
    def writable(self) -> bool:
        return self.cfg.writable

    async def start(self) -> None:
        if self.writable and self.opts.manage_marker_table:
            async with begin_write(self._engine) as conn:
                await conn.exec_driver_sql(
                    f"CREATE TABLE IF NOT EXISTS {quote_ident(conn, self.opts.apply_marker_table)} "
                    "(change_set_id TEXT PRIMARY KEY, purpose TEXT NOT NULL, preview_hash TEXT, applied_at TEXT NOT NULL)"
                )
        await self.recover()
        await self.refresh()

    async def close(self) -> None:
        await self._engine.dispose()

    async def probe(self) -> None:
        async with self._engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
            if self.writable:
                await conn.execute(text(f"SELECT change_set_id FROM {quote_ident(conn, self.opts.apply_marker_table)} "
                                        "WHERE 1 = 0"))

    async def _fingerprint(self, conn: AsyncConnection, sql: str) -> str:
        rows = (await conn.execute(text(sql))).all()
        material = json.dumps([jsonable(list(r)) for r in rows], default=str, sort_keys=True)
        return hashlib.sha256(material.encode("utf-8")).hexdigest()

    async def _current(self, names: Sequence[str]) -> dict[str, str]:
        out: dict[str, str] = {}
        wanted = [(n, self.cfg.tables[n].fingerprint_sql) for n in names if n in self.cfg.tables]
        if not any(sql for _, sql in wanted):
            return out
        async with self._engine.connect() as conn:
            for name, sql in wanted:
                if sql:
                    out[name] = await self._fingerprint(conn, sql)
        return out

    async def refresh(self, *, force: bool = False) -> list[str]:
        current = await self._current(list(self.cfg.tables))
        external: list[str] = []
        if not current:
            return external
        async with begin_write(self.state) as s:
            for name, sha in current.items():
                prev = await self._store_fingerprint(s, f"sql:{name}", sha)
                if prev is not None and prev != sha:
                    external.append(name)
        return external

    async def store_own_fingerprints(self, names: Sequence[str]) -> None:
        current = await self._current(names)
        if current:
            async with begin_write(self.state) as s:
                for name, sha in current.items():
                    await self._store_fingerprint(s, f"sql:{name}", sha)

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[WriteTxn]:
        async with self._engine.connect() as conn:
            if is_sqlite(self._engine):
                conn = await conn.execution_options(sidecar_sqlite_begin="IMMEDIATE")
            trans = await conn.begin()
            txn = SqlWriteTxn(self, conn, trans)
            try:
                yield txn
            finally:
                await txn.rollback()

    async def marker_exists(self, marker_id: str) -> bool:
        async with self._engine.connect() as conn:
            found = (await conn.execute(
                text(f"SELECT 1 FROM {quote_ident(conn, self.opts.apply_marker_table)} WHERE change_set_id = :c"),
                {"c": marker_id},
            )).first()
        return found is not None

    async def recover(self) -> list[str]:
        notes: list[str] = []
        async with self.state.connect() as s:
            rows = (await s.execute(text("SELECT change_set_id, plan FROM apply_journal ORDER BY created_at"))).all()
        for cs_id, plan_text in rows:
            plan = json.loads(plan_text)
            found = await self.marker_exists(str(plan.get("marker")))
            async with begin_write(self.state) as s:
                if found:
                    await run_statements(s, [(sql, params) for sql, params in plan.get("state") or []])
                    notes.append(f"{cs_id}: committed in the backend, state settled")
                else:
                    notes.append(f"{cs_id}: never committed in the backend, discarded")
                await s.execute(text("DELETE FROM apply_journal WHERE change_set_id = :c"), {"c": cs_id})
        if notes:
            log.warning("in-doubt sql applies settled", extra={"notes": notes})
        return notes
