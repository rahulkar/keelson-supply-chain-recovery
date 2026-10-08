"""FastAPI application factory: REST /v1, UI BFF auth, health, and the mounted MCP app."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import select, text
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from merxa_api import tables as t
from merxa_api.api import routes_auth, routes_m2, routes_m5, routes_read, routes_write
from merxa_api.config import DEV_SESSION_SECRET, Settings
from merxa_api.errors import Problem
from merxa_api.logs import get_logger, trace_id_from_headers, trace_id_var
from merxa_api.mcp.lite import ApiKeyHeaderMiddleware, build_lite
from merxa_api.mcp.server import build_mcp
from merxa_api.migrate import migrate
from merxa_api.seed.generator import UnsupportedProfile
from merxa_api.seed.loader import golden, is_seeded, loaded_fingerprint, seed
from merxa_api.services import idempotency
from merxa_api.state import AppState

log = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"


class TraceMiddleware:
    """Pure-ASGI middleware: trace id per request (W3C traceparent / X-Request-ID) + access log."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers", [])}
        trace_id = trace_id_from_headers(headers.get("traceparent"), headers.get("x-request-id"))
        token = trace_id_var.set(trace_id)
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                message.setdefault("headers", [])
                message["headers"] = [*message["headers"], (b"x-trace-id", trace_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in ("/healthz", "/readyz"):
                log.info(
                    "http request",
                    extra={
                        "method": scope.get("method"),
                        "path": path,
                        "status": status,
                        "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                    },
                )
            trace_id_var.reset(token)


def _problem_response(problem: Problem, request: Request) -> JSONResponse:
    return JSONResponse(
        problem.to_dict(instance=request.url.path),
        status_code=problem.status,
        headers=problem.headers or None,
        media_type=PROBLEM_JSON,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _problem(request: Request, exc: Problem) -> JSONResponse:
        return _problem_response(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")} for e in exc.errors()]
        return _problem_response(
            Problem(422, "validation_error", "request did not match the expected schema", extra={"errors": errors}),
            request,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _problem_response(Problem(exc.status_code, code, str(exc.detail)), request)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        return _problem_response(Problem(500, "internal_error", "unexpected server error"), request)


async def _wait_for_db(state: AppState, timeout_s: float = 60.0) -> None:
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            async with state.engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            if time.monotonic() > deadline:
                raise
            log.warning("database not ready, retrying", extra={"error": str(exc)})
            await asyncio.sleep(1.0)


async def _warm_golden(settings: Settings) -> None:
    """Generate the golden dataset off the event loop so the first demo reset is fast."""
    try:
        await asyncio.to_thread(golden, settings.seed_profile)
    except UnsupportedProfile:
        pass


async def _housekeeping(state: AppState) -> None:
    last_purge = float("-inf")
    while True:
        try:
            if time.monotonic() - last_purge >= 3600:
                last_purge = time.monotonic()
                purged = await idempotency.purge_expired(state.engine, state.settings.idempotency_retention_h)
                if purged:
                    log.info("purged idempotency records", extra={"count": purged})
            removed = await state.snapshots.cleanup()
            if removed:
                log.info("removed expired scn snapshots", extra={"count": removed})
        except Exception:
            log.exception("housekeeping failed")
        await asyncio.sleep(600)


class McpDispatch:
    """Routes ``/mcp-lite*`` to the lite server, everything else to the main MCP app."""

    def __init__(self, main: ASGIApp, lite: ASGIApp) -> None:
        self.main = main
        self.lite = lite

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and str(scope.get("path", "")).startswith("/mcp-lite"):
            await self.lite(scope, receive, send)
            return
        await self.main(scope, receive, send)


async def _warn_if_other_dataset(state: AppState) -> None:
    """A database seeded before (another profile, or M1 without ``seed_state``) is never
    reloaded implicitly; a demo reset (``POST /v1/admin/scenario:reset``) switches it."""
    try:
        async with state.engine.connect() as conn:
            loaded = await loaded_fingerprint(conn)
        expected = (await asyncio.to_thread(golden, state.settings.seed_profile)).fingerprint
    except Exception:
        log.exception("could not compare the loaded dataset with the golden seed")
        return
    if loaded != expected:
        log.warning(
            "database holds a different seed dataset than SEED_PROFILE; a scenario reset loads it",
            extra={"seed_profile": state.settings.seed_profile, "loaded_sha256": loaded, "golden_sha256": expected},
        )


def check_session_secret(settings: Settings) -> None:
    """The dev default SESSION_SECRET signs the OIDC state cookie and keys UI sessions; it is
    tolerated (with a warning) only on a ``*.localhost`` deployment."""
    if settings.session_secret != DEV_SESSION_SECRET:
        return
    if not settings.is_localhost_deployment:
        raise RuntimeError("SESSION_SECRET must be set when PUBLIC_URL is not a *.localhost deployment")
    log.warning("SESSION_SECRET is not set; using an insecure development default")


def create_app(settings: Settings | None = None, state: AppState | None = None) -> FastAPI:
    settings = settings or (state.settings if state else Settings.from_env())
    state = state or AppState.build(settings)
    mcp = build_mcp(state)
    _base, mcp_path = settings.mcp_resource_base_and_path
    mcp_app = mcp.http_app(
        path=mcp_path,
        stateless_http=True,
        json_response=settings.mcp_json_response,
        host_origin_protection="auto",
        allowed_hosts=settings.mcp_allowed_hosts,
        allowed_origins=settings.mcp_allowed_origins or None,
    )
    lite = build_lite(state)
    lite_app = lite.http_app(
        path="/mcp-lite",
        stateless_http=True,
        json_response=settings.mcp_json_response,
        host_origin_protection="auto",
        allowed_hosts=settings.mcp_allowed_hosts,
        allowed_origins=settings.mcp_allowed_origins or None,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        check_session_secret(settings)
        await _wait_for_db(state)
        applied = await migrate(state.engine)
        log.info("migrations ready", extra={"applied": applied})
        if settings.seed_on_startup:
            async with state.engine.connect() as conn:
                seeded = await is_seeded(conn)
            if not seeded:
                try:
                    await seed(state.engine, settings.seed_profile)
                except UnsupportedProfile as exc:
                    log.error("seed skipped", extra={"error": str(exc)})
            else:
                await _warn_if_other_dataset(state)
        try:
            await state.webhooks.ensure_seeded()
        except Exception:
            log.exception("could not seed the webhook subscription")
        warm_golden = asyncio.create_task(_warm_golden(settings)) if settings.demo_mode else None
        background = [asyncio.create_task(_housekeeping(state))]
        if settings.webhook_worker:
            background.append(asyncio.create_task(state.webhooks.run_forever()))
        # The mounted MCP apps' lifespans start their streamable-HTTP session managers.
        async with mcp_app.router.lifespan_context(mcp_app), lite_app.router.lifespan_context(lite_app):
            app.state.ready = True
            try:
                yield
            finally:
                app.state.ready = False
                for task in background:
                    task.cancel()
                for task in background:
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                await state.clients_seen.drain()
                if warm_golden is not None:
                    with contextlib.suppress(Exception):
                        await warm_golden
        await state.engine.dispose()

    app = FastAPI(
        title=f"{settings.brand_vendor_name} Retail Suite API",
        version="1.0.0",
        lifespan=lifespan,
        docs_url="/v1/docs",
        openapi_url="/v1/openapi.json",
        redoc_url=None,
    )
    app.state.merxa = state
    app.state.ready = False
    app.state.mcp = mcp
    install_error_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        checks: dict[str, Any] = {"lifespan": bool(app.state.ready)}
        try:
            async with state.engine.connect() as conn:
                checks["db"] = True
                checks["seeded"] = (await conn.execute(select(t.dc.c.code).limit(1))).first() is not None
        except Exception:
            checks["db"] = False
        ok = checks.get("db") and checks["lifespan"]
        return JSONResponse(
            {"status": "ready" if ok else "not_ready", "checks": checks}, status_code=200 if ok else 503
        )

    app.include_router(routes_auth.router)
    app.include_router(routes_read.router)
    app.include_router(routes_write.router)
    app.include_router(routes_m2.router)
    app.include_router(routes_m5.router)

    # RFC 9728 root variant: same document as /.well-known/oauth-protected-resource<mcp_path>.
    prm_path = f"/.well-known/oauth-protected-resource{mcp_path}"
    for route in mcp_app.routes:
        if isinstance(route, Route) and route.path == prm_path:
            app.router.routes.append(
                Route("/.well-known/oauth-protected-resource", endpoint=route.endpoint, methods=["GET", "OPTIONS"])
            )
            break

    # Must stay last: serves `/mcp`, `/mcp-lite` and `/.well-known/oauth-protected-resource/mcp`.
    app.mount("/", McpDispatch(mcp_app, ApiKeyHeaderMiddleware(lite_app)))
    app.add_middleware(TraceMiddleware)
    return app
