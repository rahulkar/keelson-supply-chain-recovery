"""FastAPI application: ``/mcp`` (FastMCP, stateless streamable HTTP), signed Parquet downloads,
health, and the admin / demo routes (contract 122 §5–7).

Routes:

* ``GET /healthz`` — process up; ``GET /readyz`` — 200 only once the config, migrations, backend
  probe, entity-SQL column checks and ``SNAPSHOT_DIR`` all passed (and the backend still answers);
* ``GET /v1/snapshots/{id}/{entity}/{file}?exp=&sig=`` — signed part download (no auth header);
* ``/v1/admin/*`` (``X-Admin-Key`` = ``SIDECAR_ADMIN_KEY``, never given to Keelson):
  ``GET stats`` (table / entity counts, fingerprints, watermark), ``GET rows/{table}?col=value``
  (read-back), ``POST reset`` (``DEMO_MODE`` only: restore the golden CSV files);
* ``/mcp`` and ``/.well-known/oauth-protected-resource/mcp`` (OAuth mode) — the MCP app.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.routing import Route
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from adapter_sidecar.auth import ADMIN_HEADER, HeaderKeyMiddleware, admin_ok, build_auth
from adapter_sidecar.errors import Problem
from adapter_sidecar.logs import get_logger, trace_id_from_headers, trace_id_var
from adapter_sidecar.mcp_server import build_mcp
from adapter_sidecar.service import Sidecar
from adapter_sidecar.settings import Settings
from adapter_sidecar.snapshots import MEDIA_TYPE

log = get_logger(__name__)

PROBLEM_JSON = "application/problem+json"
HOUSEKEEPING_S = 600


class TraceMiddleware:
    """Trace id per request + one access log line (path only: never query strings, which carry
    download signatures, nor headers)."""

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
                message["headers"] = [*message.get("headers", []), (b"x-trace-id", trace_id.encode())]
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            path = scope.get("path", "")
            if path not in ("/healthz", "/readyz"):
                log.info("http request", extra={"method": scope.get("method"), "path": path, "status": status,
                                                "duration_ms": round((time.perf_counter() - start) * 1000, 1)})
            trace_id_var.reset(token)


def _problem(problem: Problem, request: Request) -> JSONResponse:
    return JSONResponse(problem.to_dict(instance=request.url.path), status_code=problem.status,
                        headers=problem.headers or None, media_type=PROBLEM_JSON)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(Problem)
    async def _p(request: Request, exc: Problem) -> JSONResponse:
        return _problem(exc, request)

    @app.exception_handler(RequestValidationError)
    async def _v(request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [{"loc": list(e.get("loc", ())), "msg": e.get("msg")} for e in exc.errors()]
        return _problem(Problem(422, "validation_error", "request did not match the expected schema",
                                extra={"errors": errors}), request)

    @app.exception_handler(StarletteHTTPException)
    async def _h(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _problem(Problem(exc.status_code, code, str(exc.detail)), request)

    @app.exception_handler(Exception)
    async def _u(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", extra={"path": request.url.path})
        return _problem(Problem(500, "internal_error", "unexpected server error"), request)


async def _housekeeping(sidecar: Sidecar) -> None:
    while True:
        await asyncio.sleep(HOUSEKEEPING_S)
        try:
            await sidecar.housekeeping()
        except Exception:  # next round
            log.exception("housekeeping failed")


def create_app(settings: Settings | None = None, sidecar: Sidecar | None = None, *,
               auth_http_client: Any = None) -> FastAPI:
    settings = settings or (sidecar.settings if sidecar else Settings.from_env())
    sidecar = sidecar or Sidecar(settings)
    cfg = sidecar.cfg
    auth = build_auth(cfg.auth, settings, http_client=auth_http_client)
    mcp = build_mcp(sidecar, auth)
    mcp_app = mcp.http_app(path=settings.mcp_path, stateless_http=True, json_response=True,
                           host_origin_protection="auto", allowed_hosts=settings.mcp_allowed_hosts)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await sidecar.start()
        background = asyncio.create_task(_housekeeping(sidecar))
        async with mcp_app.router.lifespan_context(mcp_app):
            try:
                yield
            finally:
                background.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await background
        await sidecar.close()

    app = FastAPI(title=f"{cfg.system.name} adapter sidecar", version="1.0.0", lifespan=lifespan, docs_url=None,
                  redoc_url=None, openapi_url=None)
    app.state.sidecar = sidecar
    install_error_handlers(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz() -> JSONResponse:
        checks: dict[str, Any] = {"started": sidecar.ready}
        if sidecar.ready:
            try:
                await sidecar.backend.probe()
                checks["backend"] = True
            except Exception:
                checks["backend"] = False
        ok = sidecar.ready and checks.get("backend") is True
        return JSONResponse({"status": "ready" if ok else "not_ready", "checks": checks},
                            status_code=200 if ok else 503)

    @app.get("/v1/snapshots/{snapshot_id}/{entity}/{file}", include_in_schema=False)
    async def download(snapshot_id: str, entity: str, file: str, exp: str | None = None,
                       sig: str | None = None) -> FileResponse:
        path = sidecar.snapshots.resolve_download(snapshot_id, entity, file, exp, sig)
        return FileResponse(path, media_type=MEDIA_TYPE, filename=f"{entity}-{file}")

    def _admin(request: Request) -> None:
        if not admin_ok(settings, request.headers.get(ADMIN_HEADER)):
            raise Problem(401, "unauthorized", "a valid X-Admin-Key is required")

    @app.get("/v1/admin/stats", include_in_schema=False)
    async def admin_stats(request: Request) -> dict[str, Any]:
        _admin(request)
        return await sidecar.stats()

    @app.get("/v1/admin/rows/{table}", include_in_schema=False)
    async def admin_rows(request: Request, table: str) -> dict[str, Any]:
        _admin(request)
        return await sidecar.rows(table, dict(request.query_params))

    @app.post("/v1/admin/reset", include_in_schema=False)
    async def admin_reset(request: Request) -> dict[str, Any]:
        _admin(request)
        if not settings.demo_mode:
            raise Problem(403, "demo_mode_off", "reset is only available with DEMO_MODE=1")
        return await sidecar.reset_demo()

    # RFC 9728 root variant (OAuth mode): same document as /.well-known/oauth-protected-resource<mcp path>
    prm_path = f"/.well-known/oauth-protected-resource{settings.mcp_path}"
    for route in mcp_app.routes:
        if isinstance(route, Route) and route.path == prm_path:
            app.router.routes.append(Route("/.well-known/oauth-protected-resource", endpoint=route.endpoint,
                                           methods=["GET", "OPTIONS"]))
            break

    header = cfg.auth.static_header.header if cfg.auth.static_header is not None else "X-Adapter-Key"
    app.mount("/", HeaderKeyMiddleware(mcp_app, header))  # must stay last
    app.add_middleware(TraceMiddleware)
    return app


def run() -> None:
    import uvicorn

    from adapter_sidecar.logs import configure_logging

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, proxy_headers=True,
                forwarded_allow_ips="*", workers=1, access_log=False, log_config=None)
