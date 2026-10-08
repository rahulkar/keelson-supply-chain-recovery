"""Backends: where the system's data lives and how a change set is committed to it.

Every backend yields SQLAlchemy connections that run SQL, so one pipeline serves all of them:

* ``csv`` (:mod:`.csvdir`) — CSV files typed into staging tables of the state database; an apply
  rewrites the touched files atomically (staged files + ``apply_journal`` + start-up roll-forward);
* ``sql`` (:mod:`.sql`) — any SQLAlchemy async URL (Postgres, SQLite); an apply is one backend
  transaction carrying an exactly-once marker row;
* ``rest`` (:mod:`.rest`) — a read-only JSON API crawled into staging tables (``crawled``
  snapshots, tiers A·B·C only).

The caller (:class:`adapter_sidecar.service.Sidecar`) serialises every write and every staging
refresh with one asyncio lock; one sidecar process (uvicorn, 1 worker) per data source.
"""

from __future__ import annotations

import abc
import datetime as dt
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from adapter_sidecar.config import SidecarConfig
from adapter_sidecar.db import begin_read, jdump
from adapter_sidecar.settings import Settings

Stmt = tuple[str, dict[str, Any]]
"""One state-store statement (``text()`` SQL written by the sidecar, bound parameters with JSON
values only) — the replayable unit of an apply's state write."""


class SourceChanged(Exception):  # noqa: N818 - a business outcome, rendered as a conflict reason
    """A source changed underneath an apply (CSV file edited meanwhile): nothing was applied."""

    def __init__(self, tables: Sequence[str]) -> None:
        super().__init__(f"source changed during the apply: {', '.join(tables)}")
        self.tables = list(tables)


async def run_statements(conn: AsyncConnection, statements: Sequence[Stmt]) -> None:
    for sql, params in statements:
        await conn.execute(text(sql), params)


class WriteTxn(abc.ABC):
    """An open backend write transaction. Primitives run on :attr:`conn` and record the tables they
    change in :attr:`touched`; :meth:`commit` makes the change and its state rows durable
    together (backend-specific protocol), :meth:`rollback` discards both."""

    conn: AsyncConnection
    touched: set[str]

    @abc.abstractmethod
    async def commit(self, *, change_set_id: str, purpose: str, statements: Sequence[Stmt],
                     marker: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    async def rollback(self) -> None: ...


class Backend(abc.ABC):
    kind: str
    shared_state: bool
    """The backend's tables live in the state database (CSV / REST staging)."""

    def __init__(self, cfg: SidecarConfig, settings: Settings, state: AsyncEngine) -> None:
        self.cfg = cfg
        self.settings = settings
        self.state = state
        self.as_of_range: tuple[dt.datetime, dt.datetime] | None = None
        """``crawled`` backends: when the first / last page of the data being served was read."""

    @property
    @abc.abstractmethod
    def engine(self) -> AsyncEngine:
        """Engine holding the configured tables (the state engine for staged backends)."""

    @property
    def consistency(self) -> str:
        return self.cfg.consistency

    async def start(self) -> None:
        """Prepare (staging tables, marker table, locks) and settle in-doubt applies."""

    async def close(self) -> None:
        """Release resources."""

    @abc.abstractmethod
    async def refresh(self, *, force: bool = False) -> list[str]:
        """Load source changes; returns the tables that changed **outside** the sidecar since the
        last look (external drift). The caller holds the write lock."""

    @abc.abstractmethod
    async def probe(self) -> None:
        """Raise when the source cannot be reached (readiness)."""

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncConnection]:
        """A consistent read-only snapshot transaction over the configured tables."""
        async with begin_read(self.engine) as conn:
            yield conn

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[WriteTxn]:
        raise NotImplementedError(f"the {self.kind} backend is read-only")
        yield  # pragma: no cover

    async def fingerprints(self) -> dict[str, str]:
        async with self.state.connect() as conn:
            rows = (await conn.execute(text("SELECT source, sha256 FROM source_fingerprint "
                                            "WHERE source NOT LIKE 'schema:%' ORDER BY source"))).all()
        return {str(r[0]): str(r[1]) for r in rows}

    async def _store_fingerprint(self, conn: AsyncConnection, source: str, sha256: str) -> str | None:
        """Upsert a source fingerprint; returns the previous value."""
        prev = (await conn.execute(text("SELECT sha256 FROM source_fingerprint WHERE source = :s"),
                                   {"s": source})).first()
        await conn.execute(
            text("INSERT INTO source_fingerprint (source, sha256, observed_at) VALUES (:s, :h, :t) "
                 "ON CONFLICT (source) DO UPDATE SET sha256 = :h, observed_at = :t"),
            {"s": source, "h": sha256, "t": dt.datetime.now(dt.UTC).isoformat()},
        )
        return str(prev[0]) if prev is not None else None


def journal_plan(statements: Sequence[Stmt], **extra: Any) -> str:
    """``apply_journal.plan`` text (``statements`` replayable by :func:`run_statements`)."""
    return jdump({"state": [[sql, params] for sql, params in statements], **extra}) or "{}"


def build_backend(cfg: SidecarConfig, settings: Settings, state: AsyncEngine) -> Backend:
    if cfg.backend.kind == "csv":
        from adapter_sidecar.backends.csvdir import CsvBackend

        return CsvBackend(cfg, settings, state)
    if cfg.backend.kind == "sql":
        from adapter_sidecar.backends.sql import SqlBackend

        return SqlBackend(cfg, settings, state)
    from adapter_sidecar.backends.rest import RestBackend

    return RestBackend(cfg, settings, state)
