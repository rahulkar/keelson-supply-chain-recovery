"""Token Broker: Keelson's credentials for third-party connectors (``10-auth.md``).

* **Service principals** (reader / preparer / autocommit): client credentials with
  ``private_key_jwt`` (client assertion ``kid ca-1``) + ``resource=<MCP URL>``; cached per
  ``(tenant, connector, issuer, 'service', client_id)`` until 30 s before expiry; concurrent
  callers share one request (per-key ``asyncio.Lock``).
* **Delegated user tokens** (Connect-Merxa, ``keelson-connect``): the refresh token is stored
  AES-GCM encrypted in ``connector_credential``. Refresh is single-flight across processes:
  the row is locked ``SELECT … FOR UPDATE``; whoever holds the lock refreshes and stores the
  rotated refresh token + the new access token atomically, and waiters then reuse that
  access token instead of refreshing again. ``invalid_grant`` marks the link ``expired`` and
  raises :class:`ConnectRequired`.

Tokens never leave this module except as return values to the caller (never logged, never
put into ADK session state or sent to the browser).
"""

from __future__ import annotations

import asyncio
import datetime as dt
import time
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import jwt
from sqlalchemy import text

from keelson_engine.config import get_settings
from keelson_engine.core.connectors import ConnectorRow, get_connector
from keelson_engine.core.crypto import DATA_KEY_ID, decrypt_str, encrypt_str, sign_client_assertion
from keelson_engine.core.db import tenant_session
from keelson_engine.core.http import connector_http
from keelson_engine.core.logs import get_logger
from keelson_engine.core.oidc import OidcError, OidcProvider, TokenEndpointError
from keelson_engine.core.sql import dumps

__all__ = [
    "APPLY_SCOPE",
    "ConnectRequired",
    "ServicePrincipal",
    "TokenBroker",
    "TokenError",
    "apply_ready",
    "get_token_broker",
    "set_token_broker",
]

log = get_logger(__name__)

ServicePrincipal = Literal["reader", "preparer", "autocommit"]
_SKEW_S = 30
CLIENT_ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class TokenError(Exception):
    """The identity provider refused or could not issue a token (not a user action)."""


class ConnectRequired(Exception):
    """The user has no usable delegated link: send them to ``connect_url``."""

    def __init__(self, connect_url: str, reason: str = "connect_required") -> None:
        super().__init__(reason)
        self.connect_url = connect_url
        self.reason = reason


@dataclass(slots=True)
class _Cached:
    token: str
    expires_at: float
    link: str = ""
    """Delegated tokens: ``<ext_sub>|<linked_at>`` of the identity link the token belongs to."""


def _cred_aad(tenant: str, connector: str, issuer: str, ptype: str, psub: str, fieldname: str) -> str:
    return f"cred|{tenant}|{connector}|{issuer}|{ptype}|{psub}|{fieldname}"


def _scopes(body: dict[str, Any], claims: dict[str, Any] | None = None) -> list[str]:
    raw = body.get("scope") or (claims or {}).get("scope") or ""
    return sorted({s for s in str(raw).split() if s})


def _unverified_claims(token: str) -> dict[str, Any]:
    try:
        return dict(jwt.decode(token, options={"verify_signature": False}))
    except jwt.PyJWTError:
        return {}


def token_roles(claims: dict[str, Any]) -> list[str]:
    roles = set(claims.get("roles") or [])
    roles |= set((claims.get("realm_access") or {}).get("roles") or [])
    return sorted(str(r) for r in roles)


