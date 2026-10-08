"""LiteLLM proxy management API client (master key; bootstrap, readiness and the engine-api
AI Gateway facade — the key never leaves the server).

Endpoints verified against the installed ``litellm==1.104.0`` proxy source:
``GET /model/info``, ``POST /model/new``, ``PATCH /model/{id}/update``, ``POST /model/delete``
(``{id}``), ``GET /team/info``, ``POST /team/new``, ``POST /team/update``, ``GET /key/info``,
``POST /key/generate``, ``POST /key/update``, ``POST /key/delete`` (``{keys}`` accepts sha256
hashes, ``{key_aliases}``), ``GET /team/daily/activity`` (``SpendAnalyticsPaginatedResponse``),
``GET /health/latest`` (``{latest_health_checks: {<model_id|model_name>: {...}}}``).
"""

from __future__ import annotations

import hashlib
from typing import Any

import httpx

from keelson_engine.core.logs import get_logger

__all__ = ["LiteLLMAdmin", "LiteLLMError"]

log = get_logger(__name__)


class LiteLLMError(RuntimeError):
    def __init__(self, what: str, status: int, body: str = "") -> None:
        super().__init__(f"LiteLLM {what} failed: HTTP {status} {body[:300]}")
        self.status = status


class LiteLLMAdmin:
    def __init__(self, base_url: str, master_key: str, http: httpx.AsyncClient) -> None:
        self.base = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {master_key}"}
        self.http = http

    async def _req(
        self, method: str, path: str, *, what: str, ok: tuple[int, ...] = (200,), **kw: Any
    ) -> httpx.Response:
        resp = await self.http.request(method, f"{self.base}{path}", headers=self._headers, **kw)
        if resp.status_code not in ok:
            raise LiteLLMError(what, resp.status_code, resp.text)
        return resp

    async def ready(self) -> bool:
        try:
            resp = await self.http.get(f"{self.base}/health/readiness", timeout=5.0)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200

    # ───────────────────────────── models ─────────────────────────────

    async def models(self) -> list[dict[str, Any]]:
        resp = await self._req("GET", "/model/info", what="model/info")
        return list(resp.json().get("data") or [])

    async def delete_alias(self, alias: str) -> int:
        """Delete every DB deployment serving ``alias``; returns how many."""
        n = 0
        for m in await self.models():
            info = m.get("model_info") or {}
            if m.get("model_name") == alias and info.get("id"):
                if info.get("db_model") is False:
                    raise LiteLLMError(f"alias {alias} is defined in config.yaml (not DB); cannot delete", 409)
                await self.delete_model(str(info["id"]))
                n += 1
        return n

    async def upsert_model(self, alias: str, litellm_params: dict[str, Any], model_info: dict[str, Any]) -> str:
        """Create alias ``alias`` or update the DB-stored deployment that already serves it.

        Returns ``created`` / ``updated`` / ``unchanged``.
        """
        existing = [m for m in await self.models() if m.get("model_name") == alias]
        for m in existing:
            info = m.get("model_info") or {}
            current = m.get("litellm_params") or {}
            same = all(current.get(k) == v for k, v in litellm_params.items() if k != "api_key") and info.get(
                "mode"
            ) == model_info.get("mode")
            model_id = info.get("id")
            if not model_id:
                continue
            if same:
                return "unchanged"
            if info.get("db_model") is False:
                raise LiteLLMError(f"alias {alias} is defined in config.yaml (not DB); cannot update", 409)
            await self._req(
                "PATCH", f"/model/{model_id}/update", what=f"update {alias}",
                json={"model_name": alias, "litellm_params": litellm_params, "model_info": model_info},
            )
            return "updated"
        await self._req(
            "POST", "/model/new", what=f"create {alias}",
            json={"model_name": alias, "litellm_params": litellm_params, "model_info": model_info},
        )
        return "created"

    # ───────────────────────────── teams ─────────────────────────────

    async def team_info(self, team_id: str) -> dict[str, Any] | None:
        resp = await self.http.get(f"{self.base}/team/info", params={"team_id": team_id}, headers=self._headers)
        if resp.status_code == 200:
            data = resp.json()
            return dict(data.get("team_info") or data)
        if resp.status_code in (400, 404, 422) or (resp.status_code == 500 and "not" in resp.text.lower()):
            return None
        raise LiteLLMError("team/info", resp.status_code, resp.text)

    async def upsert_team(self, team_id: str, body: dict[str, Any]) -> str:
        payload = {"team_id": team_id, **body}
        if await self.team_info(team_id) is None:
            resp = await self.http.post(f"{self.base}/team/new", json=payload, headers=self._headers)
            if resp.status_code == 200:
                return "created"
            if "exist" not in resp.text.lower():
                raise LiteLLMError("team/new", resp.status_code, resp.text)
        await self._req("POST", "/team/update", what="team/update", json=payload)
        return "updated"

    # ───────────────────────────── keys ─────────────────────────────

    async def key_valid(self, key: str) -> bool:
        """True when ``key`` exists and is usable. The lookup passes the key's sha256 (LiteLLM
        accepts the hash), so the raw virtual key never appears in a URL / access log."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        resp = await self.http.get(f"{self.base}/key/info", params={"key": digest}, headers=self._headers)
        if resp.status_code != 200:
            return False
        info = resp.json().get("info") or {}
        status = info.get("status")
        if status is not None:
            return bool(status == "active")
        return not info.get("blocked")

    async def update_key(self, key: str, body: dict[str, Any]) -> None:
        """``POST /key/update`` (merge patch; the key travels in the JSON body, not the URL)."""
        await self._req("POST", "/key/update", what="key/update", json={"key": key, **body})

    async def delete_keys_by_alias(self, alias: str) -> None:
        resp = await self.http.post(f"{self.base}/key/delete", json={"key_aliases": [alias]}, headers=self._headers)
        if resp.status_code not in (200, 404, 400):
            log.warning("LiteLLM key delete by alias failed", status=resp.status_code)

    async def generate_key(self, body: dict[str, Any]) -> str:
        resp = await self._req("POST", "/key/generate", what="key/generate", json=body)
        key = resp.json().get("key")
        if not key:
            raise LiteLLMError("key/generate returned no key", resp.status_code)
        return str(key)

    async def delete_key(self, key: str) -> None:
        """Delete one virtual key by its sha256 hash (the raw key is never sent again)."""
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        resp = await self.http.post(f"{self.base}/key/delete", json={"keys": [digest]}, headers=self._headers)
        if resp.status_code not in (200, 404):
            raise LiteLLMError("key/delete", resp.status_code, resp.text)

    # ───────────────────────────── facade helpers ─────────────────────────────

    async def update_team(self, team_id: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._req("POST", "/team/update", what="team/update", json={"team_id": team_id, **body})
        data = resp.json()
        return dict(data.get("data") or data) if isinstance(data, dict) else {}

    async def delete_model(self, model_id: str) -> None:
        await self._req("POST", "/model/delete", what="model/delete", json={"id": model_id})

    async def health_latest(self) -> dict[str, Any] | None:
        """``/health/latest`` checks keyed by model id or name; None when unavailable."""
        try:
            resp = await self.http.get(f"{self.base}/health/latest", headers=self._headers, timeout=5.0)
        except httpx.HTTPError:
            return None
        if resp.status_code != 200:
            return None
        try:
            data = resp.json()
        except ValueError:  # health is a badge only: a malformed body must not fail the models view
            return None
        checks = data.get("latest_health_checks") if isinstance(data, dict) else None
        return dict(checks) if isinstance(checks, dict) else None

    async def team_daily_activity(self, team_id: str, start_date: str, end_date: str,
                                  *, max_pages: int = 20) -> list[dict[str, Any]]:
        """``DailySpendData`` rows of ``team_id`` between the ISO dates (all pages)."""
        rows: list[dict[str, Any]] = []
        page = 1
        while page <= max_pages:
            resp = await self._req(
                "GET", "/team/daily/activity", what="team/daily/activity",
                params={"team_ids": team_id, "start_date": start_date, "end_date": end_date,
                        "page": page, "page_size": 100},
            )
            data = resp.json()
            rows.extend(data.get("results") or [])
            meta = data.get("metadata") or {}
            if not meta.get("has_more") and page >= int(meta.get("total_pages") or 1):
                break
            page += 1
        return rows
