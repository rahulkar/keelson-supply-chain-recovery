"""Runtime configuration from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass

SERVICE_ISSUER = "keelson-engine"
SERVICE_AUDIENCE = "keelson-optimizer"
DEFAULT_MAX_BODY_BYTES = 32 * 1024 * 1024
"""~5x the full-size (180 SKUs x 40 clusters x 9 weeks) request; larger bodies would not
fit the container's 512 MB once parsed into Python objects."""


@dataclass(frozen=True, slots=True)
class Settings:
    port: int
    shared_secret: str | None
    log_level: str
    jwt_leeway_s: int
    max_body_bytes: int = DEFAULT_MAX_BODY_BYTES
    max_concurrency: int = 1
    """Concurrent ``POST /v1/*`` computations (body parse + solve). A full-size request
    peaks at ~250 MB RSS, so the 512 MB / 1.5 CPU container runs one at a time."""

    @property
    def auth_configured(self) -> bool:
        return bool(self.shared_secret)


def _read_secret(name: str) -> str | None:
    """``/run/secrets/<name>`` first, then env ``<NAME>`` (convention from 00-overview)."""
    path = f"/run/secrets/{name.lower()}"
    try:
        with open(path, encoding="utf-8") as fh:
            value = fh.read().strip()
            if value:
                return value
    except OSError:
        pass
    value = os.environ.get(name.upper(), "").strip()
    return value or None


def load_settings() -> Settings:
    return Settings(
        port=int(os.environ.get("PORT", "8200")),
        shared_secret=_read_secret("OPTIMIZER_SHARED_SECRET"),
        log_level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        jwt_leeway_s=int(os.environ.get("OPTIMIZER_JWT_LEEWAY_S", "5")),
        max_body_bytes=int(os.environ.get("OPTIMIZER_MAX_BODY_BYTES", str(DEFAULT_MAX_BODY_BYTES))),
        max_concurrency=max(1, int(os.environ.get("OPTIMIZER_MAX_CONCURRENCY", "1"))),
    )
