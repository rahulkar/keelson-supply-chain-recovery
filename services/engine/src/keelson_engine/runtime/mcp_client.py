"""Deterministic MCP client for connector calls (FastMCP 4, streamable HTTP).

* Every request goes through the egress proxy (``CONNECTOR_PROXY_URL``, Squid), except requests to
  the configured internal first-party origin (M6: ``SIGNAL_HUB_URL``, ``100`` §1, §6): the proxy
  denies internal names by design, so the Signal Hub is dialled directly (exact origin match only).
* Authentication follows the connector's ``auth_kind`` (``70-backoffice-m2.md`` §3):
  ``oauth_platform_merxa`` (default) → a Bearer token from the Token Broker for the requested
  principal (``reader``/``preparer``/``autocommit`` service accounts, or a user's delegated
  token); ``static_header`` / ``oauth_client_credentials`` / ``none`` → the HTTP headers from
  ``core.connectors.auth_headers(tenant, connector, principal)`` (decrypted header value or a
  client-credentials access token; service principals only). Header values are never logged.
* The endpoint is the connector row's ``mcp_url`` (the platform Merxa connector keeps
  ``MERXA_MCP_URL``); connector rows are cached for :data:`CONNECTOR_INFO_TTL_S`.
* Explicit timeouts: connect 15 s, call 120 s.
* Results are read from ``structuredContent`` (the text block is only a one-line summary).
* Tool errors (``isError``) are parsed as ``"<code>: <detail>"`` into :class:`McpToolError`;
  transport/protocol failures become :class:`McpTransportError`.
* No implicit pagination: callers page explicitly with ``cursor``/``next_cursor``.
"""

from __future__ import annotations

import inspect
import re
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import httpx2
import structlog
from fastmcp import Client
from fastmcp.client.transports import ClientTransport, StreamableHttpTransport
from mcp import types as mcp_types
from mcp.shared.exceptions import MCPError

from .deps import HEADER_AUTH_KINDS, PLATFORM_AUTH_KIND, ConnectorInfo, RuntimeDeps
from .errors import McpToolError, McpTransportError, RuntimeConfigError

__all__ = [
    "CALL_TIMEOUT_S",
    "CONNECTOR_INFO_TTL_S",
    "CONNECT_TIMEOUT_S",
    "CallRecord",
    "ConnectorAuth",
    "ConnectorSession",
    "McpGateway",
    "PrincipalSpec",
    "ServicePrincipal",
    "UserPrincipal",
    "connector_client",
    "parse_tool_error",
    "structured_result",
]

log = structlog.get_logger(__name__)

CONNECT_TIMEOUT_S = 15.0
CALL_TIMEOUT_S = 120.0
CONNECTOR_INFO_TTL_S = 15.0
"""How long a connector row (URL, auth kind) is reused by the gateway."""

_ERROR_RE = re.compile(r"^\s*(?:Error calling tool '[^']*':\s*)?([a-z][a-z0-9_]*)\s*:\s*(.*)\Z", re.S)

ServicePrincipal = Literal["reader", "preparer", "autocommit"]


@dataclass(frozen=True)
class UserPrincipal:
    """A delegated user token (the approver's link), identified by the Keelson ``sub``."""

    keelson_sub: str


PrincipalSpec = ServicePrincipal | UserPrincipal


def principal_label(principal: PrincipalSpec) -> str:
    return principal if isinstance(principal, str) else "user"


def parse_tool_error(text: str) -> tuple[str, str]:
    """``"auto_cap_exceeded: too many items"`` → ``("auto_cap_exceeded", "too many items")``.

    Unparseable messages map to ``("tool_error", text)``.
    """
    m = _ERROR_RE.match(text or "")
    if not m:
        return "tool_error", (text or "").strip()
    return m.group(1), m.group(2).strip()


def structured_result(tool: str, result: mcp_types.CallToolResult) -> dict[str, Any]:
    """``structuredContent`` of a successful call, or raise :class:`McpToolError`."""
    if result.is_error:
        text = " ".join(
            block.text for block in result.content if isinstance(block, mcp_types.TextContent)
        )
        code, detail = parse_tool_error(text)
        raise McpToolError(tool, code, detail)
    data = result.structured_content
    if not isinstance(data, dict):
        raise McpToolError(tool, "no_structured_content", "tool returned no structuredContent object")
    return data


TransportFactory = Callable[..., ClientTransport]
"""``(url, bearer_token, *, headers=None) -> transport``; tests plug an in-memory FastMCP
transport here. ``bearer_token`` is ``""`` for header-authenticated connectors; a factory that
does not take ``headers`` can only serve Bearer (platform) connectors."""


