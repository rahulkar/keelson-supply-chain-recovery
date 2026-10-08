"""Pure-ASGI middleware (streaming-safe, unlike ``BaseHTTPMiddleware``).

* binds a ``trace_id`` (``X-Request-ID`` if sane, else new) for structured logs and echoes it;
* **Origin check**: every non-safe method under ``/api/`` must carry
  ``Origin == PUBLIC_URL origin`` (``10-auth.md``); the CSRF *token* is checked by the
  session dependency, because only it knows the session.
"""

from __future__ import annotations

import json
import re
from typing import Any

import structlog
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from keelson_engine.config import get_settings
from keelson_engine.core.logs import bind_trace

__all__ = ["SecurityMiddleware"]

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
WEBHOOK_PREFIX = "/api/webhooks/"
_WEBHOOK_PATH_RE = re.compile(r"^/api/webhooks/(?!\.)[^/]{1,64}/(?!\.)[^/]{1,64}$")
"""Server-to-server deliveries (Standard Webhooks signatures, no cookies): no Origin check — only
for the exact receiver shape ``/api/webhooks/{tenant}/{connector}`` (no ``..`` / extra segments)."""
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,64}$")


class SecurityMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        structlog.contextvars.clear_contextvars()
        incoming = headers.get("x-request-id", "")
        trace_id = bind_trace(incoming if _REQUEST_ID_RE.match(incoming) else None)
        path: str = scope.get("path", "")
        if (scope.get("method", "GET") not in SAFE_METHODS and path.startswith("/api/")
                and not _WEBHOOK_PATH_RE.match(path)):
            origin = headers.get("origin")
            if origin != get_settings().public_origin:
                await _reject(send, 403, "bad_origin", "cross-origin or missing Origin header", trace_id)
                return

        async def send_with_trace(message: Message) -> None:
            if message["type"] == "http.response.start":
                raw: list[Any] = list(message.get("headers", []))
                raw.append((b"x-request-id", trace_id.encode("ascii")))
                message = {**message, "headers": raw}
            await send(message)

        await self.app(scope, receive, send_with_trace)


async def _reject(send: Send, status: int, code: str, message: str, trace_id: str) -> None:
    body = json.dumps({"code": code, "message": message}).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"x-request-id", trace_id.encode("ascii")),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})
