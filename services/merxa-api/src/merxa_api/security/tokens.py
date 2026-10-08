"""Merxa-realm access-token verification built on FastMCP 4's ``JWTVerifier``.

The same verifier instance backs the MCP server (via ``RemoteAuthProvider``) and the
REST API's ``Authorization: Bearer`` handling, so both enforce identical rules:
signature (realm JWKS), ``iss == OIDC_ISSUER``, ``aud`` contains ``MCP_RESOURCE``, ``exp``.

Hardening on top of FastMCP 4.0.11's ``JWTVerifier`` (verified in site-packages):

* the base class only rejects an *expired* ``exp`` and silently accepts a token that has
  no ``exp`` at all; we require ``exp`` and ``sub`` (``sub`` keys idempotency records,
  receipts' ``ext_sub`` binding and audit attribution);
* Keycloak marks access tokens ``typ: Bearer`` - ID/refresh/logout tokens are refused;
* the base class re-downloads the JWKS for *every* token carrying an unknown ``kid``;
  we serve the last document for 10 s so forged ``kid`` values cannot hammer Keycloak.
"""

from __future__ import annotations

import time
from typing import Any

import httpx2
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.jwt import JWTVerifier

from merxa_api.config import Settings
from merxa_api.logs import get_logger

log = get_logger(__name__)

_DISCOVERY_TTL_S = 3600
_MIN_JWKS_REFETCH_S = 10.0


class DiscoveringJWTVerifier(JWTVerifier):
    """``JWTVerifier`` whose JWKS URI is resolved lazily from the issuer's discovery document.

    Keycloak may still be starting when merxa-api boots, so the ``jwks_uri`` is looked up
    on first use (and refreshed hourly) from ``{issuer}/.well-known/openid-configuration``.
    If discovery fails, the Keycloak convention ``{issuer}/protocol/openid-connect/certs``
    is used. An explicit ``OIDC_JWKS_URI`` disables discovery.
    """

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        algorithm: str,
        jwks_uri: str | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        self._discover = jwks_uri is None
        self._issuer_url = issuer.rstrip("/")
        self._discovered_at = 0.0
        self._jwks_doc: dict[str, Any] | None = None
        self._jwks_attempt_at = float("-inf")
        super().__init__(
            jwks_uri=jwks_uri or f"{self._issuer_url}/protocol/openid-connect/certs",
            issuer=issuer,
            audience=audience,
            algorithm=algorithm,
            http_client=http_client,
        )

    async def _resolve_jwks_uri(self) -> None:
        if not self._discover or time.time() - self._discovered_at < _DISCOVERY_TTL_S:
            return
        url = f"{self._issuer_url}/.well-known/openid-configuration"
        try:
            if self._http_client is not None:
                resp = await self._http_client.get(url)
            else:
                async with httpx2.AsyncClient(timeout=httpx2.Timeout(10.0)) as client:
                    resp = await client.get(url)
            resp.raise_for_status()
            doc: dict[str, Any] = resp.json()
            jwks_uri = doc.get("jwks_uri")
            if isinstance(jwks_uri, str) and jwks_uri.startswith(("http://", "https://")):
                self.jwks_uri = jwks_uri
            self._discovered_at = time.time()
        except Exception as exc:  # network / JSON errors: keep the conventional URI
            log.warning("oidc discovery failed; using conventional jwks_uri", extra={"error": str(exc)})

    async def _fetch_jwks(self) -> dict[str, Any]:
        now = time.monotonic()
        if now - self._jwks_attempt_at < _MIN_JWKS_REFETCH_S:
            if self._jwks_doc is not None:
                return self._jwks_doc
            raise ValueError("realm JWKS unavailable (refetch throttled)")
        self._jwks_attempt_at = now
        await self._resolve_jwks_uri()
        doc = await super()._fetch_jwks()
        self._jwks_doc = doc
        return doc

    async def load_access_token(self, token: str) -> AccessToken | None:
        access = await super().load_access_token(token)
        if access is None:
            return None
        claims = access.claims or {}
        exp = claims.get("exp")
        if isinstance(exp, bool) or not isinstance(exp, (int, float)):
            log.info("bearer token rejected: no exp claim")
            return None
        sub = claims.get("sub")
        if not isinstance(sub, str) or not sub:
            log.info("bearer token rejected: no sub claim", extra={"client_id": access.client_id})
            return None
        typ = claims.get("typ")
        if typ is not None and str(typ).lower() != "bearer":
            log.info("bearer token rejected: not an access token", extra={"typ": str(typ)})
            return None
        return access


def build_token_verifier(settings: Settings, http_client: httpx2.AsyncClient | None = None) -> DiscoveringJWTVerifier:
    return DiscoveringJWTVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.mcp_resource,
        algorithm=settings.oidc_token_alg,
        jwks_uri=settings.oidc_jwks_uri,
        http_client=http_client,
    )


async def verify_access_token(verifier: JWTVerifier, token: str) -> AccessToken | None:
    """Verify a bearer token; ``None`` when invalid for any reason."""
    return await verifier.verify_token(token)
