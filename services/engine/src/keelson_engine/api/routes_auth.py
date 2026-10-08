"""BFF authentication (realm ``keelson``) + demo persona switcher (``10-auth.md``).

* ``GET /api/auth/login?next=`` → authorization code + PKCE S256 (Authlib helpers), state and
  nonce stored server-side, flow bound to the browser with a short-lived ``keelson_login``
  cookie (prevents login CSRF / session fixation through a forged callback).
* ``GET /api/auth/callback`` → code exchange (client secret), ID token verified (signature,
  ``iss``, ``aud``, ``nonce``), access token verified, session row + ``keelson_session`` cookie.
* ``POST /api/auth/logout`` → session deleted; returns the realm logout URL.
* ``GET /api/me``.
* Demo (``DEMO_MODE=1``): ``GET /api/demo/personas``, ``POST /api/demo/login-as`` (password grant
  on ``keelson-demo``; token must carry ``demo: true``; never returns tokens; audited).
"""

from __future__ import annotations

import datetime as dt
import hmac
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import text

from keelson_engine.api.deps import CurrentSession, check_csrf, optional_session
from keelson_engine.api.errors import ApiError
from keelson_engine.api.sessions import (
    LOGIN_COOKIE,
    SESSION_COOKIE,
    SessionData,
    consume_flow,
    create_flow,
    create_session,
    delete_session,
    sha256_hex,
)
from keelson_engine.config import get_settings
from keelson_engine.core import repo
from keelson_engine.core.db import system_session
from keelson_engine.core.http import internal_http
from keelson_engine.core.logs import get_logger
from keelson_engine.core.oidc import OidcError, OidcProvider, TokenEndpointError, new_pkce, random_token

__all__ = ["PERSONAS", "keelson_oidc", "router", "safe_return_path", "set_session_cookie"]

log = get_logger(__name__)
router = APIRouter()

_provider: OidcProvider | None = None


def keelson_oidc() -> OidcProvider:
    global _provider
    settings = get_settings()
    if _provider is None or _provider.issuer != settings.oidc_issuer:
        _provider = OidcProvider(settings.oidc_issuer, http_getter=internal_http)
    return _provider


def reset_keelson_oidc() -> None:
    global _provider
    _provider = None


PERSONAS: tuple[dict[str, Any], ...] = (
    {"username": "priya", "name": "Priya Shah", "role_label": "Planner", "initials": "PS"},
    {"username": "marco", "name": "Marco Rossi", "role_label": "Planner · Approver", "initials": "MR"},
    {"username": "lena", "name": "Lena Vogel", "role_label": "Compliance", "initials": "LV"},
    {"username": "sam", "name": "Sam Okafor", "role_label": "Tenant admin · Planner", "initials": "SO"},
    {"username": "ava", "name": "Ava Lindqvist", "role_label": "Platform admin", "initials": "AL"},
    {"username": "nora", "name": "Nora Test", "role_label": "Planner (isolation tenant)", "initials": "NT"},
)
_PLATFORM_ADMIN_PERSONAS = frozenset({"ava"})


def safe_return_path(value: str | None, default: str = "/") -> str:
    """Only same-site absolute paths (``/x``), never ``//host`` or schemes (open redirect)."""
    if not value or len(value) > 2000:
        return default
    if not value.startswith("/") or value.startswith("//") or "\\" in value or any(ord(c) < 0x20 for c in value):
        return default
    parts = urlsplit(value)
    if parts.scheme or parts.netloc:
        return default
    return value


