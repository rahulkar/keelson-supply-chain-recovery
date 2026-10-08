"""FastAPI application: ``/healthz``, ``/readyz``, ``/v1/levers``, ``/v1/impact``,
``/v1/optimize`` (contract: ``docs/contracts/40-optimizer.md``)."""

from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import cache

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from keelson_contracts.optimizer import ImpactResponse, LeverInfo

from keelson_opt import __version__
from keelson_opt.auth import Principal, authenticate, check_tenant
from keelson_opt.config import Settings, load_settings
from keelson_opt.levers import catalogue
from keelson_opt.limits import BodySizeLimit
from keelson_opt.logs import configure_logging, trace_id_var
from keelson_opt.service import PlanSpecError, run_impact, run_optimize
from keelson_opt.wire_m5 import ImpactRequestM5, OptimizeRequestM5, OptimizeResponseM5

log = logging.getLogger("keelson_opt.http")

_TRACEPARENT = re.compile(r"^[0-9a-f]{2}-([0-9a-f]{32})-[0-9a-f]{16}-[0-9a-f]{2}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MAX_VALIDATION_ERRORS = 50


def _trace_id(request: Request) -> str:
    """W3C ``traceparent`` trace id, else a sane ``x-trace-id``/``x-request-id``, else new."""
    tp = _TRACEPARENT.match(request.headers.get("traceparent", "").strip().lower())
    if tp:
        return tp.group(1)
    for header in ("x-trace-id", "x-request-id"):
        value = request.headers.get(header, "").strip()
        if _SAFE_ID.match(value):
            return value
    return uuid.uuid4().hex


@cache
def _glop_available() -> bool:
    from ortools.linear_solver.python import model_builder as mb

    return bool(mb.Solver("glop").solver_is_supported())


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)
    app = FastAPI(title="Keelson optimizer", version=__version__, docs_url=None, redoc_url=None)
    app.state.settings = settings
    compute_slots = asyncio.Semaphore(settings.max_concurrency)
    if not settings.auth_configured:
        log.error("OPTIMIZER_SHARED_SECRET is not set: every /v1 request will be rejected")
    elif len(settings.shared_secret or "") < 32:
        log.warning("OPTIMIZER_SHARED_SECRET is shorter than 32 bytes (HS256 recommends >= 32)")

    # Added first, so it sits *inside* the HTTP middleware below (Starlette wraps later
    # additions around earlier ones): requests are authenticated before their size is
    # checked, and the 413 raised while the route streams the body reaches the exception
    # middleware directly (not through BaseHTTPMiddleware's task group).
    app.add_middleware(BodySizeLimit, max_bytes=settings.max_body_bytes)

    @app.middleware("http")
    async def _context(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        token = trace_id_var.set(_trace_id(request))
        start = time.perf_counter()
        try:
            if request.url.path.startswith("/v1/"):
                # Authenticate before anything reads the body (FastAPI parses the JSON body
                # before it runs route dependencies).
                try:
                    authenticate(request)
                except HTTPException as exc:
                    response: Response = JSONResponse(
                        status_code=exc.status_code, content={"detail": exc.detail}, headers=exc.headers
                    )
                else:
                    if request.method == "POST":
                        # Bound concurrent compute (and the parsed bodies it holds) to fit
                        # the container memory; waiting requests have not read their body.
                        async with compute_slots:
                            response = await call_next(request)
                    else:
                        response = await call_next(request)
            else:
                response = await call_next(request)
            response.headers["x-trace-id"] = trace_id_var.get()
            if request.url.path not in ("/healthz", "/readyz"):
                log.info(
                    "request",
                    extra={
                        "method": request.method,
                        "path": request.url.path,
                        "status": response.status_code,
                        "ms": int((time.perf_counter() - start) * 1000),
                        "tenant": getattr(request.state, "tenant", None),
                    },
                )
            return response
        finally:
            trace_id_var.reset(token)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        # FastAPI's default handler echoes each error's ``input``; for a model-level error
        # on ``data`` that is the whole (multi-MB) ProblemData. Return a bounded body.
        errors = exc.errors()
        detail = [
            {"type": e.get("type"), "loc": list(e.get("loc", ())), "msg": e.get("msg")}
            for e in errors[:_MAX_VALIDATION_ERRORS]
        ]
        content: dict[str, object] = {"detail": detail}
        if len(errors) > _MAX_VALIDATION_ERRORS:
            content["truncated"] = len(errors) - _MAX_VALIDATION_ERRORS
        return JSONResponse(status_code=422, content=content)

    @app.exception_handler(PlanSpecError)
    async def _plan_spec_error(_: Request, exc: PlanSpecError) -> JSONResponse:
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.get("/healthz")
    def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/readyz")
    def readyz() -> JSONResponse:
        checks = {"shared_secret": settings.auth_configured, "glop": _glop_available()}
        ok = all(checks.values())
        return JSONResponse(status_code=200 if ok else 503, content={"ok": ok, "checks": checks})

    @app.get("/v1/levers")
    def levers(_: Principal) -> list[LeverInfo]:
        return catalogue()

    # Sync handlers: the CPU-bound work runs in Starlette's threadpool, not on the loop.
    @app.post("/v1/impact")
    def impact(body: ImpactRequestM5, principal: Principal) -> ImpactResponse:
        check_tenant(principal, body.data.tenant)
        try:
            return run_impact(body)
        except ValueError as exc:  # inconsistent data detected while modelling
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.post("/v1/optimize")
    def optimize(body: OptimizeRequestM5, principal: Principal) -> OptimizeResponseM5:
        check_tenant(principal, body.data.tenant)
        try:
            return run_optimize(body)
        except PlanSpecError:
            raise
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    return app


def run() -> None:  # pragma: no cover - process entry point
    import uvicorn

    settings = load_settings()
    uvicorn.run(create_app(settings), host="0.0.0.0", port=settings.port, log_config=None, proxy_headers=False)


if __name__ == "__main__":  # pragma: no cover
    run()
