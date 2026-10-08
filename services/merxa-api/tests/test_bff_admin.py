"""Merxa UI BFF (cookie sessions, demo persona switcher) and the demo director."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine

from helpers import auth, key, preparer, reader
from merxa_api import tables as t
from merxa_api.security import sessions
from merxa_api.seed.generator import generate
from merxa_api.seed.loader import dataset_fingerprint, db_fingerprint
from support import FakeRealm


async def _login_as(http: httpx.AsyncClient, username: str) -> httpx.Response:
    return await http.post("/v1/auth/demo/login-as", json={"username": username})


async def test_personas_and_login_as(http: httpx.AsyncClient) -> None:
    r = await http.get("/v1/auth/demo/personas")
    assert [p["username"] for p in r.json()] == ["priya.shah", "marco.rossi", "merxa.admin"]
    assert (await http.get("/v1/auth/me")).status_code == 401

    r = await _login_as(http, "marco.rossi")
    assert r.status_code == 200, r.text
    assert "access_token" not in r.text and "id_token" not in r.text
    cookie = r.cookies.get(sessions.SESSION_COOKIE)
    assert (
        cookie and "httponly" in r.headers["set-cookie"].lower() and "samesite=lax" in r.headers["set-cookie"].lower()
    )

    me = (await http.get("/v1/auth/me")).json()
    assert me["user"]["username"] == "marco.rossi" and me["user"]["name"] == "Marco Rossi"
    assert me["user"]["roles"] == ["ops-director"] and "changes:apply" in me["user"]["scopes"]
    assert me["demo_mode"] is True and me["brand"] == {"vendor": "Merxa"}

    # Cookie session works for REST reads.
    assert (await http.get("/v1/dcs")).status_code == 200

    # Cookie-authenticated writes from a foreign origin are rejected (CSRF).
    r = await http.post(
        "/v1/change-sets", json={"title": "x"}, headers={"Idempotency-Key": key(), "Origin": "http://evil.localhost"}
    )
    assert r.status_code == 403 and r.json()["code"] == "csrf_rejected"
    r = await http.post(
        "/v1/change-sets",
        json={"title": "from ui"},
        headers={"Idempotency-Key": key(), "Origin": "http://merxa.localhost"},
    )
    assert r.status_code == 201

    out = await http.post("/v1/auth/logout")
    assert out.status_code == 200 and "id_token_hint" in (out.json()["logout_url"] or "")
    http.cookies.clear()
    assert (await http.get("/v1/auth/me")).status_code == 401


async def test_login_as_rejects_unknown_and_bad_password(http: httpx.AsyncClient, realm: FakeRealm) -> None:
    assert (await _login_as(http, "ava")).status_code == 404
    saved = realm.users["priya.shah"]["password"]
    realm.users["priya.shah"]["password"] = "changed"
    try:
        r = await _login_as(http, "priya.shah")
        assert r.status_code == 401 and r.json()["code"] == "login_failed"
    finally:
        realm.users["priya.shah"]["password"] = saved


async def test_login_redirect_and_state_cookie(http: httpx.AsyncClient) -> None:
    r = await http.get("/v1/auth/login", params={"next": "/change-batches"})
    assert r.status_code == 302
    loc = urlsplit(r.headers["location"])
    q = parse_qs(loc.query)
    assert loc.path.endswith("/protocol/openid-connect/auth")
    assert q["client_id"] == ["merxa-web"] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"] == ["http://merxa.localhost/v1/auth/callback"]
    assert sessions.OAUTH_COOKIE in r.cookies
    # Callback with a forged state is rejected.
    bad = await http.get("/v1/auth/callback", params={"code": "abc", "state": "forged"})
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_state"


async def test_demo_director(http: httpx.AsyncClient, realm: FakeRealm, reseed: None) -> None:
    await _login_as(http, "priya.shah")
    r = await http.post("/v1/admin/scenario:advance-clock", json={"days": 1}, headers={"Idempotency-Key": key()})
    assert r.status_code == 403
    http.cookies.clear()

    await _login_as(http, "merxa.admin")
    k = key()
    r = await http.post("/v1/admin/scenario:advance-clock", json={"days": 7}, headers={"Idempotency-Key": k})
    assert r.status_code == 200 and r.json() == {
        "today": "2026-10-12",
        "iso_week": "2026-W42",
        "previous": "2026-10-05",
    }
    replay = await http.post("/v1/admin/scenario:advance-clock", json={"days": 7}, headers={"Idempotency-Key": k})
    assert replay.json()["today"] == "2026-10-12"  # not advanced twice

    r = await http.post("/v1/admin/scenario:inject", json={"kind": "trim_delay"}, headers={"Idempotency-Key": key()})
    assert r.status_code == 200 and r.json()["kind"] == "supplier_message"
    msgs = (await http.get("/v1/supplier-messages", params={"supplier_code": "TRIM-BD-01", "status": "new"})).json()
    assert any("delayed 7 days" in m["subject"] for m in msgs["items"])

    r = await http.post(
        "/v1/admin/scenario:inject",
        json={"kind": "po_eta_change", "payload": {"po_code": "PO-BD-0002", "days": 7}},
        headers={"Idempotency-Key": key()},
    )
    assert r.status_code == 200 and all(ln["eta_dc"] == "2026-11-10" for ln in r.json()["lines"])

    r = await http.post("/v1/admin/scenario:reset", headers={"Idempotency-Key": key()})
    assert r.status_code == 200 and r.json()["counts"]["store"] == 62
    po = (await http.get("/v1/purchase-orders/PO-BD-0002")).json()
    assert po["lines"][0]["eta_dc"] == "2026-11-03"
    audit = (await http.get("/v1/audit-log", params={"entity_type": "scenario"})).json()["items"]
    assert audit and audit[-1]["via"] == "Demo director" and audit[-1]["actor_username"] == "merxa.admin"
    http.cookies.clear()

    # Bearer tokens with the admin role also work; without DEMO role → 403.
    admin_token = realm.token(sub="admin-merxa-sub", azp="merxa-web", roles=["merxa-admin"])
    r = await http.post("/v1/admin/scenario:advance-clock", json={"days": 1}, headers=auth(admin_token, key()))
    assert r.status_code == 200
    r = await http.post("/v1/admin/scenario:advance-clock", json={"days": 1}, headers=auth(reader(realm), key()))
    assert r.status_code == 403


async def test_scenario_reset_restores_the_golden_seed(
    http: httpx.AsyncClient, realm: FakeRealm, engine: AsyncEngine, reseed: None
) -> None:
    async with engine.connect() as conn:
        golden_tables = await db_fingerprint(conn)
    admin = realm.token(sub="admin-merxa-sub", azp="merxa-web", roles=["merxa-admin"])

    # Drift the demo: move the BD PO, adjust stock, inject an email, advance the clock,
    # open a change set.
    for body in (
        {"kind": "po_eta_change", "payload": {"po_code": "PO-BD-0001", "days": 7}},
        {"kind": "inventory_adjust", "payload": {"location_code": "UK1", "sku": "PUF-W-BLK-M", "delta_on_hand": 5}},
        {"kind": "trim_delay"},
    ):
        r = await http.post("/v1/admin/scenario:inject", json=body, headers=auth(admin, key()))
        assert r.status_code == 200, r.text
    r = await http.post("/v1/admin/scenario:advance-clock", json={"days": 14}, headers=auth(admin, key()))
    assert r.status_code == 200
    r = await http.post("/v1/change-sets", json={"title": "drift"}, headers=auth(preparer(realm), key()))
    assert r.status_code == 201, r.text
    async with engine.connect() as conn:
        assert await db_fingerprint(conn) != golden_tables

    r = await http.post("/v1/admin/scenario:reset", headers=auth(admin, key()))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "reset" and body["profile"] == "small"
    assert body["dataset_sha256"] == dataset_fingerprint(generate("small"))
    assert isinstance(body["duration_ms"], int) and body["duration_ms"] >= 0
    assert body["counts"]["allocation"] > 10_000
    async with engine.connect() as conn:
        assert await db_fingerprint(conn) == golden_tables
        today = (await conn.execute(select(t.sim_clock.c.today))).scalar_one()
        assert today.isoformat() == "2026-10-05"
        assert (await conn.execute(select(t.change_set.c.id).limit(1))).first() is None

    # A second reset is just as good (and served from the cached golden dataset).
    again = await http.post("/v1/admin/scenario:reset", headers=auth(admin, key()))
    assert again.status_code == 200 and again.json()["dataset_sha256"] == body["dataset_sha256"]
    async with engine.connect() as conn:
        assert await db_fingerprint(conn) == golden_tables
    r = await http.get("/v1/audit-log", params={"entity_type": "scenario"}, headers=auth(reader(realm)))
    audit = r.json()["items"]
    assert audit[-1]["action"] == "reset" and audit[-1]["after"]["dataset_sha256"] == body["dataset_sha256"]
