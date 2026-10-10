"""A hardened, per-job in-process DuckDB (``docs/contracts/121-m8-mapping-compiler.md`` §2).

One connection per (tenant, connector) job — a crawl or a preview — never cached or shared, so it
only ever holds one tenant's rows. :func:`open_duckdb` configures it before anything runs:

* ``:memory:`` database, memory capped (``DUCKDB_MEMORY_LIMIT_MB``, engine-api 128 / worker 256),
  ``DUCKDB_THREADS`` threads, spilling to a fresh private ``mkdtemp`` directory that is removed when
  the session closes;
* no extension is installed or loaded automatically, community extensions are refused;
* then **external access is switched off and the configuration locked**: no file, URL, ``COPY``,
  ``ATTACH``, ``INSTALL`` / ``LOAD`` and no ``SET`` can undo it. Data only enters through Arrow
  batches the engine registers (an in-process scan, not external access).

``duckdb`` is imported lazily here only (engine-api pays for it on the first preview).
"""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from typing import Any, Final

from keelson_engine.core.logs import get_logger

__all__ = ["DuckSession", "open_duckdb"]

log = get_logger(__name__)

_MIN_MB: Final = 32
_MAX_MB: Final = 4096


class DuckSession:
    """A locked DuckDB connection plus its spill directory."""

    def __init__(self, con: Any, tmp: str) -> None:
        self.con = con
        self.tmp = tmp
        self._n = 0

    def execute(self, sql: str, params: list[Any] | None = None) -> Any:
        return self.con.execute(sql, params) if params is not None else self.con.execute(sql)

    def insert_batch(self, table: str, batch: Any) -> None:
        """Append an Arrow record batch to ``table`` (generated name; same column order)."""
        self._n += 1
        view = f"_in{self._n}"
        self.con.register(view, batch)
        try:
            self.con.execute(f"INSERT INTO {table} SELECT * FROM {view}")
        finally:
            self.con.unregister(view)

    def close(self) -> None:
        with suppress(Exception):
            self.con.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


@contextmanager
def open_duckdb(memory_mb: int, threads: int) -> Iterator[DuckSession]:
    """A fresh hardened session (see the module docstring), closed and wiped on exit."""
    import duckdb

    tmp = tempfile.mkdtemp(prefix="keelson-duckdb-")
    try:
        con = duckdb.connect(":memory:", config={
            "autoinstall_known_extensions": False,
            "autoload_known_extensions": False,
            "allow_community_extensions": False,
            "memory_limit": f"{max(_MIN_MB, min(_MAX_MB, int(memory_mb)))}MB",
            "threads": max(1, min(8, int(threads))),
            "temp_directory": tmp,
            "preserve_insertion_order": True,
        })
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    session = DuckSession(con, tmp)
    try:
        con.execute("SET enable_external_access = false")
        con.execute("SET lock_configuration = true")
        yield session
    finally:
        session.close()
