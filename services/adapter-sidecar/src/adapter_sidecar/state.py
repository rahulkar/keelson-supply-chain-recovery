"""The sidecar's own state store (SQLite via SQLAlchemy + aiosqlite, ``STATE_DB_URL``).

Migrations live in ``adapter_sidecar/migrations/NNNN_name.sql`` (own numbering, contract 120
§2: ``0001_init.sql``). :func:`migrate` applies the pending ones in order, each in one
``BEGIN IMMEDIATE`` transaction, and refuses an applied file whose checksum changed. The
statements are executed one by one (a trigger body is one statement).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from adapter_sidecar.db import begin_write, iso, utcnow
from adapter_sidecar.logs import get_logger

log = get_logger(__name__)

_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


class MigrationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()


def discover() -> list[Migration]:
    found: list[Migration] = []
    root = resources.files("adapter_sidecar").joinpath("migrations")
    for entry in root.iterdir():
        m = _FILE_RE.match(entry.name)
        if m:
            found.append(Migration(m.group(1), m.group(2), entry.read_text(encoding="utf-8")))
    found.sort(key=lambda mig: mig.version)
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions: {versions}")
    return found


def split_statements(sql: str) -> list[str]:
    """Split a migration into statements: ``;`` at the end of a line ends one, except inside a
    ``CREATE TRIGGER … BEGIN … END;`` body. Comment lines are dropped."""
    out: list[str] = []
    buf: list[str] = []
    in_trigger = False
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        upper = stripped.upper()
        if upper.startswith("CREATE TRIGGER"):
            in_trigger = True
        if stripped.endswith(";") and (not in_trigger or upper == "END;"):
            out.append("\n".join(buf))
            buf = []
            in_trigger = False
    if buf:
        out.append("\n".join(buf))
    return out


def ensure_sqlite_dir(url: str) -> None:
    """Create the parent directory of a file-based SQLite URL."""
    parsed = make_url(url)
    if parsed.drivername.startswith("sqlite") and parsed.database and parsed.database != ":memory:":
        Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)


async def migrate(engine: AsyncEngine) -> list[str]:
    """Apply pending migrations; returns the versions applied now."""
    if engine.dialect.name != "sqlite":
        raise MigrationError("the M8 state store is SQLite (a Postgres state store is deferred)")
    applied: list[str] = []
    async with begin_write(engine) as conn:
        await conn.execute(text(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, name TEXT NOT NULL, "
            "checksum TEXT NOT NULL, applied_at TEXT NOT NULL)"
        ))
    for mig in discover():
        async with begin_write(engine) as conn:
            row = (await conn.execute(text("SELECT checksum FROM schema_migrations WHERE version = :v"),
                                      {"v": mig.version})).first()
            if row is not None:
                if row[0] != mig.checksum:
                    raise MigrationError(f"migration {mig.version}_{mig.name} changed after being applied")
                continue
            for stmt in split_statements(mig.sql):
                await conn.exec_driver_sql(stmt)
            await conn.execute(
                text("INSERT INTO schema_migrations (version, name, checksum, applied_at) VALUES (:v, :n, :c, :a)"),
                {"v": mig.version, "n": mig.name, "c": mig.checksum, "a": iso(utcnow())},
            )
            applied.append(mig.version)
            log.info("state migration applied", extra={"version": mig.version, "migration": mig.name})
    return applied


async def next_value(conn: AsyncConnection, name: str, *, at_least: int = 0) -> int:
    """Increment and return the counter ``name`` (never below ``at_least``); inside the caller's
    write transaction."""
    row = (await conn.execute(text("SELECT value FROM sequence WHERE name = :n"), {"n": name})).first()
    current = int(row[0]) if row is not None else 0
    value = max(current, at_least) + 1
    await conn.execute(
        text("INSERT INTO sequence (name, value) VALUES (:n, :v) ON CONFLICT (name) DO UPDATE SET value = :v"),
        {"n": name, "v": value},
    )
    return value


async def peek_value(conn: AsyncConnection, name: str) -> int:
    row = (await conn.execute(text("SELECT value FROM sequence WHERE name = :n"), {"n": name})).first()
    return int(row[0]) if row is not None else 0
