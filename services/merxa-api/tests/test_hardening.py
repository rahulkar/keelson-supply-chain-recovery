"""Regression tests for defects found in review: token hardening, cursor/payload validation,
login CSRF, receipt-JWKS outages, session-secret policy and idempotent demo reset."""

from __future__ import annotations

import base64
import json
import time
import uuid
from typing import Any

import httpx
import jwt
import pytest

from helpers import auth, key, preparer, reader
from merxa_api.api.routes_auth import _safe_next
from merxa_api.app import check_session_secret
from merxa_api.config import DEV_SESSION_SECRET, Settings
from merxa_api.errors import Problem
from merxa_api.security.principal import Principal
from merxa_api.security.receipts import ApplyContext, ReceiptVerifier
from merxa_api.security.tokens import build_token_verifier
from support import ISSUER, KEELSON_ISS, KEELSON_JWKS, MARCO_SUB, RESOURCE, FakeKeelson, FakeRealm, httpx2_client


def _raw_token(realm: FakeRealm, claims: dict[str, Any], kid: str | None = None) -> str:
    return jwt.encode(claims, realm.key, algorithm="RS256", headers={"kid": kid or realm.kid})


def _base_claims(**overrides: Any) -> dict[str, Any]:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": ISSUER,
        "aud": RESOURCE,
        "sub": "svc-reader",
        "azp": "keelson-reader",
        "iat": now,
        "exp": now + 300,
        "scope": "catalog:read",
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


def _cursor(values: list[Any]) -> str:
    return base64.urlsafe_b64encode(json.dumps(values).encode()).decode().rstrip("=")


# ───────────── bearer token hardening ─────────────
async def test_tokens_without_exp_or_sub_or_of_wrong_typ_are_rejected(
    http: httpx.AsyncClient, realm: FakeRealm
) -> None:
    ok = _raw_token(realm, _base_claims(typ="Bearer"))
    assert (await http.get("/v1/dcs", headers=auth(ok))).status_code == 200
    for bad in (
        _base_claims(exp=None),  # FastMCP's JWTVerifier alone would accept a never-expiring token
        _base_claims(sub=None),
        _base_claims(sub=""),
        _base_claims(typ="ID"),
        _base_claims(typ="Refresh"),
    ):
        r = await http.get("/v1/dcs", headers=auth(_raw_token(realm, bad)))
        assert r.status_code == 401, bad
        assert r.json()["code"] == "invalid_token"


async def test_unknown_kid_does_not_hammer_the_realm_jwks(realm: FakeRealm, keelson: FakeKeelson) -> None:
    settings = Settings(oidc_issuer=ISSUER, mcp_resource=RESOURCE)
    verifier = build_token_verifier(settings, httpx2_client(realm, keelson))
    before = realm.jwks_fetches
    assert await verifier.verify_token(realm.token()) is not None
    assert realm.jwks_fetches == before + 1
    for _ in range(5):  # forged kids within the throttle window: served from the last document
        assert await verifier.verify_token(_raw_token(realm, _base_claims(), kid=f"forged-{uuid.uuid4()}")) is None
    assert realm.jwks_fetches == before + 1
    verifier._jwks_attempt_at -= 11  # throttle window over → one refetch for the next unknown kid
    assert await verifier.verify_token(_raw_token(realm, _base_claims(), kid="forged-again")) is None
    assert realm.jwks_fetches == before + 2
    assert await verifier.verify_token(realm.token()) is not None  # known kid still served from cache
    assert realm.jwks_fetches == before + 2


