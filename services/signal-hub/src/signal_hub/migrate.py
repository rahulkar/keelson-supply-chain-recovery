"""Numbered-SQL migrations (``migrations/NNNN_name.sql``), same rules as the engine's runner:
applied in order under an advisory lock, one transaction each, immutable once applied
(checksum), and written to be re-runnable (``IF NOT EXISTS``)."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

import asyncpg

from signal_hub.logs import get_logger

__all__ = ["Migration", "MigrationError", "discover", "migrate", "migrations_dir"]

log = get_logger(__name__)
_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LOCK_KEY = 0x5349_474E_4C53  # "SIGNLS"


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


def migrations_dir() -> Path:
    for cand in (Path("/app/migrations"), Path(__file__).resolve().parents[2] / "migrations"):
        if cand.is_dir():
            return cand
    raise MigrationError("no migrations directory found")


def discover(directory: Path | None = None) -> list[Migration]:
    root = directory or migrations_dir()
    found = [
        Migration(m.group(1), m.group(2), entry.read_text(encoding="utf-8"))
        for entry in sorted(root.iterdir())
        if (m := _FILE_RE.match(entry.name))
    ]
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions: {versions}")
    if not found:
        raise MigrationError(f"no migrations in {root}")
    return found


async def migrate(conn: asyncpg.Connection, directory: Path | None = None) -> list[str]:
    """Apply pending migrations on ``conn``; returns the versions applied by this call."""
    migrations = discover(directory)
    applied: list[str] = []
    await conn.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
    try:
        await conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
              version text PRIMARY KEY, name text NOT NULL, checksum text NOT NULL,
              applied_at timestamptz NOT NULL DEFAULT now())
            """
        )
        done = {r["version"]: r["checksum"] for r in await conn.fetch("SELECT version, checksum FROM schema_migrations")}
        for mig in migrations:
            if mig.version in done:
                if done[mig.version] != mig.checksum:
                    raise MigrationError(f"migration {mig.version}_{mig.name} changed after being applied")
                continue
            async with conn.transaction():
                await conn.execute(mig.sql)
                await conn.execute(
                    "INSERT INTO schema_migrations(version, name, checksum) VALUES ($1, $2, $3)",
                    mig.version, mig.name, mig.checksum,
                )
            applied.append(mig.version)
            log.info("migration applied", extra={"migration": f"{mig.version}_{mig.name}"})
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
    return applied
