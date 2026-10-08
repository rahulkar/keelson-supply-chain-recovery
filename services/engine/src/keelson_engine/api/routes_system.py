"""Health, JWKS, Caddy forward-auth and the console handoff.

Console handoff (gateway./traces.localhost are other hosts, so the host-only
``keelson_session`` cookie never reaches them):

1. ``GET /api/console/handoff?target=gateway|traces`` (platform-admin, on keelson.localhost)
   mints a 60 s single-use ticket and redirects to ``http://<target>.<domain>/__keelson/handoff?ticket=…``.
2. Caddy routes ``/__keelson/*`` on those hosts to engine-api **without** forward_auth;
   ``GET /__keelson/handoff`` consumes the ticket (Host must match the ticket target), stores a
   server-side console session and sets the host-only ``keelson_console`` cookie (8 h), then
   redirects to ``/``.
3. ``GET /api/internal/forward-auth?role=…`` accepts ``keelson_console`` for the request's Host
   (Caddy forwards ``Host: gateway.localhost``) or a ``keelson_session``.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import text

from keelson_engine.api.deps import CurrentSession, optional_session
from keelson_engine.api.errors import ApiError
from keelson_engine.api.routes_auth import keelson_oidc
from keelson_engine.api.sessions import (
    CONSOLE_COOKIE,
    consume_console_ticket,
    create_console_session,
    create_console_ticket,
    delete_console_session,
    load_console_session,
)
from keelson_engine.config import get_settings
from keelson_engine.core import crypto, repo
from keelson_engine.core.db import system_session
from keelson_engine.core.http import internal_http
from keelson_engine.core.logs import get_logger
from keelson_engine.core.valkey import get_valkey

__all__ = ["CONSOLE_TARGETS", "router"]

log = get_logger(__name__)
router = APIRouter()

CONSOLE_TARGETS = ("gateway", "traces")
_FORWARD_AUTH_ROLES = frozenset({"platform-admin", "tenant-admin"})
_READY_TTL_S = 10.0
_NO_STORE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_ready_cache: tuple[float, dict[str, Any]] | None = None


@router.get("/api/healthz")
async def healthz() -> dict[str, Any]:
    return {"ok": True}


async def _check(coro: Any, limit_s: float = 3.0) -> bool:
    try:
        return bool(await asyncio.wait_for(coro, limit_s))
    except Exception as exc:  # noqa: BLE001 - readiness reports, never raises
        log.info("readiness check failed", error=f"{exc.__class__.__name__}: {exc}")
        return False


async def _db_ok() -> bool:
    async with system_session() as s:
        await s.execute(text("SELECT 1"))
    return True


async def _valkey_ok() -> bool:
    return bool(await get_valkey().ping())


async def _litellm_ok() -> bool:
    settings = get_settings()
    resp = await internal_http().get(
        f"{settings.litellm_base_url}/model/info",
        headers={"Authorization": f"Bearer {settings.litellm_master_key.get_secret_value()}"},
    )
    if resp.status_code != 200:
        return False
    names = {m.get("model_name") for m in resp.json().get("data", [])}
    return "fast-default" in names


async def _keycloak_ok() -> bool:
    await keelson_oidc().discovery()
    return True


@router.get("/api/readyz")
async def readyz() -> JSONResponse:
    global _ready_cache
    now = time.monotonic()
    if _ready_cache is not None and now - _ready_cache[0] < _READY_TTL_S:
        body = _ready_cache[1]
    else:
        db, valkey, litellm, keycloak = await asyncio.gather(
            _check(_db_ok()), _check(_valkey_ok()), _check(_litellm_ok()), _check(_keycloak_ok())
        )
        checks = {"db": db, "valkey": valkey, "litellm_alias": litellm, "keycloak": keycloak}
        body = {"ok": all(checks.values()), "checks": checks}
        _ready_cache = (now, body)
    return JSONResponse(body, status_code=200 if body["ok"] else 503)


@router.get("/api/.well-known/jwks.json")
async def jwks() -> JSONResponse:
    return JSONResponse(crypto.jwks(), headers={"Cache-Control": "public, max-age=300"})


def _request_host(request: Request) -> str:
    host = request.headers.get("host", "")
    return host.rsplit(":", 1)[0].lower() if host and not host.startswith("[") else host.lower()


def _console_target_for_host(host: str) -> str | None:
    settings = get_settings()
    for target in CONSOLE_TARGETS:
        if host == settings.console_host(target):
            return target
    return None


@router.get("/api/internal/forward-auth")
async def forward_auth(request: Request, role: str = "platform-admin") -> Response:
    if role not in _FORWARD_AUTH_ROLES:
        return JSONResponse({"code": "bad_role"}, status_code=400)
    target = _console_target_for_host(_request_host(request))
    principal: dict[str, Any] | None = None
    if target is not None:
        principal = await load_console_session(request.cookies.get(CONSOLE_COOKIE), target)
    if principal is None:
        sess = await optional_session(request)
        if sess is not None:
            principal = {"sub": sess.sub, "username": sess.username, "roles": sess.roles}
    if principal is None:
        return JSONResponse({"code": "unauthenticated"}, status_code=401)
    roles = [str(r) for r in principal.get("roles") or []]
    if role not in roles:
        return JSONResponse({"code": "forbidden"}, status_code=403)
    return Response(
        status_code=200,
        headers={"X-Auth-User": str(principal["username"]), "X-Auth-Roles": ",".join(sorted(roles))},
    )


@router.get("/api/console/handoff")
async def console_handoff(sess: CurrentSession, target: str) -> RedirectResponse:
    if target not in CONSOLE_TARGETS:
        raise ApiError(400, "bad_target", "target must be gateway or traces")
    if not sess.has_any("platform-admin"):
        raise ApiError(403, "forbidden", "platform-admin only")
    settings = get_settings()
    ticket = await create_console_ticket(target=target, sub=sess.sub, username=sess.username, roles=sess.roles)
    await repo.admin_audit(sess.actor(), "console.handoff", f"console:{target}", None, None, platform=True)
    url = f"{settings.console_origin(target)}/__keelson/handoff?{urlencode({'ticket': ticket})}"
    return RedirectResponse(url, status_code=302, headers=_NO_STORE)


@router.get("/__keelson/handoff")
async def console_handoff_landing(request: Request, ticket: str = "") -> Response:
    settings = get_settings()
    target = _console_target_for_host(_request_host(request))
    if target is None:
        return JSONResponse({"code": "not_found"}, status_code=404)
    claims = await consume_console_ticket(ticket, target)
    if claims is None or "platform-admin" not in (claims.get("roles") or []):
        return JSONResponse({"code": "invalid_ticket", "message": "this link expired; open the console again"},
                            status_code=401)
    value = await create_console_session(
        target=target, sub=claims["sub"], username=claims["username"], roles=list(claims["roles"])
    )
    resp = RedirectResponse("/", status_code=302, headers=_NO_STORE)
    resp.set_cookie(
        CONSOLE_COOKIE, value, max_age=settings.console_session_seconds, path="/", httponly=True, samesite="lax",
        secure=settings.cookie_secure,
    )
    return resp


@router.get("/__keelson/logout")
async def console_logout(request: Request) -> Response:
    value = request.cookies.get(CONSOLE_COOKIE)
    if value:
        await delete_console_session(value)
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(CONSOLE_COOKIE, path="/", httponly=True, samesite="lax")
    return resp
