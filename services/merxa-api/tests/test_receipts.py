"""Approval-receipt verification rules (docs/contracts/10-auth.md)."""

from __future__ import annotations

import pytest

from merxa_api.config import Settings
from merxa_api.security.principal import Principal
from merxa_api.security.receipts import ApplyContext, ReceiptError, ReceiptVerifier
from support import ISSUER, KEELSON_ISS, KEELSON_JWKS, MARCO_SUB, RESOURCE, FakeKeelson, FakeRealm, httpx_client

H = "a" * 64
CS_ID = "01999999-0000-7000-8000-000000000001"


def _settings() -> Settings:
    return Settings(
        oidc_issuer=ISSUER, mcp_resource=RESOURCE, trusted_receipt_issuers=[KEELSON_ISS], keelson_jwks_url=KEELSON_JWKS
    )


def _marco(scopes: tuple[str, ...] = ("changes:apply",), roles: tuple[str, ...] = ("ops-director",)) -> Principal:
    return Principal(
        sub=MARCO_SUB,
        kind="bearer",
        username="marco.rossi",
        display="Marco Rossi",
        client_id="keelson-connect",
        scopes=frozenset(scopes),
        roles=frozenset(roles),
    )


def _autocommit(scopes: tuple[str, ...] = ("changes:apply:auto",), client: str = "keelson-autocommit") -> Principal:
    return Principal(
        sub="svc-auto",
        kind="bearer",
        username="service-account-keelson-autocommit",
        client_id=client,
        scopes=frozenset(scopes),
        roles=frozenset({"integration-writer"}),
    )


def _ctx(principal: Principal, h: str = H, approval_ref: str = "APR-0001") -> ApplyContext:
    return ApplyContext(
        change_set_id=CS_ID, change_set_code="CS-0001", preview_hash=h, approval_ref=approval_ref, principal=principal
    )


@pytest.fixture
def keelson() -> FakeKeelson:
    return FakeKeelson()


@pytest.fixture
def verifier(keelson: FakeKeelson) -> ReceiptVerifier:
    return ReceiptVerifier(_settings(), httpx_client(FakeRealm(), keelson))


async def test_good_user_receipt(verifier: ReceiptVerifier, keelson: FakeKeelson) -> None:
    receipt = keelson.receipt(change_set_ref=CS_ID, change_set_hash=H)
    verified = await verifier.verify(receipt, _ctx(_marco()))
    assert verified.approved_by == "user:marco"
    assert verified.initiated_by == "Priya Shah"
    # change_set_ref may also be the human code
    await verifier.verify(keelson.receipt(change_set_ref="CS-0001", change_set_hash=H), _ctx(_marco()))


@pytest.mark.parametrize(
    ("kwargs", "principal", "reason"),
    [
        ({"change_set_hash": "b" * 64}, None, "hash_mismatch"),
        ({"ext_sub": "someone-else"}, None, "ext_sub_mismatch"),
        ({"exp_in": -120}, None, "expired"),
        ({"typ": "JWT"}, None, "wrong_typ"),
        ({"iss": "http://evil.localhost"}, None, "untrusted_issuer"),
        ({"aud": "http://other.localhost/mcp"}, None, "wrong_audience"),
        ({"change_set_ref": "01999999-0000-7000-8000-000000000999"}, None, "change_set_mismatch"),
        ({"approval_id": "APR-9999"}, None, "approval_ref_mismatch"),
        ({"approved_by": "robot"}, None, "bad_approved_by"),
        ({}, _marco(scopes=("changes:preview",)), "insufficient_scope"),
        ({}, _marco(roles=("merch-planner",)), "missing_role"),
    ],
)
async def test_bad_user_receipts(
    verifier: ReceiptVerifier, keelson: FakeKeelson, kwargs: dict, principal: Principal | None, reason: str
) -> None:
    params = {"change_set_ref": CS_ID, "change_set_hash": H, **kwargs}
    receipt = keelson.receipt(**params)
    with pytest.raises(ReceiptError) as err:
        await verifier.verify(receipt, _ctx(principal or _marco()))
    assert err.value.reason == reason
    assert err.value.status == 403


async def test_bad_signature(verifier: ReceiptVerifier, keelson: FakeKeelson) -> None:
    impostor = FakeKeelson()
    receipt = impostor.receipt(change_set_ref=CS_ID, change_set_hash=H, kid=keelson.kid)
    with pytest.raises(ReceiptError) as err:
        await verifier.verify(receipt, _ctx(_marco()))
    assert err.value.reason == "bad_signature"


async def test_policy_receipt_rules(verifier: ReceiptVerifier, keelson: FakeKeelson) -> None:
    receipt = keelson.receipt(change_set_ref=CS_ID, change_set_hash=H, approved_by="policy:P-12@3", ext_sub="svc-auto")
    verified = await verifier.verify(receipt, _ctx(_autocommit()))
    assert verified.is_policy
    with pytest.raises(ReceiptError) as err:
        await verifier.verify(receipt, _ctx(_autocommit(client="keelson-preparer")))
    assert err.value.reason == "wrong_client"
    with pytest.raises(ReceiptError) as err:
        await verifier.verify(receipt, _ctx(_autocommit(scopes=("changes:preview",))))
    assert err.value.reason == "insufficient_scope"


async def test_jwks_cached_and_refetched_on_unknown_kid(verifier: ReceiptVerifier, keelson: FakeKeelson) -> None:
    for _ in range(3):
        await verifier.verify(keelson.receipt(change_set_ref=CS_ID, change_set_hash=H), _ctx(_marco()))
    assert keelson.jwks_fetches == 1  # cached within the 5-minute TTL

    keelson.rotate("ar-2")
    verifier.jwks._fetched_at -= 11  # past the unknown-kid refetch guard
    await verifier.verify(keelson.receipt(change_set_ref=CS_ID, change_set_hash=H), _ctx(_marco()))
    assert keelson.jwks_fetches == 2

    with pytest.raises(ReceiptError) as err:
        await verifier.verify(keelson.receipt(change_set_ref=CS_ID, change_set_hash=H, kid="nope"), _ctx(_marco()))
    assert err.value.reason == "unknown_kid"


async def test_jwks_ttl_expiry_refetches(verifier: ReceiptVerifier, keelson: FakeKeelson) -> None:
    await verifier.verify(keelson.receipt(change_set_ref=CS_ID, change_set_hash=H), _ctx(_marco()))
    verifier.jwks._fetched_at -= 301
    await verifier.verify(keelson.receipt(change_set_ref=CS_ID, change_set_hash=H), _ctx(_marco()))
    assert keelson.jwks_fetches == 2
