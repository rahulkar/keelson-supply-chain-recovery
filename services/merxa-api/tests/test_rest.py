"""REST semantics: auth, problem+json, cursor pagination, ETag/If-Match, Idempotency-Key."""

from __future__ import annotations

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from helpers import auth, key, preparer, reader
from merxa_api import tables as t
from merxa_api.services import idempotency
from support import FakeRealm


async def test_health(http: httpx.AsyncClient) -> None:
    assert (await http.get("/healthz")).json() == {"status": "ok"}
    r = await http.get("/readyz")
    assert r.status_code == 200 and r.json()["checks"]["db"] is True


async def test_auth_required_problem_json(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    r = await http.get("/v1/dcs")
    assert r.status_code == 401
    assert r.headers["content-type"].startswith("application/problem+json")
    body = r.json()
    assert body["status"] == 401 and body["code"] == "unauthorized" and body["type"].startswith("urn:merxa:problem:")
    bad = realm.token(aud="http://somewhere-else/api")
    assert (await http.get("/v1/dcs", headers=auth(bad))).status_code == 401
    expired = realm.token(exp_in=-60)
    assert (await http.get("/v1/dcs", headers=auth(expired))).status_code == 401


async def test_scope_enforced(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    catalog_only = realm.token(scopes=["catalog:read"])
    assert (await http.get("/v1/dcs", headers=auth(catalog_only))).status_code == 200
    r = await http.get("/v1/purchase-orders", headers=auth(catalog_only))
    assert r.status_code == 403 and r.json()["code"] == "insufficient_scope"
    r = await http.post("/v1/change-sets", json={"title": "x"}, headers=auth(reader(realm), key()))
    assert r.status_code == 403


async def test_cursor_pagination(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    token = reader(realm)
    seen: list[str] = []
    cursor = None
    pages = 0
    while True:
        params = {"limit": 10, **({"cursor": cursor} if cursor else {})}
        r = await http.get("/v1/stores", params=params, headers=auth(token))
        assert r.status_code == 200
        page = r.json()
        assert set(page) == {"items", "next_cursor"}
        assert len(page["items"]) <= 10
        seen += [s["code"] for s in page["items"]]
        pages += 1
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert pages == 7 and len(seen) == 62
    assert seen == sorted(seen) and len(set(seen)) == len(seen)

    # Multi-column keyset (inventory) also walks without gaps/duplicates.
    r1 = await http.get("/v1/inventory-positions", params={"location_type": "dc", "limit": 50}, headers=auth(token))
    r2 = await http.get(
        "/v1/inventory-positions",
        params={"location_type": "dc", "limit": 50, "cursor": r1.json()["next_cursor"]},
        headers=auth(token),
    )
    r3 = await http.get(
        "/v1/inventory-positions",
        params={"location_type": "dc", "limit": 50, "cursor": r2.json()["next_cursor"]},
        headers=auth(token),
    )
    keys = [(i["location_code"], i["sku"]) for r in (r1, r2, r3) for i in r.json()["items"]]
    assert len(keys) == 120 == len(set(keys)) and r3.json()["next_cursor"] is None
    assert all(i["atp"] == i["on_hand"] - i["allocated"] - i["reserved"] for i in r1.json()["items"])

    bad = await http.get("/v1/stores", params={"cursor": "not-a-cursor!"}, headers=auth(token))
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_cursor"


async def test_etag_and_if_none_match(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    token = preparer(realm)
    r = await http.get("/v1/purchase-orders/PO-BD-0001", headers=auth(token))
    assert r.status_code == 200 and r.headers["etag"] == f'"{r.json()["version"]}"'
    assert len(r.json()["lines"]) == 30
    assert (await http.get("/v1/purchase-orders/NOPE", headers=auth(token))).status_code == 404

    created = await http.post("/v1/change-sets", json={"title": "etag"}, headers=auth(token, key()))
    cs_id = created.json()["change_set_id"]
    g = await http.get(f"/v1/change-sets/{cs_id}", headers=auth(token))
    tag = g.headers["etag"]
    assert (
        await http.get(f"/v1/change-sets/{cs_id}", headers=auth(token, **{"If-None-Match": tag}))
    ).status_code == 304


async def test_patch_requires_if_match(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    token = preparer(realm)
    cs = (await http.post("/v1/change-sets", json={"title": "patch me"}, headers=auth(token, key()))).json()
    url = f"/v1/change-sets/{cs['change_set_id']}"
    r = await http.patch(url, json={"title": "new"}, headers=auth(token))
    assert r.status_code == 428 and r.json()["code"] == "precondition_required"
    r = await http.patch(url, json={"title": "new"}, headers=auth(token, **{"If-Match": '"99"'}))
    assert r.status_code == 412 and r.json()["current_version"] == 1 and r.headers["etag"] == '"1"'
    r = await http.patch(url, json={"title": "new"}, headers=auth(token, **{"If-Match": '"1"'}))
    assert r.status_code == 200 and r.headers["etag"] == '"2"' and r.json()["title"] == "new"
    r = await http.patch(url, json={"title": "again"}, headers=auth(token, **{"If-Match": '"1"'}))
    assert r.status_code == 412

    msgs = (await http.get("/v1/supplier-messages", headers=auth(token))).json()["items"]
    m = msgs[0]
    r = await http.patch(
        f"/v1/supplier-messages/{m['id']}",
        json={"status": "new"},
        headers=auth(token, **{"If-Match": f'"{m["version"]}"'}),
    )
    assert r.status_code == 200 and r.json()["status"] == "new" and r.json()["version"] == m["version"] + 1


async def test_idempotency_key_semantics(http: httpx.AsyncClient, realm: FakeRealm, engine: AsyncEngine) -> None:
    token = preparer(realm)
    r = await http.post("/v1/change-sets", json={"title": "idem"}, headers=auth(token))
    assert r.status_code == 400 and r.json()["code"] == "idempotency_key_required"

    k = key()
    first = await http.post("/v1/change-sets", json={"title": "idem"}, headers=auth(token, k))
    second = await http.post("/v1/change-sets", json={"title": "idem"}, headers=auth(token, k))
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers.get("idempotent-replayed") == "true"
    async with engine.connect() as conn:
        n = (
            await conn.execute(
                select(func.count()).select_from(t.change_set).where(t.change_set.c.idempotency_key == k)
            )
        ).scalar()
    assert n == 1

    reused = await http.post("/v1/change-sets", json={"title": "different"}, headers=auth(token, k))
    assert reused.status_code == 422 and reused.json()["code"] == "idempotency_key_reused"

    # Same key from another principal is an independent record.
    other = realm.token(sub="svc-other", azp="keelson-preparer", scopes=["changes:preview"])
    assert (await http.post("/v1/change-sets", json={"title": "different"}, headers=auth(other, k))).status_code == 201

    # In-flight: a concurrent request with the same key gets 409.
    k2 = key()
    claim = await idempotency.begin(
        engine,
        principal="svc-preparer",
        key=k2,
        request_sha256=idempotency.request_fingerprint("change_set.create", {"title": "busy", "origin": {}}),
    )
    assert isinstance(claim, idempotency.Claim)
    busy = await http.post("/v1/change-sets", json={"title": "busy"}, headers=auth(token, k2))
    assert busy.status_code == 409 and busy.json()["code"] == "idempotency_in_flight"
    await idempotency.abandon(engine, claim)
    assert (await http.post("/v1/change-sets", json={"title": "busy"}, headers=auth(token, k2))).status_code == 201


async def test_failed_request_is_not_stored(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    """Errors are not replayed: the same key can be retried once the cause is fixed."""
    token = preparer(realm)
    k = key()
    r = await http.post(
        "/v1/change-sets/CS-9999/items", json={"items": [{"op": "x", "payload": {}}]}, headers=auth(token, k)
    )
    assert r.status_code == 404
    r = await http.post(
        "/v1/change-sets/CS-9999/items", json={"items": [{"op": "x", "payload": {}}]}, headers=auth(token, k)
    )
    assert r.status_code == 404  # not a replay of a stored error, and not 409/422


async def test_validation_errors_are_problems(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    token = preparer(realm)
    r = await http.post("/v1/change-sets", json={"nope": 1}, headers=auth(token, key()))
    assert r.status_code == 422 and r.json()["code"] == "validation_error"
    cs = (await http.post("/v1/change-sets", json={"title": "floats"}, headers=auth(token, key()))).json()
    r = await http.post(
        f"/v1/change-sets/{cs['change_set_id']}/items",
        json={"items": [{"op": "allocation.set_qty", "target_ref": "AL-000001", "payload": {"qty": 1.5}}]},
        headers=auth(token, key()),
    )
    assert r.status_code == 400 and r.json()["code"] == "invalid_items"


async def test_prm_metadata(http: httpx.AsyncClient) -> None:
    for path in ("/.well-known/oauth-protected-resource/mcp", "/.well-known/oauth-protected-resource"):
        r = await http.get(path)
        assert r.status_code == 200, path
        doc = r.json()
        assert doc["resource"] == "http://merxa.localhost/mcp"
        assert doc["authorization_servers"] == ["http://auth.localhost/realms/merxa"]
        assert "changes:apply" in doc["scopes_supported"]