class TokenBroker:
    def __init__(self) -> None:
        self._cache: dict[tuple[str, str, str, str, str], _Cached] = {}
        self._locks: dict[tuple[str, str, str, str, str], asyncio.Lock] = {}
        self._providers: dict[str, OidcProvider] = {}

    # ───────────────────────────── helpers ─────────────────────────────

    def provider(self, issuer: str) -> OidcProvider:
        prov = self._providers.get(issuer)
        if prov is None:
            prov = OidcProvider(issuer, http_getter=connector_http)
            self._providers[issuer] = prov
        return prov

    def _lock(self, key: tuple[str, str, str, str, str]) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    def connect_url(self, connector: str, return_to: str | None = None) -> str:
        base = f"{get_settings().public_url}/api/connectors/{connector}/connect"
        return f"{base}?{urlencode({'return_to': return_to})}" if return_to else base

    def clear_cache(self) -> None:
        self._cache.clear()

    def _assertion_form(self, client_id: str, issuer: str) -> dict[str, str]:
        return {
            "client_id": client_id,
            "client_assertion_type": CLIENT_ASSERTION_TYPE,
            "client_assertion": sign_client_assertion(client_id, issuer),
        }

    async def token_request(self, conn: ConnectorRow, client_id: str, form: dict[str, str]) -> dict[str, Any]:
        """Token endpoint call authenticated with ``private_key_jwt`` (assertion aud = issuer)."""
        body = {**form, **self._assertion_form(client_id, conn.issuer)}
        return await self.provider(conn.issuer).token_request(body)

    # ───────────────────────────── service ─────────────────────────────

    async def service_token(self, *, tenant: str, connector: str, principal: ServicePrincipal) -> str:
        if principal not in ("reader", "preparer", "autocommit"):
            raise ValueError(f"unknown service principal {principal!r}")
        conn = await get_connector(tenant, connector)
        client_id = conn.client_id(principal)
        key = (tenant, connector, conn.issuer, "service", client_id)
        cached = self._cache.get(key)
        if cached and cached.expires_at - _SKEW_S > time.time():
            return cached.token
        async with self._lock(key):
            cached = self._cache.get(key)
            if cached and cached.expires_at - _SKEW_S > time.time():
                return cached.token
            try:
                body = await self.token_request(
                    conn, client_id, {"grant_type": "client_credentials", "resource": conn.resource}
                )
            except (TokenEndpointError, OidcError) as exc:
                log.error("service token request failed", tenant=tenant, connector=connector, principal=principal,
                          error=str(exc))
                raise TokenError(f"{principal} token for {connector} unavailable") from exc
            token = str(body["access_token"])
            self._cache[key] = _Cached(token, time.time() + int(body.get("expires_in") or 60))
            return token

    # ───────────────────────────── delegated ─────────────────────────────

    async def _load_link(self, tenant: str, connector: str, keelson_sub: str) -> dict[str, Any] | None:
        async with tenant_session(tenant) as s:
            res = await s.execute(
                text(
                    "SELECT ext_iss, ext_sub, ext_username, roles, scopes, status, linked_at, updated_at "
                    "FROM connector_identity_link WHERE connector = :c AND keelson_sub = :k"
                ),
                {"c": connector, "k": keelson_sub},
            )
            m = res.mappings().first()
        return dict(m) if m is not None else None

    async def link_status(self, *, tenant: str, connector: str, keelson_sub: str) -> dict[str, Any]:
        """``{status: active|missing|expired, ext_username, roles, scopes, apply_ready}`` of
        **``keelson_sub``'s own** link (the caller's, in the API).

        A disconnected (``revoked``) link reads as ``missing``. ``apply_ready`` is exactly the
        approval pre-flight of ``POST /api/interrupts/{id}/decision`` (:func:`apply_ready`):
        when it is false, approving answers ``412 connect_required``."""
        link = await self._load_link(tenant, connector, keelson_sub)
        if link is None or link["status"] == "revoked":
            return {"status": "missing", "ext_username": None, "roles": [], "scopes": [], "apply_ready": False}
        status = "active" if link["status"] == "active" else "expired"
        out: dict[str, Any] = {
            "status": status,
            "ext_username": link["ext_username"],
            "roles": list(link["roles"] or []),
            "scopes": list(link["scopes"] or []),
        }
        out["apply_ready"] = apply_ready(out)
        return out

    async def user_token(self, *, tenant: str, connector: str, keelson_sub: str) -> str:
        """Delegated access token for ``keelson_sub`` (refreshing single-flight if needed).

        The in-process cache is only a shortcut: the identity link is re-read on every call,
        so a link that another process expired, or that was re-connected to a different
        external account, is never served from a stale cache entry.
        """
        conn = await get_connector(tenant, connector)
        key = (tenant, connector, conn.issuer, "user", keelson_sub)
        link = await self._load_link(tenant, connector, keelson_sub)
        if link is None or link["status"] != "active":
            self._cache.pop(key, None)
            raise ConnectRequired(self.connect_url(connector), "link_missing" if link is None else "link_expired")
        fingerprint = f"{link['ext_sub']}|{link['linked_at'].isoformat() if link.get('linked_at') else ''}"
        cached = self._cache.get(key)
        if cached and cached.link == fingerprint and cached.expires_at - _SKEW_S > time.time():
            return cached.token
        async with self._lock(key):
            cached = self._cache.get(key)
            if cached and cached.link == fingerprint and cached.expires_at - _SKEW_S > time.time():
                return cached.token
            token, expires_at = await self._refresh_locked(conn, keelson_sub)
            self._cache[key] = _Cached(token, expires_at, fingerprint)
            return token

    async def _refresh_locked(self, conn: ConnectorRow, keelson_sub: str) -> tuple[str, float]:
        tenant, connector, issuer = conn.tenant, conn.slug, conn.issuer
        connect_url = self.connect_url(connector)
        client_id = conn.client_id("connect")
        aad_rt = _cred_aad(tenant, connector, issuer, "user", keelson_sub, "refresh")
        aad_at = _cred_aad(tenant, connector, issuer, "user", keelson_sub, "access")
        expired = False
        async with tenant_session(tenant) as s:
            link = await s.execute(
                text("SELECT status FROM connector_identity_link WHERE connector = :c AND keelson_sub = :k"),
                {"c": connector, "k": keelson_sub},
            )
            link_status = link.scalar_one_or_none()
            if link_status != "active":
                raise ConnectRequired(connect_url, "link_missing" if link_status is None else "link_expired")
            res = await s.execute(
                text(
                    """
                    SELECT refresh_token_enc, access_token_enc, access_expires_at FROM connector_credential
                    WHERE connector = :c AND issuer = :i AND principal_type = 'user' AND principal_sub = :k
                    FOR UPDATE
                    """
                ),
                {"c": connector, "i": issuer, "k": keelson_sub},
            )
            row = res.mappings().first()
            if row is None or not row["refresh_token_enc"]:
                raise ConnectRequired(connect_url, "link_missing")
            # Another process may have refreshed while we waited for the row lock.
            exp: dt.datetime | None = row["access_expires_at"]
            if row["access_token_enc"] and exp is not None and exp.timestamp() - _SKEW_S > time.time():
                return decrypt_str(row["access_token_enc"], aad_at), exp.timestamp()
            refresh_token = decrypt_str(row["refresh_token_enc"], aad_rt)
            try:
                body = await self.token_request(
                    conn, client_id,
                    {"grant_type": "refresh_token", "refresh_token": refresh_token, "resource": conn.resource},
                )
            except TokenEndpointError as exc:
                if exc.error == "invalid_grant":
                    expired = True
                    body = {}
                else:
                    raise TokenError(f"refresh for {connector} failed") from exc
            except OidcError as exc:
                raise TokenError(f"refresh for {connector} failed") from exc
            if expired:
                await s.execute(
                    text(
                        "UPDATE connector_identity_link SET status = 'expired', updated_at = now() "
                        "WHERE connector = :c AND keelson_sub = :k"
                    ),
                    {"c": connector, "k": keelson_sub},
                )
                await s.execute(
                    text(
                        "UPDATE connector_credential SET refresh_token_enc = NULL, access_token_enc = NULL, "
                        "access_expires_at = NULL, updated_at = now() "
                        "WHERE connector = :c AND issuer = :i AND principal_type = 'user' AND principal_sub = :k"
                    ),
                    {"c": connector, "i": issuer, "k": keelson_sub},
                )
            else:
                access = str(body["access_token"])
                expires_at = time.time() + int(body.get("expires_in") or 60)
                new_refresh = str(body.get("refresh_token") or refresh_token)
                claims = _unverified_claims(access)
                await s.execute(
                    text(
                        """
                        UPDATE connector_credential SET refresh_token_enc = :rt, access_token_enc = :at,
                               access_expires_at = :exp, refresh_expires_at = :rexp, key_id = :kid,
                               scopes = CAST(:scopes AS jsonb), updated_at = now()
                        WHERE connector = :c AND issuer = :i AND principal_type = 'user' AND principal_sub = :k
                        """
                    ),
                    {
                        "rt": encrypt_str(new_refresh, aad_rt),
                        "at": encrypt_str(access, aad_at),
                        "exp": dt.datetime.fromtimestamp(expires_at, dt.UTC),
                        "rexp": _refresh_expiry(body),
                        "kid": DATA_KEY_ID,
                        "scopes": dumps(_scopes(body, claims)),
                        "c": connector,
                        "i": issuer,
                        "k": keelson_sub,
                    },
                )
                await s.execute(
                    text(
                        "UPDATE connector_identity_link SET roles = CAST(:roles AS jsonb), "
                        "scopes = CAST(:scopes AS jsonb), updated_at = now() WHERE connector = :c AND keelson_sub = :k"
                    ),
                    {"roles": dumps(token_roles(claims)), "scopes": dumps(_scopes(body, claims)), "c": connector,
                     "k": keelson_sub},
                )
        if expired:
            log.info("delegated link expired", tenant=tenant, connector=connector)
            raise ConnectRequired(connect_url, "link_expired")
        return access, expires_at

    async def store_user_link(
        self,
        *,
        conn: ConnectorRow,
        keelson_sub: str,
        token_body: dict[str, Any],
        ext_sub: str,
        ext_username: str | None,
        roles: list[str],
        scopes: list[str],
    ) -> None:
        """Persist a freshly connected delegated credential + identity link (Connect callback)."""
        tenant, connector, issuer = conn.tenant, conn.slug, conn.issuer
        refresh = token_body.get("refresh_token")
        if not refresh:
            raise TokenError("identity provider returned no refresh token (offline_access missing)")
        access = str(token_body["access_token"])
        expires_at = time.time() + int(token_body.get("expires_in") or 60)
        aad_rt = _cred_aad(tenant, connector, issuer, "user", keelson_sub, "refresh")
        aad_at = _cred_aad(tenant, connector, issuer, "user", keelson_sub, "access")
        async with tenant_session(tenant) as s:
            await s.execute(
                text(
                    """
                    INSERT INTO connector_credential (tenant, connector, issuer, principal_type, principal_sub,
                        refresh_token_enc, access_token_enc, access_expires_at, refresh_expires_at, key_id, scopes)
                    VALUES (:t, :c, :i, 'user', :k, :rt, :at, :exp, :rexp, :kid, CAST(:scopes AS jsonb))
                    ON CONFLICT (tenant, connector, issuer, principal_type, principal_sub) DO UPDATE SET
                      refresh_token_enc = EXCLUDED.refresh_token_enc, access_token_enc = EXCLUDED.access_token_enc,
                      access_expires_at = EXCLUDED.access_expires_at,
                      refresh_expires_at = EXCLUDED.refresh_expires_at, key_id = EXCLUDED.key_id,
                      scopes = EXCLUDED.scopes, updated_at = now()
                    """
                ),
                {
                    "t": tenant, "c": connector, "i": issuer, "k": keelson_sub,
                    "rt": encrypt_str(str(refresh), aad_rt),
                    "at": encrypt_str(access, aad_at),
                    "exp": dt.datetime.fromtimestamp(expires_at, dt.UTC),
                    "rexp": _refresh_expiry(token_body),
                    "kid": DATA_KEY_ID,
                    "scopes": dumps(scopes),
                },
            )
            linked = await s.execute(
                text(
                    """
                    INSERT INTO connector_identity_link (tenant, connector, keelson_sub, ext_iss, ext_sub, ext_username,
                                                         roles, scopes, status)
                    VALUES (:t, :c, :k, :iss, :esub, :euser, CAST(:roles AS jsonb), CAST(:scopes AS jsonb), 'active')
                    ON CONFLICT (tenant, connector, keelson_sub) DO UPDATE SET
                      ext_iss = EXCLUDED.ext_iss, ext_sub = EXCLUDED.ext_sub, ext_username = EXCLUDED.ext_username,
                      roles = EXCLUDED.roles, scopes = EXCLUDED.scopes, status = 'active', linked_at = now(),
                      updated_at = now()
                    RETURNING linked_at
                    """
                ),
                {
                    "t": tenant, "c": connector, "k": keelson_sub, "iss": issuer, "esub": ext_sub,
                    "euser": ext_username, "roles": dumps(roles), "scopes": dumps(scopes),
                },
            )
            linked_at: dt.datetime = linked.scalar_one()
        self._cache[(tenant, connector, issuer, "user", keelson_sub)] = _Cached(
            access, expires_at, f"{ext_sub}|{linked_at.isoformat()}"
        )

    async def revoke_user_link(self, *, tenant: str, connector: str, keelson_sub: str) -> dict[str, Any]:
        """Disconnect ``keelson_sub``'s delegated link (``DELETE /api/connectors/{slug}/link``).

        Under the row locks (so no concurrent refresh rotates the token meanwhile): revoke the
        refresh token at the IdP (RFC 7009, ``private_key_jwt`` as the connect client,
        ``token_type_hint=refresh_token``; Keycloak then also ends the offline session), wipe
        the stored tokens and mark the identity link ``revoked``. The local revocation happens
        even if the IdP call fails (``idp_revocation: "failed"``). Idempotent.

        Returns ``{"revoked": bool, "idp_revocation": "revoked"|"failed"|"not_needed", "ext_username"}``.
        """
        conn = await get_connector(tenant, connector)
        issuer = conn.issuer
        aad_rt = _cred_aad(tenant, connector, issuer, "user", keelson_sub, "refresh")
        idp = "not_needed"
        async with tenant_session(tenant) as s:
            link = (await s.execute(
                text("SELECT status, ext_username FROM connector_identity_link "
                     "WHERE connector = :c AND keelson_sub = :k FOR UPDATE"),
                {"c": connector, "k": keelson_sub},
            )).mappings().first()
            row = (await s.execute(
                text(
                    "SELECT refresh_token_enc FROM connector_credential WHERE connector = :c AND issuer = :i "
                    "AND principal_type = 'user' AND principal_sub = :k FOR UPDATE"
                ),
                {"c": connector, "i": issuer, "k": keelson_sub},
            )).mappings().first()
            if row is not None and row["refresh_token_enc"]:
                refresh_token = decrypt_str(row["refresh_token_enc"], aad_rt)
                form = {"token": refresh_token, "token_type_hint": "refresh_token",
                        **self._assertion_form(conn.client_id("connect"), issuer)}
                try:
                    await self.provider(issuer).revoke(form)
                    idp = "revoked"
                except (TokenEndpointError, OidcError) as exc:
                    idp = "failed"
                    log.warning("delegated token revocation at the IdP failed; revoking locally",
                                tenant=tenant, connector=connector, error=type(exc).__name__,
                                status=getattr(exc, "status", None), oauth_error=getattr(exc, "error", None))
            if row is not None:
                await s.execute(
                    text(
                        "UPDATE connector_credential SET refresh_token_enc = NULL, access_token_enc = NULL, "
                        "access_expires_at = NULL, refresh_expires_at = NULL, updated_at = now() "
                        "WHERE connector = :c AND issuer = :i AND principal_type = 'user' AND principal_sub = :k"
                    ),
                    {"c": connector, "i": issuer, "k": keelson_sub},
                )
            revoked = link is not None and link["status"] != "revoked"
            if link is not None:
                await s.execute(
                    text("UPDATE connector_identity_link SET status = 'revoked', updated_at = now() "
                         "WHERE connector = :c AND keelson_sub = :k"),
                    {"c": connector, "k": keelson_sub},
                )
        self._cache.pop((tenant, connector, issuer, "user", keelson_sub), None)
        return {"revoked": revoked, "idp_revocation": idp,
                "ext_username": link["ext_username"] if link is not None else None}

    async def ext_identity(self, *, tenant: str, connector: str, keelson_sub: str) -> dict[str, Any]:
        """``{ext_iss, ext_sub, ext_username}`` of an active link (receipt claims); raises ConnectRequired."""
        link = await self._load_link(tenant, connector, keelson_sub)
        if link is None or link["status"] != "active":
            raise ConnectRequired(self.connect_url(connector))
        return {"ext_iss": link["ext_iss"], "ext_sub": link["ext_sub"], "ext_username": link["ext_username"]}


APPLY_SCOPE = "changes:apply"


def apply_ready(link: dict[str, Any]) -> bool:
    """Can this link approve (apply) changes? Active and granted ``changes:apply``: the single
    rule behind both ``GET /api/connectors/{slug}/link`` and the decision API's 412."""
    return link.get("status") == "active" and APPLY_SCOPE in (link.get("scopes") or [])


def _refresh_expiry(body: dict[str, Any]) -> dt.datetime | None:
    try:
        seconds = int(body.get("refresh_expires_in") or 0)
    except (TypeError, ValueError):
        return None
    if seconds <= 0:
        return None  # offline tokens: no fixed expiry
    return dt.datetime.fromtimestamp(time.time() + seconds, dt.UTC)


_broker: TokenBroker | None = None


def get_token_broker() -> TokenBroker:
    global _broker
    if _broker is None:
        _broker = TokenBroker()
    return _broker


def set_token_broker(broker: TokenBroker | None) -> None:
    global _broker
    _broker = broker