@dataclass(frozen=True)
class ConnectorAuth:
    """How one session authenticates: a Bearer token and/or extra headers (never logged)."""

    bearer: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __repr__(self) -> str:  # never leak credentials into logs or tracebacks
        return f"ConnectorAuth(bearer={'***' if self.bearer else None}, headers={sorted(self.headers)})"


def proxied_client_kwargs(proxy_url: str | None) -> dict[str, Any]:
    """``httpx2.AsyncClient`` options for connector traffic: egress proxy, no env proxies, no
    redirects (credentials never follow a redirect), explicit timeouts."""
    return {
        "proxy": proxy_url,
        "follow_redirects": False,
        "trust_env": False,
        "timeout": httpx2.Timeout(CALL_TIMEOUT_S, connect=CONNECT_TIMEOUT_S),
    }


def url_origin(url: str | None) -> tuple[str, str, int] | None:
    """``(scheme, host, port)`` of an absolute http(s) URL (default ports normalised), else ``None``."""
    try:
        parts = urlsplit(str(url or ""))
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return None
    host = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not host:
        return None
    return parts.scheme, host, port


def proxied_transport_factory(
    proxy_url: str | None,
    *,
    client_transport: httpx2.AsyncBaseTransport | None = None,
    direct_origins: Sequence[str] = (),
) -> TransportFactory:
    """Streamable HTTP through the egress proxy with explicit timeouts.

    ``direct_origins``: internal first-party origins (M6: ``SIGNAL_HUB_URL``) dialled without the
    proxy — Squid denies single-label / internal hosts and non-web ports by design, so the Signal
    Hub's MCP endpoint is unreachable through it. Only an exact ``scheme://host:port`` match
    bypasses the proxy; redirects are never followed either way.

    ``client_transport`` (tests only) replaces the network transport of the HTTP client, e.g. an
    ``httpx2.ASGITransport`` around a FastMCP test server; every other option is unchanged."""
    direct = frozenset(o for o in (url_origin(u) for u in direct_origins) if o is not None)

    def factory(url: str, token: str, *, headers: Mapping[str, str] | None = None) -> ClientTransport:
        bypass = url_origin(url) in direct

        def http_client(
            headers: dict[str, str] | None = None,
            timeout: httpx2.Timeout | None = None,
            auth: httpx2.Auth | None = None,
            **_: Any,
        ) -> httpx2.AsyncClient:
            kwargs = proxied_client_kwargs(None if bypass else proxy_url)
            if client_transport is not None:
                kwargs.pop("proxy")
                kwargs["transport"] = client_transport
            return httpx2.AsyncClient(headers=headers, auth=auth, **kwargs)

        return StreamableHttpTransport(
            url, headers=dict(headers or {}), auth=token or None, httpx_client_factory=http_client  # type: ignore[arg-type]
        )

    return factory


def _checked_url(connector: str, url: str) -> str:
    """Defence in depth behind the admin URL policy and the egress proxy: only absolute
    http(s) URLs without embedded credentials are ever dialled."""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise RuntimeConfigError(f"connector {connector!r} has an invalid MCP URL (http(s) only, no credentials)")
    return url


_RESERVED_AUTH_HEADERS = frozenset({
    "host", "cookie", "content-length", "content-type", "transfer-encoding", "connection", "upgrade",
    "proxy-authorization", "proxy-connection", "mcp-session-id", "mcp-protocol-version", "accept", "te",
    "trailer", "last-event-id",
})
"""Headers a connector credential may never set (transport, proxy and MCP session headers)."""
_HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,64}$")
_HEADER_VALUE_RE = re.compile(r"^[\x20-\x7e\t]{1,8192}$")


def _auth_header_ok(name: str, value: str) -> bool:
    """Defence in depth behind the admin validation (``tenants.yaml`` / pack rows bypass it): a
    credential header is a token-shaped, non-reserved name with a printable single-line value."""
    return (
        bool(_HEADER_NAME_RE.match(name))
        and name.lower() not in _RESERVED_AUTH_HEADERS
        and bool(_HEADER_VALUE_RE.match(value or ""))
    )


def _accepts_headers(factory: TransportFactory) -> bool:
    try:
        params = inspect.signature(factory).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(p.name == "headers" or p.kind is p.VAR_KEYWORD for p in params)


@dataclass(frozen=True)
class CallRecord:
    tenant: str
    connector: str
    principal: str
    tool: str
    args: dict[str, Any]
    ok: bool
    error_code: str | None
    duration_ms: int


