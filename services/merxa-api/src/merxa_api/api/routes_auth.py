"""Merxa UI BFF auth (docs/contracts/10-auth.md § Merxa UI auth). Cookie ``merxa_session``."""

from __future__ import annotations

import secrets
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel

from merxa_api.api.deps import StateDep, check_same_origin
from merxa_api.errors import Problem, unauthorized
from merxa_api.logs import get_logger
from merxa_api.security import sessions
from merxa_api.security.oidc import pkce_pair
from merxa_api.security.principal import roles_from_claims, session_scopes
from merxa_api.services import audit
from merxa_api.services.audit import Attribution
from merxa_api.state import AppState

log = get_logger(__name__)
router = APIRouter(prefix="/v1/auth", tags=["auth"])

# Demo personas of the merxa realm (demo config set). Passwords are never exposed.
PERSONAS: tuple[dict[str, str], ...] = (
    {"username": "priya.shah", "name": "Priya Shah", "role_label": "Merchandise Planner", "initials": "PS"},
    {"username": "marco.rossi", "name": "Marco Rossi", "role_label": "Operations Director", "initials": "MR"},
    {"username": "merxa.admin", "name": "Merxa Admin", "role_label": "Merxa Administrator", "initials": "MA"},
)


class LoginAs(BaseModel):
    username: str


def _safe_next(value: str | None) -> str:
    """Only same-site absolute paths; anything a browser could read as another origin → ``/``.

    Browsers strip ASCII tab/CR/LF from URLs and treat ``\\`` like ``/``, so ``/\\t/evil``
    would become the protocol-relative ``//evil``; control and space characters are refused.
    """
    if (
        not value
        or len(value) > 2048
        or not value.startswith("/")
        or value.startswith("//")
        or "\\" in value
        or any(ord(ch) <= 0x20 or ord(ch) == 0x7F for ch in value)
    ):
        return "/"
    return value


def _cookie_secure(state: AppState) -> bool:
    return state.settings.public_url.startswith("https://")


def _set_session_cookie(resp: Any, state: AppState, token: str) -> None:
    resp.set_cookie(
        sessions.SESSION_COOKIE,
        token,
        max_age=state.settings.session_ttl_s,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(state),
        path="/",
    )


async def _start_session(
    state: AppState, tokens: dict[str, Any], *, nonce: str | None, demo: bool
) -> tuple[str, dict[str, Any]]:
    access = await state.oidc.verify(tokens["access_token"], audience=None)
    id_claims: dict[str, Any] = {}
    if tokens.get("id_token"):
        id_claims = await state.oidc.verify(
            tokens["id_token"],
            audience=state.settings.demo_client_id if demo else state.settings.web_client_id,
            nonce=nonce,
        )
    claims = {**access, **{k: v for k, v in id_claims.items() if k in ("name", "email", "preferred_username")}}
    if id_claims and id_claims.get("sub") != access.get("sub"):
        raise Problem(401, "invalid_token", "id_token and access_token subjects differ")
    roles = sorted(roles_from_claims(access))
    user = {
        "sub": str(claims["sub"]),
        "username": str(claims.get("preferred_username") or claims["sub"]),
        "name": claims.get("name"),
        "email": claims.get("email"),
        "roles": roles,
    }
    token = await sessions.create(
        state.engine,
        state.settings.session_secret,
        sub=user["sub"],
        username=user["username"],
        name=user["name"],
        email=user["email"],
        roles=roles,
        id_token=tokens.get("id_token"),
        demo=demo,
        ttl_s=state.settings.session_ttl_s,
    )
    return token, user


@router.get("/login")
async def login(state: StateDep, next: str | None = None) -> RedirectResponse:
    verifier, challenge = pkce_pair()
    st, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    url = await state.oidc.authorize_url(state=st, nonce=nonce, code_challenge=challenge)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        sessions.OAUTH_COOKIE,
        sessions.sign(
            state.settings.session_secret, {"state": st, "nonce": nonce, "cv": verifier, "next": _safe_next(next)}
        ),
        max_age=600,
        httponly=True,
        samesite="lax",
        secure=_cookie_secure(state),
        path="/v1/auth",
    )
    return resp


