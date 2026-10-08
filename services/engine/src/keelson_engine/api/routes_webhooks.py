"""``POST /api/webhooks/{tenant}/{connector}`` — Standard Webhooks receiver (no cookie auth,
CSRF/Origin-exempt, body ≤ ``WEBHOOK_MAX_BODY_BYTES``, rate-limited per ``(tenant, connector)``).
Verification + storage: :mod:`keelson_engine.core.webhooks`. Always 204 on success (duplicates too).
"""

from __future__ import annotations

import time

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from keelson_engine.config import get_settings
from keelson_engine.core.logs import get_logger
from keelson_engine.core.webhooks import WebhookRejected, receive

__all__ = ["router"]

log = get_logger(__name__)
router = APIRouter()

_SLUG_OK = frozenset("abcdefghijklmnopqrstuvwxyz0123456789-")


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"code": code, "message": message}, status_code=status)


def _slug(value: str) -> bool:
    return 0 < len(value) <= 63 and value[0] != "-" and set(value) <= _SLUG_OK


async def _rate_limited(tenant: str, connector: str) -> bool:
    limit = get_settings().webhook_rate_per_minute
    if limit <= 0:
        return False
    try:
        from keelson_engine.core.valkey import get_valkey

        key = f"keelson:wh:rl:{tenant}:{connector}:{int(time.time() // 60)}"
        valkey = get_valkey()
        count = await valkey.incr(key)
        if count == 1:
            await valkey.expire(key, 120)
        return int(count) > limit
    except Exception as exc:  # noqa: BLE001 - fail open: signature + dedupe still protect
        log.warning("webhook rate limiter unavailable", error=exc.__class__.__name__)
        return False


@router.post("/api/webhooks/{tenant}/{connector}", status_code=204)
async def webhook(tenant: str, connector: str, request: Request) -> Response:
    if not _slug(tenant) or not _slug(connector):
        return _error(404, "not_found", "unknown webhook endpoint")
    max_bytes = get_settings().webhook_max_body_bytes
    declared = request.headers.get("content-length")
    if declared is not None:
        try:
            if int(declared) > max_bytes:
                return _error(413, "payload_too_large", "the body exceeds the size limit")
        except ValueError:
            return _error(400, "invalid_request", "invalid Content-Length")
    if await _rate_limited(tenant, connector):
        return _error(429, "rate_limited", "too many webhook deliveries; retry later")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > max_bytes:
            return _error(413, "payload_too_large", "the body exceeds the size limit")
        chunks.append(chunk)
    headers = {k.lower(): v for k, v in request.headers.items()}
    try:
        await receive(tenant, connector, headers, b"".join(chunks))
    except WebhookRejected as exc:
        return _error(exc.status, exc.code, exc.message)
    return Response(status_code=204)
