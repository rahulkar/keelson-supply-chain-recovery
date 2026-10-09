"""Approval receipts: every claim and binding failure maps to its ``invalid_receipt`` reason; the
JWKS outage is the transient ``receipt_jwks_unavailable``; single-use ``jti``; the sidecar's own
auto-commit caps; receipts minted with Keelson's ``ApprovalReceiptClaims.issue(aud=…)`` verify."""

from __future__ import annotations

import time
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric import ec
from fastmcp.exceptions import ToolError

from adapter_sidecar.config import AutocommitSpec, ReceiptsSpec
from adapter_sidecar.errors import Problem
from adapter_sidecar.receipts import ReceiptContext, ReceiptError, ReceiptVerifier
from conftest import EXT_SUB, KEELSON_ISS, PUBLIC, RESOURCE, FakeKeelson, Running, key
from helpers import TO_ITEM, apply, call, prepare
from keelson_contracts.receipts import MERXA_ISSUER, MERXA_MCP_RESOURCE

HASH = "a" * 64
CTX = ReceiptContext(change_set_id="0199c0de-0000-7000-8000-000000000001", change_set_code="CS-0001",
                     preview_hash=HASH, approval_ref="APR-1", scopes=frozenset({"changes:apply", "changes:apply:auto"}))


def verifier(keelson: FakeKeelson, **rules: Any) -> ReceiptVerifier:
    spec = ReceiptsSpec(tenants=["northwind-test"], expected_ext_sub=EXT_SUB,
                        autocommit=AutocommitSpec(ops=["transfer_order.create"], max_items=5, max_units=100),
                        **rules)
    return ReceiptVerifier(audience=RESOURCE, issuers=[KEELSON_ISS], ext_iss=PUBLIC, rules=spec,
                           jwks_url="http://keelson.localhost/api/.well-known/jwks.json", http_client=keelson.client())


def token(keelson: FakeKeelson, **kw: Any) -> str:
    return keelson.receipt(change_set_ref=CTX.change_set_id, change_set_hash=HASH, approval_id="APR-1", **kw)


async def test_good_receipts_verify(keelson: FakeKeelson) -> None:
    v = verifier(keelson)
    policy = await v.verify(token(keelson), CTX)
    assert policy.is_policy and policy.approved_by == "policy:auto-small-transfer@p-1"
    user = await v.verify(token(keelson, policy=False, co_approved_by=("user:lena",)), CTX)
    assert user.approver_name == "Marco Rossi" and user.co_approvers == [{"ref": "user:lena", "name": "lena"}]
    by_code = await v.verify(keelson.receipt(change_set_ref="CS-0001", change_set_hash=HASH, approval_id="APR-1"), CTX)
    assert by_code.jti


@pytest.mark.parametrize(("kw", "reason"), [
    ({"header": {"alg": "ES256", "kid": "ar-1", "typ": "JWT"}}, "wrong_typ"),
    ({"header": {"alg": "ES256", "typ": "approval-receipt+jwt"}}, "missing_kid"),
    ({"header": {"alg": "ES256", "kid": "ar-9", "typ": "approval-receipt+jwt"}}, "unknown_kid"),
    ({"aud": MERXA_MCP_RESOURCE, "ext_iss": MERXA_ISSUER}, "wrong_audience"),
    ({"aud": "http://other-sidecar.localhost/mcp"}, "wrong_audience"),
    ({"claims_update": {"iss": "http://evil.localhost"}}, "untrusted_issuer"),
    ({"now": int(time.time()) - 3600}, "expired"),
    ({"ext_iss": "http://auth.localhost/realms/merxa"}, "ext_iss_mismatch"),
    ({"tenant": "demo-retail"}, "wrong_tenant"),
    ({"ext_sub": "connector:merxa-lite"}, "ext_sub_mismatch"),
    ({"claims_update": {"change_set_hash": "b" * 64}}, "hash_mismatch"),
    ({"claims_update": {"change_set_ref": "CS-0002"}}, "change_set_mismatch"),
    ({"claims_update": {"approval_id": "APR-2"}}, "approval_ref_mismatch"),
    ({"claims_update": {"approved_by": "admin"}}, "bad_approved_by"),
    ({"claims_update": {"co_approved_by": ["user:marco"]}, "policy": False}, "bad_co_approved_by"),
    ({"claims_update": {"jti": None}}, "missing_claim"),
    ({"key": ec.generate_private_key(ec.SECP256R1())}, "bad_signature"),
])
async def test_refusals(keelson: FakeKeelson, kw: dict[str, Any], reason: str) -> None:
    claims_update = kw.pop("claims_update", None)
    if claims_update and None in claims_update.values():
        # drop a claim entirely
        import jwt

        tok = token(keelson, **kw)
        claims = jwt.decode(tok, options={"verify_signature": False})
        for k in [k for k, val in claims_update.items() if val is None]:
            claims.pop(k)
        tok = jwt.encode(claims, keelson.key, algorithm="ES256",
                         headers={"alg": "ES256", "kid": "ar-1", "typ": "approval-receipt+jwt"})
    else:
        tok = token(keelson, claims_update=claims_update, **kw)
    with pytest.raises(ReceiptError) as err:
        await verifier(keelson).verify(tok, CTX)
    assert err.value.reason == reason and err.value.code == "invalid_receipt" and err.value.status == 403


