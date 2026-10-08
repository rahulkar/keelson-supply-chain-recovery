"""engine-api application (``uvicorn keelson_engine.api.main:app``)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from keelson_engine import __version__
from keelson_engine.api import (
    routes_admin,
    routes_admin_kb,
    routes_admin_m7,
    routes_admin_playground,
    routes_alerts,
    routes_auth,
    routes_cases,
    routes_connectors,
    routes_demo,
    routes_interrupts,
    routes_system,
    routes_webhooks,
    shutdown,
)
from keelson_engine.api import routes_admin_evals, routes_internal_evals  # M8 Evals UI (128)
from keelson_engine.api.errors import install_error_handlers
from keelson_engine.api.middleware import SecurityMiddleware
from keelson_engine.config import get_settings
from keelson_engine.core.db import close_db, init_db
from keelson_engine.core.http import close_http
from keelson_engine.core.logs import configure_logging, get_logger
from keelson_engine.core.valkey import close_valkey

__all__ = ["app", "create_app"]

log = get_logger(__name__)


@asynccontextmanager
async def _streams_lifespan() -> AsyncIterator[None]:
    """End open SSE streams as soon as the process is asked to stop (see ``api.shutdown``)."""
    shutdown.reset_shutdown()
    uninstall = shutdown.install_signal_hook()
    try:
        yield
    finally:
        shutdown.notify_shutdown()
        uninstall()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    configure_logging(settings.log_level, json_logs=settings.log_json)
    await init_db(settings)
    from keelson_engine.core.policies import install_runtime_evaluator

    install_runtime_evaluator()  # CEL evaluator for /api/admin/policies (runtime/policy.py)
    log.info("engine-api started", version=__version__, demo_mode=settings.demo_mode,
             signal_hub=settings.signal_hub_enabled)
    watcher_task: asyncio.Task[None] | None = None
    if settings.signal_hub_enabled and settings.alerts_watch:
        from keelson_engine.core.alerts import AlertWatcher

        watcher = AlertWatcher()  # M6: alert polls, SSE fan-out, auto-open, forward retries
        watcher_task = asyncio.create_task(watcher.run(), name="alert-watcher")
    try:
        async with _streams_lifespan():
            yield
    finally:
        if watcher_task is not None:
            watcher_task.cancel()
            try:
                await watcher_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001 - shutting down
                pass
        from keelson_engine.core.kb import cancel_jobs

        await cancel_jobs()
        await close_http()
        await close_valkey()
        await close_db()


@asynccontextmanager
async def streams_only_lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """``create_app(manage_resources=False)``: no DB/Valkey/HTTP lifecycle, only the stream shutdown."""
    async with _streams_lifespan():
        yield


def create_app(*, manage_resources: bool = True) -> FastAPI:
    """Build the app. ``manage_resources=False`` (tests) skips opening/closing the process-wide
    DB/Valkey/HTTP clients (the lifespan then only wires the stream shutdown)."""
    application = FastAPI(
        title="Keelson engine API",
        version=__version__,
        lifespan=lifespan if manage_resources else streams_only_lifespan,
        docs_url="/api/docs",
        openapi_url="/api/openapi.json",
        redoc_url=None,
    )
    install_error_handlers(application)
    application.add_middleware(SecurityMiddleware)
    for router in (
        routes_system.router,
        routes_auth.router,
        routes_demo.router,
        routes_cases.router,
        routes_alerts.router,
        routes_interrupts.router,
        routes_connectors.router,
        routes_admin.router,
        routes_admin_kb.router,
        routes_admin_m7.router,
        routes_admin_playground.router,
        routes_webhooks.router,
        routes_admin_evals.router,
        routes_internal_evals.router,
    ):
        application.include_router(router)
    return application


app = create_app()
