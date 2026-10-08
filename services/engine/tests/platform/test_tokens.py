"""TokenBroker against a mocked merxa realm (respx): private_key_jwt client credentials,
caching, delegated refresh single-flight (row lock) with rotation, invalid_grant → ConnectRequired."""

from __future__ import annotations

import asyncio
import datetime as dt
from typing import Any
from urllib.parse import parse_qs

import jwt
import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_env import MERXA_ISS, MERXA_MCP, TENANT_A
from e1_idp import FakeRealm
from keelson_engine.core import connectors
from keelson_engine.core.db import tenant_session
from keelson_engine.core.tokens import ConnectRequired, TokenBroker, TokenError

pytestmark = pytest.mark.asyncio(loop_scope="session")

MERXA_SPEC: dict[str, Any] = {
    "slug": "merxa",
    "name": "Merxa Retail Suite",
    "mcp_url": MERXA_MCP,
    "issuer": MERXA_ISS,
    "trust_level": "first_party",
    "auth": {
        "reader_client": "keelson-reader",
        "preparer_client": "keelson-preparer",
        "autocommit_client": "keelson-autocommit",
        "connect_client": "keelson-connect",
    },
    "lanes": [{"from_dc": "DE1", "to_dc": "NL1", "transit_days": 2, "cost_per_unit_cents": 35, "max_units_wk": 20000}],
}


@pytest_asyncio.fixture(loop_scope="session")
async def merxa(platform: None) -> None:
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)


async def _link(broker: TokenBroker, realm: FakeRealm, keelson_sub: str, username: str = "marco.rossi") -> None:
    """Simulate a completed Connect-Merxa flow (code exchange) and store the link."""
    conn = await connectors.get_connector(TENANT_A, "merxa")
    code = realm.authorize(
        {"client_id": "keelson-connect", "response_type": "code", "redirect_uri": "cb",
         "code_challenge_method": "S256", "code_challenge": "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM",
         "scope": "openid offline_access changes:apply", "nonce": "n"},
        username,
    )
    body = await broker.token_request(conn, "keelson-connect", {
        "grant_type": "authorization_code", "code": code, "redirect_uri": "cb",
        "code_verifier": "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk", "resource": MERXA_MCP,
    })
    claims = jwt.decode(body["access_token"], options={"verify_signature": False})
    await broker.store_user_link(conn=conn, keelson_sub=keelson_sub, token_body=body, ext_sub=claims["sub"],
                                 ext_username=username, roles=claims["roles"], scopes=body["scope"].split())


async def _expire_access(keelson_sub: str) -> None:
    async with tenant_session(TENANT_A) as s:
        await s.execute(
            text("UPDATE connector_credential SET access_expires_at = :t WHERE principal_sub = :k"),
            {"t": dt.datetime.now(dt.UTC) - dt.timedelta(seconds=5), "k": keelson_sub},
        )


async def test_service_token_private_key_jwt_with_resource_and_cache(merxa: None, merxa_realm: FakeRealm) -> None:
    broker = TokenBroker()
    before = len(merxa_realm.grants("client_credentials"))
    tokens = await asyncio.gather(*[broker.service_token(tenant=TENANT_A, connector="merxa", principal="reader")
                                    for _ in range(5)])
    assert len(set(tokens)) == 1
    calls = merxa_realm.grants("client_credentials")[before:]
    assert len(calls) == 1
    form = calls[0]["form"]
    assert form["client_id"] == "keelson-reader" and form["resource"] == MERXA_MCP
    assert form["client_assertion_type"] == "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
    assert "client_secret" not in form
    claims = jwt.decode(tokens[0], options={"verify_signature": False})
    assert claims["azp"] == "keelson-reader" and "inventory:read" in claims["scope"]
    # different principal → different client
    prep = await broker.service_token(tenant=TENANT_A, connector="merxa", principal="preparer")
    assert jwt.decode(prep, options={"verify_signature": False})["azp"] == "keelson-preparer"
    with pytest.raises(ValueError):
        await broker.service_token(tenant=TENANT_A, connector="merxa", principal="admin")  # type: ignore[arg-type]


async def test_service_token_refetched_near_expiry(merxa: None, merxa_realm: FakeRealm) -> None:
    broker = TokenBroker()
    merxa_realm.access_ttl = 20  # < 30 s skew → never cached
    try:
        before = len(merxa_realm.grants("client_credentials"))
        await broker.service_token(tenant=TENANT_A, connector="merxa", principal="autocommit")
        await broker.service_token(tenant=TENANT_A, connector="merxa", principal="autocommit")
        assert len(merxa_realm.grants("client_credentials")) - before == 2
    finally:
        merxa_realm.access_ttl = 300


async def test_service_token_failure_is_token_error(merxa: None, merxa_realm: FakeRealm) -> None:
    broker = TokenBroker()
    client = merxa_realm.clients.pop("keelson-autocommit")
    try:
        with pytest.raises(TokenError):
            await broker.service_token(tenant=TENANT_A, connector="merxa", principal="autocommit")
    finally:
        merxa_realm.clients["keelson-autocommit"] = client


