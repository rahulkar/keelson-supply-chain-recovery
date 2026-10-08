"""Valkey client: blocking stream reads must outlive their ``BLOCK`` time.

Regression (integration): redis-py 8 defaults ``socket_timeout`` to 5 s, so every
``XREAD BLOCK 5000`` (SSE tail, worker wake-up) died with "Timeout reading from valkey" and
the worker fell back to its poll interval (~5 s extra latency before a run started).
"""

from __future__ import annotations

import time

import pytest

from keelson_engine.core.valkey import SOCKET_TIMEOUT_S, get_valkey

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_socket_timeout_exceeds_every_block_time(platform: None) -> None:
    client = get_valkey()
    assert client.connection_pool.connection_kwargs["socket_timeout"] == SOCKET_TIMEOUT_S
    assert SOCKET_TIMEOUT_S > 5  # EventBus block_ms=5000, worker idle poll 5 s


async def test_xread_block_returns_empty_instead_of_timing_out(platform: None) -> None:
    started = time.monotonic()
    resp = await get_valkey().xread({"keelson:test:empty-stream": "$"}, count=1, block=5500)
    assert not resp
    assert time.monotonic() - started >= 5.0