class ConnectorSession:
    """An open MCP session for one principal (stateless server: cheap to open per batch)."""

    def __init__(
        self,
        client: Client,
        *,
        tenant: str,
        connector: str,
        principal: PrincipalSpec,
        observer: Callable[[CallRecord], None] | None,
    ) -> None:
        self._client = client
        self.tenant = tenant
        self.connector = connector
        self.principal = principal
        self._observer = observer

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> dict[str, Any]:
        arguments = {k: v for k, v in (args or {}).items() if v is not None}
        started = time.monotonic()
        error_code: str | None = None
        try:
            try:
                raw = await self._client.call_tool_mcp(tool, arguments, timeout=CALL_TIMEOUT_S)
            except (MCPError, httpx2.HTTPError, OSError, TimeoutError) as exc:
                error_code = "transport"
                raise McpTransportError(tool, f"{type(exc).__name__}: {exc}") from exc
            try:
                return structured_result(tool, raw)
            except McpToolError as exc:
                error_code = exc.code
                raise
        finally:
            record = CallRecord(
                tenant=self.tenant,
                connector=self.connector,
                principal=principal_label(self.principal),
                tool=tool,
                args=arguments,
                ok=error_code is None,
                error_code=error_code,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
            log.info(
                "connector call", tool=tool, connector=self.connector, principal=record.principal,
                ok=record.ok, error_code=error_code, duration_ms=record.duration_ms,
            )
            if self._observer is not None:
                self._observer(record)

    async def list_tools(self) -> list[mcp_types.Tool]:
        try:
            return list(await self._client.list_tools())
        except (MCPError, httpx2.HTTPError, OSError, TimeoutError) as exc:
            raise McpTransportError("tools/list", f"{type(exc).__name__}: {exc}") from exc


UrlFor = Callable[[str, str], "str | None | Awaitable[str | None]"]
"""``(tenant, connector) -> url``; ``None`` = use the connector row's ``mcp_url``."""


class McpGateway:
    """Opens connector sessions with the right principal's credentials."""

    def __init__(
        self,
        deps: Callable[[], RuntimeDeps],
        *,
        url_for: UrlFor | None = None,
        transport_factory: TransportFactory,
        observer: Callable[[CallRecord], None] | None = None,
        info_ttl_s: float = CONNECTOR_INFO_TTL_S,
    ) -> None:
        self._deps = deps
        self._url_for = url_for
        self._transport_factory = transport_factory
        self._headers_ok = _accepts_headers(transport_factory)
        self._observer = observer
        self._info_ttl = info_ttl_s
        self._info: dict[tuple[str, str], tuple[float, ConnectorInfo]] = {}

    async def connector_info(self, *, tenant: str, connector: str) -> ConnectorInfo:
        """The connector row (URL, auth kind, vendor label), cached for ``info_ttl_s``."""
        key = (tenant, connector)
        now = time.monotonic()
        cached = self._info.get(key)
        if cached is not None and cached[0] > now:
            return cached[1]
        try:
            info = await self._deps().connectors.get_connector(tenant=tenant, slug=connector)
        except LookupError as exc:
            raise RuntimeConfigError(f"connector {connector!r} is not configured for this tenant") from exc
        self._info[key] = (now + self._info_ttl, info)
        return info

    def forget(self, *, tenant: str | None = None) -> None:
        """Drop cached connector rows (all, or one tenant's)."""
        if tenant is None:
            self._info.clear()
        else:
            for key in [k for k in self._info if k[0] == tenant]:
                del self._info[key]

    async def usable_info(self, *, tenant: str, connector: str) -> ConnectorInfo:
        """The connector row, refused unless the runtime may call it: a ``draft`` / ``tested`` /
        ``disabled`` connector is never called, whatever its auth kind (the Token Broker and
        ``none`` auth do not check the status themselves)."""
        info = await self.connector_info(tenant=tenant, connector=connector)
        if not info.usable:
            raise RuntimeConfigError(f"connector {connector!r} is {info.status}, not active, for this tenant")
        return info

    async def token(self, *, tenant: str, connector: str, principal: PrincipalSpec) -> str:
        return await self._token(tenant, connector, principal)

    async def _token(self, tenant: str, connector: str, principal: PrincipalSpec) -> str:
        tokens = self._deps().tokens
        if isinstance(principal, UserPrincipal):
            return await tokens.user_token(tenant=tenant, connector=connector, keelson_sub=principal.keelson_sub)
        return await tokens.service_token(tenant=tenant, connector=connector, principal=principal)

    async def auth(self, *, tenant: str, connector: str, principal: PrincipalSpec) -> ConnectorAuth:
        """Credentials for ``principal`` according to the connector's ``auth_kind``."""
        info = await self.usable_info(tenant=tenant, connector=connector)
        kind = info.auth_kind or PLATFORM_AUTH_KIND
        if kind == PLATFORM_AUTH_KIND:
            return ConnectorAuth(bearer=await self._token(tenant, connector, principal))
        if kind not in HEADER_AUTH_KINDS:
            raise RuntimeConfigError(f"connector {connector!r} has unsupported auth kind {kind!r}")
        if isinstance(principal, UserPrincipal):
            raise RuntimeConfigError(
                f"connector {connector!r} ({kind}) has no delegated user tokens; only service principals"
            )
        if kind == "none":
            return ConnectorAuth()
        try:
            headers = await self._deps().connectors.auth_headers(
                tenant=tenant, connector=connector, principal=principal
            )
        except LookupError as exc:  # ConnectorNotFound / ConnectorDisabled (not active)
            raise RuntimeConfigError(f"connector {connector!r} is not active for this tenant") from exc
        if not headers:
            raise RuntimeConfigError(f"connector {connector!r} ({kind}) returned no auth headers")
        bad = sorted(name for name in headers if not _auth_header_ok(name, headers[name]))
        if bad:  # never echo the value; the name is admin config, not a secret
            raise RuntimeConfigError(f"connector {connector!r} has unusable auth header(s) {bad}")
        return ConnectorAuth(headers=dict(headers))

    async def url(self, *, tenant: str, connector: str) -> str:
        """``url_for`` may override platform connectors (``MERXA_MCP_URL``); a registry connector
        always uses its row's ``mcp_url`` (e.g. a tenant whose ``merxa`` slug is ``/mcp-lite``)."""
        info = await self.connector_info(tenant=tenant, connector=connector)
        if self._url_for is not None and (info.auth_kind == PLATFORM_AUTH_KIND or not info.url):
            result = self._url_for(tenant, connector)
            if inspect.isawaitable(result):
                result = await result
            if result:
                return str(result)
        if not info.url:
            raise RuntimeConfigError(f"connector {connector!r} has no MCP URL")
        return _checked_url(connector, info.url)

    async def client(
        self, *, tenant: str, connector: str, principal: PrincipalSpec, token: str | None = None
    ) -> Client:
        """``token`` overrides the broker lookup (a Bearer token the caller already fetched for
        this principal, e.g. to read its ``sub`` for the approval receipt)."""
        await self.usable_info(tenant=tenant, connector=connector)  # also with a caller's token
        auth = ConnectorAuth(bearer=token) if token else await self.auth(
            tenant=tenant, connector=connector, principal=principal
        )
        url = await self.url(tenant=tenant, connector=connector)
        if auth.headers:
            if not self._headers_ok:
                raise RuntimeConfigError("the MCP transport factory cannot send connector auth headers")
            transport = self._transport_factory(url, auth.bearer or "", headers=dict(auth.headers))
        else:
            transport = self._transport_factory(url, auth.bearer or "")
        return Client(transport, timeout=CALL_TIMEOUT_S, init_timeout=CONNECT_TIMEOUT_S)

    @asynccontextmanager
    async def session(
        self, *, tenant: str, connector: str, principal: PrincipalSpec, token: str | None = None
    ) -> AsyncIterator[ConnectorSession]:
        client = await self.client(tenant=tenant, connector=connector, principal=principal, token=token)
        async with AsyncExitStack() as stack:
            try:
                await stack.enter_async_context(client)
            except (MCPError, httpx2.HTTPError, OSError, TimeoutError, RuntimeError) as exc:
                # FastMCP wraps connect/initialize failures (proxy denied, 401, DNS) in
                # RuntimeError; only the connect step is translated, never the caller's body.
                raise McpTransportError("session", f"{type(exc).__name__}: {exc}") from exc
            yield ConnectorSession(
                client, tenant=tenant, connector=connector, principal=principal, observer=self._observer
            )

    async def call(
        self, *, tenant: str, connector: str, principal: PrincipalSpec, tool: str, args: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        async with self.session(tenant=tenant, connector=connector, principal=principal) as s:
            return await s.call(tool, args)


_gateway: McpGateway | None = None


def _default_url_for(tenant: str, connector: str) -> str | None:
    """The platform Merxa connector keeps ``MERXA_MCP_URL``; every other connector uses its row's
    ``mcp_url`` (registered and URL-policy checked by the admin API)."""
    from .settings import engine_settings  # noqa: PLC0415

    if connector != "merxa":
        return None
    return str(engine_settings().merxa_mcp_url)


def get_gateway() -> McpGateway:
    global _gateway
    if _gateway is None:
        from .deps import get_deps  # noqa: PLC0415
        from .settings import engine_settings  # noqa: PLC0415

        _gateway = McpGateway(
            get_deps,
            url_for=_default_url_for,
            transport_factory=proxied_transport_factory(
                engine_settings().connector_proxy_url,
                direct_origins=tuple(u for u in (getattr(engine_settings(), "signal_hub_url", ""),) if u),
            ),
        )
    return _gateway


def set_gateway(gateway: McpGateway | None) -> None:
    global _gateway
    _gateway = gateway


async def connector_client(tenant: str, connector: str, principal: PrincipalSpec) -> Client:
    """A (not yet connected) FastMCP client for ``principal`` via the egress proxy."""
    return await get_gateway().client(tenant=tenant, connector=connector, principal=principal)
