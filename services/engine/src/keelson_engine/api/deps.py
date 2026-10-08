"""Request dependencies: session principal, CSRF, tenant + role checks, object loading.

Every tenant route resolves the case/interrupt/run/block through ``tenant_session(active_tenant)``
(RLS) **and** re-checks ``row.tenant == active_tenant``; objects of another tenant are 404.
"""

from __future__ import annotations

import hmac
from typing import Annotated, Any

from fastapi import Depends, Request

from keelson_engine.api.errors import ApiError
from keelson_engine.api.sessions import SESSION_COOKIE, SessionData, load_session
from keelson_engine.core import repo

__all__ = [
    "READ_ROLES",
    "WRITE_ROLES",
    "MutatingSession",
    "TenantSession",
    "CurrentSession",
    "check_csrf",
    "current_session",
    "load_case_or_404",
    "mutating_session",
    "optional_session",
    "require_any_role",
    "tenant_session_dep",
]

READ_ROLES = ("planner", "approver", "compliance", "tenant-admin")
WRITE_ROLES = ("planner", "approver", "tenant-admin")


async def optional_session(request: Request) -> SessionData | None:
    cached = getattr(request.state, "keelson_session", None)
    if cached is not None:
        return cached  # type: ignore[no-any-return]
    sess = await load_session(request.cookies.get(SESSION_COOKIE))
    request.state.keelson_session = sess
    return sess


_SUSPENSION_EXEMPT = ("/api/me", "/api/auth/")


async def current_session(request: Request) -> SessionData:
    sess = await optional_session(request)
    if sess is None:
        raise ApiError(401, "unauthenticated", "sign in required")
    # M7 (110 §2): a suspended tenant blocks every tenant-scoped call (platform-admins keep access
    # to reactivate it; /api/me and sign-out keep working so the web can say why)
    if (sess.active_tenant and not sess.has_any("platform-admin")
            and not request.url.path.startswith(_SUSPENSION_EXEMPT)):
        from keelson_engine.core.tenants import is_suspended

        if await is_suspended(sess.active_tenant):
            raise ApiError(403, "tenant_suspended", "this tenant is suspended; contact your platform admin")
    return sess


def check_csrf(request: Request, sess: SessionData) -> None:
    token = request.headers.get("x-csrf-token", "")
    if not token or not hmac.compare_digest(token.encode("utf-8"), sess.csrf_token.encode("utf-8")):
        raise ApiError(403, "csrf_failed", "missing or invalid X-CSRF-Token")


async def mutating_session(request: Request, sess: Annotated[SessionData, Depends(current_session)]) -> SessionData:
    check_csrf(request, sess)
    return sess


CurrentSession = Annotated[SessionData, Depends(current_session)]
MutatingSession = Annotated[SessionData, Depends(mutating_session)]


def tenant_session_dep(sess: SessionData) -> str:
    if not sess.active_tenant:
        raise ApiError(403, "no_tenant", "your account is not a member of any tenant")
    return sess.active_tenant


TenantSession = CurrentSession


def require_any_role(sess: SessionData, roles: tuple[str, ...] | list[str], what: str = "this") -> str:
    """Tenant + role check; returns the active tenant."""
    tenant = tenant_session_dep(sess)
    if not sess.has_any(*roles):
        raise ApiError(403, "forbidden", f"you do not have a role that allows {what}")
    return tenant


async def load_case_or_404(tenant: str, case_id: str) -> dict[str, Any]:
    if not case_id or len(case_id) > 64:
        raise ApiError(404, "not_found", "case not found")
    case = await repo.get_case(case_id, tenant=tenant)
    if case is None or case.get("tenant") != tenant:
        raise ApiError(404, "not_found", "case not found")
    return case
