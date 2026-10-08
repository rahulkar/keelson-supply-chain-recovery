"""Token presets and change-set helpers for API tests."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import httpx

from support import MARCO_SUB, READ, FakeKeelson, FakeRealm


def reader(realm: FakeRealm) -> str:
    return realm.token(sub="svc-reader", azp="keelson-reader", scopes=READ, roles=["integration-writer"])


def preparer(realm: FakeRealm) -> str:
    return realm.token(
        sub="svc-preparer", azp="keelson-preparer", scopes=[*READ, "changes:preview"], roles=["integration-writer"]
    )


def marco(realm: FakeRealm) -> str:
    return realm.token(
        sub=MARCO_SUB,
        azp="keelson-connect",
        scopes=["openid", "offline_access", "changes:apply"],
        roles=["ops-director"],
        username="marco.rossi",
        name="Marco Rossi",
    )


def autocommit(realm: FakeRealm) -> str:
    return realm.token(
        sub="svc-autocommit", azp="keelson-autocommit", scopes=["changes:apply:auto"], roles=["integration-writer"]
    )


def auth(token: str, key: str | None = None, **extra: str) -> dict[str, str]:
    headers = {"Authorization": f"Bearer {token}", **extra}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def independent_hash(items: list[dict[str, Any]]) -> str:
    """The contract's H, computed without merxa_api code."""
    rows = [
        {
            "seq": i,
            "op": it["op"],
            "target_ref": it.get("target_ref"),
            "expected_version": it.get("expected_version"),
            "payload": it.get("payload", {}),
        }
        for i, it in enumerate(items, start=1)
    ]
    canonical = json.dumps(rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode()).hexdigest()


def key() -> str:
    return f"test-{uuid.uuid4()}"


async def build_change_set(
    http: httpx.AsyncClient, token: str, items: list[dict[str, Any]], title: str = "test"
) -> dict:
    r = await http.post(
        "/v1/change-sets",
        json={"title": title, "origin": {"system": "Keelson", "case_ref": "C-1"}},
        headers=auth(token, key()),
    )
    assert r.status_code == 201, r.text
    cs_id = r.json()["change_set_id"]
    r = await http.post(f"/v1/change-sets/{cs_id}/items", json={"items": items}, headers=auth(token, key()))
    assert r.status_code == 200, r.text
    r = await http.post(f"/v1/change-sets/{cs_id}:validate", headers=auth(token, key()))
    assert r.status_code == 200, r.text
    return r.json()


async def po_line_version(http: httpx.AsyncClient, token: str, po: str, line_no: int) -> int:
    r = await http.get(f"/v1/purchase-orders/{po}", headers=auth(token))
    assert r.status_code == 200, r.text
    return next(ln["version"] for ln in r.json()["lines"] if ln["line_no"] == line_no)


async def dc_position(http: httpx.AsyncClient, token: str, dc: str, sku: str) -> dict[str, Any]:
    r = await http.get(
        "/v1/inventory-positions", params={"location_type": "dc", "location_code": dc, "sku": sku}, headers=auth(token)
    )
    assert r.status_code == 200, r.text
    return r.json()["items"][0]


def admin(realm: FakeRealm) -> str:
    """Merxa administrator (role merxa-admin)."""
    return realm.token(
        sub="admin-merxa-sub", azp="merxa-web", scopes=READ, roles=["merxa-admin"], username="merxa.admin"
    )


async def apply_validated(
    http: httpx.AsyncClient,
    realm: FakeRealm,
    keelson: FakeKeelson,
    validated: dict[str, Any],
    approval_id: str | None = None,
) -> httpx.Response:
    """Apply a validated change set as Marco with a fresh user receipt."""
    approval_id = approval_id or f"APR-{uuid.uuid4().hex[:6]}"
    receipt = keelson.receipt(
        change_set_ref=validated["change_set_id"], change_set_hash=validated["preview_hash"], approval_id=approval_id
    )
    return await http.post(
        f"/v1/change-sets/{validated['change_set_id']}:apply",
        json={"preview_hash": validated["preview_hash"], "approval_ref": approval_id, "approval_receipt": receipt},
        headers=auth(marco(realm), key()),
    )


async def rollback(
    http: httpx.AsyncClient,
    realm: FakeRealm,
    keelson: FakeKeelson,
    validated: dict[str, Any],
    approval_id: str | None = None,
    **receipt_kw: Any,
) -> httpx.Response:
    """Roll back an applied change set; the receipt binds to the original set + hash."""
    approval_id = approval_id or f"APR-RB-{uuid.uuid4().hex[:6]}"
    receipt = keelson.receipt(
        change_set_ref=validated["change_set_id"],
        change_set_hash=validated["preview_hash"],
        approval_id=approval_id,
        **receipt_kw,
    )
    return await http.post(
        f"/v1/change-sets/{validated['change_set_id']}:rollback",
        json={"approval_ref": approval_id, "approval_receipt": receipt},
        headers=auth(marco(realm), key()),
    )
