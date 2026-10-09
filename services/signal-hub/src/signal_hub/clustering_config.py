"""Platform clustering settings (``docs/contracts/123`` §7): one versioned row in ``cluster_config``.

Global feeds cluster once for every tenant (plan §15), so the thresholds are platform-wide: every
tenant token may read them, only a platform-scoped engine token may change them (the write runs
in the system context; RLS refuses it anywhere else). No row = :data:`DEFAULT_CLUSTERING_CONFIG`.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final

import asyncpg
from pydantic import ValidationError

from keelson_contracts.signals import DEFAULT_CLUSTERING_CONFIG, ClusteringConfig, ClusteringConfigUpdate
from signal_hub.logs import get_logger

__all__ = ["CONFIG_LOCK", "ConfigConflict", "load_config", "save_config"]

log = get_logger(__name__)
CONFIG_LOCK: Final = "cluster_config"


class ConfigConflict(RuntimeError):
    """The update's ``base_version`` is not the stored version (someone saved in between)."""

    def __init__(self, current: int) -> None:
        super().__init__(f"clustering settings changed (version {current}); reload and retry")
        self.current = current


async def load_config(conn: asyncpg.Connection) -> ClusteringConfig:
    """The stored settings, else the defaults (also when a stored row no longer validates: then at
    the stored version, so a save goes forward from it and versions never repeat)."""
    row = await conn.fetchrow("SELECT version, config, updated_by, updated_at FROM cluster_config WHERE id = 1")
    if row is None:
        return DEFAULT_CLUSTERING_CONFIG
    try:
        return ClusteringConfig.model_validate({**dict(row["config"]), "version": row["version"],
                                                "updated_by": row["updated_by"], "updated_at": row["updated_at"]})
    except (ValidationError, TypeError, ValueError):
        log.warning("stored clustering settings are invalid; using the defaults", extra={"version": row["version"]})
        return DEFAULT_CLUSTERING_CONFIG.model_copy(update={"version": int(row["version"])})


async def save_config(conn: asyncpg.Connection, update: ClusteringConfigUpdate, now: dt.datetime) -> ClusteringConfig:
    """Store a new version (``conn`` in the system context). Raises :class:`ConfigConflict` when
    ``base_version`` is stale. Serialised by an advisory lock."""
    await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1, 0))", CONFIG_LOCK)
    current = await load_config(conn)
    if update.base_version != current.version:
        raise ConfigConflict(current.version)
    config: dict[str, Any] = {"simhash": update.simhash.model_dump(mode="json"),
                              "vector": update.vector.model_dump(mode="json")}
    version = current.version + 1
    await conn.execute(
        """INSERT INTO cluster_config (id, version, config, updated_by, updated_at) VALUES (1, $1, $2, $3, $4)
           ON CONFLICT (id) DO UPDATE SET version = EXCLUDED.version, config = EXCLUDED.config,
             updated_by = EXCLUDED.updated_by, updated_at = EXCLUDED.updated_at""",
        version, config, update.updated_by, now,
    )
    saved = ClusteringConfig.model_validate({**config, "version": version, "updated_by": update.updated_by,
                                             "updated_at": now})
    log.info("clustering settings saved", extra={"version": version, "updated_by": update.updated_by})
    return saved
