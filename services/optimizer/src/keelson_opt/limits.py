"""Request body size limit (pure ASGI middleware).

The optimizer parses whole JSON bodies into Pydantic models inside a 512 MB container,
so an oversized body must be refused before it is buffered: by ``Content-Length`` when
the client declares one, otherwise by counting the streamed chunks.
"""

from __future__ import annotations

import json

from fastapi import HTTPException, status
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_DETAIL = "request body too large"


class BodySizeLimit:
    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        declared = _content_length(scope)
        if declared is not None and declared > self.max_bytes:
            await _reject(send)
            return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    # Raised inside FastAPI's body read; it re-raises HTTPException, so the
                    # app's exception middleware turns it into a 413 response.
                    raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail=_DETAIL)
            return message

        await self.app(scope, limited_receive, send)


def _content_length(scope: Scope) -> int | None:
    for name, value in scope.get("headers", ()):
        if name == b"content-length":
            try:
                return int(value)
            except ValueError:
                return None
    return None


async def _reject(send: Send) -> None:
    body = json.dumps({"detail": _DETAIL}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status.HTTP_413_CONTENT_TOO_LARGE,
            "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())],
        }
    )
    await send({"type": "http.response.body", "body": body})
