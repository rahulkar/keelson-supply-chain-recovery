"""API error type: ``{"code": "...", "message": "...", ...extra}`` with an HTTP status.

Unhandled exceptions become ``500 {"code": "internal_error", "message": <generic>, "error_id"}``;
the exception (with traceback) is logged under the same ``error_id`` and never sent to the client.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from keelson_engine.core.logs import get_logger
from keelson_engine.public_errors import report_exception, user_message

__all__ = ["ApiError", "install_error_handlers", "internal_error"]

log = get_logger(__name__)


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str = "", **extra: Any) -> None:
        super().__init__(message or code)
        self.status = status
        self.code = code
        self.message = message or code.replace("_", " ")
        self.extra = extra

    def body(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


def install_error_handlers(app: FastAPI) -> None:
    from fastapi.exceptions import RequestValidationError

    from keelson_engine.core.connector_admin import AdminError

    @app.exception_handler(ApiError)
    async def _api_error(_request: Request, exc: ApiError) -> JSONResponse:
        headers = {"WWW-Authenticate": 'Cookie realm="keelson"'} if exc.status == 401 else None
        return JSONResponse(exc.body(), status_code=exc.status, headers=headers)

    @app.exception_handler(AdminError)
    async def _admin_error(_request: Request, exc: AdminError) -> JSONResponse:
        return JSONResponse({"code": exc.code, "message": exc.message, **exc.extra}, status_code=exc.status)

    from keelson_engine.core.change_requests import GuardedChange, create_request

    @app.exception_handler(GuardedChange)
    async def _guarded_change(request: Request, exc: GuardedChange) -> JSONResponse:
        """M7 four-eyes (110 §1): a guarded change becomes a pending change request → 202 with the
        ``AdminChangeRequest`` (nothing applied)."""
        sess = getattr(request.state, "keelson_session", None)
        roles = list(getattr(sess, "roles", None) or exc.actor.get("roles") or [])
        tenants = list(getattr(sess, "tenants", None) or [exc.tenant])
        try:
            body = await create_request(exc, actor_roles=roles, actor_tenants=tenants)
        except AdminError as err:
            return JSONResponse({"code": err.code, "message": err.message, **err.extra}, status_code=err.status)
        return JSONResponse(body, status_code=202, headers={"X-Keelson-Change-Request": body["id"]})

    @app.exception_handler(RequestValidationError)
    async def _invalid_request(_request: Request, exc: RequestValidationError) -> JSONResponse:
        # keeps FastAPI's ``detail`` (input values stripped) next to the common {code, message}
        detail = [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", ""))[:200], "type": e.get("type")}
                  for e in exc.errors()[:20]]
        return JSONResponse({"code": "invalid_request", "message": "the request body or parameters are invalid",
                             "detail": detail}, status_code=422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        error_id = report_exception(log, "unhandled API error", exc, method=request.method, path=request.url.path)
        return JSONResponse(
            {"code": "internal_error", "message": user_message(error_id, "Something went wrong."), "error_id": error_id},
            status_code=500,
        )


def internal_error(status: int, code: str, event: str, exc: BaseException | None = None,
                   message: str = "Something went wrong.", **fields: Any) -> ApiError:
    """An :class:`ApiError` with a generic message + ``error_id``; logs ``exc`` under that id."""
    error_id = report_exception(log, event, exc, **fields)
    return ApiError(status, code, user_message(error_id, message), error_id=error_id)
