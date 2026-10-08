"""Valkey (Redis protocol) client used for run wake-ups and the live event tail."""

from __future__ import annotations

from redis.asyncio import Redis

from keelson_engine.config import get_settings

__all__ = ["close_valkey", "get_valkey", "set_valkey"]

_client: Redis | None = None

SOCKET_TIMEOUT_S = 30.0
"""Read timeout; every ``XREAD BLOCK`` in the engine must block for less than this."""


def get_valkey() -> Redis:
    """Shared client (``decode_responses=True``, RESP2 replies; one pool per process)."""
    global _client
    if _client is None:
        _client = Redis.from_url(
            get_settings().valkey_url.get_secret_value(),
            decode_responses=True,
            protocol=2,
            socket_connect_timeout=5,
            # redis-py 8 defaults socket_timeout to 5 s, which aborts every XREAD BLOCK of
            # 5 s (event tail, worker wake-up). Blocking reads stay well below this.
            socket_timeout=SOCKET_TIMEOUT_S,
            health_check_interval=30,
        )
    return _client


def set_valkey(client: Redis | None) -> None:
    global _client
    _client = client


async def close_valkey() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
    _client = None
