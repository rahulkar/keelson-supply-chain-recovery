"""Domain errors rendered as RFC 9457 ``application/problem+json`` (REST) or tool errors (MCP)."""

from __future__ import annotations

from typing import Any

PROBLEM_BASE = "urn:merxa:problem:"


class Problem(Exception):
    """A client-visible error with a stable machine ``code``.

    ``extra`` members are added to the problem document as RFC 9457 extension members.
    """

    def __init__(
        self,
        status: int,
        code: str,
        detail: str | None = None,
        *,
        title: str | None = None,
        extra: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(detail or code)
        self.status = status
        self.code = code
        self.detail = detail
        self.title = title or code.replace("_", " ").capitalize()
        self.extra = extra or {}
        self.headers = headers or {}

    def to_dict(self, instance: str | None = None) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "type": PROBLEM_BASE + self.code,
            "title": self.title,
            "status": self.status,
            "code": self.code,
        }
        if self.detail:
            doc["detail"] = self.detail
        if instance:
            doc["instance"] = instance
        doc.update(self.extra)
        return doc


def not_found(entity: str, ref: str) -> Problem:
    return Problem(404, "not_found", f"{entity} {ref!r} not found")


def bad_request(code: str, detail: str, **extra: Any) -> Problem:
    return Problem(400, code, detail, extra=extra or None)


def forbidden(code: str, detail: str) -> Problem:
    return Problem(403, code, detail)


def unauthorized(detail: str = "authentication required") -> Problem:
    return Problem(401, "unauthorized", detail, headers={"WWW-Authenticate": 'Bearer realm="merxa"'})
