"""Service-to-service auth: ``Authorization: Bearer <JWT>`` minted by the engine.

M1: HS256 with ``OPTIMIZER_SHARED_SECRET``; required claims ``iss=keelson-engine``,
``aud=keelson-optimizer``, ``exp`` and a non-empty string ``tenant``. Anything else is
rejected with 401. A request whose ``data.tenant`` differs from the token's tenant is
rejected with 403.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, Request, status

from keelson_opt.config import SERVICE_AUDIENCE, SERVICE_ISSUER, Settings

log = logging.getLogger(__name__)

_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="keelson-optimizer"'}


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    tenant: str
    subject: str | None


def _unauthorized(reason: str) -> HTTPException:
    log.warning("service token rejected", extra={"reason": reason})
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail="invalid or missing service token", headers=_CHALLENGE)


def verify_token(token: str, settings: Settings) -> ServicePrincipal:
    if not settings.shared_secret:
        raise _unauthorized("OPTIMIZER_SHARED_SECRET not configured")
    try:
        claims = jwt.decode(
            token,
            settings.shared_secret,
            algorithms=["HS256"],
            audience=SERVICE_AUDIENCE,
            issuer=SERVICE_ISSUER,
            leeway=settings.jwt_leeway_s,
            options={"require": ["exp", "iss", "aud", "tenant"]},
        )
    except jwt.PyJWTError as exc:
        raise _unauthorized(type(exc).__name__) from None
    tenant = claims.get("tenant")
    if not isinstance(tenant, str) or not tenant:
        raise _unauthorized("tenant claim must be a non-empty string")
    sub = claims.get("sub")
    return ServicePrincipal(tenant=tenant, subject=sub if isinstance(sub, str) else None)


def authenticate(request: Request) -> ServicePrincipal:
    """Verify the bearer token of ``request`` and remember the principal on its state.

    Called by the HTTP middleware for every ``/v1`` path *before* the body is read (so an
    unauthenticated caller can never make the service parse a large JSON body), and again
    by the route dependency, which then reuses the cached principal."""
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, ServicePrincipal):
        return cached
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise _unauthorized("missing bearer token")
    principal = verify_token(token.strip(), request.app.state.settings)
    request.state.principal = principal
    request.state.tenant = principal.tenant
    return principal


def require_service(request: Request) -> ServicePrincipal:
    return authenticate(request)


Principal = Annotated[ServicePrincipal, Depends(require_service)]


def check_tenant(principal: ServicePrincipal, tenant: str) -> None:
    if principal.tenant != tenant:
        log.warning("tenant mismatch", extra={"token_tenant": principal.tenant, "body_tenant": tenant})
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail="token tenant does not match data.tenant")
