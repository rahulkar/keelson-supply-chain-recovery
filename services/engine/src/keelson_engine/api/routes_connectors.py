"""Connectors: list, delegated link status, Connect-<connector> OAuth (``keelson-connect``,
``private_key_jwt`` + PKCE S256 + ``prompt=login`` + ``resource``), tool snapshot refresh."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

from keelson_engine.api.deps import CurrentSession, MutatingSession, optional_session, require_any_role
from keelson_engine.api.errors import ApiError, internal_error
from keelson_engine.api.routes_auth import safe_return_path
from keelson_engine.api.sessions import consume_flow, create_flow
from keelson_engine.config import get_settings
from keelson_engine.core import repo
from keelson_engine.core.connectors import ConnectorNotFound, get_connector, list_connectors, refresh_tool_snapshot
from keelson_engine.core.logs import get_logger
from keelson_engine.core.oidc import OidcError, TokenEndpointError, new_pkce, random_token
from keelson_engine.core.tokens import TokenError, get_token_broker, token_roles

__all__ = ["CONNECT_SCOPE", "router"]

log = get_logger(__name__)
router = APIRouter()

CONNECT_SCOPE = "openid offline_access changes:apply"
_ANY_TENANT_ROLE = ("planner", "approver", "compliance", "tenant-admin", "platform-admin")


@router.get("/api/connectors")
async def connectors(sess: CurrentSession) -> dict[str, Any]:
    tenant = require_any_role(sess, _ANY_TENANT_ROLE, "viewing connectors")
    broker = get_token_broker()
    items = []
    for conn in await list_connectors(tenant):
        link = await broker.link_status(tenant=tenant, connector=conn.slug, keelson_sub=sess.sub)
        items.append(
            {
                "slug": conn.slug,
                "name": conn.name,
                "url": conn.url,
                "tier": conn.tier,
                "tiers": conn.tiers,
                "status": conn.status,
                "link": {"status": link["status"], "ext_username": link["ext_username"]},
            }
        )
    return {"items": items}


async def _connector_or_404(tenant: str, slug: str) -> Any:
    try:
        return await get_connector(tenant, slug)
    except ConnectorNotFound as exc:
        raise ApiError(404, "not_found", "connector not found") from exc


@router.get("/api/connectors/{slug}/link")
async def link(slug: str, sess: CurrentSession) -> dict[str, Any]:
    """The **current user's** delegated link: ``{status, ext_username, roles, scopes, apply_ready}``.

    Per viewer, never per case: an approval card's ``connect`` block prop describes the
    initiator at card creation, so a client deciding whether to show "Connect <system>" next to
    Approve must use ``apply_ready`` from here (false ⇔ approve answers ``412 connect_required``).
    A disconnected (revoked) link reads as ``missing``."""
    tenant = require_any_role(sess, _ANY_TENANT_ROLE, "viewing connectors")
    await _connector_or_404(tenant, slug)
    return await get_token_broker().link_status(tenant=tenant, connector=slug, keelson_sub=sess.sub)


@router.delete("/api/connectors/{slug}/link")
async def disconnect(slug: str, sess: MutatingSession) -> dict[str, Any]:
    """Disconnect the current user's delegated link (demo resets, "disconnect" in settings).

    Revokes the refresh token at the connector's IdP (RFC 7009 with ``private_key_jwt``), wipes
    the stored tokens and marks the link ``revoked`` (then ``GET …/link`` reads ``missing``).
    Idempotent. ``200 {status: "missing", revoked, idp_revocation}``; ``idp_revocation`` is
    ``revoked`` | ``failed`` (revoked locally only; logged) | ``not_needed`` (nothing stored)."""
    tenant = require_any_role(sess, _ANY_TENANT_ROLE, "disconnecting systems")
    await _connector_or_404(tenant, slug)
    result = await get_token_broker().revoke_user_link(tenant=tenant, connector=slug, keelson_sub=sess.sub)
    if result["revoked"]:
        await repo.admin_audit(
            sess.actor(), "connector.unlink", f"connector:{slug}", {"ext_username": result["ext_username"]},
            {"idp_revocation": result["idp_revocation"]}, tenant=tenant,
        )
    return {"status": "missing", "revoked": result["revoked"], "idp_revocation": result["idp_revocation"]}


@router.get("/api/connectors/{slug}/connect")
async def connect(slug: str, request: Request, return_to: str | None = None) -> RedirectResponse:
    settings = get_settings()
    sess = await optional_session(request)
    back = safe_return_path(return_to)
    if sess is None:
        here = f"/api/connectors/{slug}/connect?{urlencode({'return_to': back})}"
        return RedirectResponse(f"/api/auth/login?{urlencode({'next': here})}", status_code=302)
    tenant = require_any_role(sess, _ANY_TENANT_ROLE, "connecting systems")
    conn = await _connector_or_404(tenant, slug)
    client_id = conn.clients.get("connect")
    if not client_id:
        raise ApiError(404, "not_supported", "this connector does not support delegated sign-in")
    state, nonce, pkce = random_token(43), random_token(43), new_pkce()
    await create_flow(
        state=state, kind="connect", nonce=nonce, code_verifier=pkce.verifier, return_to=back,
        session_id=sess.id, keelson_sub=sess.sub, tenant=tenant, connector=slug,
    )
    params = {
        "client_id": client_id,
        "response_type": "code",
        "scope": CONNECT_SCOPE,
        "redirect_uri": settings.connector_redirect_uri(slug),
        "state": state,
        "nonce": nonce,
        "code_challenge": pkce.challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "resource": conn.resource,
    }
    if sess.merxa_username:
        params["login_hint"] = sess.merxa_username
    try:
        url = await get_token_broker().provider(conn.issuer).authorization_url(params)
    except OidcError as exc:
        log.error("connect: identity provider unavailable", connector=slug, error=str(exc))
        raise ApiError(503, "idp_unavailable", f"{conn.name} sign-in is temporarily unavailable") from exc
    return RedirectResponse(url, status_code=302)


def _back(return_to: str, **params: str) -> RedirectResponse:
    sep = "&" if "?" in return_to else "?"
    return RedirectResponse(f"{return_to}{sep}{urlencode(params)}", status_code=302)


@router.get("/api/connectors/{slug}/callback")
async def callback(
    slug: str, request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> RedirectResponse:
    settings = get_settings()
    flow = await consume_flow(state or "", "connect")
    sess = await optional_session(request)
    if flow is None or sess is None or flow.connector != slug:
        log.warning("connect callback rejected: unknown state or no session", connector=slug)
        return _back("/", connect_error="invalid_state")
    if flow.session_id != sess.id or flow.keelson_sub != sess.sub or flow.tenant != sess.active_tenant:
        log.warning("connect callback rejected: flow bound to another session", connector=slug)
        return _back("/", connect_error="invalid_state")
    if error or not code:
        return _back(flow.return_to, connect_error="denied" if error == "access_denied" else "connect_failed")
    tenant = flow.tenant
    assert tenant is not None
    broker = get_token_broker()
    try:
        conn = await get_connector(tenant, slug)
        client_id = conn.client_id("connect")
        body = await broker.token_request(
            conn,
            client_id,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.connector_redirect_uri(slug),
                "code_verifier": flow.code_verifier,
                "resource": conn.resource,
            },
        )
        provider = broker.provider(conn.issuer)
        if not body.get("id_token"):
            raise OidcError("no id_token")
        id_claims = await provider.verify(str(body["id_token"]), audience=client_id, nonce=flow.nonce)
        access_claims = await provider.verify(str(body["access_token"]))
        if access_claims.get("sub") != id_claims.get("sub"):
            raise OidcError("id_token / access_token subject mismatch")
        aud = access_claims.get("aud")
        audiences = aud if isinstance(aud, list) else [aud]
        if conn.resource not in audiences:
            log.warning("connect: access token audience lacks the resource", connector=slug)
        scopes = sorted({s for s in str(body.get("scope") or access_claims.get("scope") or "").split() if s})
        roles = token_roles(access_claims)
        await broker.store_user_link(
            conn=conn,
            keelson_sub=sess.sub,
            token_body=body,
            ext_sub=str(id_claims["sub"]),
            ext_username=id_claims.get("preferred_username"),
            roles=roles,
            scopes=scopes,
        )
    except (ConnectorNotFound, OidcError, TokenEndpointError, TokenError) as exc:
        log.warning("connect callback failed", connector=slug, error=str(exc))
        return _back(flow.return_to, connect_error="connect_failed")
    await repo.admin_audit(
        sess.actor(), "connector.link", f"connector:{slug}", None,
        {"ext_username": id_claims.get("preferred_username"), "scopes": scopes, "roles": roles},
        tenant=tenant,
    )
    return _back(flow.return_to, connected=slug)


@router.post("/api/connectors/{slug}/refresh-tools")
async def refresh_tools(slug: str, sess: MutatingSession) -> dict[str, Any]:
    tenant = require_any_role(sess, ("tenant-admin", "platform-admin"), "refreshing connector tools")
    await _connector_or_404(tenant, slug)
    try:
        return await refresh_tool_snapshot(tenant, slug, actor=sess.actor())
    except TokenError as exc:
        raise internal_error(502, "connector_auth_failed", "tool snapshot refresh: token refused", exc,
                             message="The connector refused Keelson's credentials.", connector=slug) from exc
    except Exception as exc:  # noqa: BLE001 - MCP/transport failures surface as 502
        raise internal_error(502, "connector_unreachable", "tool snapshot refresh failed", exc,
                             message="The connector could not be reached.", connector=slug) from exc
