"""Minimal OIDC/OAuth 2 client helpers for Keycloak realms (discovery, JWKS, token
endpoint, JWT verification). Authorization-request parameters (PKCE S256, state, nonce)
come from Authlib; HTTP goes through httpx so tests can mock it with respx.

Two instances exist per process: realm ``keelson`` (BFF login, direct HTTP) and realm
``merxa`` (connector principals, through the egress proxy).
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from keelson_engine.core.logs import get_logger

__all__ = [
    "OidcError",
    "OidcProvider",
    "PkcePair",
    "TokenEndpointError",
    "new_pkce",
    "random_token",
]

log = get_logger(__name__)

_DISCOVERY_TTL_S = 3600
_JWKS_MIN_REFRESH_S = 10


class OidcError(Exception):
    pass


class TokenEndpointError(OidcError):
    def __init__(self, status: int, error: str, description: str = "") -> None:
        super().__init__(f"token endpoint {status}: {error} {description}".strip())
        self.status = status
        self.error = error
        self.description = description


@dataclass(frozen=True, slots=True)
class PkcePair:
    verifier: str
    challenge: str


def random_token(length: int = 43) -> str:
    """URL-safe random string (Authlib's ``generate_token``: unreserved characters)."""
    return str(generate_token(length))


def new_pkce() -> PkcePair:
    verifier = str(generate_token(64))
    return PkcePair(verifier, create_s256_code_challenge(verifier))


class OidcProvider:
    def __init__(self, issuer: str, http: httpx.AsyncClient | None = None, *, http_getter: Any = None) -> None:
        self.issuer = issuer.rstrip("/")
        self._http = http
        self._http_getter = http_getter
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self._jwks: dict[str, Any] | None = None
        self._jwks_at = 0.0
        self._lock = asyncio.Lock()

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is not None:
            return self._http
        if self._http_getter is not None:
            return self._http_getter()  # type: ignore[no-any-return]
        raise RuntimeError("OidcProvider has no HTTP client")

    # ───────────────────────────── metadata ─────────────────────────────

    async def discovery(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._discovery is not None and now - self._discovery_at < _DISCOVERY_TTL_S:
            return self._discovery
        async with self._lock:
            if self._discovery is not None and time.monotonic() - self._discovery_at < _DISCOVERY_TTL_S:
                return self._discovery
            resp = await self.http.get(f"{self.issuer}/.well-known/openid-configuration")
            if resp.status_code != 200:
                raise OidcError(f"discovery failed: HTTP {resp.status_code}")
            data: dict[str, Any] = dict(resp.json())
            if data.get("issuer") != self.issuer:
                raise OidcError(f"discovery issuer mismatch: {data.get('issuer')!r} != {self.issuer!r}")
            self._discovery = data
            self._discovery_at = time.monotonic()
            return data

    async def endpoint(self, name: str) -> str:
        meta = await self.discovery()
        value = meta.get(name)
        if not value:
            raise OidcError(f"discovery has no {name}")
        return str(value)

    async def _fetch_jwks(self, *, force: bool) -> dict[str, Any]:
        now = time.monotonic()
        if self._jwks is not None and not force:
            return self._jwks
        if self._jwks is not None and force and now - self._jwks_at < _JWKS_MIN_REFRESH_S:
            return self._jwks
        uri = await self.endpoint("jwks_uri")
        resp = await self.http.get(uri)
        if resp.status_code != 200:
            raise OidcError(f"JWKS fetch failed: HTTP {resp.status_code}")
        self._jwks = resp.json()
        self._jwks_at = time.monotonic()
        return self._jwks

    async def signing_key(self, kid: str | None) -> jwt.PyJWK:
        for force in (False, True):
            jwks = await self._fetch_jwks(force=force)
            for k in jwks.get("keys", []):
                if k.get("use", "sig") == "sig" and (kid is None or k.get("kid") == kid):
                    return jwt.PyJWK.from_dict(k)
        raise OidcError(f"no signing key for kid {kid!r}")

    # ───────────────────────────── tokens ─────────────────────────────

    async def verify(
        self,
        token: str,
        *,
        audience: str | list[str] | None = None,
        nonce: str | None = None,
        leeway: int = 30,
    ) -> dict[str, Any]:
        """Verify signature (realm JWKS), ``iss``, ``exp``, optional ``aud`` and ``nonce``."""
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise OidcError(f"malformed JWT: {exc}") from exc
        alg = header.get("alg")
        if alg not in ("RS256", "RS384", "RS512", "ES256", "ES384", "PS256"):
            raise OidcError(f"unexpected JWT alg {alg!r}")
        key = await self.signing_key(header.get("kid"))
        options: jwt.types.Options = {"require": ["exp", "iat", "iss", "sub"], "verify_aud": audience is not None}
        try:
            claims: dict[str, Any] = jwt.decode(
                token,
                key=key.key,
                algorithms=[alg],
                audience=audience,
                issuer=self.issuer,
                leeway=leeway,
                options=options,
            )
        except jwt.PyJWTError as exc:
            raise OidcError(f"invalid token: {exc}") from exc
        if nonce is not None and claims.get("nonce") != nonce:
            raise OidcError("nonce mismatch")
        return claims

    async def token_request(self, form: dict[str, str]) -> dict[str, Any]:
        """POST the token endpoint; raises :class:`TokenEndpointError` on an OAuth error."""
        uri = await self.endpoint("token_endpoint")
        try:
            resp = await self.http.post(uri, data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OidcError(f"token endpoint unreachable: {exc.__class__.__name__}") from exc
        try:
            body = resp.json()
        except ValueError:
            body = {}
        if resp.status_code != 200 or "access_token" not in body:
            raise TokenEndpointError(
                resp.status_code, str(body.get("error") or "http_error"), str(body.get("error_description") or "")
            )
        return dict(body)

    async def revoke(self, form: dict[str, str]) -> None:
        """RFC 7009 token revocation (``revocation_endpoint`` from discovery).

        Raises :class:`TokenEndpointError` on an error response (RFC 7009 answers 200 also for
        an unknown / already invalid token) and :class:`OidcError` if unreachable."""
        uri = await self.endpoint("revocation_endpoint")
        try:
            resp = await self.http.post(uri, data=form, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise OidcError(f"revocation endpoint unreachable: {exc.__class__.__name__}") from exc
        if resp.status_code != 200:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            raise TokenEndpointError(
                resp.status_code, str(body.get("error") or "http_error"), str(body.get("error_description") or "")
            )

    async def authorization_url(self, params: dict[str, str]) -> str:
        base = await self.endpoint("authorization_endpoint")
        return f"{base}?{urlencode(params)}"


def token_expiry(body: dict[str, Any], key: str = "expires_in", default: int = 300) -> float:
    try:
        seconds = int(body.get(key) or default)
    except (TypeError, ValueError):
        seconds = default
    return time.time() + max(seconds, 0)
