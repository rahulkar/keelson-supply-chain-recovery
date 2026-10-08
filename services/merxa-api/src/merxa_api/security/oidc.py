"""Minimal OIDC relying-party client for the Merxa UI BFF (realm ``merxa``).

Used for the authorization-code + PKCE login of ``merxa-web`` and the demo persona
switcher's password grant on ``merxa-demo``. Tokens are verified with the realm JWKS.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
import time
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from merxa_api.config import Settings
from merxa_api.errors import Problem

_DISCOVERY_TTL_S = 3600
_JWKS_TTL_S = 300


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


class OidcClient:
    def __init__(self, settings: Settings, http_client: httpx.AsyncClient | None = None) -> None:
        self.settings = settings
        self._client = http_client
        self._discovery: dict[str, Any] | None = None
        self._discovery_at = 0.0
        self._jwks: dict[str, Any] = {}
        self._jwks_at = 0.0

    async def _get(self, url: str) -> httpx.Response:
        if self._client is not None:
            return await self._client.get(url)
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            return await client.get(url)

    async def _post(self, url: str, data: dict[str, str]) -> httpx.Response:
        if self._client is not None:
            return await self._client.post(url, data=data)
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0)) as client:
            return await client.post(url, data=data)

    async def discovery(self) -> dict[str, Any]:
        if self._discovery is None or time.monotonic() - self._discovery_at > _DISCOVERY_TTL_S:
            try:
                resp = await self._get(f"{self.settings.oidc_issuer}/.well-known/openid-configuration")
                resp.raise_for_status()
                self._discovery = resp.json()
                self._discovery_at = time.monotonic()
            except (httpx.HTTPError, ValueError) as exc:
                raise Problem(503, "idp_unavailable", f"identity provider unavailable: {exc}") from exc
        return self._discovery

    async def _key(self, kid: str | None) -> Any:
        if not self._jwks or kid not in self._jwks or time.monotonic() - self._jwks_at > _JWKS_TTL_S:
            disc = await self.discovery()
            uri = self.settings.oidc_jwks_uri or disc.get("jwks_uri")
            try:
                resp = await self._get(str(uri))
                resp.raise_for_status()
                keys = {}
                for jwk in resp.json().get("keys", []):
                    if jwk.get("use") in (None, "sig") and jwk.get("kid"):
                        try:
                            keys[jwk["kid"]] = jwt.PyJWK.from_dict(jwk)
                        except jwt.PyJWTError:
                            continue
                self._jwks, self._jwks_at = keys, time.monotonic()
            except (httpx.HTTPError, ValueError) as exc:
                raise Problem(503, "idp_unavailable", f"cannot fetch realm JWKS: {exc}") from exc
        if kid not in self._jwks:
            raise Problem(401, "invalid_token", "token signed by unknown key")
        return self._jwks[kid]

    async def verify(self, token: str, *, audience: str | None, nonce: str | None = None) -> dict[str, Any]:
        try:
            header = jwt.get_unverified_header(token)
            key = await self._key(header.get("kid"))
            claims: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=[self.settings.oidc_token_alg],
                issuer=self.settings.oidc_issuer,
                audience=audience,
                options={"verify_aud": audience is not None, "require": ["exp", "iat", "sub"]},
                leeway=30,
            )
        except jwt.PyJWTError as exc:
            raise Problem(401, "invalid_token", f"token rejected: {exc}") from exc
        if nonce is not None and claims.get("nonce") != nonce:
            raise Problem(401, "invalid_token", "nonce mismatch")
        return claims

    async def authorize_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        disc = await self.discovery()
        params = {
            "client_id": self.settings.web_client_id,
            "response_type": "code",
            "scope": "openid profile email",
            "redirect_uri": f"{self.settings.public_url}/v1/auth/callback",
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return f"{disc['authorization_endpoint']}?{urlencode(params)}"

    async def _token(self, data: dict[str, str]) -> dict[str, Any]:
        disc = await self.discovery()
        try:
            resp = await self._post(disc["token_endpoint"], data)
        except httpx.HTTPError as exc:
            raise Problem(503, "idp_unavailable", f"token endpoint unreachable: {exc}") from exc
        if resp.status_code != 200:
            try:
                err = resp.json().get("error", "invalid_grant")
            except ValueError:
                err = "invalid_grant"
            raise Problem(401, "login_failed", f"identity provider rejected the request ({err})")
        return resp.json()

    async def exchange_code(self, code: str, code_verifier: str) -> dict[str, Any]:
        if not self.settings.web_client_secret:
            raise Problem(503, "not_configured", "MERXA_WEB_CLIENT_SECRET is not configured")
        return await self._token(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": f"{self.settings.public_url}/v1/auth/callback",
                "client_id": self.settings.web_client_id,
                "client_secret": self.settings.web_client_secret,
                "code_verifier": code_verifier,
            }
        )

    async def password_grant(self, username: str, password: str) -> dict[str, Any]:
        if not self.settings.demo_client_secret:
            raise Problem(503, "not_configured", "MERXA_DEMO_CLIENT_SECRET is not configured")
        return await self._token(
            {
                "grant_type": "password",
                "client_id": self.settings.demo_client_id,
                "client_secret": self.settings.demo_client_secret,
                "username": username,
                "password": password,
                "scope": "openid profile email",
            }
        )

    async def end_session_url(self, id_token: str | None) -> str | None:
        try:
            disc = await self.discovery()
        except Problem:
            return None
        endpoint = disc.get("end_session_endpoint")
        if not endpoint:
            return None
        params = {"client_id": self.settings.web_client_id}
        if id_token:
            params["id_token_hint"] = id_token
        return f"{endpoint}?{urlencode(params)}"