async def test_user_token_requires_link(merxa: None) -> None:
    broker = TokenBroker()
    with pytest.raises(ConnectRequired) as exc:
        await broker.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-nobody")
    assert exc.value.connect_url == "http://keelson.localhost/api/connectors/merxa/connect"
    status = await broker.link_status(tenant=TENANT_A, connector="merxa", keelson_sub="sub-nobody")
    assert status == {"status": "missing", "ext_username": None, "roles": [], "scopes": [], "apply_ready": False}


async def test_link_stores_encrypted_credentials(merxa: None, merxa_realm: FakeRealm) -> None:
    broker = TokenBroker()
    await _link(broker, merxa_realm, "sub-marco-1")
    status = await broker.link_status(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-1")
    assert status["status"] == "active" and status["ext_username"] == "marco.rossi"
    assert "changes:apply" in status["scopes"] and "ops-director" in status["roles"]
    async with tenant_session(TENANT_A) as s:
        row = (await s.execute(text(
            "SELECT refresh_token_enc, access_token_enc, key_id FROM connector_credential WHERE principal_sub = :k"),
            {"k": "sub-marco-1"})).one()
    assert row[0].startswith("dk-1:") and row[1].startswith("dk-1:") and row[2] == "dk-1"
    ident = await broker.ext_identity(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-1")
    assert ident["ext_sub"] == "sub-marco.rossi" and ident["ext_iss"] == MERXA_ISS
    # cached access token served without a refresh
    before = len(merxa_realm.grants("refresh_token"))
    token = await broker.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-1")
    assert jwt.decode(token, options={"verify_signature": False})["sub"] == "sub-marco.rossi"
    assert len(merxa_realm.grants("refresh_token")) == before


async def test_refresh_is_single_flight_across_processes(merxa: None, merxa_realm: FakeRealm) -> None:
    await _link(TokenBroker(), merxa_realm, "sub-marco-2")
    await _expire_access("sub-marco-2")
    merxa_realm.refresh_delay = 0.3
    try:
        before = len(merxa_realm.grants("refresh_token"))
        brokers = [TokenBroker() for _ in range(4)]  # four "processes" with empty in-memory caches
        tokens = await asyncio.gather(*[
            b.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-2") for b in brokers
        ])
    finally:
        merxa_realm.refresh_delay = 0.0
    calls = merxa_realm.grants("refresh_token")[before:]
    assert len(calls) == 1
    assert len(set(tokens)) == 1
    form = calls[0]["form"]
    assert form["client_id"] == "keelson-connect" and form["resource"] == MERXA_MCP and "client_assertion" in form
    # the rotated refresh token was stored: the next refresh uses it and succeeds
    await _expire_access("sub-marco-2")
    fresh = TokenBroker()
    await fresh.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-2")
    latest = merxa_realm.grants("refresh_token")[-1]["form"]["refresh_token"]
    assert latest != form["refresh_token"]


async def test_invalid_grant_expires_link(merxa: None, merxa_realm: FakeRealm) -> None:
    await _link(TokenBroker(), merxa_realm, "sub-marco-3")
    await _expire_access("sub-marco-3")
    merxa_realm.refresh_tokens.clear()  # revoked at the IdP
    broker = TokenBroker()
    with pytest.raises(ConnectRequired) as exc:
        await broker.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-3")
    assert exc.value.reason == "link_expired"
    status = await broker.link_status(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-3")
    assert status["status"] == "expired"
    with pytest.raises(ConnectRequired):
        await broker.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-3")
    async with tenant_session(TENANT_A) as s:
        enc = (await s.execute(text(
            "SELECT refresh_token_enc FROM connector_credential WHERE principal_sub = 'sub-marco-3'"))).scalar_one()
    assert enc is None


async def test_client_assertions_are_unique(merxa: None, merxa_realm: FakeRealm) -> None:
    broker = TokenBroker()
    for principal in ("reader", "preparer"):
        await broker.service_token(tenant=TENANT_A, connector="merxa", principal=principal)  # type: ignore[arg-type]
    assertions = [parse_qs(f"client_assertion={c['form']['client_assertion']}")["client_assertion"][0]
                  for c in merxa_realm.calls if "client_assertion" in c["form"]]
    jtis = [jwt.decode(a, options={"verify_signature": False})["jti"] for a in assertions]
    assert len(jtis) == len(set(jtis))


async def test_user_token_cache_follows_the_identity_link(merxa: None, merxa_realm: FakeRealm) -> None:
    """Regression: an in-process cached delegated token is never served after the link was
    expired by another process or re-connected to a different Merxa account."""
    broker, other = TokenBroker(), TokenBroker()
    await _link(broker, merxa_realm, "sub-marco-4")
    first = await broker.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-4")
    assert await other.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-4") == first

    # another process re-links the same Keelson user to a different Merxa account
    await _link(TokenBroker(), merxa_realm, "sub-marco-4", username="priya.shah")
    relinked = await other.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-4")
    assert relinked != first
    claims = jwt.decode(relinked, options={"verify_signature": False})
    assert claims["preferred_username"] == "priya.shah"

    # another process expires the link: cached tokens are not served any more
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE connector_identity_link SET status = 'expired' WHERE keelson_sub = 'sub-marco-4'"))
    for b in (broker, other):
        with pytest.raises(ConnectRequired) as exc:
            await b.user_token(tenant=TENANT_A, connector="merxa", keelson_sub="sub-marco-4")
        assert exc.value.reason == "link_expired"
