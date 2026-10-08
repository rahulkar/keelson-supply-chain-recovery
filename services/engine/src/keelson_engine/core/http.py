"""Shared ``httpx.AsyncClient`` instances.

* :func:`internal_http` — Keelson's own infrastructure (realm ``keelson`` via Caddy,
  Keycloak admin, LiteLLM, optimizer). No proxy.
* :func:`connector_http` — third-party connector traffic (Merxa MCP + merxa realm). Always
  through the egress proxy ``CONNECTOR_PROXY_URL`` (``00-overview.md``).

Tests replace either client with :func:`set_http_clients` (e.g. respx-mocked clients).
"""

from __future__ import annotations

import httpx

from keelson_engine.config import get_settings

__all__ = ["close_http", "connector_http", "internal_http", "set_http_clients"]

_internal: httpx.AsyncClient | None = None
_connector: httpx.AsyncClient | None = None


def _timeout() -> httpx.Timeout:
    t = get_settings().http_timeout_seconds
    return httpx.Timeout(t, connect=min(t, 10.0))


def internal_http() -> httpx.AsyncClient:
    global _internal
    if _internal is None:
        _internal = httpx.AsyncClient(timeout=_timeout(), follow_redirects=False, trust_env=False)
    return _internal


def connector_http() -> httpx.AsyncClient:
    global _connector
    if _connector is None:
        proxy = get_settings().connector_proxy_url
        _connector = httpx.AsyncClient(
            timeout=_timeout(), follow_redirects=False, trust_env=False, proxy=proxy
        )
    return _connector


def set_http_clients(
    *, internal: httpx.AsyncClient | None = None, connector: httpx.AsyncClient | None = None
) -> None:
    global _internal, _connector
    if internal is not None:
        _internal = internal
    if connector is not None:
        _connector = connector


async def close_http() -> None:
    global _internal, _connector
    for client in (_internal, _connector):
        if client is not None:
            await client.aclose()
    _internal = None
    _connector = None