def set_session_cookie(response: Response, value: str) -> None:
    settings = get_settings()
    response.set_cookie(
        SESSION_COOKIE,
        value,
        max_age=settings.session_max_seconds,
        path="/",
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(SESSION_COOKIE, path="/", httponly=True, samesite="lax")


async def _existing_tenants(tenants: list[str]) -> list[str]:
    """The claimed tenants that exist and are not suspended (M7: a suspended tenant blocks logins:
    it never becomes a session's active tenant)."""
    if not tenants:
        return []
    async with system_session() as s:
        res = await s.execute(text("SELECT slug FROM tenant WHERE slug = ANY(:t) AND status <> 'suspended'"),
                              {"t": list(tenants)})
        known = {r[0] for r in res.all()}
    return [t for t in tenants if t in known]


async def _merxa_username_hint(sub: str) -> str | None:
    """``merxa_username`` user attribute (Connect-Merxa ``login_hint``) via the Keycloak admin
    API; best effort (``None`` when the admin API is unavailable)."""
    settings = get_settings()
    password = settings.kc_admin_password.get_secret_value()
    if not password:
        return None
    realm = settings.oidc_issuer.rstrip("/").rsplit("/", 1)[-1]
    base = settings.keycloak_internal_url.rstrip("/")
    http = internal_http()
    try:
        tok = await http.post(
            f"{base}/realms/master/protocol/openid-connect/token",
            data={"grant_type": "password", "client_id": "admin-cli", "username": "admin", "password": password},
            timeout=5.0,
        )
        if tok.status_code != 200:
            return None
        admin_token = tok.json()["access_token"]
        user = await http.get(
            f"{base}/admin/realms/{realm}/users/{sub}", headers={"Authorization": f"Bearer {admin_token}"},
            timeout=5.0,
        )
        if user.status_code != 200:
            return None
        values = (user.json().get("attributes") or {}).get("merxa_username") or []
        return str(values[0]) if values else None
    except Exception as exc:  # noqa: BLE001 - optional enrichment
        log.info("merxa_username lookup failed", error=exc.__class__.__name__)
        return None


async def session_from_tokens(tokens: dict[str, Any], *, nonce: str | None, demo: bool) -> tuple[str, SessionData]:
    """Verify tokens from the keelson realm and create a BFF session."""
    settings = get_settings()
    provider = keelson_oidc()
    access_claims = await provider.verify(str(tokens["access_token"]))
    id_claims: dict[str, Any] = {}
    if tokens.get("id_token"):
        client_id = settings.demo_client_id if demo else settings.oidc_client_id
        id_claims = await provider.verify(str(tokens["id_token"]), audience=client_id, nonce=nonce)
        if id_claims.get("sub") != access_claims.get("sub"):
            raise OidcError("id_token / access_token subject mismatch")
    elif nonce is not None:
        raise OidcError("no id_token in code exchange")
    claims = {**access_claims, **id_claims}
    sub = str(claims["sub"])
    username = str(claims.get("preferred_username") or sub)
    name = str(claims.get("name") or " ".join(x for x in (claims.get("given_name"), claims.get("family_name")) if x)
               or username)
    roles = [str(r) for r in (access_claims.get("roles") or claims.get("roles") or [])]
    tenants = [str(t) for t in (access_claims.get("tenants") or claims.get("tenants") or [])]
    known = await _existing_tenants(tenants)
    auth_time_raw = claims.get("auth_time") or claims.get("iat")
    auth_time = dt.datetime.fromtimestamp(int(auth_time_raw), dt.UTC) if auth_time_raw else None
    merxa_username = claims.get("merxa_username") or await _merxa_username_hint(sub)
    return await create_session(
        sub=sub,
        username=username,
        name=name,
        email=claims.get("email"),
        roles=roles,
        tenants=tenants,
        active_tenant=known[0] if known else None,
        tokens=tokens,
        demo=demo,
        auth_time=auth_time,
        merxa_username=merxa_username,
    )


def _me(sess: SessionData) -> dict[str, Any]:
    settings = get_settings()
    return {
        "user": sess.user_json(),
        "csrf_token": sess.csrf_token,
        "demo_mode": settings.demo_mode,
        "brand": {"engine": settings.brand_engine_name, "vendor": settings.brand_vendor_name},
    }


# ───────────────────────────── routes ─────────────────────────────


@router.get("/api/auth/login")
async def login(request: Request, next: str | None = None) -> RedirectResponse:  # noqa: A002 - contract name
    settings = get_settings()
    state, nonce, pkce = random_token(43), random_token(43), new_pkce()
    binding = random_token(43)
    await create_flow(
        state=state, kind="login", nonce=nonce, code_verifier=pkce.verifier,
        return_to=safe_return_path(next), session_id=sha256_hex(binding),
    )
    try:
        url = await keelson_oidc().authorization_url(
            {
                "client_id": settings.oidc_client_id,
                "response_type": "code",
                "scope": "openid profile email",
                "redirect_uri": settings.bff_redirect_uri,
                "state": state,
                "nonce": nonce,
                "code_challenge": pkce.challenge,
                "code_challenge_method": "S256",
            }
        )
    except OidcError as exc:
        log.error("login: identity provider unavailable", error=str(exc))
        raise ApiError(503, "idp_unavailable", "sign-in is temporarily unavailable") from exc
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(LOGIN_COOKIE, binding, max_age=600, path="/api/auth", httponly=True, samesite="lax",
                    secure=settings.cookie_secure)
    return resp


@router.get("/api/auth/callback")
async def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    settings = get_settings()
    flow = await consume_flow(state or "", "login")
    binding = request.cookies.get(LOGIN_COOKIE, "")
    if flow is None or not binding or not hmac.compare_digest(sha256_hex(binding), flow.session_id or ""):
        log.warning("login callback rejected: unknown state or browser binding")
        return _login_error_redirect("invalid_state")
    if error or not code:
        return _login_error_redirect("denied" if error == "access_denied" else "login_failed", flow.return_to)
    try:
        tokens = await keelson_oidc().token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.bff_redirect_uri,
                "client_id": settings.oidc_client_id,
                "client_secret": settings.keelson_bff_client_secret.get_secret_value(),
                "code_verifier": flow.code_verifier,
            }
        )
        cookie_value, sess = await session_from_tokens(tokens, nonce=flow.nonce, demo=False)
    except (OidcError, TokenEndpointError) as exc:
        log.warning("login callback failed", error=str(exc))
        return _login_error_redirect("login_failed", flow.return_to)
    old = await optional_session(request)
    if old is not None:
        await delete_session(old.id)
    if sess.active_tenant:
        await repo.admin_audit(sess.actor(), "auth.login", f"user:{sess.username}", None, {"demo": False},
                               tenant=sess.active_tenant)
    resp = RedirectResponse(flow.return_to, status_code=302)
    set_session_cookie(resp, cookie_value)
    resp.delete_cookie(LOGIN_COOKIE, path="/api/auth")
    return resp


