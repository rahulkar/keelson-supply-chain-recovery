"""Client-visible errors with a stable machine ``code``.

REST routes render them as RFC 9457 ``application/problem+json``; MCP tools as a tool error
``"<code>: <detail> (reason: <reason>)"`` (the Merxa format the engine's ``parse_tool_error``
reads, see :func:`adapter_sidecar.mcp_server._call`).
"""

from __future__ import annotations

from typing import Any

PROBLEM_BASE = "urn:keelson-sidecar:problem:"

TRANSIENT_CODES = frozenset({"receipt_jwks_unavailable", "idempotency_in_flight"})
"""The only codes the engine retries with the same idempotency key (``runtime/errors.py``)."""


class Problem(Exception):
    """A client-visible error. ``extra`` members are added to the problem document (``reason`` is
    also appended to the MCP tool error text)."""

    def __init__(
        self,
        status: int,
        code: str,
        detail: str | None = None,
        *,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.extra = extra or {}
        self.headers = headers or {}

    @property
    def title(self) -> str:
        return self.code.replace("_", " ").capitalize()

    def to_dict(self, instance: str | None = None) -> dict[str, Any]:
        doc: dict[str, Any] = {"type": PROBLEM_BASE + self.code, "title": self.title, "status": self.status,
                               "code": self.code}
        if self.detail:
            doc["detail"] = self.detail
        if instance:
            doc["instance"] = instance
        doc.update(self.extra)
        return doc

    def tool_text(self) -> str:
        reason = self.extra.get("reason")
        suffix = f" (reason: {reason})" if reason else ""
        return f"{self.code}: {self.detail or self.title}{suffix}"


def not_found(entity: str, ref: str) -> Problem:
    return Problem(404, "not_found", f"{entity} {ref!r} not found")


class ConfigError(ValueError):
    """The sidecar configuration (``SIDECAR_CONFIG``) or the environment is invalid. Raised at
    start-up and by ``python -m adapter_sidecar check``; the process refuses to serve."""


class SourceDataError(ValueError):
    """A source file / endpoint holds data the configuration cannot type (row and column named)."""

    def __init__(self, source: str, detail: str, *, row: int | None = None, column: str | None = None) -> None:
        where = source + (f" row {row}" if row is not None else "") + (f" column {column!r}" if column else "")
        super().__init__(f"{where}: {detail}")
        self.source = source
        self.row = row
        self.column = column
        self.detail = detail
