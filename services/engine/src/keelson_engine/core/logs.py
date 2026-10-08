"""Structured JSON logging (structlog) with a ``trace_id`` per request / run.

``configure_logging()`` routes both structlog and stdlib loggers (uvicorn, sqlalchemy,
httpx) through one JSON renderer. ``bind_trace(trace_id)`` stores the id in
structlog's contextvars so every log line of the request/run carries it.
"""

from __future__ import annotations

import logging
import sys
import uuid
from typing import Any

import structlog

__all__ = ["bind_trace", "configure_logging", "get_logger", "new_trace_id"]

_configured = False


def new_trace_id() -> str:
    return uuid.uuid4().hex


def bind_trace(trace_id: str | None = None, **values: Any) -> str:
    tid = trace_id or new_trace_id()
    structlog.contextvars.bind_contextvars(trace_id=tid, **values)
    return tid


def configure_logging(level: str = "INFO", *, json_logs: bool = True) -> None:
    global _configured
    if _configured:
        return
    shared: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.format_exc_info,
    ]
    renderer: Any = structlog.processors.JSONRenderer() if json_logs else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[*shared, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            foreign_pre_chain=shared,
            processors=[structlog.stdlib.ProcessorFormatter.remove_processors_meta, renderer],
        )
    )
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).handlers[:] = []
        logging.getLogger(name).propagate = True
    for noisy in ("httpx", "httpcore", "httpcore2", "httpx2"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
