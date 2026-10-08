"""Inbound auth for ``/mcp`` (contract 122 §5.5).

* **Static header keys** (``auth.static_header``): each key's value comes from the environment
  variable its config names (``key_env``, ≥ 32 characters); only its sha256 is kept and every
  presented token is compared with *every* key in constant time (``hmac.compare_digest``). The
  configured header (default ``X-Adapter-Key``) is mapped to ``Authorization: Bearer`` by
  :class:`HeaderKeyMiddleware` (an explicit ``Authorization`` wins), so Keelson's
  ``static_header`` connector auth and plain bearer clients both work. The access token carries
  ``client_id = key:<name>`` and the key's configured scopes.
* **OAuth** (``auth.oauth``): JWT bearer tokens verified by FastMCP's ``JWTVerifier`` (issuer,
  JWKS, ``aud`` = ``MCP_RESOURCE``); a ``RemoteAuthProvider`` serves the RFC 9728 protected
  resource metadata at ``/.well-known/oauth-protected-resource/mcp`` — matching Keelson's
  ``oauth_client_credentials`` connector auth.
* Per-tool scopes are enforced when a tool is *called* (``mcp_server._authorize``); ``tools/list``
  shows every tool (Keelson snapshots the list once).
* ``/v1/admin/*`` uses a separate ``SIDECAR_ADMIN_KEY`` (``X-Admin-Key``) that Keelson never gets.

Never log a header value, key or token: only key *names*.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from fastmcp.server.auth import AccessToken, AuthProvider, RemoteAuthProvider, TokenVerifier
from fastmcp.server.auth.providers.jwt import JWTVerifier
from pydantic import AnyHttpUrl
from starlette.types import ASGIApp, Receive, Scope, Send

from adapter_sidecar.config import SCOPES, AuthSpec
from adapter_sidecar.errors import ConfigError
from adapter_sidecar.logs import get_logger
from adapter_sidecar.settings import MIN_SECRET_CHARS, Settings

log = get_logger(__name__)

ADMIN_HEADER = "x-admin-key"


@dataclass(frozen=True, slots=True)
class Principal:
    client_id: str
    subject: str
    scopes: frozenset[str]

    def has_any(self, *scopes: str) -> bool:
        return bool(self.scopes & set(scopes))

    @classmethod
    def of(cls, token: AccessToken) -> Principal:
        claims = token.claims or {}
        sub = str(claims.get("sub") or token.client_id)
        return cls(client_id=str(token.client_id), subject=sub, scopes=frozenset(token.scopes or ()))


@dataclass(frozen=True, slots=True)
class _Key:
    name: str
    digest: bytes
    scopes: tuple[str, ...]


def digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def load_keys(spec: AuthSpec, settings: Settings) -> list[_Key]:
    if spec.static_header is None:
        return []
    keys: list[_Key] = []
    for k in spec.static_header.keys:
        value = settings.secret(k.key_env)
        if value is None:
            raise ConfigError(f"{k.key_env} (key {k.name!r}) is not set")
        if len(value) < MIN_SECRET_CHARS:
            raise ConfigError(f"{k.key_env} (key {k.name!r}) must have at least {MIN_SECRET_CHARS} characters")
        keys.append(_Key(k.name, digest(value), tuple(k.scopes)))
    return keys


class SidecarTokenVerifier(TokenVerifier):
    """Static keys first (constant time over all of them), then the optional JWT verifier."""

    def __init__(self, keys: Sequence[_Key], jwt_verifier: JWTVerifier | None = None, *,
                 scopes_claim: str = "scope", base_url: str | None = None,
                 resource_base_url: str | None = None) -> None:
        super().__init__(base_url=base_url, resource_base_url=resource_base_url)
        self._keys = list(keys)
        self._jwt = jwt_verifier
        self._scopes_claim = scopes_claim

    async def verify_token(self, token: str) -> AccessToken | None:
        presented = digest(token)
        match: _Key | None = None
        for key in self._keys:
            if hmac.compare_digest(key.digest, presented) and match is None:
                match = key
        if match is not None:
            return AccessToken(token=token, client_id=f"key:{match.name}", scopes=list(match.scopes),
                               expires_at=None, claims={"sub": f"key:{match.name}"})
        if self._jwt is None:
            return None
        access = await self._jwt.verify_token(token)
        if access is None:
            return None
        claims = access.claims or {}
        if not isinstance(claims.get("exp"), int) or not isinstance(claims.get("sub"), str):
            return None  # FastMCP accepts a token without exp; we do not
        raw = claims.get(self._scopes_claim)
        scopes = raw.split() if isinstance(raw, str) else [str(s) for s in raw] if isinstance(raw, list) else []
        known = [s for s in scopes if s in SCOPES]
        return access.model_copy(update={"scopes": known})


def build_auth(spec: AuthSpec, settings: Settings, *, http_client: Any = None) -> AuthProvider:
    keys = load_keys(spec, settings)
    if spec.oauth is None:
        return SidecarTokenVerifier(keys)
    jwt_verifier = JWTVerifier(
        jwks_uri=spec.oauth.jwks_url, issuer=spec.oauth.issuer, audience=spec.oauth.audience or settings.mcp_resource,
        algorithm=None, http_client=http_client,
    )
    verifier = SidecarTokenVerifier(keys, jwt_verifier, scopes_claim=spec.oauth.scopes_claim)
    return RemoteAuthProvider(
        token_verifier=verifier,
        authorization_servers=[AnyHttpUrl(spec.oauth.issuer)],
        base_url=settings.public_url,
        resource_base_url=settings.public_url,
        scopes_supported=list(SCOPES),
        resource_name=f"{settings.public_origin} adapter sidecar",
    )


class HeaderKeyMiddleware:
    """Maps the static key header (e.g. ``X-Adapter-Key``) to ``Authorization: Bearer``."""

    def __init__(self, app: ASGIApp, header: str) -> None:
        self.app = app
        self.header = header.lower().encode("latin-1")

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = list(scope.get("headers", []))
            names = {k.lower() for k, _ in headers}
            key = next((v for k, v in headers if k.lower() == self.header), None)
            if key is not None and b"authorization" not in names:
                headers.append((b"authorization", b"Bearer " + key.strip()))
                scope = {**scope, "headers": headers}
        await self.app(scope, receive, send)


def admin_ok(settings: Settings, presented: str | None) -> bool:
    if not settings.admin_key or not presented:
        return False
    return hmac.compare_digest(digest(settings.admin_key), digest(presented))
