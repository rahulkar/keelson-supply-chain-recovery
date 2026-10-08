"""Outbound HTTP for the feed adapters (``100`` §3 "All HTTP").

* conditional requests: ``If-None-Match`` (ETag) / ``If-Modified-Since`` → ``304`` = not modified;
* per-source rate limit: at least ``min_interval_s`` between two requests to one source
  (``connector_id``), enforced in-process (the poller is a single leader);
* explicit timeouts and a response size cap (streamed, aborted beyond ``max_bytes``);
* public feeds go **only** through the egress proxy when one is configured (``HTTPS_PROXY``,
  compose: ``http://egress:3128``); ``trust_env`` is off so a stray ``NO_PROXY`` can never route a
  feed around it. Internal calls (demo fixture route, LiteLLM) use a separate client without proxy;
* a destination guard on every request **and every redirect hop** of the feed client
  (:func:`destination_guard`): only ``http``/``https`` to public host names or public IP literals —
  no single-label/internal names (``litellm``, ``pg-core``, ``*.internal``, ``*.localhost``…), no
  private/loopback/link-local/metadata addresses. Without an egress proxy (Cloud Run jobs leave
  through Cloud NAT with VPC egress) the host name is also resolved and refused when any address is
  not public; with a proxy the proxy enforces that (Squid ``private_dst``).
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass

import httpx

from signal_hub.config import Settings

__all__ = [
    "BlockedDestination",
    "FetchError",
    "FetchResult",
    "Fetcher",
    "build_feed_client",
    "build_internal_client",
    "destination_guard",
    "host_is_public_name",
]

Resolver = Callable[[str, int], Awaitable[Iterable[str]]]
_INTERNAL_SUFFIXES = (".localhost", ".local", ".internal", ".lan", ".home.arpa", ".localdomain", ".intranet", ".corp")


class FetchError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True, slots=True)
class FetchResult:
    status: int
    body: bytes
    etag: str | None
    last_modified: str | None
    content_type: str | None

    @property
    def not_modified(self) -> bool:
        return self.status == 304

    @property
    def empty(self) -> bool:
        return self.status == 204 or not self.body.strip()


class BlockedDestination(httpx.RequestError):
    """A feed request (or one of its redirects) aimed at a non-public destination."""


def _ip(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    try:
        return ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return None


def _public_ip(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped
    return bool(addr.is_global) and not addr.is_multicast


def host_is_public_name(host: str) -> bool:
    """A host that may be fetched: a public IP literal, or a dotted name outside internal TLDs."""
    host = (host or "").strip().rstrip(".").lower()
    if not host:
        return False
    addr = _ip(host)
    if addr is not None:
        return _public_ip(addr)
    if "." not in host or host == "localhost" or host.endswith(_INTERNAL_SUFFIXES):
        return False
    return True


async def _system_resolver(host: str, port: int) -> list[str]:
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def destination_guard(*, resolve: bool, resolver: Resolver | None = None) -> Callable[[httpx.Request], Awaitable[None]]:
    """httpx ``request`` event hook (runs before every request, redirects included)."""
    lookup = resolver or _system_resolver

    async def check(request: httpx.Request) -> None:
        url = request.url
        host = url.host or ""
        if url.scheme not in ("http", "https") or not host_is_public_name(host):
            raise BlockedDestination(f"destination not allowed: {url.scheme}://{host}", request=request)
        if resolve and _ip(host) is None:
            try:
                addresses = list(await lookup(host, url.port or (443 if url.scheme == "https" else 80)))
            except OSError as exc:
                raise httpx.ConnectError(f"cannot resolve {host} ({type(exc).__name__})", request=request) from None
            bad = [a for a in addresses if (ip := _ip(a)) is None or not _public_ip(ip)]
            if not addresses or bad:
                raise BlockedDestination(f"destination not allowed: {host} resolves to a non-public address",
                                         request=request)

    return check


def _timeout(settings: Settings) -> httpx.Timeout:
    return httpx.Timeout(
        connect=settings.http_connect_timeout_s,
        read=settings.http_read_timeout_s,
        write=10.0,
        pool=10.0,
    )


def build_feed_client(settings: Settings, *, resolver: Resolver | None = None,
                      transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    """Public feeds: through the egress proxy when configured, destination-guarded on every hop."""
    guard = destination_guard(resolve=settings.outbound_proxy is None, resolver=resolver)
    return httpx.AsyncClient(
        proxy=settings.outbound_proxy if transport is None else None,
        transport=transport,
        trust_env=False,
        timeout=_timeout(settings),
        follow_redirects=True,
        max_redirects=3,
        headers={"User-Agent": settings.user_agent, "Accept-Encoding": "gzip, deflate"},
        event_hooks={"request": [guard]},
    )


def build_internal_client(settings: Settings) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        trust_env=False,
        timeout=_timeout(settings),
        follow_redirects=False,
        headers={"User-Agent": settings.user_agent},
    )


class Fetcher:
    def __init__(self, client: httpx.AsyncClient, *, min_interval_s: float = 5.0, max_bytes: int = 12 * 1024 * 1024) -> None:
        self.client = client
        self.min_interval_s = min_interval_s
        self.max_bytes = max_bytes
        self._next_at: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    async def _throttle(self, source: str) -> None:
        lock = self._locks.setdefault(source, asyncio.Lock())
        async with lock:
            wait = self._next_at.get(source, 0.0) - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._next_at[source] = time.monotonic() + self.min_interval_s

    async def get(
        self,
        source: str,
        url: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        accept: str | None = None,
        throttle: bool = True,
    ) -> FetchResult:
        if throttle and self.min_interval_s > 0:
            await self._throttle(source)
        headers: dict[str, str] = {}
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified
        if accept:
            headers["Accept"] = accept
        try:
            async with self.client.stream("GET", url, headers=headers) as resp:
                if resp.status_code == 304:
                    return FetchResult(304, b"", etag, last_modified, None)
                if resp.status_code >= 400:
                    raise FetchError(f"{source}: HTTP {resp.status_code}", status=resp.status_code)
                declared = resp.headers.get("content-length")
                if declared and declared.isdigit() and int(declared) > self.max_bytes:
                    raise FetchError(f"{source}: response larger than {self.max_bytes} bytes")
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > self.max_bytes:
                        raise FetchError(f"{source}: response larger than {self.max_bytes} bytes")
                    chunks.append(chunk)
                return FetchResult(
                    resp.status_code,
                    b"".join(chunks),
                    resp.headers.get("etag"),
                    resp.headers.get("last-modified"),
                    resp.headers.get("content-type"),
                )
        except BlockedDestination as exc:
            raise FetchError(f"{source}: {exc}"[:300]) from exc
        except httpx.HTTPError as exc:
            raise FetchError(f"{source}: {type(exc).__name__}") from exc
