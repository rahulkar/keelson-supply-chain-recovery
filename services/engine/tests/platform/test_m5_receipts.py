"""M5 quorum receipts (``95`` §1): the executor is the member counted under ``approver`` whatever
the approval order; the other quorum members travel as ``co_approved_by``; a single approver
keeps the exact M1 claim set."""

from __future__ import annotations

import jwt
import pytest

from keelson_contracts.runs import Decider
from keelson_engine.config import Settings
from keelson_engine.core import crypto

H = "c" * 64
LENA = {"sub": "sub-lena", "username": "lena", "name": "Lena Vogel", "role": "compliance"}
MARCO = {"sub": "sub-marco", "username": "marco", "name": "Marco Rossi", "role": "approver"}


def _key() -> jwt.PyJWK:
    keys = {k["kid"]: k for k in crypto.jwks()["keys"]}
    return jwt.PyJWK.from_dict(keys["ar-1"])


def _issue(decided_by: list[object], **kw: object) -> tuple[str, dict[str, object]]:
    token, claims = crypto.sign_quorum_receipt(
        approval_id="APR-0042", change_set_ref="cs-ext-1", change_set_hash=H, tenant="demo-retail",
        decided_by=decided_by, ext_sub="merxa-sub-marco", initiated_by="Priya Shah", policy_version="p-2",
        now=1_800_000_000, **kw,
    )
    decoded = jwt.decode(token, _key().key, algorithms=["ES256"], audience="http://merxa.localhost/mcp",
                         options={"verify_exp": False, "verify_iat": False})
    assert decoded == claims.to_claims()
    return token, decoded


@pytest.mark.parametrize("order", [[LENA, MARCO], [MARCO, LENA]])
def test_executor_is_the_approver_regardless_of_order(settings: Settings, order: list[dict[str, str]]) -> None:
    _token, claims = _issue(order)
    assert claims["approved_by"] == "user:marco" and claims["approver_name"] == "Marco Rossi"
    assert claims["keelson_sub"] == "sub-marco" and claims["ext_sub"] == "merxa-sub-marco"
    assert claims["co_approved_by"] == ["user:lena"]


def test_decider_models_and_no_approver_role(settings: Settings) -> None:
    deciders = [Decider(**LENA), Decider(sub="sub-x", username="xavier", name="Xavier", role="compliance")]
    _t, claims = _issue(deciders)  # no 'approver' member: the first member executes
    assert claims["approved_by"] == "user:lena" and claims["co_approved_by"] == ["user:xavier"]
    executor, others = crypto.split_quorum([MARCO, MARCO, LENA])  # one entry per person
    assert executor["sub"] == "sub-marco" and [o["sub"] for o in others] == ["sub-lena"]
    with pytest.raises(ValueError):
        crypto.split_quorum([])


def test_single_approver_keeps_the_m1_claim_set(settings: Settings) -> None:
    _t, claims = _issue([MARCO])
    assert "co_approved_by" not in claims
    assert set(claims) == {"iss", "aud", "iat", "exp", "jti", "approval_id", "change_set_ref", "change_set_hash",
                           "tenant", "keelson_sub", "ext_iss", "ext_sub", "approved_by", "approver_name",
                           "initiated_by", "policy_version"}
    assert claims["exp"] - claims["iat"] == 300
