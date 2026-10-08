"""FastAPI dependencies: app state, caller identity (Bearer or UI session), scopes, ETags."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Header, Request

from merxa_api.errors import Problem, unauthorized
from merxa_api.security import sessions
from merxa_api.security.principal import READ_SCOPES, ROLE_ADMIN, Principal
from merxa_api.services.apikeys import looks_like_key
from merxa_api.state import AppState

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def get_state(request: Request) -> AppState:
    return request.app.state.merxa


StateDep = Annotated[AppState, Depends(get_state)]


async def get_principal(request: Request) -> Principal:
    cached = getattr(request.state, "principal", None)
    if cached is not None:
        return cached
    state: AppState = request.app.state.merxa
    auth = request.headers.get("authorization")
    api_key = request.headers.get("x-merxa-api-key")
    principal: Principal | None = None
    if api_key is not None and not auth:
        auth = f"Bearer {api_key.strip()}"
    if auth:
        scheme, _, token = auth.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise unauthorized("Authorization header must be 'Bearer <token>'")
        token = token.strip()
        if looks_like_key(token):
            info = await state.apikeys.verify(token)
            if info is None:
                raise Problem(
                    401,
                    "invalid_api_key",
                    "API key is invalid or revoked",
                    headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
                )
            request.state.principal = info.principal()
            return request.state.principal
        access = await state.token_verifier.verify_token(token)
        if access is None:
            raise Problem(
                401,
                "invalid_token",
                "access token is invalid or expired",
                headers={"WWW-Authenticate": 'Bearer error="invalid_token"'},
            )
        principal = Principal.from_claims(access.claims)
        state.clients_seen.touch_soon(principal, "rest")
    else:
        user = await sessions.load(
            state.engine, state.settings.session_secret, request.cookies.get(sessions.SESSION_COOKIE)
        )
        if user is None:
            raise unauthorized()
        if request.method not in SAFE_METHODS:
            check_same_origin(request, state.settings.public_origin)
        principal = Principal.from_session(
            sub=user.sub,
            username=user.username,
            name=user.name,
            email=user.email,
            roles=user.roles,
            client_id=state.settings.demo_client_id if user.demo else state.settings.web_client_id,
        )
    request.state.principal = principal
    return principal


def check_same_origin(request: Request, public_origin: str) -> None:
    """Cookie-authenticated writes must not come from another browser origin (CSRF)."""
    origin = request.headers.get("origin")
    if origin is None and (referer := request.headers.get("referer")):
        parts = urlsplit(referer)
        origin = f"{parts.scheme}://{parts.netloc}"
    if origin is not None and origin.rstrip("/") != public_origin:
        raise Problem(403, "csrf_rejected", "cross-origin request rejected")


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_scope(*any_of: str) -> Callable[..., Awaitable[Principal]]:
    """Dependency: caller must hold at least one of ``any_of``."""

    async def dep(principal: PrincipalDep) -> Principal:
        if not principal.has_any_scope(any_of):
            raise Problem(
                403,
                "insufficient_scope",
                f"requires scope {' or '.join(any_of)}",
                headers={"WWW-Authenticate": f'Bearer error="insufficient_scope", scope="{" ".join(any_of)}"'},
            )
        return principal

    return dep


CHANGESET_READ_SCOPES = ("changes:preview", *READ_SCOPES)


async def require_demo_admin(request: Request, principal: PrincipalDep) -> Principal:
    state: AppState = request.app.state.merxa
    if not state.settings.demo_mode:
        raise Problem(404, "not_found", "demo director is disabled (DEMO_MODE=0)")
    if not principal.has_role(ROLE_ADMIN):
        raise Problem(403, "forbidden", f"requires role {ROLE_ADMIN}")
    return principal


async def require_admin(principal: PrincipalDep) -> Principal:
    """Merxa administrators (role ``merxa-admin``): API keys, webhooks, connected clients."""
    if not principal.has_role(ROLE_ADMIN):
        raise Problem(403, "forbidden", f"requires role {ROLE_ADMIN}")
    return principal


def etag(version: int) -> str:
    return f'"{version}"'


def parse_if_match(value: str | None) -> int:
    """Parse ``If-Match: "<version>"`` (428 if missing, 400 if malformed)."""
    if value is None or not value.strip():
        raise Problem(428, "precondition_required", 'this request requires an If-Match: "<version>" header')
    raw = value.strip()
    if raw.startswith("W/"):
        raw = raw[2:]
    raw = raw.strip('"')
    if not raw.isdigit():
        raise Problem(400, "invalid_if_match", 'If-Match must be a quoted record version, e.g. "3"')
    return int(raw)


IdempotencyKey = Annotated[str | None, Header(alias="Idempotency-Key")]
IfMatch = Annotated[str | None, Header(alias="If-Match")]
IfNoneMatch = Annotated[str | None, Header(alias="If-None-Match")]
