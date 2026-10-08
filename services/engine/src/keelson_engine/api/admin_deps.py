"""Back-office request context (``/api/admin/*``).

* Role: ``tenant-admin`` or ``platform-admin`` (anything else → 403 ``forbidden``).
* Tenant: the session's ``active_tenant``. A **platform-admin** may act on another tenant with
  ``?tenant=<slug>`` — explicit, and every such request is written to that tenant's
  ``admin_audit`` (``admin.cross_tenant``). For anyone else a foreign ``?tenant=`` is 404.
* Mutations additionally need the CSRF token (``MutatingSession``) + Origin (middleware).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, Request
from sqlalchemy import text

from keelson_engine.api.deps import CurrentSession, MutatingSession
from keelson_engine.api.errors import ApiError
from keelson_engine.api.sessions import SessionData
from keelson_engine.core.db import check_tenant_slug, system_session
from keelson_engine.core.repo import admin_audit

__all__ = ["ADMIN_ROLES", "AdminCtx", "AdminRead", "AdminWrite", "require_platform"]

ADMIN_ROLES = ("tenant-admin", "platform-admin")


@dataclass(slots=True)
class AdminCtx:
    sess: SessionData
    tenant: str
    platform: bool
    cross_tenant: bool = False

    def actor(self) -> dict[str, Any]:
        return self.sess.actor()


async def _tenant_exists(slug: str) -> bool:
    try:
        check_tenant_slug(slug)
    except ValueError:
        return False
    async with system_session() as s:
        return (await s.execute(text("SELECT 1 FROM tenant WHERE slug = :s"), {"s": slug})).first() is not None


async def _resolve(request: Request, sess: SessionData) -> AdminCtx:
    if not sess.has_any(*ADMIN_ROLES):
        raise ApiError(403, "forbidden", "the back-office requires a tenant admin or platform admin role")
    platform = sess.has_any("platform-admin")
    requested = request.query_params.get("tenant")
    if requested and requested != sess.active_tenant:
        if not platform or not await _tenant_exists(requested):
            raise ApiError(404, "not_found", "tenant not found")
        await admin_audit(sess.actor(), "admin.cross_tenant", f"{request.method} {request.url.path}",
                          None, {"home_tenant": sess.active_tenant}, tenant=requested)
        return AdminCtx(sess, requested, platform, cross_tenant=True)
    if not sess.active_tenant:
        raise ApiError(403, "no_tenant", "your account is not a member of any tenant")
    return AdminCtx(sess, sess.active_tenant, platform)


async def admin_read(request: Request, sess: CurrentSession) -> AdminCtx:
    return await _resolve(request, sess)


async def admin_write(request: Request, sess: MutatingSession) -> AdminCtx:
    return await _resolve(request, sess)


AdminRead = Annotated[AdminCtx, Depends(admin_read)]
AdminWrite = Annotated[AdminCtx, Depends(admin_write)]


def require_platform(ctx: AdminCtx) -> None:
    if not ctx.platform:
        raise ApiError(403, "platform_admin_required", "this action requires a platform admin")
