"""Numbered-SQL migration runner (table ``schema_migrations``).

Files ``NNNN_name.sql`` from the migrations directory are applied in order, each in its
own transaction, under a Postgres advisory lock (bootstrap and a second process can
never race). Applied files are skipped; a changed checksum of an applied file is an
error (migrations are immutable once shipped). The SQL itself is written to be
re-runnable (``IF NOT EXISTS`` / ``CREATE OR REPLACE``) as a second safety net.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from keelson_engine.config import get_settings
from keelson_engine.core.logs import get_logger

__all__ = ["Migration", "MigrationError", "discover", "migrate", "migrations_dir"]

log = get_logger(__name__)

_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LOCK_KEY = 0x4B45_454C_534E  # "KEELSN"


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
    """Configured dir, else ``/app/migrations`` (image), else the source tree."""
    configured = get_settings().migrations_dir
    candidates = [configured] if configured else []
    candidates += [Path("/app/migrations"), Path(__file__).resolve().parents[3] / "migrations"]
    for cand in candidates:
        if cand is not None and cand.is_dir():
            return cand
    raise MigrationError(f"no migrations directory found (tried {[str(c) for c in candidates]})")


def discover(directory: Path | None = None) -> list[Migration]:
    root = directory or migrations_dir()
    found: list[Migration] = []
    for entry in sorted(root.iterdir()):
        m = _FILE_RE.match(entry.name)
        if m:
            found.append(Migration(m.group(1), m.group(2), entry.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions: {versions}")
    if not found:
        raise MigrationError(f"no migrations in {root}")
    return found


async def _raw(conn: Any) -> Any:
    raw = await conn.get_raw_connection()
    return raw.driver_connection


async def migrate(engine: AsyncEngine, directory: Path | None = None) -> list[str]:
    """Apply pending migrations; returns the versions applied by this call."""
    migrations = discover(directory)
    applied_now: list[str] = []
    async with engine.connect() as conn:
        pg = await _raw(conn)
        await pg.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
        try:
            await pg.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                  version    text PRIMARY KEY,
                  name       text NOT NULL,
                  checksum   text NOT NULL,
                  applied_at timestamptz NOT NULL DEFAULT now()
                )
                """
            )
            rows = await pg.fetch("SELECT version, checksum FROM schema_migrations")
            done = {r["version"]: r["checksum"] for r in rows}
            for mig in migrations:
                if mig.version in done:
                    if done[mig.version] != mig.checksum:
                        raise MigrationError(f"migration {mig.version}_{mig.name} changed after being applied")
                    continue
                async with pg.transaction():
                    await pg.execute(mig.sql)
                    await pg.execute(
                        "INSERT INTO schema_migrations(version, name, checksum) VALUES ($1, $2, $3)",
                        mig.version,
                        mig.name,
                        mig.checksum,
                    )
                applied_now.append(mig.version)
                log.info("migration applied", migration=f"{mig.version}_{mig.name}")
        finally:
            await pg.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
        await conn.commit()
    return applied_now
