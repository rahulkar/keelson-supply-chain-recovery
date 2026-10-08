"""Bootstrap against a mocked LiteLLM: aliases, teams, one key per tenant, connectors; idempotent."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from sqlalchemy import text

from e1_env import MASTER_KEY, TENANT_A, TENANT_B
from e1_litellm import FakeLiteLLM
from keelson_engine.bootstrap import BootstrapError, run_bootstrap
from keelson_engine.config import Settings
from keelson_engine.core import connectors
from keelson_engine.core.db import system_session, tenant_session

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_bootstrap_is_idempotent(platform: None, settings: Settings, litellm_fake: FakeLiteLLM) -> None:
    await run_bootstrap(settings, wait_timeout_s=5)

    lock = yaml.safe_load(Path(settings.models_lock).read_text(encoding="utf-8"))["aliases"]
    by_alias = {m["model_name"]: m for m in litellm_fake.models.values()}
    assert set(by_alias) == set(lock)
    assert by_alias["fast-default"]["litellm_params"]["model"] == "gemini/gemini-3.8-flash"
    assert by_alias["fast-default"]["litellm_params"]["reasoning_effort"] == "low"
    assert by_alias["embed-default"]["litellm_params"]["dimensions"] == 1536
    assert by_alias["embed-default"]["model_info"]["mode"] == "embedding"
    assert by_alias["reasoning-default"]["model_info"]["mode"] == "chat"

    assert set(litellm_fake.teams) == {f"keelson-{TENANT_A}", f"keelson-{TENANT_B}"}
    team = litellm_fake.teams[f"keelson-{TENANT_A}"]
    assert team["max_budget"] == 50.0 and team["rpm_limit"] == 120 and team["tpm_limit"] == 400000
    assert sorted(team["models"]) == sorted(lock)

    assert len(litellm_fake.keys) == 2
    key_a = await connectors.get_tenant_llm_key(TENANT_A)
    assert litellm_fake.keys[key_a]["team_id"] == f"keelson-{TENANT_A}"
    async with tenant_session(TENANT_A) as s:
        row = (await s.execute(text("SELECT key_enc, key_id, litellm_key_alias FROM tenant_llm_key"))).one()
    assert row[0].startswith("dk-1:") and key_a not in row[0] and row[1] == "dk-1"
    assert row[2] == f"keelson-{TENANT_A}"

    conn = await connectors.get_connector(TENANT_A, "merxa")
    assert conn.url == "http://merxa.localhost/mcp" and len(conn.lanes) == 5
    assert conn.clients["reader"] == "keelson-reader"
    async with system_session() as s:
        tenants = {r[0]: r[1] for r in (await s.execute(text("SELECT slug, config FROM tenant"))).all()}
    assert tenants[TENANT_A]["budget_eur"] == 50

    # second run: nothing new is created, the stored key is reused
    model_ids = set(litellm_fake.models)
    connectors.clear_llm_key_cache()
    mark = len(litellm_fake.calls)
    await run_bootstrap(settings, wait_timeout_s=5)
    assert set(litellm_fake.models) == model_ids
    assert len(litellm_fake.keys) == 2
    assert await connectors.get_tenant_llm_key(TENANT_A) == key_a
    second_run = litellm_fake.calls[mark:]
    assert ("POST", "/key/generate") not in second_run and ("POST", "/model/new") not in second_run
    # the raw virtual key never travels in a URL (LiteLLM / proxy access logs)
    key_b = await connectors.get_tenant_llm_key(TENANT_B)
    assert not [u for u in litellm_fake.urls if key_a in u or key_b in u]

    # a new alias in models.lock reaches the REUSED key's allow-list (else the tenant gets 403)
    lock["fast-extra"] = {"model": "gemini/gemini-3.8-flash", "mode": "chat"}
    Path(settings.models_lock).write_text(yaml.safe_dump({"version": 1, "aliases": lock}), encoding="utf-8")
    connectors.clear_llm_key_cache()
    try:
        await run_bootstrap(settings, wait_timeout_s=5)
    finally:
        del lock["fast-extra"]
        Path(settings.models_lock).write_text(yaml.safe_dump({"version": 1, "aliases": lock}), encoding="utf-8")
    assert await connectors.get_tenant_llm_key(TENANT_A) == key_a
    assert "fast-extra" in litellm_fake.keys[key_a]["models"]
    assert litellm_fake.keys[key_a]["team_id"] == f"keelson-{TENANT_A}"
    for m in [m for m in litellm_fake.models.values() if m["model_name"] == "fast-extra"]:
        del litellm_fake.models[m["model_info"]["id"]]
    model_ids = set(litellm_fake.models)

    # a key revoked (blocked) in LiteLLM is not reused
    litellm_fake.keys[key_b]["blocked"] = True
    connectors.clear_llm_key_cache()
    await run_bootstrap(settings, wait_timeout_s=5)
    assert await connectors.get_tenant_llm_key(TENANT_B) != key_b

    # a changed alias is PATCHed in place; a key revoked in LiteLLM is regenerated
    lock["fast-default"]["params"]["reasoning_effort"] = "medium"
    Path(settings.models_lock).write_text(yaml.safe_dump({"version": 1, "aliases": lock}), encoding="utf-8")
    del litellm_fake.keys[key_a]
    connectors.clear_llm_key_cache()
    try:
        await run_bootstrap(settings, wait_timeout_s=5)
    finally:
        lock["fast-default"]["params"]["reasoning_effort"] = "low"
        Path(settings.models_lock).write_text(yaml.safe_dump({"version": 1, "aliases": lock}), encoding="utf-8")
    assert set(litellm_fake.models) == model_ids
    patched = next(m for m in litellm_fake.models.values() if m["model_name"] == "fast-default")
    assert patched["litellm_params"]["reasoning_effort"] == "medium"
    new_key = await connectors.get_tenant_llm_key(TENANT_A)
    assert new_key != key_a and new_key in litellm_fake.keys
    assert len(litellm_fake.keys) == 2


async def test_bootstrap_rejects_bad_config(platform: None, settings: Settings, tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    bad = tmp_path / "models.lock"
    bad.write_text("version: 1\naliases: {}\n", encoding="utf-8")
    monkeypatch.setattr(settings, "models_lock", bad)
    with pytest.raises(BootstrapError):
        await run_bootstrap(settings, wait_timeout_s=1)


async def test_bootstrap_fails_when_litellm_rejects_master_key(
    platform: None, settings: Settings, litellm_fake: FakeLiteLLM
) -> None:
    litellm_fake.master_key = "sk-other"
    try:
        with pytest.raises(Exception, match="model/info"):
            await run_bootstrap(settings, wait_timeout_s=5)
    finally:
        litellm_fake.master_key = MASTER_KEY
