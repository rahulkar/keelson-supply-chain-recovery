"""M8 Signals admin — clustering settings (``docs/contracts/123-m8-signal-clustering.md`` §7) against an
in-memory hub (``fakes/e1_signal_hub.py`` + the clustering routes, respx): the platform-scoped service
token, read for every admin / write and run for platform admins only (403 before any hub call), the
hub's 409 / 422 mapped through, the platform ``admin_audit`` row with the diff, and memberships passed
through ``GET /api/alerts/{id}`` unchanged. No LLM calls."""

from __future__ import annotations

import copy
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
import respx
from pydantic import SecretStr, ValidationError
from sqlalchemy import text

from e1_browser import Browser
from e1_env import TENANT_A
from e1_signal_hub import HUB, HUB_SECRET, FakeHub
from keelson_contracts.signals import DEFAULT_CLUSTERING_CONFIG, ClusteringConfigUpdate
from keelson_engine.config import Settings
from keelson_engine.core import signal_hub
from keelson_engine.core.db import system_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

CL = "/api/admin/signals/clustering"
COUNTS = dict.fromkeys(("simhash_near_duplicates", "same_source_duplicates", "extractions_reused", "anchor",
                        "same_hazard", "founded", "attach_geo", "attach_ref", "attach_vector", "near_duplicate",
                        "absorbed", "merged_vector", "embeddings_pending", "embeddings_failed"), 0)


class ClusteringHub(FakeHub):
    """The FakeHub plus ``/v1/clustering*`` (platform scope checked like the real hub) and
    memberships on alert details."""

    def __init__(self) -> None:
        super().__init__()
        self.config: dict[str, Any] = DEFAULT_CLUSTERING_CONFIG.model_dump(mode="json")
        self.writes: list[dict[str, Any]] = []
        self.runs = 0
        self.memberships: dict[str, list[dict[str, Any]]] = {}  # alert id → memberships
        self.invalid = False

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if not path.startswith("/v1/clustering"):
            resp = super().handle(request)
            alert_id = path.rsplit("/", 1)[-1]
            if request.method == "GET" and path.startswith("/v1/alerts/") and alert_id in self.memberships \
                    and resp.status_code == 200:
                body = json.loads(resp.content)
                body["memberships"] = self.memberships[alert_id]
                return httpx.Response(200, json=body)
            return resp
        if self.down:
            return httpx.Response(503, json={"detail": "down"})
        claims = self._claims(request)
        if claims is None:
            return httpx.Response(401, json={"detail": "bad token"})
        self.requests.append((request.method, path, str(claims["tenant"])))
        platform = claims.get("scope") == "platform"
        if request.method == "GET" and path == "/v1/clustering":
            if self.invalid:
                return httpx.Response(200, json={"config": {}})
            return httpx.Response(200, json={
                "config": self.config, "window_days": 7, "counts": {**COUNTS, "near_duplicate": 2},
                "vector_backend": {"pgvector": True, "local_profile": "local-terms-v1", "remote": None,
                                   "remote_reason": "provider_local"}})
        if not platform:
            return httpx.Response(403, json={"detail": "changing the clustering settings needs a platform admin"})
        if request.method == "PUT" and path == "/v1/clustering/config":
            body = json.loads(request.content)
            self.writes.append(body)
            try:
                update = ClusteringConfigUpdate.model_validate(body)
            except ValidationError:
                return httpx.Response(422, json={"detail": "invalid data"})
            if update.base_version != self.config["version"]:
                return httpx.Response(409, json={"detail": {"message": "changed", "current_version": self.config["version"]}})
            self.config = {**update.model_dump(mode="json", include={"simhash", "vector"}), "version": update.base_version + 1,
                           "updated_by": update.updated_by, "updated_at": "2026-10-08T06:00:00Z"}
            return httpx.Response(200, json=self.config)
        if request.method == "POST" and path == "/v1/clustering/run":
            self.runs += 1
            return httpx.Response(200, json={"merged": [], "embedded": 0, "embedding_failed": 0})
        return httpx.Response(404, json={"detail": "not found"})


@pytest_asyncio.fixture(loop_scope="session")
async def hub(platform: None, settings: Settings, http_mock: respx.Router,
              monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[ClusteringHub]:
    monkeypatch.setattr(settings, "signal_hub_url", HUB)
    monkeypatch.setattr(settings, "signal_hub_shared_secret", SecretStr(HUB_SECRET))
    fake = ClusteringHub()
    fake.install(http_mock)
    yield fake


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any, platform: None) -> dict[str, Browser]:
    return {n: await browser_factory(n) for n in ("sam", "ava", "priya")}


def _claims(token: str) -> dict[str, Any]:
    return jwt.decode(token, HUB_SECRET, algorithms=["HS256"], audience="signal-hub", issuer="keelson-engine")


async def test_platform_scope_claim(hub: ClusteringHub) -> None:
    assert "scope" not in _claims(signal_hub.mint_hub_token(TENANT_A))
    assert _claims(signal_hub.mint_hub_token(TENANT_A, platform=True))["scope"] == "platform"


