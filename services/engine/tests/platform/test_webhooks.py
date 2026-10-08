"""Standard Webhooks receiver: signature (v1 HMAC over id.timestamp.body, whsec_ secret from the
connector's webhook_secret_ref), ±300 s timestamp window, dedupe on webhook-id, tenant from the
path only, size limit, rate limit, no Origin/CSRF; events listing for admins."""

from __future__ import annotations

import base64
import json
import time
import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio

from e1_admin import drop_connector
from e1_browser import Browser
from e1_env import TENANT_A, TENANT_B
from keelson_engine.config import Settings
from keelson_engine.core import connectors
from keelson_engine.core.webhooks import sign, verify_signature, webhook_key
from test_tokens import MERXA_SPEC

pytestmark = pytest.mark.asyncio(loop_scope="session")

SECRET = "whsec_" + base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
URL = f"/api/webhooks/{TENANT_A}/merxa"


@pytest_asyncio.fixture(loop_scope="session")
async def merxa_webhooks(platform: None, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    monkeypatch.setenv("MERXA_WEBHOOK_SECRET", SECRET)
    await connectors.upsert_connector(TENANT_A, {**MERXA_SPEC, "webhook_secret_env": "MERXA_WEBHOOK_SECRET"})
    # northwind has a connector with the same slug but no webhook secret
    await connectors.upsert_connector(TENANT_B, {**MERXA_SPEC})
    yield
    await drop_connector(TENANT_B, "merxa")


def _delivery(event_id: str | None = None, ts: int | None = None, body: dict[str, Any] | None = None,
              secret: str = SECRET) -> tuple[bytes, dict[str, str]]:
    eid = event_id or f"msg_{uuid.uuid4().hex}"
    stamp = str(ts if ts is not None else int(time.time()))
    raw = json.dumps(body or {"type": "change_set.applied", "id": eid, "timestamp": stamp, "tenant_hint": None,
                              "data": {"change_set_code": "CS-0001"}}).encode()
    return raw, {"webhook-id": eid, "webhook-timestamp": stamp, "webhook-signature": sign(secret, eid, stamp, raw),
                 "content-type": "application/json"}


async def _events(b: Browser) -> list[dict[str, Any]]:
    r = await b.get("/api/admin/connectors/merxa/events", params={"limit": 100})
    assert r.status_code == 200, r.text
    return list(r.json()["items"])


async def test_signature_helpers() -> None:
    raw = b'{"a":1}'
    sig = sign(SECRET, "msg_1", "1700000000", raw)
    assert sig.startswith("v1,")
    assert verify_signature(SECRET, "msg_1", "1700000000", raw, f"v1,bogus {sig}")
    assert not verify_signature(SECRET, "msg_1", "1700000001", raw, sig)
    assert not verify_signature(SECRET, "msg_2", "1700000000", raw, sig)
    assert not verify_signature("whsec_" + base64.b64encode(b"other").decode(), "msg_1", "1700000000", raw, sig)
    assert not verify_signature(SECRET, "msg_1", "1700000000", raw, sig.replace("v1,", "v2,"))
    assert webhook_key("plain-secret") == b"plain-secret"
    # a whsec_ secret whose base64 padding was stripped still decodes (as the Merxa sender does)
    key = b"0123456789abcdef0"  # 17 bytes -> base64 needs padding
    padded = base64.b64encode(key).decode()
    assert padded.endswith("=")
    assert webhook_key("whsec_" + padded.rstrip("=")) == key == webhook_key("whsec_" + padded)
    assert verify_signature("whsec_" + padded.rstrip("="), "msg_1", "1700000000", raw,
                            sign("whsec_" + padded, "msg_1", "1700000000", raw))


async def test_receive_dedupe_and_list(merxa_webhooks: None, api: httpx.AsyncClient, browser_factory: Any) -> None:
    sam: Browser = await browser_factory("sam")
    before = len(await _events(sam))
    raw, headers = _delivery()
    r = await api.post(URL, content=raw, headers=headers)  # no cookie, no Origin, no CSRF
    assert r.status_code == 204, r.text
    again = await api.post(URL, content=raw, headers=headers)
    assert again.status_code == 204
    events = await _events(sam)
    assert len(events) == before + 1
    assert events[0]["event_id"] == headers["webhook-id"] and events[0]["type"] == "change_set.applied"
    assert events[0]["payload"]["data"] == {"change_set_code": "CS-0001"} and events[0]["processed_at"] is None
    # other tenants never see it; planners are not admins
    nora: Browser = await browser_factory("nora")
    assert (await nora.get("/api/admin/connectors/merxa/events")).status_code == 403
    priya: Browser = await browser_factory("priya")
    assert (await priya.get("/api/admin/connectors/merxa/events")).status_code == 403


async def test_rejections(merxa_webhooks: None, api: httpx.AsyncClient, settings: Settings,
                          monkeypatch: pytest.MonkeyPatch, browser_factory: Any) -> None:
    raw, headers = _delivery()
    bad_sig = {**headers, "webhook-signature": "v1," + base64.b64encode(b"x" * 32).decode()}
    r = await api.post(URL, content=raw, headers=bad_sig)
    assert r.status_code == 401 and r.json()["code"] == "invalid_signature"
    tampered = raw.replace(b"CS-0001", b"CS-9999")
    assert (await api.post(URL, content=tampered, headers=headers)).status_code == 401
    wrong_secret = _delivery(secret="whsec_" + base64.b64encode(b"another-secret").decode())
    assert (await api.post(URL, content=wrong_secret[0], headers=wrong_secret[1])).status_code == 401

    # (±305 s: int() truncation of the sender clock plus request time must not straddle the 300 s edge)
    old_raw, old_headers = _delivery(ts=int(time.time()) - 305)
    r = await api.post(URL, content=old_raw, headers=old_headers)
    assert r.status_code == 401 and r.json()["code"] == "timestamp_out_of_range"
    future_raw, future_headers = _delivery(ts=int(time.time()) + 305)
    assert (await api.post(URL, content=future_raw, headers=future_headers)).status_code == 401
    edge_raw, edge_headers = _delivery(ts=int(time.time()) - 290)
    assert (await api.post(URL, content=edge_raw, headers=edge_headers)).status_code == 204

    for missing in ("webhook-id", "webhook-timestamp", "webhook-signature"):
        h = {k: v for k, v in headers.items() if k != missing}
        r = await api.post(URL, content=raw, headers=h)
        assert r.status_code == 400 and r.json()["code"] == "missing_headers"
    r = await api.post(URL, content=raw, headers={**headers, "webhook-timestamp": "yesterday"})
    assert r.status_code == 400

    not_json = b"not json"
    eid, stamp = "msg_notjson", str(int(time.time()))
    nj_headers = {"webhook-id": eid, "webhook-timestamp": stamp, "webhook-signature": sign(SECRET, eid, stamp, not_json)}
    assert (await api.post(URL, content=not_json, headers=nj_headers)).status_code == 400

    # tenant comes from the path: northwind's merxa has no webhook secret → 404
    r = await api.post(f"/api/webhooks/{TENANT_B}/merxa", content=raw, headers=headers)
    assert r.status_code == 404
    assert (await api.post(f"/api/webhooks/{TENANT_A}/nope", content=raw, headers=headers)).status_code == 404
    assert (await api.post("/api/webhooks/Bad_Tenant/merxa", content=raw, headers=headers)).status_code == 404
    assert (await api.post("/api/webhooks/no-such-tenant/merxa", content=raw, headers=headers)).status_code == 404

    # size limit (declared and streamed)
    monkeypatch.setattr(settings, "webhook_max_body_bytes", 64)
    big, big_headers = _delivery(body={"type": "x", "data": "y" * 200})
    r = await api.post(URL, content=big, headers=big_headers)
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"

    async def chunks() -> Any:
        yield big[:50]
        yield big[50:]

    streamed = {k: v for k, v in big_headers.items()}
    r = await api.post(URL, content=chunks(), headers=streamed)
    assert r.status_code == 413
    monkeypatch.setattr(settings, "webhook_max_body_bytes", 256 * 1024)

    # a disabled connector receives nothing
    sam: Browser = await browser_factory("sam")
    from sqlalchemy import text

    from keelson_engine.core.db import tenant_session

    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE connector SET status = 'disabled' WHERE slug = 'merxa'"))
    try:
        fresh = _delivery()
        assert (await api.post(URL, content=fresh[0], headers=fresh[1])).status_code == 404
    finally:
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("UPDATE connector SET status = 'active' WHERE slug = 'merxa'"))
    assert sam  # (admin session unused beyond setup)


