"""Test doubles: a fake Keycloak realm (RS256) and a fake Keelson receipt issuer (ES256).

Both are served through ``httpx``/``httpx2`` ``MockTransport`` so no network is used.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import httpx2
import jwt
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from jwt.algorithms import ECAlgorithm, RSAAlgorithm

ISSUER = "http://auth.localhost/realms/merxa"
RESOURCE = "http://merxa.localhost/mcp"
KEELSON_ISS = "http://keelson.localhost"
KEELSON_JWKS = "http://keelson.localhost/api/.well-known/jwks.json"

READ = ["catalog:read", "inventory:read", "po:read", "orders:read", "snapshot:read"]
WEBHOOK_URL = "http://keelson.localhost/api/webhooks/demo-retail/merxa"
# whsec_ + base64 (the base64 part is the HMAC key, Standard Webhooks)
WEBHOOK_SECRET = "whsec_dGVzdC13ZWJob29rLXNlY3JldC0wMTIzNDU2Nzg5"
MARCO_SUB = "6f1c2c3e-0000-4000-8000-00000000marco"
PRIYA_SUB = "6f1c2c3e-0000-4000-8000-00000000priya"


@dataclass
class FakeRealm:
    """Signs merxa-realm access/id tokens and serves discovery + JWKS + token endpoint."""

    kid: str = "kc-test-1"
    key: rsa.RSAPrivateKey = field(
        default_factory=lambda: rsa.generate_private_key(public_exponent=65537, key_size=2048)
    )
    jwks_fetches: int = 0
    users: dict[str, dict[str, Any]] = field(default_factory=dict)

    def jwk(self) -> dict[str, Any]:
        d = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        d.update({"kid": self.kid, "use": "sig", "alg": "RS256"})
        return d

    def token(
        self,
        *,
        sub: str = "svc-reader",
        scopes: list[str] | None = None,
        roles: list[str] | None = None,
        azp: str = "keelson-reader",
        username: str | None = None,
        name: str | None = None,
        aud: Any = RESOURCE,
        iss: str = ISSUER,
        exp_in: int = 300,
        extra: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": iss,
            "sub": sub,
            "aud": aud,
            "azp": azp,
            "iat": now,
            "exp": now + exp_in,
            "jti": str(uuid.uuid4()),
            "scope": " ".join(scopes if scopes is not None else READ),
            "realm_access": {"roles": roles or []},
            "preferred_username": username or f"service-account-{azp}",
        }
        if name:
            claims["name"] = name
        claims.update(extra or {})
        return jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": self.kid})

    def handle(self, method: str, url: str, form: dict[str, str]) -> tuple[int, dict[str, Any]] | None:
        if url == f"{ISSUER}/.well-known/openid-configuration":
            return 200, {
                "issuer": ISSUER,
                "authorization_endpoint": f"{ISSUER}/protocol/openid-connect/auth",
                "token_endpoint": f"{ISSUER}/protocol/openid-connect/token",
                "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
                "end_session_endpoint": f"{ISSUER}/protocol/openid-connect/logout",
            }
        if url == f"{ISSUER}/protocol/openid-connect/certs":
            self.jwks_fetches += 1
            return 200, {"keys": [self.jwk()]}
        if url == f"{ISSUER}/protocol/openid-connect/token" and method == "POST":
            user = self.users.get(form.get("username", ""))
            if form.get("grant_type") != "password" or user is None or form.get("password") != user["password"]:
                return 401, {"error": "invalid_grant"}
            access = self.token(
                sub=user["sub"],
                scopes=["openid", "profile", "email"],
                roles=user["roles"],
                azp=form["client_id"],
                username=form["username"],
                name=user["name"],
                aud="account",
            )
            id_token = self.token(
                sub=user["sub"],
                scopes=[],
                roles=[],
                azp=form["client_id"],
                username=form["username"],
                name=user["name"],
                aud=form["client_id"],
                extra={"email": user.get("email")},
            )
            return 200, {"access_token": access, "id_token": id_token, "token_type": "Bearer", "expires_in": 300}
        return None


@dataclass
class FakeKeelson:
    """Signs approval receipts (ES256) and serves the Keelson JWKS."""

    kid: str = "ar-1"
    key: ec.EllipticCurvePrivateKey = field(default_factory=lambda: ec.generate_private_key(ec.SECP256R1()))
    old_keys: list[tuple[str, ec.EllipticCurvePrivateKey]] = field(default_factory=list)
    jwks_fetches: int = 0

    def rotate(self, new_kid: str) -> None:
        self.old_keys.append((self.kid, self.key))
        self.kid, self.key = new_kid, ec.generate_private_key(ec.SECP256R1())

    def jwks(self) -> dict[str, Any]:
        keys = []
        for kid, key in [(self.kid, self.key), *self.old_keys]:
            d = json.loads(ECAlgorithm.to_jwk(key.public_key()))
            d.update({"kid": kid, "use": "sig", "alg": "ES256"})
            keys.append(d)
        return {"keys": keys}

    def receipt(
        self,
        *,
        change_set_ref: str,
        change_set_hash: str,
        ext_sub: str = MARCO_SUB,
        approved_by: str = "user:marco",
        approval_id: str = "APR-0001",
        typ: str = "approval-receipt+jwt",
        iss: str = KEELSON_ISS,
        aud: str = RESOURCE,
        exp_in: int = 300,
        kid: str | None = None,
        key: ec.EllipticCurvePrivateKey | None = None,
        overrides: dict[str, Any] | None = None,
    ) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": iss,
            "aud": aud,
            "iat": now,
            "exp": now + exp_in,
            "jti": str(uuid.uuid4()),
            "approval_id": approval_id,
            "change_set_ref": change_set_ref,
            "change_set_hash": change_set_hash,
            "tenant": "demo-retail",
            "keelson_sub": "keelson-marco",
            "ext_iss": ISSUER,
            "ext_sub": ext_sub,
            "approved_by": approved_by,
            "approver_name": "Marco Rossi",
            "initiated_by": "Priya Shah",
            "policy_version": "p-1",
        }
        claims.update(overrides or {})
        return jwt.encode(claims, key or self.key, algorithm="ES256", headers={"kid": kid or self.kid, "typ": typ})


def _route(realm: FakeRealm, keelson: FakeKeelson, method: str, url: str, body: bytes) -> tuple[int, dict[str, Any]]:
    form: dict[str, str] = {}
    if body:
        from urllib.parse import parse_qsl

        form = dict(parse_qsl(body.decode()))
    if url == KEELSON_JWKS:
        keelson.jwks_fetches += 1
        return 200, keelson.jwks()
    hit = realm.handle(method, url, form)
    if hit is not None:
        return hit
    return 404, {"error": "not_found", "url": url}


def httpx_client(realm: FakeRealm, keelson: FakeKeelson) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url).split("?")[0]
        status, data = _route(realm, keelson, request.method, url, request.content)
        return httpx.Response(status, json=data)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def httpx2_client(realm: FakeRealm, keelson: FakeKeelson) -> httpx2.AsyncClient:
    def handler(request: httpx2.Request) -> httpx2.Response:
        url = str(request.url).split("?")[0]
        status, data = _route(realm, keelson, request.method, url, request.content)
        return httpx2.Response(status, json=data)

    return httpx2.AsyncClient(transport=httpx2.MockTransport(handler))


@dataclass
class FakeReceiver:
    """Webhook endpoint double: records every POST; answers with queued status codes (200 when empty)."""

    requests: list[httpx.Request] = field(default_factory=list)
    statuses: list[int] = field(default_factory=list)
    fail_with: type[Exception] | None = None

    def client(self) -> httpx.AsyncClient:
        def handler(request: httpx.Request) -> httpx.Response:
            request.read()
            self.requests.append(request)
            if self.fail_with is not None:
                raise self.fail_with("receiver unreachable", request=request)
            status = self.statuses.pop(0) if self.statuses else 200
            return httpx.Response(status)

        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def reset(self) -> None:
        self.requests.clear()
        self.statuses.clear()
        self.fail_with = None
