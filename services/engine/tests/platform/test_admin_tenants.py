"""Template packs + tenant creation (platform-admin): pack files, LiteLLM team/key, connectors from
the pack, spec v1, Keycloak ``tenant`` attribute (mocked admin API), idempotency on slug; bootstrap
applies packs per tenants.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
import respx
from sqlalchemy import text

from e1_admin import reset_specs
from e1_browser import Browser
from e1_env import HAVE_PACKS, REPO_PACKS, TENANT_A, TENANT_B
from e1_keycloak_admin import FakeKeycloakAdmin
from e1_litellm import FakeLiteLLM
from keelson_engine.config import Settings
from keelson_engine.core import agent_specs, connectors
from keelson_engine.core.db import system_session
from keelson_engine.core.keycloak_admin import KeycloakAdmin
from keelson_engine.core.packs import PackError, list_packs, load_pack
from keelson_engine.core.tenants import set_keycloak_admin
from keelson_engine.runtime.spec import AgentSpec

pytestmark = [pytest.mark.asyncio(loop_scope="session"),
              pytest.mark.skipif(not HAVE_PACKS, reason="data/packs not in this image")]

KC = "http://keycloak:8080"


@pytest.fixture
def kc(http_mock: respx.Router) -> Any:
    fake = FakeKeycloakAdmin(KC, "kc-admin-pw")
    fake.install(http_mock)
    fake.add_user("olga", tenant=["demo-retail"])
    fake.add_user("piet")
    set_keycloak_admin(KeycloakAdmin(KC, "kc-admin-pw"))
    yield fake
    set_keycloak_admin(None)


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any) -> dict[str, Browser]:
    return {n: await browser_factory(n) for n in ("sam", "ava")}


async def _drop_tenant(slug: str) -> None:
    async with system_session() as s:
        for table in ("connector_tool", "connector_event", "connector", "agent_spec", "tenant_llm_key"):
            await s.execute(text(f"DELETE FROM {table} WHERE tenant = :t"), {"t": slug})
        await s.execute(text("DELETE FROM tenant WHERE slug = :t"), {"t": slug})


async def test_repo_packs_are_valid(platform: None) -> None:
    packs = {p.name: p for p in list_packs(REPO_PACKS)}
    assert set(packs) >= {"value-fashion-eu", "blank"}
    vf = packs["value-fashion-eu"]
    merxa = vf.connectors[0]
    assert merxa["slug"] == "merxa" and merxa["platform"] is True and len(merxa["lanes"]) == 5
    assert merxa["webhook_secret_env"] == "MERXA_WEBHOOK_SECRET"
    assert vf.budgets == {"budget_eur": 50, "rpm_limit": 120, "tpm_limit": 400000}
    assert packs["blank"].connectors == [] and packs["blank"].spec_overrides["answer_tools"] == []
    # spec_overrides produce specs the runtime loads
    for pack in packs.values():
        spec = agent_specs.normalize("x-tenant", agent_specs.merge_patch(agent_specs.default_spec_dict("x-tenant"),
                                                                        pack.spec_overrides))
        AgentSpec.from_row("x-tenant", "v1", spec)
    with pytest.raises(PackError):
        load_pack("../etc", REPO_PACKS)
    with pytest.raises(PackError):
        load_pack("no-such-pack", REPO_PACKS)


async def test_pack_validation(tmp_path: Path) -> None:
    (tmp_path / "bad").mkdir()
    (tmp_path / "bad" / "pack.yaml").write_text("name: other\n", encoding="utf-8")
    with pytest.raises(PackError, match="directory"):
        load_pack("bad", tmp_path)
    (tmp_path / "bad" / "pack.yaml").write_text(
        "name: bad\nconnectors:\n  - {slug: x, mcp_url: 'http://x', auth: {kind: static_header, secret: s}}\n",
        encoding="utf-8")
    with pytest.raises(PackError, match="secrets"):
        load_pack("bad", tmp_path)
    (tmp_path / "bad" / "pack.yaml").write_text("name: bad\nbudgets: {max_budget: 1}\n", encoding="utf-8")
    with pytest.raises(PackError, match="budgets"):
        load_pack("bad", tmp_path)
    (tmp_path / "bad" / "pack.yaml").write_text(
        "name: bad\nconnectors:\n  - {slug: x, mcp_url: 'http://x', webhook_secret_env: SESSION_SECRET}\n",
        encoding="utf-8")
    with pytest.raises(PackError, match="WEBHOOK_SECRET"):
        load_pack("bad", tmp_path)


async def test_packs_and_tenants_are_platform_admin_only(people: dict[str, Browser], gateway_ready: FakeLiteLLM) -> None:
    sam, ava = people["sam"], people["ava"]
    assert (await sam.get("/api/admin/packs")).status_code == 403
    assert (await sam.get("/api/admin/tenants")).status_code == 403
    r = await sam.post("/api/admin/tenants", json={"slug": "x-co", "name": "X", "pack": "blank"})
    assert r.status_code == 403 and r.json()["code"] == "platform_admin_required"
    packs = {p["name"]: p for p in (await ava.get("/api/admin/packs")).json()["items"]}
    assert packs["value-fashion-eu"]["connectors"] == [{"slug": "merxa", "name": "Merxa Retail Suite",
                                                        "platform": True}]
    tenants = {t["slug"]: t for t in (await ava.get("/api/admin/tenants")).json()["items"]}
    assert {TENANT_A, TENANT_B} <= set(tenants)


async def test_create_tenant_from_pack(people: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                       kc: FakeKeycloakAdmin) -> None:
    ava = people["ava"]
    for slug in ("acme-eu", "acme-blank"):
        await _drop_tenant(slug)
    body = {"slug": "acme-eu", "name": "Acme EU", "pack": "value-fashion-eu",
            "admin_usernames": ["olga", "piet", "ghost"]}
    r = await ava.post("/api/admin/tenants", json=body)
    assert r.status_code == 201, r.text
    res = r.json()
    assert res["created"] is True and res["pack"] == "value-fashion-eu" and res["team_id"] == "keelson-acme-eu"
    assert res["connectors"] == [{"slug": "merxa", "status": "active"}] and res["spec_version"] == "v1"
    assert {a["username"]: a["status"] for a in res["admins"]} == {"olga": "added", "piet": "added",
                                                                   "ghost": "not_found"}
    assert kc.by_username("olga")["attributes"]["tenant"] == ["demo-retail", "acme-eu"]
    assert kc.by_username("piet")["attributes"]["tenant"] == ["acme-eu"]

    team = gateway_ready.teams["keelson-acme-eu"]
    assert team["max_budget"] == 50.0 and team["rpm_limit"] == 120 and team["tpm_limit"] == 400000
    key = await connectors.get_tenant_llm_key("acme-eu")
    assert gateway_ready.keys[key]["team_id"] == "keelson-acme-eu"
    conn = await connectors.get_connector("acme-eu", "merxa")
    assert conn.is_platform and len(conn.lanes) == 5 and conn.webhook_secret_ref == "MERXA_WEBHOOK_SECRET"
    spec = await agent_specs.latest("acme-eu")
    assert spec["summary"] == "initial (pack value-fashion-eu)" and spec["created_by"] == "ava"
    async with system_session() as s:
        cfg = (await s.execute(text("SELECT config FROM tenant WHERE slug = 'acme-eu'"))).scalar_one()
        audit = (await s.execute(text(
            "SELECT action FROM admin_audit WHERE tenant = 'acme-eu' AND action LIKE 'tenant.%'"))).scalars().all()
    assert cfg["pack"] == "value-fashion-eu" and "tenant.create" in audit

    # idempotent on slug: same pack → 200, nothing duplicated
    keys_before = len(gateway_ready.keys)
    again = await ava.post("/api/admin/tenants", json={**body, "admin_usernames": ["olga"]})
    assert again.status_code == 200, again.text
    assert again.json()["created"] is False and again.json()["spec_version"] == "v1"
    assert again.json()["admins"] == [{"username": "olga", "status": "already"}]
    assert len(gateway_ready.keys) == keys_before and await connectors.get_tenant_llm_key("acme-eu") == key
    assert kc.by_username("olga")["attributes"]["tenant"] == ["demo-retail", "acme-eu"]
    # another pack for the same slug → 409; an existing (yaml) tenant cannot be re-packed either
    other = await ava.post("/api/admin/tenants", json={**body, "pack": "blank"})
    assert other.status_code == 409 and other.json()["code"] == "tenant_exists"
    bad = await ava.post("/api/admin/tenants", json={**body, "slug": "acme-x", "pack": "nope"})
    assert bad.status_code == 422 and bad.json()["code"] == "unknown_pack"
    assert (await ava.post("/api/admin/tenants", json={**body, "slug": "Acme"})).status_code == 422

    # blank pack: no connectors, answer path only; Keycloak down → reported, tenant still created
    kc.down = True
    blank = await ava.post("/api/admin/tenants", json={"slug": "acme-blank", "name": "Acme Blank", "pack": "blank",
                                                      "admin_usernames": ["piet"]})
    kc.down = False
    assert blank.status_code == 201, blank.text
    assert blank.json()["connectors"] == [] and blank.json()["admins"] == [{"username": "piet", "status": "failed"}]
    spec = (await agent_specs.latest("acme-blank"))["spec"]
    assert spec["answer_tools"] == [] and spec["specialists"][0]["tools"] == []
    assert gateway_ready.teams["keelson-acme-blank"]["max_budget"] == 1.0
    # retry fixes the Keycloak part
    retry = await ava.post("/api/admin/tenants", json={"slug": "acme-blank", "name": "Acme Blank", "pack": "blank",
                                                      "admin_usernames": ["piet"]})
    assert retry.status_code == 200 and retry.json()["admins"] == [{"username": "piet", "status": "added"}]

    tenants = {t["slug"]: t for t in (await ava.get("/api/admin/tenants")).json()["items"]}
    assert tenants["acme-eu"]["pack"] == "value-fashion-eu" and tenants["acme-eu"]["connectors"] == 1
    assert tenants["acme-blank"]["spec_version"] == "v1"
    for slug in ("acme-eu", "acme-blank"):
        await _drop_tenant(slug)


async def test_litellm_down_then_retry(people: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                       kc: FakeKeycloakAdmin) -> None:
    ava = people["ava"]
    await _drop_tenant("acme-retry")
    gateway_ready.master_key = "sk-wrong"
    try:
        r = await ava.post("/api/admin/tenants", json={"slug": "acme-retry", "name": "R", "pack": "blank"})
        assert r.status_code == 502 and r.json()["code"] == "gateway_unavailable"
    finally:
        gateway_ready.master_key = "sk-test-master"
    r = await ava.post("/api/admin/tenants", json={"slug": "acme-retry", "name": "R", "pack": "blank"})
    assert r.status_code == 200 and r.json()["created"] is False and r.json()["spec_version"] == "v1"
    assert "keelson-acme-retry" in gateway_ready.teams
    await _drop_tenant("acme-retry")


async def test_bootstrap_applies_packs(platform: None, settings: Settings, litellm_fake: FakeLiteLLM) -> None:
    from keelson_engine.bootstrap import run_bootstrap

    await reset_specs()
    try:
        await run_bootstrap(settings, wait_timeout_s=5)
        a = await agent_specs.latest(TENANT_A)
        b = await agent_specs.latest(TENANT_B)
        assert a["version"] == "v1" and a["summary"] == "initial (pack value-fashion-eu)"
        assert b["version"] == "v1" and b["summary"] == "initial (pack blank)" and b["spec"]["answer_tools"] == []
        assert [c.slug for c in await connectors.list_connectors(TENANT_B)] == []
        merxa = await connectors.get_connector(TENANT_A, "merxa")
        assert merxa.webhook_secret_ref == "MERXA_WEBHOOK_SECRET" and merxa.status == "active"
        # re-run: nothing new
        await run_bootstrap(settings, wait_timeout_s=5)
        assert [v["version"] for v in await agent_specs.list_versions(TENANT_A)] == ["v1"]
    finally:
        await reset_specs()