# ───────────── forged cursors are 400s, never 500s ─────────────
async def test_forged_cursor_values_are_rejected(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    token = reader(realm)
    cases = [
        ("/v1/dcs", [1]),  # int for a text key: asyncpg would raise DataError
        ("/v1/stores", [True]),
        ("/v1/inventory-positions", ["dc", "UK1", {"x": 1}]),
        ("/v1/audit-log", [10**30]),  # beyond bigint
        ("/v1/audit-log", ["abc"]),
        ("/v1/supplier-messages", ["2026-10-05T00:00:00Z", "not-a-uuid"]),
    ]
    for path, values in cases:
        r = await http.get(path, params={"cursor": _cursor(values)}, headers=auth(token))
        assert r.status_code == 400, (path, values, r.text)
        assert r.json()["code"] == "invalid_cursor"
    # A well-formed cursor still works.
    r = await http.get("/v1/dcs", params={"cursor": _cursor(["DE1"])}, headers=auth(token))
    assert r.status_code == 200 and [d["code"] for d in r.json()["items"]] == ["NL1", "PL1", "UK1"]


# ───────────── change-set payload validation ─────────────
async def test_out_of_range_and_nul_values_rejected_at_add_time(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    token = preparer(realm)
    r = await http.post("/v1/change-sets", json={"title": "bounds"}, headers=auth(token, key()))
    assert r.status_code == 201
    cs_id = r.json()["change_set_id"]
    bad_items = [
        {"op": "allocation.set_qty", "target_ref": "AL-000001", "expected_version": 1, "payload": {"qty": 2**31}},
        {"op": "allocation.set_qty", "target_ref": "AL-000001", "expected_version": 2**31, "payload": {"qty": 1}},
        {"op": "po_line.change_qty", "target_ref": "PO-BD-0001#1", "expected_version": 1, "payload": {"qty": 1.5}},
        {
            "op": "po_line.reschedule",
            "target_ref": "PO-BD-0001#1",
            "expected_version": 1,
            "payload": {"eta_dc": "\x00"},
        },
        {"op": "po_line.reschedule", "target_ref": "PO\x00#1", "expected_version": 1, "payload": {}},
    ]
    for item in bad_items:
        r = await http.post(f"/v1/change-sets/{cs_id}/items", json={"items": [item]}, headers=auth(token, key()))
        assert r.status_code == 400, (item, r.text)
        assert r.json()["code"] == "invalid_items"
    r = await http.post(
        "/v1/change-sets", json={"title": "x", "origin": {"plan_version": 2**40}}, headers=auth(token, key())
    )
    assert r.status_code == 400 and r.json()["code"] == "invalid_origin"


async def test_dates_beyond_the_planning_horizon_are_invalid(
    http: httpx.AsyncClient, realm: FakeRealm, reseed: None
) -> None:
    token = preparer(realm)
    po = (await http.get("/v1/purchase-orders/PO-BD-0001", headers=auth(token))).json()
    line = po["lines"][0]
    items = [
        {
            "op": "transfer_order.create",
            "target_ref": None,
            "expected_version": None,
            "payload": {
                "from_dc": "DE1",
                "to_dc": "NL1",
                "ship_date": "9999-12-31",
                "lines": [{"sku": line["sku"], "qty": 1}],
            },
        },
        {
            "op": "po_line.reschedule",
            "target_ref": f"PO-BD-0001#{line['line_no']}",
            "expected_version": line["version"],
            "payload": {"eta_dc": "9999-12-30"},
        },
    ]
    r = await http.post("/v1/change-sets", json={"title": "far future"}, headers=auth(token, key()))
    cs_id = r.json()["change_set_id"]
    r = await http.post(f"/v1/change-sets/{cs_id}/items", json={"items": items}, headers=auth(token, key()))
    assert r.status_code == 200
    r = await http.post(f"/v1/change-sets/{cs_id}:validate", headers=auth(token, key()))
    assert r.status_code == 200, r.text  # previously: OverflowError → 500
    body = r.json()
    assert body["status"] == "invalid"
    assert all(not it["ok"] and any("days ahead" in e for e in it["errors"]) for it in body["items"])


# ───────────── BFF: login CSRF + redirect hardening ─────────────
async def test_login_as_and_logout_reject_foreign_origins(http: httpx.AsyncClient) -> None:
    evil = {"Origin": "http://evil.localhost"}
    r = await http.post("/v1/auth/demo/login-as", json={"username": "marco.rossi"}, headers=evil)
    assert r.status_code == 403 and r.json()["code"] == "csrf_rejected"
    assert "merxa_session" not in r.cookies
    r = await http.post(
        "/v1/auth/demo/login-as", json={"username": "marco.rossi"}, headers={"Origin": "http://merxa.localhost"}
    )
    assert r.status_code == 200
    r = await http.post("/v1/auth/logout", headers=evil)
    assert r.status_code == 403
    assert (await http.get("/v1/auth/me")).status_code == 200  # still logged in
    assert (await http.post("/v1/auth/logout", headers={"Origin": "http://merxa.localhost"})).status_code == 200


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("/change-batches/CS-0001?tab=audit", "/change-batches/CS-0001?tab=audit"),
        ("//evil.example", "/"),
        ("/\t/evil.example", "/"),
        ("/\n/evil.example", "/"),
        ("/\\evil.example", "/"),
        ("https://evil.example", "/"),
        ("", "/"),
        (None, "/"),
    ],
)
def test_safe_next(value: str | None, expected: str) -> None:
    assert _safe_next(value) == expected


# ───────────── receipt JWKS outage is transient (503), not a rejection ─────────────
async def test_receipt_jwks_outage_is_a_503() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"error": "bad gateway"})

    settings = Settings(
        oidc_issuer=ISSUER, mcp_resource=RESOURCE, trusted_receipt_issuers=[KEELSON_ISS], keelson_jwks_url=KEELSON_JWKS
    )
    verifier = ReceiptVerifier(settings, httpx.AsyncClient(transport=httpx.MockTransport(down)))
    receipt = FakeKeelson().receipt(change_set_ref="CS-0001", change_set_hash="a" * 64)
    principal = Principal(
        sub=MARCO_SUB,
        kind="bearer",
        client_id="keelson-connect",
        scopes=frozenset({"changes:apply"}),
        roles=frozenset({"ops-director"}),
    )
    ctx = ApplyContext(
        change_set_id=str(uuid.uuid4()),
        change_set_code="CS-0001",
        preview_hash="a" * 64,
        approval_ref="APR-0001",
        principal=principal,
    )
    with pytest.raises(Problem) as err:
        await verifier.verify(receipt, ctx)
    assert err.value.status == 503 and err.value.code == "receipt_jwks_unavailable"


# ───────────── session secret policy ─────────────
def test_dev_session_secret_only_on_localhost() -> None:
    check_session_secret(Settings(public_url="http://merxa.localhost", session_secret=DEV_SESSION_SECRET))
    check_session_secret(Settings(public_url="https://merxa.example.com", session_secret="s3cr3t-from-env"))
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        check_session_secret(Settings(public_url="https://merxa.example.com", session_secret=DEV_SESSION_SECRET))


# ───────────── idempotent demo reset ─────────────
async def test_scenario_reset_replays_with_the_same_key(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    admin = realm.token(sub="admin-merxa-sub", azp="merxa-web", roles=["merxa-admin"])
    k = key()
    first = await http.post("/v1/admin/scenario:reset", headers=auth(admin, k))
    assert first.status_code == 200, first.text
    # Reset truncates idempotency_record (which held this very request's in-flight row);
    # the completed response must still be stored so a retry replays instead of re-seeding.
    second = await http.post("/v1/admin/scenario:reset", headers=auth(admin, k))
    assert second.status_code == 200 and second.headers.get("idempotent-replayed") == "true"
    assert second.json() == first.json()