async def test_scope_and_policy_rules(keelson: FakeKeelson) -> None:
    ctx_auto = ReceiptContext(CTX.change_set_id, CTX.change_set_code, HASH, "APR-1", frozenset({"changes:apply:auto"}))
    ctx_user = ReceiptContext(CTX.change_set_id, CTX.change_set_code, HASH, "APR-1", frozenset({"changes:apply"}))
    with pytest.raises(ReceiptError, match="changes:apply"):
        await verifier(keelson).verify(token(keelson, policy=False), ctx_auto)
    with pytest.raises(ReceiptError, match="changes:apply:auto"):
        await verifier(keelson).verify(token(keelson), ctx_user)
    no_auto = ReceiptVerifier(audience=RESOURCE, issuers=[KEELSON_ISS], ext_iss=PUBLIC, rules=ReceiptsSpec(),
                              jwks_url="http://keelson.localhost/api/.well-known/jwks.json",
                              http_client=keelson.client())
    with pytest.raises(ReceiptError) as err:
        await no_auto.verify(token(keelson), CTX)
    assert err.value.reason == "policy_not_accepted"


async def test_jwks_outage_is_transient(keelson: FakeKeelson) -> None:
    keelson.jwks_status = 500
    with pytest.raises(Problem) as err:
        await verifier(keelson).verify(token(keelson), CTX)
    assert err.value.code == "receipt_jwks_unavailable" and err.value.status == 503


async def test_jwks_is_cached_and_refetched_for_new_kids(keelson: FakeKeelson) -> None:
    v = verifier(keelson)
    await v.verify(token(keelson), CTX)
    await v.verify(token(keelson), CTX)
    assert keelson.jwks_fetches == 1
    with pytest.raises(ReceiptError):  # unknown kid right after a fetch: no refetch storm
        await v.verify(token(keelson, header={"alg": "ES256", "kid": "ar-2", "typ": "approval-receipt+jwt"}), CTX)
    assert keelson.jwks_fetches == 1


async def test_receipts_over_mcp(fernhill: Running, keelson: FakeKeelson, monkeypatch: pytest.MonkeyPatch) -> None:
    async with fernhill.client() as c:
        v = await prepare(c, [TO_ITEM])
        # a receipt minted for Merxa (Keelson's platform audience) is refused here
        with pytest.raises(ToolError, match=r"invalid_receipt: .*\(reason: wrong_audience\)"):
            await apply(c, keelson, v, aud=MERXA_MCP_RESOURCE, ext_iss=MERXA_ISSUER)
        keelson.jwks_status = 503
        fernhill.sidecar.receipts.jwks._fetched_at = float("-inf")
        with pytest.raises(ToolError, match="receipt_jwks_unavailable"):
            await apply(c, keelson, v)
        keelson.jwks_status = 200
        # the sidecar's own auto-commit caps (defence in depth)
        svc = fernhill.sidecar.changesets
        caps = svc.cfg.receipts.model_copy(update={"autocommit": AutocommitSpec(ops=["transfer_order.create"],
                                                                                 max_units=50)})
        monkeypatch.setattr(svc, "cfg", svc.cfg.model_copy(update={"receipts": caps}))
        with pytest.raises(ToolError, match="auto_cap_exceeded: 100 units exceeds the cap of 50"):
            await apply(c, keelson, v)
        monkeypatch.undo()
        receipt = keelson.receipt(change_set_ref=v["change_set_id"], change_set_hash=v["preview_hash"],
                                  approval_id="APR-1")
        args = {"change_set_id": v["change_set_id"], "preview_hash": v["preview_hash"], "approval_ref": "APR-1",
                "approval_receipt": receipt}
        assert (await call(c, "apply_change_set", {**args, "idempotency_key": key()}))["status"] == "applied"
        # the same receipt (jti) cannot apply another change set
        v2 = await prepare(c, [{**TO_ITEM, "payload": {**TO_ITEM["payload"], "ship_date": "2026-10-29"}}])
        with pytest.raises(ToolError, match=r"\(reason: (replayed_jti|hash_mismatch)\)"):
            await call(c, "apply_change_set", {**args, "change_set_id": v2["change_set_id"],
                                               "preview_hash": v2["preview_hash"], "idempotency_key": key()})
        jti_reuse = keelson.receipt(change_set_ref=v2["change_set_id"], change_set_hash=v2["preview_hash"],
                                    approval_id="APR-1", claims_update={"jti": _jti(receipt)})
        with pytest.raises(ToolError, match=r"\(reason: replayed_jti\)"):
            await call(c, "apply_change_set", {**args, "change_set_id": v2["change_set_id"],
                                               "preview_hash": v2["preview_hash"], "approval_receipt": jti_reuse,
                                               "idempotency_key": key()})


def _jti(tok: str) -> str:
    import jwt

    return str(jwt.decode(tok, options={"verify_signature": False})["jti"])