def _body(**vector_local: int) -> dict[str, Any]:
    cfg = copy.deepcopy(DEFAULT_CLUSTERING_CONFIG.model_dump(mode="json"))
    cfg["vector"]["local"].update(vector_local)
    return {"base_version": 1, "simhash": cfg["simhash"], "vector": cfg["vector"]}


async def test_read_for_admins_write_for_platform_admins(people: dict[str, Browser], hub: ClusteringHub) -> None:
    sam, ava, priya = people["sam"], people["ava"], people["priya"]
    r = await sam.get(CL)
    assert r.status_code == 200, r.text
    assert r.json()["editable"] is False and r.json()["config"]["version"] == 1
    assert r.json()["counts"]["near_duplicate"] == 2 and r.json()["vector_backend"]["remote_reason"] == "provider_local"
    assert (await ava.get(CL)).json()["editable"] is True
    assert (await priya.get(CL)).status_code == 403  # not an admin
    # a tenant admin may not change platform-wide settings: refused before any hub call
    writes = len(hub.writes)
    r = await sam.put(CL, json=_body(attach_bp=7200))
    assert r.status_code == 403 and r.json()["code"] == "platform_admin_required"
    assert (await sam.post(f"{CL}/run")).status_code == 403
    assert len(hub.writes) == writes and hub.runs == 0
    # the platform admin's change reaches the hub with a platform-scoped token, and is audited
    r = await ava.put(CL, json=_body(attach_bp=7200))
    assert r.status_code == 200, r.text
    assert (r.json()["version"], r.json()["vector"]["local"]["attach_bp"]) == (2, 7200)
    sent = hub.writes[-1]
    assert sent["updated_by"] and sent["vector"]["local"]["attach_bp"] == 7200
    put = [x for x in hub.requests if x[0] == "PUT" and x[1] == "/v1/clustering/config"]
    assert put[-1][2] == TENANT_A
    async with system_session() as s:
        row = (await s.execute(text(
            "SELECT tenant, actor_username, target, before, after FROM admin_audit WHERE action = 'signals.clustering' "
            "ORDER BY id DESC LIMIT 1"))).mappings().one()
    assert row["tenant"] is None and row["target"] == "signals:clustering"  # a platform row
    assert row["before"]["vector"]["local"]["attach_bp"] == 7000
    assert {"op": "replace", "path": "/vector/local/attach_bp", "before": 7000, "after": 7200} in row["after"]["diff"]
    # stale base → 409 stale_version; the engine validates the bounds itself (422, no hub call)
    r = await ava.put(CL, json=_body(attach_bp=7300))
    assert r.status_code == 409 and r.json()["code"] == "stale_version" and r.json()["current_version"] == 2
    writes = len(hub.writes)
    bad = _body(attach_bp=4000)
    bad["base_version"] = 2
    assert (await ava.put(CL, json=bad)).status_code == 422
    assert (await ava.put(CL, json={**_body(), "base_version": 2, "extra": 1})).status_code == 422
    assert len(hub.writes) == writes
    r = await ava.post(f"{CL}/run")
    assert r.status_code == 200 and r.json() == {"merged": [], "embedded": 0} and hub.runs == 1
    # hub problems
    hub.invalid = True
    assert (await sam.get(CL)).status_code == 502
    hub.invalid = False
    hub.down = True
    r = await sam.get(CL)
    assert r.status_code == 503
    hub.down = False


async def test_alert_detail_passes_memberships_through(people: dict[str, Browser], hub: ClusteringHub) -> None:
    alert = hub.add(TENANT_A, member_provenance=("inference", "inference"))
    event = hub.events[alert["event_id"]]
    first, second = event["member_signal_ids"]
    memberships = [
        {"signal_id": first, "event_id": event["id"], "method": "founded", "prior_method": None, "from_event_id": None,
         "distance_km": None, "shared_refs": [], "similarity_bp": None, "similarity_profile": None, "hamming_bits": None,
         "duplicate_of_signal_id": None, "config_version": 1, "explanation": "First report of this event.",
         "decided_at": "2026-10-08T06:00:00Z"},
        {"signal_id": second, "event_id": event["id"], "method": "near_duplicate", "prior_method": None,
         "from_event_id": None, "distance_km": None, "shared_refs": [], "similarity_bp": None,
         "similarity_profile": None, "hamming_bits": 3, "duplicate_of_signal_id": first, "config_version": 1,
         "explanation": "Near-duplicate of another report in this event (3 of 64 fingerprint bits differ).",
         "decided_at": "2026-10-08T06:00:00Z"},
    ]
    hub.memberships[alert["id"]] = memberships
    r = await people["priya"].get(f"/api/alerts/{alert['id']}")
    assert r.status_code == 200, r.text
    assert r.json()["memberships"] == memberships
    # a membership of another event does not match the contract: the hub answer is refused
    hub.memberships[alert["id"]] = [{**memberships[0], "event_id": "00000000-0000-5000-8000-000000000009"}]
    assert (await people["priya"].get(f"/api/alerts/{alert['id']}")).status_code == 503
    # an older hub without memberships: the field is simply absent
    del hub.memberships[alert["id"]]
    r = await people["priya"].get(f"/api/alerts/{alert['id']}")
    assert r.status_code == 200 and "memberships" not in r.json()
