"""Signed part downloads (no auth header) and snapshot housekeeping."""

from __future__ import annotations

import time
from urllib.parse import urlsplit

import pytest
from conftest import Running
from helpers import call
from sqlalchemy import text

from adapter_sidecar.db import begin_write


async def test_signed_download_round_trip(fernhill: Running) -> None:
    async with fernhill.client() as c:
        m = await call(c, "scn.snapshot_open", {"entities": ["allocation"]})
        res = await c.call_tool("scn.snapshot_export", {"snapshot_id": m["snapshot_id"], "entity": "allocation"})
    data = res.structured_content
    assert data is not None
    part = data["parts"][0]
    links = [b for b in res.content if getattr(b, "type", None) == "resource_link"]
    assert [str(link.uri) for link in links] == [part["uri"]]
    assert links[0].mime_type == "application/vnd.apache.parquet" and links[0].size == part["bytes"]
    url = urlsplit(part["uri"])
    assert f"{url.scheme}://{url.netloc}" == "http://fernhill.localhost"
    async with fernhill.http() as http:
        r = await http.get(f"{url.path}?{url.query}")  # no auth header
        assert r.status_code == 200 and r.headers["content-type"] == "application/vnd.apache.parquet"
        import hashlib

        assert hashlib.sha256(r.content).hexdigest() == part["sha256"]


@pytest.mark.parametrize(("mutate", "status", "code"), [
    (lambda p, q: (p, q.replace("sig=", "sig=0")), 403, "invalid_signature"),
    (lambda p, q: (p, q.split("&sig=")[0] + "&sig=" + "g" * 64), 403, "invalid_signature"),
    (lambda p, q: (p, q.split("&sig=")[0] + "&sig=" + "é" * 64), 403, "invalid_signature"),
    (lambda p, q: (p, q.split("&")[0]), 403, "invalid_signature"),
    (lambda p, q: (p.replace("/allocation/", "/promotion/"), q), 403, "invalid_signature"),
    (lambda p, q: (p.replace("/allocation/", "/shipment/"), q), 404, "not_found"),
    (lambda p, q: (p.replace("part-0001.parquet", "manifest.json"), q), 404, "not_found"),
    (lambda p, q: (p.replace("part-0001.parquet", "part-1.parquet"), q), 404, "not_found"),
    (lambda p, q: (p.replace("/v1/snapshots/", "/v1/snapshots/../"), q), 404, None),  # never reaches the route
])
async def test_bad_download_urls(fernhill: Running, mutate: object, status: int, code: str | None) -> None:
    async with fernhill.client() as c:
        m = await call(c, "scn.snapshot_open", {"entities": ["allocation"]})
        e = await call(c, "scn.snapshot_export", {"snapshot_id": m["snapshot_id"], "entity": "allocation"})
    url = urlsplit(e["parts"][0]["uri"])
    path, query = mutate(url.path, url.query)  # type: ignore[operator]
    async with fernhill.http() as http:
        r = await http.get(f"{path}?{query}")
    assert r.status_code == status
    if code is not None:
        assert r.json()["code"] == code


async def test_expired_url_and_cleanup(fernhill: Running) -> None:
    snaps = fernhill.sidecar.snapshots
    async with fernhill.client() as c:
        m = await call(c, "scn.snapshot_open", {"entities": ["allocation"]})
    sid = m["snapshot_id"]
    path = f"{sid}/allocation/part-0001.parquet"
    past = int(time.time()) - 5
    async with fernhill.http() as http:
        r = await http.get(f"/v1/snapshots/{path}?exp={past}&sig={snaps.sign(path, past)}")
    assert r.status_code == 403 and r.json()["code"] == "url_expired"
    assert await snaps.cleanup() == 0
    async with begin_write(fernhill.sidecar.state) as conn:
        await conn.execute(text("UPDATE snapshot SET expires_at = '2000-01-01T00:00:00Z' WHERE id = :i"), {"i": sid})
    assert await snaps.cleanup() == 1
    assert not (fernhill.tmp / "snapshots" / sid).exists()
    async with fernhill.client() as c:
        from fastmcp.exceptions import ToolError

        with pytest.raises(ToolError, match="not_found"):
            await c.call_tool("scn.snapshot_export", {"snapshot_id": sid, "entity": "allocation"})
