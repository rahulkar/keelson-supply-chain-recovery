"""Tiny numbered-SQL migration runner (table ``merxa.schema_migrations``).

Migrations live in ``merxa_api/migrations/NNNN_name.sql`` and are applied in order,
each in its own transaction, under a Postgres advisory lock so concurrent replicas do
not race. Already-applied files are skipped; a changed checksum of an applied file is
reported as an error (migrations are immutable once shipped).
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from importlib import resources
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine

from merxa_api.logs import get_logger

log = get_logger(__name__)

_FILE_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
_LOCK_KEY = 0x4D45_5258_4131  # "MERXA1"


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
    """Return the packaged migrations sorted by version."""
    found: list[Migration] = []
    root = resources.files("merxa_api").joinpath("migrations")
    for entry in root.iterdir():
        m = _FILE_RE.match(entry.name)
        if not m:
            continue
        found.append(Migration(m.group(1), m.group(2), entry.read_text(encoding="utf-8")))
    found.sort(key=lambda mig: mig.version)
    versions = [m.version for m in found]
    if len(versions) != len(set(versions)):
        raise MigrationError(f"duplicate migration versions: {versions}")
    return found


async def _raw(conn: Any) -> Any:
    raw = await conn.get_raw_connection()
    return raw.driver_connection


async def migrate(engine: AsyncEngine) -> list[str]:
    """Apply pending migrations. Returns the versions applied in this call."""
    applied_now: list[str] = []
    migrations = discover()
    async with engine.connect() as conn:
        pg = await _raw(conn)
        await pg.execute("SELECT pg_advisory_lock($1)", _LOCK_KEY)
        try:
            await pg.execute(
                """
                CREATE SCHEMA IF NOT EXISTS merxa;
                CREATE TABLE IF NOT EXISTS merxa.schema_migrations (
                  version    text PRIMARY KEY,
                  name       text NOT NULL,
                  checksum   text NOT NULL,
                  applied_at timestamptz NOT NULL DEFAULT now()
                );
                """
            )
            rows = await pg.fetch("SELECT version, checksum FROM merxa.schema_migrations")
            done = {r["version"]: r["checksum"] for r in rows}
            for mig in migrations:
                if mig.version in done:
                    if done[mig.version] != mig.checksum:
                        raise MigrationError(f"migration {mig.version}_{mig.name} changed after being applied")
                    continue
                async with pg.transaction():
                    await pg.execute(mig.sql)
                    await pg.execute(
                        "INSERT INTO merxa.schema_migrations(version, name, checksum) VALUES ($1,$2,$3)",
                        mig.version,
                        mig.name,
                        mig.checksum,
                    )
                applied_now.append(mig.version)
                log.info("migration applied", extra={"migration": f"{mig.version}_{mig.name}"})
        finally:
            await pg.execute("SELECT pg_advisory_unlock($1)", _LOCK_KEY)
        await conn.commit()
    return applied_now
