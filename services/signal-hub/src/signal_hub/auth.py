"""Service-to-service auth (``100`` §1): every REST/MCP call carries an engine **service JWT**.

HS256 with ``SIGNAL_HUB_SHARED_SECRET``; required claims ``iss=keelson-engine``, ``aud=signal-hub``,
``exp`` and a tenant slug ``tenant`` (``iat``, when present, is validated by PyJWT).
Tokens must be short-lived: ``exp`` more than
``SIGNAL_HUB_MAX_TOKEN_TTL_S`` (900 s) in the future is rejected. The tenant claim scopes all tenant
data (it becomes ``app.tenant_id`` for RLS); a path ``{tenant}`` must equal it (403 otherwise).
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Annotated, Any

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastmcp.server.auth import AccessToken, TokenVerifier

from keelson_contracts.common import TENANT_SLUG_PATTERN
from signal_hub.config import SERVICE_AUDIENCE, SERVICE_ISSUER, Settings
from signal_hub.logs import get_logger

__all__ = ["Principal", "ServicePrincipal", "ServiceTokenVerifier", "TokenError", "check_tenant", "verify_service_token"]

log = get_logger(__name__)
_TENANT_RE = re.compile(TENANT_SLUG_PATTERN)
_CHALLENGE = {"WWW-Authenticate": 'Bearer realm="signal-hub"'}


class TokenError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ServicePrincipal:
    tenant: str
    subject: str | None
    claims: dict[str, Any]


def verify_service_token(token: str, settings: Settings, *, now: float | None = None) -> ServicePrincipal:
    if not settings.shared_secret:
        raise TokenError("SIGNAL_HUB_SHARED_SECRET not configured")
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
        raise TokenError(type(exc).__name__) from None
    current = time.time() if now is None else now
    exp = claims.get("exp")
    if not isinstance(exp, (int, float)) or exp - current > settings.max_token_ttl_s:
        raise TokenError("token lifetime too long")
    tenant = claims.get("tenant")
    if not isinstance(tenant, str) or not _TENANT_RE.match(tenant):
        raise TokenError("tenant claim must be a tenant slug")
    sub = claims.get("sub")
    return ServicePrincipal(tenant=tenant, subject=sub if isinstance(sub, str) else None, claims=claims)


def require_service(request: Request) -> ServicePrincipal:
    cached = getattr(request.state, "principal", None)
    if isinstance(cached, ServicePrincipal):
        return cached
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing service token", headers=_CHALLENGE)
    try:
        principal = verify_service_token(token.strip(), request.app.state.hub.settings)
    except TokenError as exc:
        log.warning("service token rejected", extra={"reason": str(exc)})
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing service token", headers=_CHALLENGE) from None
    request.state.principal = principal
    return principal


Principal = Annotated[ServicePrincipal, Depends(require_service)]


def check_tenant(principal: ServicePrincipal, tenant: str) -> None:
    if principal.tenant != tenant:
        log.warning("tenant mismatch", extra={"token_tenant": principal.tenant, "path_tenant": tenant})
        raise HTTPException(status.HTTP_403_FORBIDDEN, "token tenant does not match the path tenant")


class ServiceTokenVerifier(TokenVerifier):
    """FastMCP bearer verifier for the same service JWT (MCP at ``/mcp``)."""

    def __init__(self, settings: Settings) -> None:
        super().__init__()
        self.settings = settings

    async def verify_token(self, token: str) -> AccessToken | None:
        try:
            principal = verify_service_token(token, self.settings)
        except TokenError as exc:
            log.warning("mcp service token rejected", extra={"reason": str(exc)})
            return None
        return AccessToken(
            token=token,
            client_id=SERVICE_ISSUER,
            scopes=[],
            expires_at=int(principal.claims["exp"]),
            subject=principal.subject,
            claims={**principal.claims, "tenant": principal.tenant},
        )
