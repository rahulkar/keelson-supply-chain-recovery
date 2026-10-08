"""M8 option scoring in the back-office (``126-m8-co2-robustness.md`` §6): the Policies page saves
``planning.scoring`` / ``planning.robustness.samples`` through ``PUT /api/admin/agents/spec`` (RFC 7386
merge patch). Not a guarded change: a tenant admin's save is a new spec version (201, no change
request); invalid profiles are 422 ``spec_invalid``; a stale base is 409; restore brings the old
profiles back; the runtime loads the stored settings."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import pytest_asyncio

from e1_admin import reset_specs
from e1_browser import Browser
from e1_env import TENANT_A
from e1_litellm import FakeLiteLLM
from keelson_engine.core import connectors
from keelson_engine.runtime.spec import AgentSpec
from test_api_connectors import TOOLS
from test_tokens import MERXA_SPEC

pytestmark = pytest.mark.asyncio(loop_scope="session")

SPEC = "/api/admin/agents/spec"


@pytest_asyncio.fixture(loop_scope="session")
async def merxa_ready(platform: None) -> None:
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    await connectors.upsert_tool_snapshot(TENANT_A, "merxa", copy.deepcopy(TOOLS), auto_approve=True,
                                          untrusted=("list_supplier_messages",))


@pytest_asyncio.fixture(loop_scope="session")
async def admins(browser_factory: Any) -> dict[str, Browser]:
    await reset_specs(TENANT_A)
    return {n: await browser_factory(n) for n in ("sam", "priya")}


async def _put(b: Browser, base: str, patch: dict[str, Any]) -> Any:
    return await b.put(SPEC, json={"base_version": base, "spec_patch": patch})


async def test_scoring_defaults_save_validate_and_restore(admins: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                                          merxa_ready: None) -> None:
    sam = admins["sam"]
    cur = (await sam.get(SPEC)).json()
    planning = cur["spec"]["planning"]
    assert planning["scoring"]["default_profile"] == "net-benefit"
    assert [p["id"] for p in planning["scoring"]["profiles"]] == ["net-benefit", "cost-first", "service-first",
                                                                  "low-carbon"]
    assert planning["robustness"]["samples"] == 64 and planning["robustness"]["enabled"] is True
    assert planning["emissions"]["factors_id"] == "glec-v3-indicative"
    base = cur["version"]

    profiles = copy.deepcopy(planning["scoring"]["profiles"])
    profiles[3]["carbon_price_cents_per_t"] = 12_000
    profiles.append({"id": "board", "label": "Board view", "lost_margin_bp": 12_000, "cost_bp": 10_000,
                     "carbon_price_cents_per_t": 5_000, "risk_aversion_bp": 3_000})
    patch = {"planning": {"scoring": {"default_profile": "board", "profiles": profiles}, "robustness": {"samples": 128}}}
    r = await _put(sam, base, patch)
    assert r.status_code == 201, r.text  # not a guarded change: no change request
    saved = r.json()
    assert saved["created_by"] == "sam" and saved["unchanged"] is False
    assert "planning.scoring" in saved["summary"] and "planning.robustness" in saved["summary"]
    assert saved["spec"]["planning"]["scoring"]["default_profile"] == "board"
    assert saved["spec"]["planning"]["robustness"]["samples"] == 128
    # other planning settings are kept (merge patch), and the runtime model loads the stored spec
    assert saved["spec"]["planning"]["solver_seed"] == planning["solver_seed"]
    runtime = AgentSpec.from_row(TENANT_A, saved["version"], saved["spec"])
    assert runtime.planning.scoring.default.label == "Board view" and runtime.planning.robustness.samples == 128

    async def bad(change: dict[str, Any]) -> None:
        res = await _put(sam, saved["version"], {"planning": change})
        assert res.status_code == 422, res.text
        assert res.json()["code"] == "spec_invalid", res.text

    await bad({"scoring": {"default_profile": "nobody"}})
    await bad({"scoring": {"profiles": profiles + [{**profiles[0], "id": f"p{i}"} for i in range(5)]}})
    await bad({"scoring": {"profiles": [*profiles, profiles[0]]}})
    await bad({"scoring": {"profiles": [{**profiles[0], "cost_bp": 50_001}, *profiles[1:]]}})
    await bad({"scoring": {"profiles": [{**profiles[0], "risk_aversion_bp": -1}, *profiles[1:]]}})
    await bad({"scoring": {"profiles": [{**profiles[0], "label": "x" * 41}, *profiles[1:]]}})
    await bad({"scoring": {"profiles": [{**profiles[0], "weight": 1}, *profiles[1:]]}})
    await bad({"robustness": {"samples": 8}})
    await bad({"robustness": {"samples": 1000}})
    await bad({"emissions": {"unit_weights": [{"style_prefix": "P.*", "grams": 900}]}})

    stale = await _put(sam, base, {"planning": {"robustness": {"samples": 32}}})
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"

    restored = await sam.post(f"{SPEC}/restore", json={"version": base})
    assert restored.status_code == 201, restored.text
    assert restored.json()["spec"]["planning"]["scoring"]["default_profile"] == "net-benefit"
    assert restored.json()["spec"]["planning"]["robustness"]["samples"] == 64
    await reset_specs(TENANT_A)


async def test_only_admins_change_scoring(admins: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                          merxa_ready: None) -> None:
    priya = admins["priya"]  # a planner: reads cases, never the admin API
    base = (await admins["sam"].get(SPEC)).json()["version"]
    r = await _put(priya, base, {"planning": {"scoring": {"default_profile": "cost-first"}}})
    assert r.status_code == 403, r.text
