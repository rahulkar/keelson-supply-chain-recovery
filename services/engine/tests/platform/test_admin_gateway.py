"""Back-office AI Gateway facade over a fake LiteLLM (respx): tenant vs platform model views,
alias rules + upsert/delete, tenant team view/patch (rpm/tpm vs budget), key rotation with the
runner-cache signal, usage aggregation."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
import pytest_asyncio

from e1_admin import reset_specs
from e1_browser import Browser
from e1_env import TENANT_A, TENANT_B
from e1_litellm import FakeLiteLLM
from keelson_engine.core import connectors, gateway
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.valkey import get_valkey

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any) -> dict[str, Browser]:
    await reset_specs()
    return {n: await browser_factory(n) for n in ("sam", "ava", "priya", "lena")}


async def test_models_views_and_health(people: dict[str, Browser], gateway_ready: FakeLiteLLM) -> None:
    fast_id = next(mid for mid, m in gateway_ready.models.items() if m["model_name"] == "fast-default")
    gateway_ready.health = {fast_id: {"status": "healthy", "checked_at": "2026-10-06T08:00:00Z",
                                      "response_time_ms": 412}}
    team = gateway_ready.teams[f"keelson-{TENANT_A}"]
    team["models"] = [m for m in team["models"] if m != "critic-default"]

    tenant_view = (await people["sam"].get("/api/admin/gateway/models")).json()
    assert tenant_view["view"] == "tenant"
    items = {i["alias"]: i for i in tenant_view["items"]}
    assert "critic-default" not in items  # not in the tenant team
    assert set(items["fast-default"]) == {"alias", "mode", "capabilities", "health"}  # no provider model
    assert items["fast-default"]["health"]["status"] == "healthy"
    assert items["reasoning-default"]["health"] is None
    assert items["embed-default"]["capabilities"] == {"structured_output": False, "reasoning_stream": False,
                                                      "tool_loop_signatures": False}

    platform_view = (await people["ava"].get("/api/admin/gateway/models")).json()
    assert platform_view["view"] == "platform"
    pitems = {i["alias"]: i for i in platform_view["items"]}
    assert pitems["fast-default"]["provider_model"] == "gemini/gemini-3.8-flash"
    assert pitems["fast-default"]["params"] == {"reasoning_effort": "low"}  # api_key reference hidden
    assert pitems["critic-default"]["tenant_access"] is False

    assert (await people["priya"].get("/api/admin/gateway/models")).status_code == 403
    assert (await people["lena"].get("/api/admin/gateway/models")).status_code == 403


async def test_models_view_survives_malformed_health(people: dict[str, Browser], gateway_ready: FakeLiteLLM) -> None:
    gateway_ready.health_raw = "<html>proxy error</html>"
    try:
        r = await people["sam"].get("/api/admin/gateway/models")
        assert r.status_code == 200, r.text
        assert r.json()["items"] and all(i["health"] is None for i in r.json()["items"])
    finally:
        gateway_ready.health_raw = None


async def test_alias_rules() -> None:
    assert gateway.normalize_alias("claude-fast", "anthropic/claude-sonnet-5") == "anthropic/claude-fast"
    assert gateway.normalize_alias("claude-b", "bedrock/eu.anthropic.claude-opus-5") == "anthropic/claude-b"
    assert gateway.normalize_alias("claude-v", "vertex_ai/claude-sonnet-5") == "anthropic/claude-v"
    assert gateway.normalize_alias("anthropic/x", "anthropic/claude-x") == "anthropic/x"
    assert gateway.normalize_alias("fast-2", "gemini/gemini-3.8-flash") == "fast-2"
    for alias, model in (("gemini/x", "gemini/x"), ("openai/x", "openai/gpt-6"), ("Fast", "gemini/x"),
                         ("-x", "gemini/x"), ("a_b", "gemini/x"), ("anthropic/x", "gemini/gemini-3")):
        with pytest.raises(AdminError):
            gateway.normalize_alias(alias, model)
    assert gateway.capabilities("o-1", "openai/gpt-6", "chat") == {
        "structured_output": True, "reasoning_stream": True, "tool_loop_signatures": False}
    assert gateway.capabilities("anthropic/c", "anthropic/claude-x", "chat")["tool_loop_signatures"] is True


async def test_model_upsert_grant_and_delete(people: dict[str, Browser], gateway_ready: FakeLiteLLM) -> None:
    sam, ava = people["sam"], people["ava"]
    body = {"provider_model": "gemini/gemini-3.8-flash", "mode": "chat", "params": {"reasoning_effort": "high"}}
    assert (await sam.put("/api/admin/gateway/models/fast-high", json=body)).status_code == 403

    r = await ava.put("/api/admin/gateway/models/fast-high", json={**body, "grant_all_tenants": True})
    assert r.status_code == 200, r.text
    assert r.json()["alias"] == "fast-high" and r.json()["result"] == "created"
    assert {TENANT_A, TENANT_B} <= set(r.json()["granted_tenants"])
    created = next(m for m in gateway_ready.models.values() if m["model_name"] == "fast-high")
    assert created["litellm_params"]["api_key"] == "os.environ/GEMINI_API_KEY"
    for tenant in (TENANT_A, TENANT_B):
        assert "fast-high" in gateway_ready.team_models(f"keelson-{tenant}")
        key = await connectors.get_tenant_llm_key(tenant)
        assert "fast-high" in gateway_ready.keys[key]["models"]  # the tenant key allow-list too
    again = await ava.put("/api/admin/gateway/models/fast-high", json=body)
    assert again.json()["result"] == "unchanged"
    patched = await ava.put("/api/admin/gateway/models/fast-high",
                            json={**body, "params": {"reasoning_effort": "medium"}})
    assert patched.json()["result"] == "updated"

    # Claude: auto-prefixed, needs reasoning_effort + max_tokens >= 16k
    claude = {"provider_model": "anthropic/claude-sonnet-5", "params": {"reasoning_effort": "medium"}}
    r = await ava.put("/api/admin/gateway/models/claude-plan", json=claude)
    assert r.status_code == 422 and r.json()["code"] == "invalid_params"
    r = await ava.put("/api/admin/gateway/models/claude-plan",
                      json={**claude, "params": {"reasoning_effort": "medium", "max_tokens": 16000}})
    assert r.status_code == 200 and r.json()["alias"] == "anthropic/claude-plan"
    bad = await ava.put("/api/admin/gateway/models/gemini-x",
                        json={"provider_model": "gemini/x", "params": {"api_key": "sk-literal"}})
    assert bad.status_code == 422  # secrets only as os.environ/ references
    for key in ("aws_session_token", "azure_ad_token", "client_secret"):
        bad = await ava.put("/api/admin/gateway/models/bedrock-x",
                            json={"provider_model": "openai/gpt-x", "params": {key: "literal-secret"}})
        assert bad.status_code == 422 and bad.json()["code"] == "invalid_params", key
    assert (await ava.put("/api/admin/gateway/models/Bad_Alias", json=body)).status_code == 422

    # delete: 409 while a tenant's latest spec (here: the built-in default) uses the alias
    r = await ava.delete("/api/admin/gateway/models/fast-default")
    assert r.status_code == 409 and r.json()["code"] == "alias_in_use" and TENANT_A in r.json()["tenants"]
    assert (await sam.delete("/api/admin/gateway/models/fast-high")).status_code == 403
    r = await ava.delete("/api/admin/gateway/models/fast-high")
    assert r.status_code == 200 and r.json()["deleted"] == 1
    assert not [m for m in gateway_ready.models.values() if m["model_name"] == "fast-high"]
    assert "fast-high" not in gateway_ready.team_models(f"keelson-{TENANT_A}")
    assert (await ava.delete("/api/admin/gateway/models/fast-high")).status_code == 404
    r = await ava.delete("/api/admin/gateway/models/anthropic/claude-plan")
    assert r.status_code == 200


async def test_tenant_view_and_patch(people: dict[str, Browser], gateway_ready: FakeLiteLLM) -> None:
    sam, ava = people["sam"], people["ava"]
    view = (await sam.get("/api/admin/gateway/tenant")).json()
    assert view["team_alias"] == "Keelson Demo Retail" and view["budget_eur"] == 50.0
    assert view["rpm_limit"] == 120 and view["tpm_limit"] == 400000 and view["budget_duration"] == "30d"
    key = await connectors.get_tenant_llm_key(TENANT_A)
    assert view["key_prefix"] == key[:7] + "…" and key not in str(view)

    r = await sam.patch("/api/admin/gateway/tenant", json={"rpm_limit": 200, "tpm_limit": 500000})
    assert r.status_code == 200, r.text
    assert r.json()["rpm_limit"] == 200 and gateway_ready.teams[f"keelson-{TENANT_A}"]["tpm_limit"] == 500000
    r = await sam.patch("/api/admin/gateway/tenant", json={"rpm_limit": 1_000_000})
    assert r.status_code == 403 and r.json()["code"] == "guarded_change"
    r = await sam.patch("/api/admin/gateway/tenant", json={"budget_eur": 500})
    assert r.status_code == 403 and r.json()["code"] == "guarded_change"
    r = await ava.patch("/api/admin/gateway/tenant", json={"budget_eur": 75, "budget_duration": "7d"})
    assert r.status_code == 200 and r.json()["budget_eur"] == 75.0 and r.json()["budget_duration"] == "7d"
    assert (await sam.patch("/api/admin/gateway/tenant", json={})).status_code == 422
    assert (await sam.patch("/api/admin/gateway/tenant", json={"rpm_limit": 0})).status_code == 422
    overrides = await gateway.tenant_overrides(TENANT_A)
    assert overrides == {"rpm_limit": 200, "tpm_limit": 500000, "budget_eur": 75.0, "budget_duration": "7d"}


async def test_bootstrap_keeps_admin_overrides(people: dict[str, Browser], settings: Any,
                                               litellm_fake: FakeLiteLLM) -> None:
    from keelson_engine.bootstrap import run_bootstrap
    from keelson_engine.core.db import system_session
    from sqlalchemy import text

    async with system_session() as s:
        await s.execute(text("UPDATE tenant SET admin_overrides = '{\"rpm_limit\": 77}'::jsonb WHERE slug = :t"),
                        {"t": TENANT_A})
    try:
        await run_bootstrap(settings, wait_timeout_s=5)
        team = litellm_fake.teams[f"keelson-{TENANT_A}"]
        assert team["rpm_limit"] == 77 and team["tpm_limit"] == 400000  # yaml for the rest
    finally:
        async with system_session() as s:
            await s.execute(text("UPDATE tenant SET admin_overrides = '{}'::jsonb"))
        await reset_specs()


async def test_bootstrap_keeps_granted_aliases(people: dict[str, Browser], settings: Any,
                                              gateway_ready: FakeLiteLLM) -> None:
    """Review fix: bootstrap re-provisioning used to reset every team/key allow-list to models.lock,
    revoking aliases granted in the back-office (a spec bound to one then failed at LLM call time)."""
    from keelson_engine.bootstrap import run_bootstrap

    ava = people["ava"]
    body = {"provider_model": "gemini/gemini-granted", "grant_all_tenants": True}
    assert (await ava.put("/api/admin/gateway/models/granted-x", json=body)).status_code == 200
    try:
        await run_bootstrap(settings, wait_timeout_s=5)
        for tenant in (TENANT_A, TENANT_B):
            assert "granted-x" in gateway_ready.team_models(f"keelson-{tenant}")
            key = await connectors.get_tenant_llm_key(tenant)
            assert "granted-x" in gateway_ready.keys[key]["models"]
            assert "fast-default" in gateway_ready.team_models(f"keelson-{tenant}")  # models.lock still there
    finally:
        assert (await ava.delete("/api/admin/gateway/models/granted-x")).status_code == 200
        await reset_specs()
    assert "granted-x" not in gateway_ready.team_models(f"keelson-{TENANT_A}")


async def test_grant_never_restricts_an_unrestricted_team(people: dict[str, Browser],
                                                         gateway_ready: FakeLiteLLM) -> None:
    """Review fix: granting an alias to a team without an allow-list (= every model) used to turn it
    into a one-alias allow-list."""
    team = gateway_ready.teams[f"keelson-{TENANT_B}"]
    saved = list(team.get("models") or [])
    team["models"] = []
    try:
        r = await people["ava"].put("/api/admin/gateway/models/granted-y",
                                    json={"provider_model": "gemini/gemini-y", "grant_all_tenants": True})
        assert r.status_code == 200, r.text
        assert gateway_ready.team_models(f"keelson-{TENANT_B}") == []
        assert "granted-y" in gateway_ready.team_models(f"keelson-{TENANT_A}")
        assert (await people["ava"].delete("/api/admin/gateway/models/granted-y")).status_code == 200
        assert gateway_ready.team_models(f"keelson-{TENANT_B}") == []
    finally:
        team["models"] = saved


async def test_key_version_read_drops_a_stale_cached_key(people: dict[str, Browser],
                                                         gateway_ready: FakeLiteLLM) -> None:
    """Review fix: after a rotation in another process the 10 s in-process key cache must not hand the
    rebuilt runner the deleted key once the new version has been observed."""
    old = await connectors.get_tenant_llm_key(TENANT_A)  # cached now
    version = await connectors.get_tenant_llm_key_version(TENANT_A)
    # another process rotates (stores a new key → key_version + 1)
    await connectors.store_tenant_llm_key(TENANT_A, "sk-rotated-elsewhere", key_alias="keelson-demo-retail",
                                          team_id=f"keelson-{TENANT_A}")
    connectors._llm_key_cache[TENANT_A] = (old, version, 1e18)  # this process still caches the old key
    assert await connectors.get_tenant_llm_key(TENANT_A) == old  # (cache window)
    assert await connectors.get_tenant_llm_key_version(TENANT_A) == version + 1
    assert await connectors.get_tenant_llm_key(TENANT_A) == "sk-rotated-elsewhere"
    connectors._llm_key_cache[TENANT_A] = (old, version + 1, 1e18)
    connectors.forget_tenant_llm_key(TENANT_A)
    assert await connectors.get_tenant_llm_key(TENANT_A) == "sk-rotated-elsewhere"


async def test_rotate_key(people: dict[str, Browser], gateway_ready: FakeLiteLLM) -> None:
    sam = people["sam"]
    old = await connectors.get_tenant_llm_key(TENANT_A)
    v_before = await connectors.get_tenant_llm_key_version(TENANT_A)
    r = await sam.post("/api/admin/gateway/tenant/rotate-key")
    assert r.status_code == 200, r.text
    body = r.json()
    new = await connectors.get_tenant_llm_key(TENANT_A)
    assert new != old and body["key_prefix"] == new[:7] + "…" and body["old_key_deleted"] is True
    assert old not in gateway_ready.keys and new in gateway_ready.keys
    assert gateway_ready.keys[new]["team_id"] == f"keelson-{TENANT_A}"
    assert sorted(gateway_ready.keys[new]["models"]) == sorted(gateway_ready.team_models(f"keelson-{TENANT_A}"))
    assert body["key_version"] == v_before + 1 == await connectors.get_tenant_llm_key_version(TENANT_A)
    # runner-cache invalidation signal for the worker
    assert await get_valkey().get(connectors.llm_key_version_key(TENANT_A)) in (str(v_before + 1),
                                                                                 str(v_before + 1).encode())
    # raw keys never travel in a URL; the old key is deleted by its sha256 hash
    assert not [u for u in gateway_ready.urls if old in u or new in u]
    import hashlib

    delete_body = next(b for b in reversed(gateway_ready.json_bodies) if '"keys"' in b)
    assert hashlib.sha256(old.encode()).hexdigest() in delete_body and old not in delete_body
    # the session tenant is pinned: northwind's key is untouched
    assert (await sam.post("/api/admin/gateway/tenant/rotate-key", params={"tenant": TENANT_B})).status_code == 404


async def test_usage(people: dict[str, Browser], gateway_ready: FakeLiteLLM) -> None:
    today = dt.date.today()
    d1, d2 = (today - dt.timedelta(days=1)).isoformat(), today.isoformat()

    def row(date: str, team: str, groups: dict[str, tuple[float, int, int, int]]) -> dict[str, Any]:
        bd = {alias: {"metrics": {"spend": s, "prompt_tokens": i, "completion_tokens": o, "api_requests": n}}
              for alias, (s, i, o, n) in groups.items()}
        tot = [sum(v[k] for v in groups.values()) for k in range(4)]
        return {"team_id": team, "date": date, "breakdown": {"model_groups": bd},
                "metrics": {"spend": tot[0], "prompt_tokens": tot[1], "completion_tokens": tot[2],
                            "api_requests": tot[3]}}

    gateway_ready.daily = [
        row(d1, f"keelson-{TENANT_A}", {"fast-default": (0.01, 100, 20, 2), "reasoning-default": (0.2, 900, 80, 1)}),
        row(d2, f"keelson-{TENANT_A}", {"fast-default": (0.02, 200, 40, 4)}),
        row(d2, f"keelson-{TENANT_B}", {"fast-default": (9.0, 1, 1, 1)}),
    ]
    r = await people["sam"].get("/api/admin/gateway/usage", params={"days": 7})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["days"] == 7 and len(data["daily"]) == 7 and data["daily"][-1]["date"] == d2
    assert data["daily"][-1] == {"date": d2, "spend_eur": 0.02, "tokens_in": 200, "tokens_out": 40, "requests": 4}
    items = {(i["date"], i["alias"]): i for i in data["items"]}
    assert items[(d1, "reasoning-default")]["tokens_in"] == 900 and len(items) == 3  # northwind excluded
    assert (await people["sam"].get("/api/admin/gateway/usage", params={"days": 0})).status_code == 422


async def test_gateway_unavailable(people: dict[str, Browser], gateway_ready: FakeLiteLLM) -> None:
    gateway_ready.master_key = "sk-rotated-elsewhere"
    try:
        r = await people["sam"].get("/api/admin/gateway/tenant")
        assert r.status_code == 502 and r.json()["code"] == "gateway_unavailable"
        r = await people["sam"].get("/api/admin/gateway/models")
        assert r.status_code == 502 and r.json()["code"] == "gateway_unavailable"
        assert "sk-" not in r.text
    finally:
        gateway_ready.master_key = "sk-test-master"


# ───────────────────────────── reviewer hardening (EA review) ─────────────────────────────


async def test_concurrent_rotations_never_orphan_a_live_key(people: dict[str, Browser],
                                                           gateway_ready: FakeLiteLLM) -> None:
    """Review fix: two concurrent rotations both read the same old key; the key generated by the
    first was overwritten in the DB but stayed valid in LiteLLM (an orphaned live credential).
    Also: the rotated key alias had second resolution, so a second rotation within the same second
    was refused by LiteLLM (unique key aliases) → 502."""
    import asyncio

    actor = {"sub": "sub-sam", "username": "sam"}
    team = f"keelson-{TENANT_A}"
    v_before = await connectors.get_tenant_llm_key_version(TENANT_A)
    old = await connectors.get_tenant_llm_key(TENANT_A)
    before = {k for k, v in gateway_ready.keys.items() if v.get("team_id") == team}
    results = await asyncio.gather(*(gateway.rotate_key(TENANT_A, actor) for _ in range(3)))
    assert all(r["old_key_deleted"] for r in results)
    stored = await connectors.get_tenant_llm_key(TENANT_A)
    after = {k for k, v in gateway_ready.keys.items() if v.get("team_id") == team}
    assert old not in after and after - before == {stored}
    assert await connectors.get_tenant_llm_key_version(TENANT_A) == v_before + 3
    assert sorted(r["key_version"] for r in results) == [v_before + 1, v_before + 2, v_before + 3]


async def test_model_upsert_rejects_non_integer_max_tokens(people: dict[str, Browser],
                                                          gateway_ready: FakeLiteLLM) -> None:
    """Review fix: a non-numeric ``params.max_tokens`` on a Claude alias was an unhandled 500."""
    ava = people["ava"]
    for value in ("lots", 0, True, 1.5):
        r = await ava.put("/api/admin/gateway/models/claude-odd",
                          json={"provider_model": "anthropic/claude-odd",
                                "params": {"reasoning_effort": "high", "max_tokens": value}})
        assert r.status_code == 422 and r.json()["code"] == "invalid_params", (value, r.text)
    assert not [m for m in gateway_ready.models.values() if m["model_name"] == "anthropic/claude-odd"]
