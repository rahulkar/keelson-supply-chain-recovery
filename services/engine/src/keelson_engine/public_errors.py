"""User-visible failures: a generic message + an ``error_id``, never exception text.

Anything a browser can see (``RUN_ERROR`` / ``SUBAGENT_ERROR`` events on the case stream, API
error bodies) carries a fixed, user-facing message and a random ``error_id`` (UUID4). The full
exception (type, message, traceback) is logged server-side with the same ``error_id``, so
support can find it with ``error_id=<id>`` in the engine logs. Exception strings can contain
SQL, URLs, internal hostnames, tokens in query strings or other tenants' identifiers, so they
are never copied into an event or a response.

This module has no dependencies (the runtime may import it at module import time).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

__all__ = [
    "GENERIC_RUN_FAILURE",
    "GENERIC_SUBAGENT_FAILURE",
    "INTERNAL_ERROR",
    "new_error_id",
    "report_exception",
    "safe_code",
    "user_message",
]

INTERNAL_ERROR = "INTERNAL_ERROR"
GENERIC_RUN_FAILURE = "Something went wrong while working on this case."
GENERIC_SUBAGENT_FAILURE = "This step could not be completed."

_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def new_error_id() -> str:
    return str(uuid.uuid4())


def user_message(error_id: str, message: str = GENERIC_RUN_FAILURE) -> str:
    """The user-facing text: ``message`` + how to report it (the id, never the cause)."""
    return f"{message} Please try again; if it keeps happening, contact support with error ID {error_id}."


def safe_code(code: Any, default: str = INTERNAL_ERROR) -> str:
    """``code`` if it is an enum-like constant (``CANCELLED``, ``MAX_TOKENS``), else ``default``.

    Exception class names (``ProgrammingError``) and free text never pass."""
    value = str(code or "")
    return value if _CODE_RE.match(value) else default


def report_exception(logger: Any, event: str, exc: BaseException | None = None, *,
                     error_id: str | None = None, **fields: Any) -> str:
    """Log ``exc`` (with traceback) under a new or given ``error_id``; returns the id."""
    eid = error_id or new_error_id()
    if exc is not None:
        logger.error(event, error_id=eid, error_type=type(exc).__name__, exc_info=exc, **fields)
    else:
        logger.error(event, error_id=eid, **fields)
    return eid
