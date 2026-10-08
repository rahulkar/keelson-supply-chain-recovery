"""A logged-in test browser for the engine API (cookie + CSRF + Origin)."""

from __future__ import annotations

from typing import Any

import httpx

from e1_env import PUBLIC


class Browser:
    """A logged-in browser: cookie + CSRF token + Origin on mutations."""

    def __init__(self, client: httpx.AsyncClient, cookie: str, csrf: str, me: dict[str, Any]) -> None:
        self.client = client
        self.cookie = cookie
        self.csrf = csrf
        self.me = me

    def _headers(self, extra: dict[str, str] | None, mutating: bool) -> dict[str, str]:
        headers = {"Cookie": f"keelson_session={self.cookie}"}
        if mutating:
            headers.update({"Origin": PUBLIC, "X-CSRF-Token": self.csrf})
        headers.update(extra or {})
        return headers

    async def get(self, url: str, headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        return await self.client.get(url, headers=self._headers(headers, False), **kw)

    async def post(self, url: str, headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        return await self.client.post(url, headers=self._headers(headers, True), **kw)

    async def delete(self, url: str, headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        return await self.client.delete(url, headers=self._headers(headers, True), **kw)

    async def put(self, url: str, headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        return await self.client.put(url, headers=self._headers(headers, True), **kw)

    async def patch(self, url: str, headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        return await self.client.patch(url, headers=self._headers(headers, True), **kw)


async def login_as(client: httpx.AsyncClient, username: str) -> Browser:
    client.cookies.clear()
    resp = await client.post("/api/demo/login-as", json={"username": username}, headers={"Origin": PUBLIC})
    assert resp.status_code == 200, resp.text
    cookie = resp.cookies.get("keelson_session")
    assert cookie
    client.cookies.clear()
    me = await client.get("/api/me", headers={"Cookie": f"keelson_session={cookie}"})
    assert me.status_code == 200, me.text
    return Browser(client, cookie, me.json()["csrf_token"], me.json())