def _login_error_redirect(code: str, return_to: str = "/") -> RedirectResponse:
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(f"{return_to}{sep}{urlencode({'auth_error': code})}", status_code=302)


@router.post("/api/auth/logout")
async def logout(request: Request) -> JSONResponse:
    settings = get_settings()
    sess = await optional_session(request)
    if sess is not None:
        check_csrf(request, sess)
        await delete_session(sess.id)
    logout_url: str | None = None
    try:
        end = await keelson_oidc().endpoint("end_session_endpoint")
        logout_url = f"{end}?" + urlencode(
            {"client_id": settings.oidc_client_id, "post_logout_redirect_uri": f"{settings.public_url}/"}
        )
    except OidcError:
        logout_url = None
    resp = JSONResponse({"ok": True, "logout_url": logout_url})
    clear_session_cookie(resp)
    return resp


@router.get("/api/me")
async def me(sess: CurrentSession) -> dict[str, Any]:
    return _me(sess)


# ───────────────────────────── demo ─────────────────────────────


def _require_demo() -> None:
    if not get_settings().demo_mode:
        raise ApiError(404, "not_found", "not found")


def _visible_personas() -> list[dict[str, Any]]:
    local = get_settings().is_local_demo
    return [dict(p) for p in PERSONAS if local or p["username"] not in _PLATFORM_ADMIN_PERSONAS]


@router.get("/api/demo/personas")
async def demo_personas() -> list[dict[str, Any]]:
    _require_demo()
    return _visible_personas()


class LoginAsBody(BaseModel):
    username: str = Field(min_length=1, max_length=64)


@router.post("/api/demo/login-as")
async def demo_login_as(request: Request, body: LoginAsBody) -> JSONResponse:
    _require_demo()
    settings = get_settings()
    current = await optional_session(request)
    if current is not None:
        check_csrf(request, current)
    if not settings.is_local_demo:
        presented = request.headers.get("x-demo-access", "")
        secret = settings.demo_access_secret.get_secret_value()
        presenter = current is not None and current.has_any("demo-presenter")
        if not presenter and not (secret and presented and hmac.compare_digest(presented.encode(), secret.encode())):
            raise ApiError(403, "demo_access_required", "the persona switcher needs demo access on hosted demos")
    allowed = {p["username"] for p in _visible_personas()}
    if body.username not in allowed:
        raise ApiError(404, "unknown_persona", "unknown persona")
    try:
        tokens = await keelson_oidc().token_request(
            {
                "grant_type": "password",
                "client_id": settings.demo_client_id,
                "client_secret": settings.keelson_demo_client_secret.get_secret_value(),
                "username": body.username,
                "password": settings.demo_user_password.get_secret_value(),
                "scope": "openid profile email",
            }
        )
        claims = await keelson_oidc().verify(str(tokens["access_token"]))
        if claims.get("demo") is not True:
            raise OidcError("token is not a demo token")
        if claims.get("preferred_username") != body.username:
            raise OidcError("token is for a different user")
        cookie_value, sess = await session_from_tokens(tokens, nonce=None, demo=True)
    except TokenEndpointError as exc:
        log.warning("demo login-as refused by identity provider", username=body.username, error=exc.error)
        raise ApiError(502, "idp_refused", "the identity provider refused the demo sign-in") from exc
    except OidcError as exc:
        log.warning("demo login-as failed", username=body.username, error=str(exc))
        raise ApiError(502, "idp_unavailable", "demo sign-in failed") from exc
    if current is not None:
        await delete_session(current.id)
    audit_tenant = sess.active_tenant
    before = {"username": current.username if current else None}
    after = {"username": sess.username, "roles": sess.roles}
    if audit_tenant:
        await repo.admin_audit(sess.actor(), "demo.login_as", f"user:{sess.username}", before, after,
                               tenant=audit_tenant)
    else:
        await repo.admin_audit(sess.actor(), "demo.login_as", f"user:{sess.username}", before, after, platform=True)
    resp = JSONResponse({"ok": True, **_me(sess)})
    set_session_cookie(resp, cookie_value)
    return resp
