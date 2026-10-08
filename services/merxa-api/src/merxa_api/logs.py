"""Structured JSON logging with a per-request ``trace_id`` (stdlib only)."""

from __future__ import annotations

import json
import logging
import sys
import uuid
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, TextIO

trace_id_var: ContextVar[str | None] = ContextVar("merxa_trace_id", default=None)

_RESERVED = set(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime", "color_message"}


def current_trace_id() -> str | None:
    return trace_id_var.get()


def new_trace_id() -> str:
    return uuid.uuid4().hex


def trace_id_from_headers(traceparent: str | None, request_id: str | None) -> str:
    """Use the W3C ``traceparent`` trace-id when valid, else X-Request-ID, else a new id."""
    if traceparent:
        parts = traceparent.strip().split("-")
        if len(parts) >= 4 and len(parts[1]) == 32 and all(c in "0123456789abcdef" for c in parts[1]):
            return parts[1]
    if request_id and 0 < len(request_id) <= 128 and request_id.isprintable():
        return request_id
    return new_trace_id()


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname.lower(),
            "logger": record.name,
            "msg": record.getMessage(),
            "service": "merxa-api",
        }
        trace_id = trace_id_var.get()
        if trace_id:
            payload["trace_id"] = trace_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str = "INFO", stream: TextIO | None = None) -> None:
    root = logging.getLogger()
    handler = logging.StreamHandler(stream or sys.stdout)
    handler.setFormatter(JsonFormatter())
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    for noisy in ("uvicorn.access",):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    for name in ("uvicorn", "uvicorn.error"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
