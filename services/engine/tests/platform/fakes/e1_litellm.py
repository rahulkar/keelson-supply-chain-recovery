"""In-memory LiteLLM management API (respx) for bootstrap and AI Gateway facade tests."""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from typing import Any

import httpx
import respx


class FakeLiteLLM:
    def __init__(self, base: str, master_key: str) -> None:
        self.base = base.rstrip("/")
        self.master_key = master_key
        self.models: dict[str, dict[str, Any]] = {}
        self.teams: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.health: dict[str, dict[str, Any]] = {}
        """``/health/latest`` payload (keyed by model id or name)."""
        self.health_raw: str | None = None
        """When set, ``/health/latest`` answers 200 with this raw (non-JSON) body."""
        self.daily: list[dict[str, Any]] = []
        """``/team/daily/activity`` results (``DailySpendData`` dicts incl. ``team_id``)."""
        self.json_bodies: list[str] = []
        self.urls: list[str] = []
        """Every requested URL (incl. query) — tests assert no raw key ever appears in one."""

    def install(self, router: respx.Router) -> None:
        b = self.base
        healthy = httpx.Response(200, json={"status": "healthy"})
        router.get(f"{b}/health/readiness", name="litellm-health").mock(return_value=healthy)
        router.get(f"{b}/model/info", name="litellm-model-info").mock(side_effect=self._model_info)
        router.post(f"{b}/model/new", name="litellm-model-new").mock(side_effect=self._model_new)
        router.patch(url__regex=rf"^{b}/model/[^/]+/update$", name="litellm-model-update").mock(
            side_effect=self._model_update
        )
        router.get(url__startswith=f"{b}/team/info", name="litellm-team-info").mock(side_effect=self._team_info)
        router.post(f"{b}/team/new", name="litellm-team-new").mock(side_effect=self._team_new)
        router.post(f"{b}/team/update", name="litellm-team-update").mock(side_effect=self._team_update)
        router.get(url__startswith=f"{b}/key/info", name="litellm-key-info").mock(side_effect=self._key_info)
        router.post(f"{b}/key/generate", name="litellm-key-generate").mock(side_effect=self._key_generate)
        router.post(f"{b}/key/delete", name="litellm-key-delete").mock(side_effect=self._key_delete)
        router.post(f"{b}/key/update", name="litellm-key-update").mock(side_effect=self._key_update)
        router.post(f"{b}/model/delete", name="litellm-model-delete").mock(side_effect=self._model_delete)
        router.get(f"{b}/health/latest", name="litellm-health-latest").mock(side_effect=self._health_latest)
        router.get(url__startswith=f"{b}/team/daily/activity", name="litellm-team-daily").mock(
            side_effect=self._team_daily
        )

    def _auth(self, req: httpx.Request) -> bool:
        return req.headers.get("authorization") == f"Bearer {self.master_key}"

    def _log(self, req: httpx.Request) -> None:
        self.calls.append((req.method, req.url.path))
        self.urls.append(str(req.url))
        if req.content:
            self.json_bodies.append(req.content.decode("utf-8", "replace"))

    def team_models(self, team_id: str) -> list[str]:
        return list(self.teams[team_id].get("models") or [])

    def _model_delete(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        if not self._auth(req):
            return httpx.Response(401)
        mid = json.loads(req.content).get("id")
        if mid not in self.models:
            return httpx.Response(400, json={"detail": {"error": "model not found"}})
        del self.models[mid]
        return httpx.Response(200, json={"message": f"Model: {mid} deleted successfully"})

    def _health_latest(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        if not self._auth(req):
            return httpx.Response(401)
        if self.health_raw is not None:
            return httpx.Response(200, text=self.health_raw)
        return httpx.Response(200, json={"latest_health_checks": self.health, "total_models": len(self.health)})

    def _team_daily(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        if not self._auth(req):
            return httpx.Response(401)
        team = req.url.params.get("team_ids")
        start, end = req.url.params.get("start_date"), req.url.params.get("end_date")
        page, size = int(req.url.params.get("page", "1")), int(req.url.params.get("page_size", "10"))
        rows = [r for r in self.daily if r.get("team_id") == team and start <= r["date"] <= end]
        chunk = rows[(page - 1) * size: page * size]
        pages = max(1, -(-len(rows) // size))
        return httpx.Response(200, json={
            "results": [{k: v for k, v in r.items() if k != "team_id"} for r in chunk],
            "metadata": {"page": page, "total_pages": pages, "has_more": page < pages},
        })

    def _find_key(self, value: str | None) -> str | None:
        """Real LiteLLM accepts the plaintext key or its sha256 hex digest."""
        for key in self.keys:
            if value in (key, hashlib.sha256(key.encode("utf-8")).hexdigest()):
                return key
        return None

    def _model_info(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        if not self._auth(req):
            return httpx.Response(401)
        return httpx.Response(200, json={"data": list(self.models.values())})

    def _model_new(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        if not self._auth(req):
            return httpx.Response(401)
        body = json.loads(req.content)
        mid = uuid.uuid4().hex
        self.models[mid] = {
            "model_name": body["model_name"],
            "litellm_params": body["litellm_params"],
            "model_info": {**body.get("model_info", {}), "id": mid, "db_model": True},
        }
        return httpx.Response(200, json={"model_id": mid})

    def _model_update(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        mid = req.url.path.split("/")[2]
        if mid not in self.models:
            return httpx.Response(404)
        body = json.loads(req.content)
        m = self.models[mid]
        m["litellm_params"].update(body.get("litellm_params") or {})
        m["model_info"].update(body.get("model_info") or {})
        return httpx.Response(200, json=m)

    def _team_info(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        if not self._auth(req):
            return httpx.Response(401, json={"detail": "invalid master key"})
        team_id = req.url.params.get("team_id")
        if team_id not in self.teams:
            return httpx.Response(404, json={"detail": {"error": f"Team not found, passed team_id={team_id}"}})
        return httpx.Response(200, json={"team_id": team_id, "team_info": self.teams[team_id]})

    def _team_new(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        body = json.loads(req.content)
        if body["team_id"] in self.teams:
            return httpx.Response(400, json={"detail": "Team id = already exists"})
        self.teams[body["team_id"]] = body
        return httpx.Response(200, json=body)

    def _team_update(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        if not self._auth(req):
            return httpx.Response(401)
        body = json.loads(req.content)
        if body["team_id"] not in self.teams:
            return httpx.Response(404, json={"detail": "team not found"})
        self.teams[body["team_id"]].update(body)
        return httpx.Response(200, json={"team_id": body["team_id"], "data": self.teams[body["team_id"]]})

    def _key_info(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        if not self._auth(req):
            return httpx.Response(401)
        passed = req.url.params.get("key")
        key = self._find_key(passed)
        if key is None:
            return httpx.Response(404, json={"detail": "key not found"})
        info = self.keys[key]
        status = "revoked" if info.get("blocked") else "active"
        return httpx.Response(200, json={"key": passed, "info": {**info, "status": status}})

    def _key_update(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        if not self._auth(req):
            return httpx.Response(401)
        body = json.loads(req.content)
        key = self._find_key(body.pop("key", None))
        if key is None:
            return httpx.Response(404, json={"detail": "key not found"})
        self.keys[key].update(body)
        return httpx.Response(200, json={"key": key, **self.keys[key]})

    def _key_generate(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        body = json.loads(req.content)
        if any(k.get("key_alias") == body.get("key_alias") for k in self.keys.values()):
            return httpx.Response(400, json={"detail": "Unique key aliases across all keys are required"})
        key = "sk-" + secrets.token_urlsafe(16)
        self.keys[key] = dict(body)
        return httpx.Response(200, json={"key": key, **body})

    def _key_delete(self, req: httpx.Request) -> httpx.Response:
        self._log(req)
        body = json.loads(req.content)
        aliases = set(body.get("key_aliases") or [])
        wanted = {self._find_key(k) for k in body.get("keys") or []}
        doomed = [k for k, v in self.keys.items() if v.get("key_alias") in aliases or k in wanted]
        for k in doomed:
            del self.keys[k]
        return httpx.Response(200, json={"deleted_keys": doomed})
