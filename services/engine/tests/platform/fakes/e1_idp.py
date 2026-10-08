"""In-memory Keycloak realm for platform tests (served through respx).

Implements discovery, JWKS, and the token endpoint grants Keelson uses:
``authorization_code`` (PKCE S256), ``password``, ``client_credentials``, ``refresh_token``
(rotating), plus RFC 7009 revocation (``/revoke``). Clients authenticate with ``client_secret`` or ``private_key_jwt`` (assertion
verified against Keelson's real JWKS from ``core.crypto.jwks()``).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import time
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

import httpx
import jwt
import respx
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


@dataclass
class User:
    username: str
    sub: str
    password: str
    name: str
    roles: list[str]
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Client:
    client_id: str
    secret: str | None = None
    private_key_jwt: bool = False
    default_scope: str = ""
    service_roles: list[str] = field(default_factory=list)
    extra_claims: dict[str, Any] = field(default_factory=dict)
    audience: str | None = None


class FakeRealm:
    def __init__(self, issuer: str, *, jwks_provider: Any = None) -> None:
        self.issuer = issuer
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = f"kid-{uuid.uuid4().hex[:8]}"
        self.users: dict[str, User] = {}
        self.clients: dict[str, Client] = {}
        self.codes: dict[str, dict[str, Any]] = {}
        self.refresh_tokens: dict[str, dict[str, Any]] = {}
        self.calls: list[dict[str, Any]] = []
        self.refresh_delay = 0.0
        self.revoked_refresh: set[str] = set()
        self.access_ttl = 300
        self.jwks_provider = jwks_provider
        self.assertion_jtis: set[str] = set()
        self.revocation_status = 200

    # ───────────────────────────── setup ─────────────────────────────

    def add_user(self, username: str, *, roles: list[str], name: str, password: str = "pw", **extra: Any) -> User:
        user = User(username, f"sub-{username}", password, name, roles, extra)
        self.users[username] = user
        return user

    def add_client(self, client_id: str, **kw: Any) -> Client:
        c = Client(client_id, **kw)
        self.clients[client_id] = c
        return c

    @property
    def token_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/token"

    @property
    def revocation_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/revoke"

    @property
    def auth_endpoint(self) -> str:
        return f"{self.issuer}/protocol/openid-connect/auth"

    def install(self, router: respx.Router) -> None:
        router.get(f"{self.issuer}/.well-known/openid-configuration").mock(side_effect=self._discovery)
        router.get(f"{self.issuer}/protocol/openid-connect/certs").mock(side_effect=self._certs)
        router.post(self.token_endpoint).mock(side_effect=self._token)
        router.post(self.revocation_endpoint).mock(side_effect=self._revoke)

    def _discovery(self, _req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": self.issuer,
                "authorization_endpoint": self.auth_endpoint,
                "token_endpoint": self.token_endpoint,
                "revocation_endpoint": self.revocation_endpoint,
                "jwks_uri": f"{self.issuer}/protocol/openid-connect/certs",
                "end_session_endpoint": f"{self.issuer}/protocol/openid-connect/logout",
            },
        )

    def jwks(self) -> dict[str, Any]:
        jwk = RSAAlgorithm.to_jwk(self.key.public_key(), as_dict=True)
        jwk.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return {"keys": [jwk]}

    def _certs(self, _req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=self.jwks())

    # ───────────────────────────── tokens ─────────────────────────────

    def mint(self, claims: dict[str, Any], ttl: int | None = None) -> str:
        now = int(time.time())
        body = {"iss": self.issuer, "iat": now, "exp": now + (ttl or self.access_ttl), **claims}
        return jwt.encode(body, self.key, algorithm="RS256", headers={"kid": self.kid})

    def authorize(self, params: dict[str, str], username: str) -> str:
        """Simulate the browser leg: validate the authorization request, return a code."""
        client = self.clients[params["client_id"]]
        assert params["response_type"] == "code"
        assert params["code_challenge_method"] == "S256"
        code = secrets.token_urlsafe(16)
        self.codes[code] = {
            "client_id": client.client_id,
            "redirect_uri": params["redirect_uri"],
            "challenge": params["code_challenge"],
            "nonce": params.get("nonce"),
            "username": username,
            "scope": params.get("scope", ""),
        }
        return code

    def _authenticate_client(self, form: dict[str, str]) -> Client | None:
        client = self.clients.get(form.get("client_id", ""))
        if client is None:
            return None
        if client.private_key_jwt:
            if form.get("client_assertion_type") != ASSERTION_TYPE:
                return None
            assertion = form.get("client_assertion", "")
            try:
                header = jwt.get_unverified_header(assertion)
                keys = {k["kid"]: k for k in self.jwks_provider()["keys"]}
                key = jwt.PyJWK.from_dict(keys[header["kid"]])
                claims = jwt.decode(assertion, key.key, algorithms=["ES256"], audience=self.issuer)
            except Exception:  # noqa: BLE001
                return None
            if header.get("kid") != "ca-1" or claims["iss"] != client.client_id or claims["sub"] != client.client_id:
                return None
            if claims["exp"] - claims["iat"] > 120 or claims["jti"] in self.assertion_jtis:
                return None
            self.assertion_jtis.add(claims["jti"])
            return client
        if client.secret is not None and form.get("client_secret") == client.secret:
            return client
        return None

    def _issue(self, client: Client, user: User | None, scope: str, *, nonce: str | None = None,
               refresh: bool = True) -> dict[str, Any]:
        now = int(time.time())
        scopes = sorted(set(scope.split()) | set(client.default_scope.split()))
        sub = user.sub if user else f"service-account-{client.client_id}"
        claims: dict[str, Any] = {
            "sub": sub,
            "azp": client.client_id,
            "typ": "Bearer",
            "scope": " ".join(scopes),
            "roles": list(user.roles if user else client.service_roles),
            "preferred_username": user.username if user else f"service-account-{client.client_id}",
            "jti": uuid.uuid4().hex,
        }
        if client.audience:
            claims["aud"] = [client.audience, "account"]
        if user:
            claims.update({"name": user.name, "email": f"{user.username}@example.test", **user.extra})
        claims.update(client.extra_claims)
        body: dict[str, Any] = {
            "access_token": self.mint(claims),
            "token_type": "Bearer",
            "expires_in": self.access_ttl,
            "scope": " ".join(scopes),
        }
        if user and "openid" in scopes:
            id_claims = {
                "sub": sub, "aud": client.client_id, "azp": client.client_id, "auth_time": now,
                "preferred_username": user.username, "name": user.name, **user.extra,
            }
            if nonce:
                id_claims["nonce"] = nonce
            body["id_token"] = self.mint(id_claims)
        if user and refresh:
            rt = secrets.token_urlsafe(24)
            self.refresh_tokens[rt] = {"username": user.username, "client_id": client.client_id, "scope": scope}
            body["refresh_token"] = rt
            body["refresh_expires_in"] = 0 if "offline_access" in scopes else 1800
        return body

    async def _token(self, request: httpx.Request) -> httpx.Response:
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        grant = form.get("grant_type", "")
        self.calls.append({"grant_type": grant, "form": form})
        client = self._authenticate_client(form)
        if client is None:
            return httpx.Response(401, json={"error": "invalid_client"})
        if grant == "authorization_code":
            info = self.codes.pop(form.get("code", ""), None)
            redirect_ok = info is not None and info["redirect_uri"] == form.get("redirect_uri")
            if info is None or info["client_id"] != client.client_id or not redirect_ok:
                return httpx.Response(400, json={"error": "invalid_grant"})
            verifier = form.get("code_verifier", "")
            challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
            if challenge != info["challenge"]:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "PKCE"})
            user = self.users[info["username"]]
            return httpx.Response(200, json=self._issue(client, user, info["scope"], nonce=info["nonce"]))
        if grant == "password":
            user = self.users.get(form.get("username", ""))
            if user is None or user.password != form.get("password"):
                return httpx.Response(401, json={"error": "invalid_grant"})
            return httpx.Response(200, json=self._issue(client, user, form.get("scope", "")))
        if grant == "client_credentials":
            return httpx.Response(200, json=self._issue(client, None, form.get("scope", ""), refresh=False))
        if grant == "refresh_token":
            if self.refresh_delay:
                await asyncio.sleep(self.refresh_delay)
            rt = form.get("refresh_token", "")
            info = self.refresh_tokens.pop(rt, None)
            if info is None or rt in self.revoked_refresh or info["client_id"] != client.client_id:
                return httpx.Response(400, json={"error": "invalid_grant", "error_description": "Token is not active"})
            user = self.users[info["username"]]
            return httpx.Response(200, json=self._issue(client, user, info["scope"]))
        return httpx.Response(400, json={"error": "unsupported_grant_type"})

    async def _revoke(self, request: httpx.Request) -> httpx.Response:
        """RFC 7009: 200 for valid and unknown tokens alike; client auth required."""
        form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
        self.calls.append({"grant_type": "revoke", "form": form})
        if self.revocation_status != 200:
            return httpx.Response(self.revocation_status, json={"error": "server_error"})
        client = self._authenticate_client(form)
        if client is None:
            return httpx.Response(401, json={"error": "invalid_client"})
        token = form.get("token", "")
        info = self.refresh_tokens.get(token)
        if info is not None and info["client_id"] == client.client_id:
            self.refresh_tokens.pop(token, None)
            self.revoked_refresh.add(token)
        return httpx.Response(200)

    def grants(self, grant: str) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["grant_type"] == grant]