async def test_rate_limit_and_missing_secret(merxa_webhooks: None, api: httpx.AsyncClient, settings: Settings,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    from keelson_engine.core.valkey import get_valkey

    for key in [k async for k in get_valkey().scan_iter(match="keelson:wh:rl:*")]:
        await get_valkey().delete(key)
    monkeypatch.setattr(settings, "webhook_rate_per_minute", 3)
    codes = []
    for _ in range(5):
        raw, headers = _delivery()
        codes.append((await api.post(URL, content=raw, headers=headers)).status_code)
    assert codes[:3] == [204, 204, 204] and codes[3:] == [429, 429]
    monkeypatch.setattr(settings, "webhook_rate_per_minute", 600)
    for key in [k async for k in get_valkey().scan_iter(match="keelson:wh:rl:*")]:
        await get_valkey().delete(key)

    monkeypatch.delenv("MERXA_WEBHOOK_SECRET")
    raw, headers = _delivery()
    assert (await api.post(URL, content=raw, headers=headers)).status_code == 404


async def test_secret_ref_must_name_a_webhook_secret(merxa_webhooks: None, api: httpx.AsyncClient,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Review fix (defence in depth): a connector row whose webhook_secret_ref names some other
    platform secret (e.g. via a mis-authored pack) never keys the receiver."""
    from sqlalchemy import text

    from keelson_engine.core.db import tenant_session

    monkeypatch.setenv("SESSION_SECRET", SECRET)
    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE connector SET webhook_secret_ref = 'SESSION_SECRET' WHERE slug = 'merxa'"))
    try:
        raw, headers = _delivery()
        assert (await api.post(URL, content=raw, headers=headers)).status_code == 404
    finally:
        async with tenant_session(TENANT_A) as s:
            await s.execute(text(
                "UPDATE connector SET webhook_secret_ref = 'MERXA_WEBHOOK_SECRET' WHERE slug = 'merxa'"))
    raw, headers = _delivery()
    assert (await api.post(URL, content=raw, headers=headers)).status_code == 204


async def test_origin_exemption_is_only_the_receiver_path(api: httpx.AsyncClient) -> None:
    """Review fix: the Origin-check exemption covered every path under /api/webhooks/."""
    r = await api.post(f"{URL}/extra", content=b"{}")
    assert r.status_code == 403 and r.json()["code"] == "bad_origin"
    r = await api.post("/api/webhooks/.hidden/merxa", content=b"{}")
    assert r.status_code == 403 and r.json()["code"] == "bad_origin"
