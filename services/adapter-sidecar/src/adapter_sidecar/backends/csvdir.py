"""CSV directory backend (the Fernhill demo): ``DATA_DIR/<table.file>``.

Reads: every file is typed into a staging table of the state database (cached by the file's
sha256; a quick ``stat`` check avoids re-hashing unchanged files). A file whose sha256 differs
from the one last loaded — and that the sidecar did not write itself — is **external drift**:
it is reloaded and reported (the change feed then asks Keelson for a full resync).

Applies (one transaction on the state database, so staging rows, change-set state, audit rows,
change-feed rows and the idempotency record commit together):

1. the write primitives run on the staging tables;
2. each touched table is serialised (header order of the file, deterministic cell formatting) to
   a staged ``.<file>.sidecar-tmp`` next to it and fsynced;
3. the current bytes of each touched file must still hash to what was loaded, else nothing is
   applied (conflict reason ``source_changed``; the files stay byte-identical);
4. ``apply_journal`` records the staged files (before / after sha256) and the transaction commits;
5. each staged file replaces its target (``os.replace``, then a directory fsync) and the
   ``apply_journal`` row is deleted.

A crash after 4 is rolled forward at start-up (:meth:`CsvBackend.recover`): a staged file still
present replaces its target; a target already at the after-sha256 is fine; anything else marks
the change set ``failed`` (and the change feed asks for a resync). One sidecar per directory: an
``fcntl`` lock on ``DATA_DIR/.sidecar.lock`` refuses a second process.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncTransaction

from adapter_sidecar.backends import Backend, SourceChanged, Stmt, WriteTxn, run_statements
from adapter_sidecar.backends import staging
from adapter_sidecar.config import TableSpec
from adapter_sidecar.db import begin_write, iso, jdump, utcnow
from adapter_sidecar.errors import ConfigError, SourceDataError
from adapter_sidecar.logs import get_logger

log = get_logger(__name__)

TMP_SUFFIX = ".sidecar-tmp"
LOCK_FILE = ".sidecar.lock"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def tmp_path(path: Path) -> Path:
    return path.with_name(f".{path.name}{TMP_SUFFIX}")


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


class CsvWriteTxn(WriteTxn):
    def __init__(self, backend: CsvBackend, conn: AsyncConnection, trans: AsyncTransaction) -> None:
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
        staged: list[dict[str, str]] = []
        try:
            await run_statements(self.conn, statements)
            for name in sorted(self.touched):
                table = b.cfg.tables[name]
                path = b.path_of(table)
                loaded = (await self.conn.execute(text("SELECT sha256 FROM source_fingerprint WHERE source = :s"),
                                                  {"s": f"csv:{name}"})).scalar_one_or_none()
                data = await b.render_file(self.conn, name, table)
                tmp = tmp_path(path)
                with tmp.open("wb") as fh:
                    fh.write(data)
                    fh.flush()
                    os.fsync(fh.fileno())
                staged.append({"table": name, "path": str(path), "tmp": str(tmp), "before": str(loaded),
                               "after": hashlib.sha256(data).hexdigest()})
            # last look before committing: every touched file still holds the bytes that were loaded
            changed = [f["table"] for f in staged
                       if not Path(f["path"]).is_file() or sha256_file(Path(f["path"])) != f["before"]]
            if changed:
                raise SourceChanged(changed)
            for f in staged:
                await b._store_fingerprint(self.conn, f"csv:{f['table']}", f["after"])
            await self.conn.execute(
                text("INSERT INTO apply_journal (change_set_id, purpose, plan, created_at) VALUES (:c, :p, :j, :t)"),
                {"c": change_set_id, "p": purpose, "j": jdump({"files": staged}), "t": iso(utcnow())},
            )
            await self.trans.commit()
        except BaseException:
            for f in staged:
                with suppress(OSError):
                    Path(f["tmp"]).unlink()
            await self.rollback()
            raise
        # committed: publish the staged files (a crash from here on is rolled forward at start-up)
        try:
            for f in staged:
                os.replace(f["tmp"], f["path"])
            if staged:
                _fsync_dir(b.dir)
        except Exception:
            log.exception("publishing staged CSV files failed; rolling forward", extra={"change_set_id": change_set_id})
            await b.recover()
        else:
            async with begin_write(b.state) as conn:
                await conn.execute(text("DELETE FROM apply_journal WHERE change_set_id = :c"), {"c": change_set_id})
        for f in staged:
            b.remember_stat(f["table"])


class CsvBackend(Backend):
    kind = "csv"
    shared_state = True

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        if self.settings.data_dir is None:
            raise ConfigError("DATA_DIR must name the CSV directory of the csv backend")
        self.dir = self.settings.data_dir
        self.opts = self.cfg.backend.csv_opts
        self._stat: dict[str, tuple[int, int, int]] = {}
        self._lock_fd: int | None = None

    @property
    def engine(self) -> AsyncEngine:
        return self.state

    def path_of(self, table: TableSpec) -> Path:
        assert table.file is not None
        return self.dir / table.file

    def remember_stat(self, name: str) -> None:
        st = self.path_of(self.cfg.tables[name]).stat()
        self._stat[name] = (st.st_size, st.st_mtime_ns, st.st_ino)

    # ───────────── lifecycle ─────────────

    async def start(self) -> None:
        if not self.dir.is_dir():
            raise ConfigError(f"DATA_DIR {self.dir} is not a directory")
        self._acquire_dir_lock()
        async with begin_write(self.state) as conn:
            await staging.ensure_tables(conn, self.cfg.tables)
        await self.recover()
        for stale in self.dir.glob(f".*{TMP_SUFFIX}"):
            with suppress(OSError):
                stale.unlink()
                log.warning("removed a staged CSV file of an apply that never committed", extra={"file": stale.name})
        await self.refresh()

    def _acquire_dir_lock(self) -> None:
        import fcntl

        fd = os.open(self.dir / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ConfigError(f"another sidecar process holds {self.dir} (one sidecar per data directory)") from None
        self._lock_fd = fd

    async def close(self) -> None:
        if self._lock_fd is not None:
            with suppress(OSError):
                os.close(self._lock_fd)
            self._lock_fd = None

    async def probe(self) -> None:
        for table in self.cfg.tables.values():
            if not self.path_of(table).is_file():
                raise SourceDataError(table.file or "?", "file is missing")

    # ───────────── reads ─────────────

    def _parse(self, name: str, table: TableSpec, data: bytes) -> tuple[list[str], list[list[Any]]]:
        source = table.file or name
        try:
            content = data.decode(self.opts.encoding)
        except UnicodeDecodeError as exc:
            raise SourceDataError(source, f"not {self.opts.encoding} text (byte {exc.start})") from None
        reader = csv.reader(io.StringIO(content, newline=""), delimiter=self.opts.delimiter, strict=True)
        try:
            header = next(reader)
        except StopIteration:
            raise SourceDataError(source, "empty file (a header row is required)") from None
        except csv.Error as exc:
            raise SourceDataError(source, f"malformed CSV ({exc})", row=1) from None
        columns = list(table.column_types)
        if sorted(header) != sorted(columns) or len(header) != len(set(header)):
            raise SourceDataError(source, f"header {header} must name exactly the configured columns {columns}", row=1)

        def records() -> Any:
            try:
                for line, rec in enumerate(reader, start=2):
                    if not rec:
                        continue
                    if len(rec) != len(header):
                        raise SourceDataError(source, f"{len(rec)} cells, header has {len(header)}", row=line)
                    yield line, dict(zip(header, rec, strict=True))
            except csv.Error as exc:
                raise SourceDataError(source, f"malformed CSV ({exc})", row=reader.line_num) from None

        return header, staging.typed_rows(source, table, records())

    def header_of(self, table: TableSpec) -> list[str]:
        """The file's header order (write-back keeps it); the config order for a new file."""
        path = self.path_of(table)
        columns = list(table.column_types)
        try:
            with path.open(encoding=self.opts.encoding, newline="") as fh:
                first = next(csv.reader(fh, delimiter=self.opts.delimiter), None)
        except OSError:
            return columns
        return first if first and sorted(first) == sorted(columns) else columns

    async def refresh(self, *, force: bool = False) -> list[str]:
        pending: list[tuple[str, TableSpec, bytes, str, tuple[int, int, int]]] = []
        for name, table in self.cfg.tables.items():
            path = self.path_of(table)
            try:
                st = path.stat()
            except FileNotFoundError:
                raise SourceDataError(table.file or name, "file is missing") from None
            sig = (st.st_size, st.st_mtime_ns, st.st_ino)
            if not force and self._stat.get(name) == sig:
                continue
            data = path.read_bytes()
            pending.append((name, table, data, hashlib.sha256(data).hexdigest(), sig))
        if not pending:
            return []
        external: list[str] = []
        async with begin_write(self.state) as conn:
            for name, table, data, sha, _sig in pending:
                loaded = (await conn.execute(text("SELECT sha256 FROM source_fingerprint WHERE source = :s"),
                                             {"s": f"csv:{name}"})).scalar_one_or_none()
                if loaded == sha:
                    continue
                _header, rows = self._parse(name, table, data)
                await staging.replace_rows(conn, name, table, rows)
                await self._store_fingerprint(conn, f"csv:{name}", sha)
                if loaded is not None:
                    external.append(name)
                log.info("csv table loaded", extra={"table": name, "rows": len(rows), "external": loaded is not None})
        for name, _table, _data, _sha, sig in pending:
            self._stat[name] = sig
        return external

    async def render_file(self, conn: AsyncConnection, name: str, table: TableSpec) -> bytes:
        """The table's staging rows (``conn`` sees the open transaction) as CSV bytes."""
        rows = await staging.dump_rows(conn, name, table)
        types = table.column_types
        header = self.header_of(table)
        buf = io.StringIO()
        writer = csv.writer(buf, delimiter=self.opts.delimiter, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
        writer.writerow(header)
        for row in rows:
            writer.writerow([staging.format_cell(types[c], row[c]) for c in header])
        return buf.getvalue().encode(self.opts.encoding)

    # ───────────── writes ─────────────

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[WriteTxn]:
        async with self.state.connect() as conn:
            conn = await conn.execution_options(sidecar_sqlite_begin="IMMEDIATE")
            trans = await conn.begin()
            txn = CsvWriteTxn(self, conn, trans)
            try:
                yield txn
            finally:
                await txn.rollback()

    async def recover(self) -> list[str]:
        """Roll forward the applies whose transaction committed before their files were published."""
        notes: list[str] = []
        async with self.state.connect() as conn:
            rows = (await conn.execute(text("SELECT change_set_id, plan FROM apply_journal ORDER BY created_at"))).all()
        for cs_id, plan_text in rows:
            plan = json.loads(plan_text)
            lost: list[str] = []
            for f in plan.get("files") or []:
                path, tmp = Path(f["path"]), Path(f["tmp"])
                if tmp.exists():
                    os.replace(tmp, path)
                    notes.append(f"{cs_id}: rolled {path.name} forward")
                elif not (path.is_file() and sha256_file(path) == f["after"]):
                    lost.append(path.name)
            if plan.get("files"):
                _fsync_dir(self.dir)
            async with begin_write(self.state) as conn:
                if lost:
                    log.error("an applied change set's staged CSV files are gone; marking it failed",
                              extra={"change_set": cs_id, "files": lost})
                    now = iso(utcnow())
                    await conn.execute(
                        text("UPDATE change_set SET status = 'failed', result = :r, updated_at = :t WHERE id = :c"),
                        {"c": cs_id, "t": now, "r": jdump({"status": "failed", "error": "apply_lost", "files": lost})},
                    )
                    await conn.execute(
                        text("INSERT INTO journal (at, kind, detail) VALUES (:t, 'external', :d)"),
                        {"t": now, "d": f"apply of {cs_id} lost its staged files {lost}"},
                    )
                    for f in plan.get("files") or []:
                        await conn.execute(text("DELETE FROM source_fingerprint WHERE source = :s"),
                                           {"s": f"csv:{f['table']}"})
                await conn.execute(text("DELETE FROM apply_journal WHERE change_set_id = :c"), {"c": cs_id})
        if notes:
            log.warning("in-doubt csv applies rolled forward", extra={"notes": notes})
        return notes

    async def restore(self, golden: Path) -> None:
        """Demo reset: copy every configured file from ``golden`` (atomic per file)."""
        for table in self.cfg.tables.values():
            src = golden / (table.file or "")
            if not src.is_file():
                raise ConfigError(f"golden file {src} is missing")
            dst = self.path_of(table)
            tmp = tmp_path(dst)
            tmp.write_bytes(src.read_bytes())
            os.replace(tmp, dst)
        _fsync_dir(self.dir)
        self._stat.clear()

    async def forget_fingerprints(self, conn: AsyncConnection) -> None:
        await conn.execute(text("DELETE FROM source_fingerprint WHERE source LIKE 'csv:%'"))
