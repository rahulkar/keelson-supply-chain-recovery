"""Envelope encryption, JWKS, client assertions and approval receipts."""

from __future__ import annotations

import jwt
import pytest

from keelson_contracts.receipts import ApprovalReceiptClaims, approved_by_policy, approved_by_user
from keelson_engine.config import Settings
from keelson_engine.core import crypto
from keelson_engine.core.secrets import SecretNotFound, read_secret

H = "a" * 64


def _jwk(kid: str) -> jwt.PyJWK:
    keys = {k["kid"]: k for k in crypto.jwks()["keys"]}
    return jwt.PyJWK.from_dict(keys[kid])


def test_encrypt_round_trip_and_aad_binding(settings: Settings) -> None:
    token = crypto.encrypt(b"refresh-token-value", "bff|session-1")
    assert token.startswith("dk-1:")
    assert b"refresh-token-value" not in token.encode()
    assert crypto.decrypt(token, "bff|session-1") == b"refresh-token-value"
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(token, "bff|session-2")
    assert crypto.encrypt(b"x", "a") != crypto.encrypt(b"x", "a")  # random nonce


def test_decrypt_rejects_tampering_and_unknown_keys(settings: Settings) -> None:
    token = crypto.encrypt_str("secret", "aad")
    body = token.split(":", 1)[1]
    flipped = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1]
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt(f"dk-1:{flipped}", "aad")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt("dk-9:" + body, "aad")
    with pytest.raises(crypto.DecryptionError):
        crypto.decrypt("garbage", "aad")
    assert crypto.decrypt_str(token, "aad") == "secret"


def test_jwks_has_public_keys_only(settings: Settings) -> None:
    keys = crypto.jwks()["keys"]
    assert [k["kid"] for k in keys] == ["ca-1", "ar-1"]
    for k in keys:
        assert k["kty"] == "EC" and k["crv"] == "P-256" and k["alg"] == "ES256" and k["use"] == "sig"
        assert "d" not in k


def test_client_assertion_claims(settings: Settings) -> None:
    aud = "http://auth.localhost/realms/merxa"
    token = crypto.sign_client_assertion("keelson-reader", aud)
    header = jwt.get_unverified_header(token)
    assert header == {"alg": "ES256", "kid": "ca-1", "typ": "JWT"}
    claims = jwt.decode(token, _jwk("ca-1").key, algorithms=["ES256"], audience=aud)
    assert claims["iss"] == claims["sub"] == "keelson-reader"
    assert claims["exp"] - claims["iat"] == 60
    assert claims["jti"]
    other = jwt.decode(crypto.sign_client_assertion("keelson-reader", aud), _jwk("ca-1").key,
                       algorithms=["ES256"], audience=aud)
    assert other["jti"] != claims["jti"]
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, _jwk("ar-1").key, algorithms=["ES256"], audience=aud)


def test_user_receipt_verifies_with_pyjwt(settings: Settings) -> None:
    claims = ApprovalReceiptClaims.issue(
        approval_id="APR-0001", change_set_ref="cs-uuid", change_set_hash=H, tenant="demo-retail",
        keelson_sub="sub-marco", ext_sub="merxa-sub-marco", approved_by=approved_by_user("marco"),
        approver_name="Marco Rossi", initiated_by="Priya Shah", policy_version="p-1",
    )
    token = crypto.sign_approval_receipt(claims)
    header = jwt.get_unverified_header(token)
    assert header == {"alg": "ES256", "kid": "ar-1", "typ": "approval-receipt+jwt"}
    decoded = jwt.decode(token, _jwk("ar-1").key, algorithms=["ES256"], audience="http://merxa.localhost/mcp",
                         issuer="http://keelson.localhost")
    assert decoded == claims.to_claims()
    assert decoded["approved_by"] == "user:marco"
    assert decoded["ext_iss"] == "http://auth.localhost/realms/merxa"


def test_policy_receipt(settings: Settings) -> None:
    claims = ApprovalReceiptClaims.issue(
        approval_id="APR-0002", change_set_ref="cs", change_set_hash=H, tenant="demo-retail", keelson_sub="sub-priya",
        ext_sub="service-account-keelson-autocommit", approved_by=approved_by_policy("small-transfer", "p-1"),
        approver_name=None, initiated_by="Priya Shah", policy_version="p-1",
    )
    decoded = jwt.decode(crypto.sign_approval_receipt(claims), _jwk("ar-1").key, algorithms=["ES256"],
                         audience="http://merxa.localhost/mcp")
    assert decoded["approved_by"] == "policy:small-transfer@p-1"
    assert decoded["approver_name"] is None


def test_read_secret_file_then_env(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    assert read_secret("keelson_data_key")
    monkeypatch.setenv("SOME_TEST_SECRET", "from-env")
    assert read_secret("some_test_secret") == "from-env"
    with pytest.raises(SecretNotFound):
        read_secret("definitely_missing_secret")
    with pytest.raises(ValueError):
        read_secret("../etc/passwd")