@router.get("/callback")
async def callback(
    request: Request, state: StateDep, code: str | None = None, error: str | None = None
) -> RedirectResponse:
    returned_state = request.query_params.get("state")
    if error:
        raise Problem(401, "login_failed", f"identity provider returned error {error!r}")
    data = sessions.unsign(state.settings.session_secret, request.cookies.get(sessions.OAUTH_COOKIE))
    if data is None or not returned_state or not secrets.compare_digest(str(data.get("state")), returned_state):
        raise Problem(400, "invalid_state", "login state is missing, expired or does not match")
    if not code:
        raise Problem(400, "invalid_request", "missing authorization code")
    tokens = await state.oidc.exchange_code(code, str(data["cv"]))
    token, user = await _start_session(state, tokens, nonce=str(data["nonce"]), demo=False)
    log.info("ui login", extra={"username": user["username"]})
    resp = RedirectResponse(_safe_next(data.get("next")), status_code=302)
    _set_session_cookie(resp, state, token)
    resp.delete_cookie(sessions.OAUTH_COOKIE, path="/v1/auth")
    return resp


@router.post("/logout")
async def logout(request: Request, state: StateDep) -> JSONResponse:
    check_same_origin(request, state.settings.public_origin)  # no cross-site forced logout
    user = await sessions.destroy(
        state.engine, state.settings.session_secret, request.cookies.get(sessions.SESSION_COOKIE)
    )
    logout_url = await state.oidc.end_session_url(user.id_token if user else None) if user else None
    resp = JSONResponse({"status": "logged_out", "logout_url": logout_url})
    resp.delete_cookie(sessions.SESSION_COOKIE, path="/")
    return resp


@router.get("/me")
async def me(request: Request, state: StateDep) -> dict[str, Any]:
    user = await sessions.load(
        state.engine, state.settings.session_secret, request.cookies.get(sessions.SESSION_COOKIE)
    )
    if user is None:
        raise unauthorized("no Merxa session")
    return {
        "user": {
            "sub": user.sub,
            "username": user.username,
            "name": user.name,
            "email": user.email,
            "roles": list(user.roles),
            "scopes": sorted(session_scopes(frozenset(user.roles))),
        },
        "demo_mode": state.settings.demo_mode,
        "brand": {"vendor": state.settings.brand_vendor_name},
    }


def _require_demo(request: Request, state: AppState) -> None:
    if not state.settings.demo_mode:
        raise Problem(404, "not_found", "demo persona switcher is disabled")
    if state.settings.is_localhost_deployment:
        return
    secret = state.settings.demo_access_secret
    supplied = request.headers.get("x-demo-access")
    if not secret or not supplied or not secrets.compare_digest(secret, supplied):
        raise Problem(403, "forbidden", "demo access requires X-Demo-Access outside localhost")


@router.get("/demo/personas")
async def personas(request: Request, state: StateDep) -> list[dict[str, str]]:
    _require_demo(request, state)
    return [dict(p) for p in PERSONAS]


@router.post("/demo/login-as")
async def login_as(body: LoginAs, request: Request, state: StateDep) -> JSONResponse:
    _require_demo(request, state)
    # Login CSRF: a foreign page could otherwise POST a typeless body (``fetch`` + Blob,
    # no preflight, parsed as JSON by FastAPI) and swap the victim into a demo persona.
    check_same_origin(request, state.settings.public_origin)
    if body.username not in {p["username"] for p in PERSONAS}:
        raise Problem(404, "not_found", f"unknown demo persona {body.username!r}")
    if not state.settings.demo_user_password:
        raise Problem(503, "not_configured", "DEMO_USER_PASSWORD is not configured for merxa-api")
    tokens = await state.oidc.password_grant(body.username, state.settings.demo_user_password)
    token, user = await _start_session(state, tokens, nonce=None, demo=True)
    async with state.engine.begin() as conn:
        await audit.write(
            conn,
            Attribution(
                actor_sub=user["sub"],
                actor_username=user["username"],
                actor_display=user["name"],
                client_id=state.settings.demo_client_id,
                via="Merxa UI",
            ),
            entity_type="ui_session",
            entity_ref=user["username"],
            action="demo_login",
        )
    resp = JSONResponse({"user": user, "demo_mode": True})
    _set_session_cookie(resp, state, token)
    return resp
