"""Back-office: versioned agent specs (GET/PUT/versions/restore/catalog) — optimistic concurrency,
alias/tool/budget validation, guarded policy changes, prompt addenda, runs pin the latest version."""

from __future__ import annotations

import copy
from typing import Any

import pytest
import pytest_asyncio

from e1_admin import reset_specs
from e1_browser import Browser
from e1_env import TENANT_A
from e1_litellm import FakeLiteLLM
from keelson_engine.core import agent_specs, connectors, runs
from keelson_engine.runtime.spec import AgentSpec
from test_api_connectors import TOOLS
from test_tokens import MERXA_SPEC

pytestmark = pytest.mark.asyncio(loop_scope="session")

SPEC = "/api/admin/agents/spec"


@pytest_asyncio.fixture(loop_scope="session")
async def merxa_tools(platform: None) -> None:
    """Merxa (active platform connector) with an approved snapshot: get_inventory (read),
    list_supplier_messages (read, untrusted), change-set writes."""
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    await connectors.upsert_tool_snapshot(TENANT_A, "merxa", copy.deepcopy(TOOLS), auto_approve=True,
                                          untrusted=("list_supplier_messages",))


@pytest_asyncio.fixture(loop_scope="session")
async def people(browser_factory: Any) -> dict[str, Browser]:
    await reset_specs(TENANT_A)
    return {n: await browser_factory(n) for n in ("sam", "ava", "priya")}


async def _put(b: Browser, base: str, patch: dict[str, Any]) -> Any:
    return await b.put(SPEC, json={"base_version": base, "spec_patch": patch})


async def test_versions_concurrency_and_restore(people: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                                merxa_tools: None) -> None:
    sam = people["sam"]
    cur = (await sam.get(SPEC)).json()
    assert cur["version"] == "default" and cur["active"] is True and cur["created_by"] is None
    assert cur["spec"]["models"]["intake"] == "fast-default" and "tenant" not in cur["spec"]

    r = await _put(sam, "default", {"models": {"intake": "extract-fast"}})
    assert r.status_code == 201, r.text
    v1 = r.json()
    assert v1["version"] == "v1" and v1["spec"]["models"]["intake"] == "extract-fast"
    assert v1["created_by"] == "sam" and v1["summary"] == "models.intake" and v1["unchanged"] is False
    # the full spec is stored (diff-friendly), not the patch
    assert v1["spec"]["models"]["recommender"] == "reasoning-default" and v1["spec"]["specialists"]

    stale = await _put(sam, "default", {"models": {"intake": "fast-default"}})
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
    assert stale.json()["latest_version"] == "v1"
    same = await _put(sam, "v1", {"models": {"intake": "extract-fast"}})
    assert same.status_code == 200 and same.json()["unchanged"] is True and same.json()["version"] == "v1"

    assert (await sam.get(SPEC)).json()["version"] == "v1"
    versions = (await sam.get(f"{SPEC}/versions")).json()
    assert [v["version"] for v in versions] == ["v1"] and set(versions[0]) == {"version", "created_by", "created_at",
                                                                               "summary"}
    got = (await sam.get(f"{SPEC}/versions/v1")).json()
    assert got["spec"] == v1["spec"] and got["active"] is True
    assert (await sam.get(f"{SPEC}/versions/default")).json()["active"] is False
    assert (await sam.get(f"{SPEC}/versions/v99")).status_code == 404

    restored = await sam.post(f"{SPEC}/restore", json={"version": "default"})
    assert restored.status_code == 201, restored.text
    assert restored.json()["version"] == "v2" and restored.json()["summary"] == "restored from default"
    assert restored.json()["spec"]["models"]["intake"] == "fast-default"
    again = await sam.post(f"{SPEC}/restore", json={"version": "v2"})
    assert again.status_code == 409 and again.json()["code"] == "already_active"

    # stored specs load in the runtime model, and new runs pin the latest version
    stored = (await sam.get(f"{SPEC}/versions/v1")).json()["spec"]
    assert AgentSpec.from_row(TENANT_A, "v1", stored).models.intake == "extract-fast"
    priya = people["priya"]
    case_id = (await priya.post("/api/cases", json={"title": "pin"})).json()["id"]
    run_id = (await priya.post(f"/api/cases/{case_id}/messages", json={"text": "hi"},
                               headers={"Idempotency-Key": "pin-1"})).json()["run_id"]
    run = await runs.get_run(TENANT_A, run_id)
    assert run is not None and run.agent_spec_version == "v2"


async def test_concurrent_puts_one_wins(people: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                        merxa_tools: None) -> None:
    import asyncio

    sam, ava = people["sam"], people["ava"]
    base = (await sam.get(SPEC)).json()["version"]
    a, b = await asyncio.gather(_put(sam, base, {"history_messages": 5}), _put(ava, base, {"history_messages": 6}))
    assert sorted([a.status_code, b.status_code]) == [201, 409]


async def test_validation(people: dict[str, Browser], gateway_ready: FakeLiteLLM, merxa_tools: None) -> None:
    sam = people["sam"]
    base = (await sam.get(SPEC)).json()["version"]

    async def bad(patch: dict[str, Any], code: str, status: int = 422) -> None:
        r = await _put(sam, base, patch)
        assert r.status_code == status, r.text
        assert r.json()["code"] == code, r.text

    await bad({"models": {"intake": "no-such-alias"}}, "unknown_alias")
    await bad({"models": {"intake": "embed-default"}}, "unknown_alias")  # embedding, not chat
    await bad({"budgets": {"max_llm_calls": 10_000}}, "budget_exceeds_platform_max")
    await bad({"budgets": {"max_total_tokens": 50_000_000}}, "budget_exceeds_platform_max")
    await bad({"specialists": [{"slug": "inventory_demand", "tools": [], "max_tool_calls": 99}]}, "spec_invalid")
    await bad({"unknown_field": 1}, "spec_invalid")
    await bad({"tenant": "northwind-test"}, "spec_invalid")
    await bad({"answer_tools": ["get_inventory", "list_supplier_messages"]}, "tool_not_bindable")  # untrusted
    await bad({"answer_tools": ["get_inventory", "create_change_set"]}, "tool_not_bindable")  # write
    await bad({"answer_tools": ["get_inventory", "list_sailings"]}, "unknown_tool")  # not in the snapshot
    # an alias the tenant team may not use
    team = gateway_ready.teams[f"keelson-{TENANT_A}"]
    team["models"] = [m for m in team["models"] if m != "critic-default"]
    await bad({"models": {"recommender": "critic-default"}}, "unknown_alias")

    ok = await _put(sam, base, {"answer_tools": ["get_inventory"], "models": {"recommender": "extract-fast"}})
    assert ok.status_code == 201, ok.text
    assert ok.json()["spec"]["answer_tools"] == ["get_inventory"]
    # unchanged parts are not re-validated (e.g. specialist tools missing from this small snapshot)
    ok2 = await _put(sam, ok.json()["version"], {"history_messages": 3})
    assert ok2.status_code == 201, ok2.text

    # a disabled connector cannot receive new bindings
    await connectors.upsert_connector(TENANT_A, MERXA_SPEC)
    from keelson_engine.core.db import tenant_session
    from sqlalchemy import text

    async with tenant_session(TENANT_A) as s:
        await s.execute(text("UPDATE connector SET status = 'disabled' WHERE slug = 'merxa'"))
    try:
        r = await _put(sam, ok2.json()["version"], {"answer_tools": ["get_inventory", "get_purchase_order"]})
        assert r.status_code == 422 and r.json()["code"] == "connector_not_active"
    finally:
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("UPDATE connector SET status = 'active' WHERE slug = 'merxa'"))


async def test_guarded_policy_thresholds(people: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                         merxa_tools: None) -> None:
    sam, ava = people["sam"], people["ava"]
    base = (await sam.get(SPEC)).json()
    lines = base["spec"]["policy"]["auto_max_lines"]
    r = await _put(sam, base["version"], {"policy": {"auto_max_lines": lines + 5}})
    # M7 four-eyes: a pending change request instead of 403; nothing applied
    assert r.status_code == 202 and r.json()["kind"] == "agent_spec" and r.json()["status"] == "pending", r.text
    assert [(x["code"], x["field"]) for x in r.json()["reasons"]] == [("auto_cap_raised", "policy.auto_max_lines")]
    assert r.json()["base_version"] == base["version"]
    assert (await sam.get(SPEC)).json()["version"] == base["version"]
    r = await _put(sam, base["version"], {"policy": {"auto_max_units": base["spec"]["policy"]["auto_max_units"] + 1}})
    assert r.status_code == 202
    lowered = await _put(sam, base["version"], {"policy": {"auto_max_lines": max(0, lines - 1)}})
    assert lowered.status_code == 201, lowered.text  # lowering is never guarded
    raised = await _put(ava, lowered.json()["version"], {"policy": {"auto_max_lines": lines + 5}})
    assert raised.status_code == 201, raised.text
    assert raised.json()["created_by"] == "ava"


async def test_guarded_approval_policy_and_planning_connector(people: dict[str, Browser],
                                                             gateway_ready: FakeLiteLLM, merxa_tools: None) -> None:
    """Review fix: a tenant-admin could make planners approvers, drop the quorum or point change-set
    writes (incl. policy auto-commit) at an unreviewed third-party connector."""
    sam, ava = people["sam"], people["ava"]
    base = (await sam.get(SPEC)).json()
    v = base["version"]
    r = await _put(sam, v, {"policy": {"approver_role": "planner"}})
    assert r.status_code == 202 and r.json()["kind"] == "agent_spec", r.text
    assert [(x["code"], x["field"]) for x in r.json()["reasons"]] == [("approver_changed", "policy.approver_role")]
    # M7: a guarded change is validated first; a planning connector that does not exist is 422, no request
    r = await _put(sam, v, {"connector": "erp-lite"})
    assert r.status_code == 422 and r.json()["code"] == "unknown_connector", r.text
    raised = await _put(ava, v, {"policy": {"approval_quorum": 2}})
    assert raised.status_code == 201, raised.text  # raising the quorum is stricter: platform admin here
    v = raised.json()["version"]
    lowered = await _put(sam, v, {"policy": {"approval_quorum": 1}})
    assert lowered.status_code == 202
    assert [x["field"] for x in lowered.json()["reasons"]] == ["policy.approval_quorum"]
    assert lowered.json()["reasons"][0]["code"] == "quorum_lowered"
    assert (await _put(sam, v, {"policy": {"approval_quorum": 3}})).status_code == 201  # stricter: allowed
    v = (await sam.get(SPEC)).json()["version"]
    # a platform admin may re-point the planning connector, but only at an existing, active one
    nowhere = await _put(ava, v, {"connector": "nope"})
    assert nowhere.status_code == 422 and nowhere.json()["code"] == "unknown_connector"
    # restoring a version with looser approval settings is guarded the same way
    restore = await sam.post(f"{SPEC}/restore", json={"version": base["version"]})
    assert restore.status_code == 202 and [x["field"] for x in restore.json()["reasons"]] == ["policy.approval_quorum"]
    assert restore.json()["payload"] == {"restore_version": base["version"]}


async def test_prompt_addenda_and_bindings(people: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                          merxa_tools: None) -> None:
    """Contract-70 fields of the runtime ``AgentSpec``: prompts, answer_connector, per-specialist
    connector/display_name."""
    sam = people["sam"]
    base = (await sam.get(SPEC)).json()["version"]
    r = await _put(sam, base, {"prompts": {"recommender": "Prefer rebalancing over expediting."}})
    assert r.status_code == 201, r.text
    spec = r.json()["spec"]
    assert spec["prompts"]["recommender"] == "Prefer rebalancing over expediting."
    assert spec["answer_connector"] == "merxa" and spec["specialists"][0]["connector"] == "merxa"
    v = r.json()["version"]
    too_long = await _put(sam, v, {"prompts": {"intake": "x" * 4001}})
    assert too_long.status_code == 422 and too_long.json()["code"] == "spec_invalid"
    assert (await _put(sam, v, {"prompts": {"intake": "x" * 4000}})).status_code == 201
    v = (await sam.get(SPEC)).json()["version"]
    unknown_role = await _put(sam, v, {"prompts": {"answer_structurer": "no"}})
    assert unknown_role.status_code == 422
    nowhere = await _put(sam, v, {"answer_connector": "nope", "answer_tools": ["list_items"]})
    assert nowhere.status_code == 422 and nowhere.json()["code"] == "unknown_connector"
    named = await _put(sam, v, {"specialists": [{"slug": "inventory_demand", "display_name": "Stock analyst",
                                                 "tools": ["get_inventory"]}]})
    assert named.status_code == 201, named.text
    # clearing an addendum with null (merge patch) restores the default
    cleared = await _put(sam, named.json()["version"], {"prompts": {"recommender": None}})
    assert cleared.status_code == 201 and cleared.json()["spec"]["prompts"]["recommender"] is None


async def test_catalog(people: dict[str, Browser], gateway_ready: FakeLiteLLM, merxa_tools: None) -> None:
    sam = people["sam"]
    cat = (await sam.get("/api/admin/agents/catalog")).json()
    roles = {r["role"]: r for r in cat["roles"]}
    assert roles["intake"]["display_name"] == "Intake" and roles["recommender"]["display_name"] == "Strategist"
    assert roles["answer_investigator"]["uses_tools"] is True and roles["intake"]["prompt_role"] == "intake"
    assert roles["answer_structurer"]["prompt_role"] is None
    aliases = {a["alias"]: a for a in cat["aliases"]}
    assert aliases["fast-default"]["capabilities"] == {"structured_output": True, "reasoning_stream": True,
                                                        "tool_loop_signatures": True}
    assert aliases["embed-default"]["mode"] == "embedding"
    merxa = next(c for c in cat["connectors"] if c["slug"] == "merxa")
    tools = {t["name"]: t for t in merxa["tools"]}
    assert tools["get_inventory"]["bindable"] is True and tools["get_inventory"]["reason"] is None
    assert tools["list_supplier_messages"]["bindable"] is False
    assert "untrusted" in tools["list_supplier_messages"]["reason"]
    assert tools["apply_change_set"]["bindable"] is False and "destructive" in tools["apply_change_set"]["reason"]
    assert cat["guarded_fields"] == ["policy.auto_max_lines", "policy.auto_max_units", "policy.approver_role",
                                     "policy.approval_quorum", "connector"]
    assert cat["prompt_max_chars"] == 4000 and cat["gateway_available"] is True
    assert cat["specialists"][0]["display_name"] in ("Inventory & demand analyst", "Stock analyst")


async def test_initial_spec_from_pack_is_idempotent(platform: None) -> None:
    await reset_specs(TENANT_A)
    v = await agent_specs.create_initial(TENANT_A, {"history_messages": 4}, actor="test", summary="initial (pack x)")
    assert v == "v1"
    assert await agent_specs.create_initial(TENANT_A, {"history_messages": 9}, actor="test", summary="again") is None
    latest = await agent_specs.latest(TENANT_A)
    assert latest["version"] == "v1" and latest["spec"]["history_messages"] == 4
    assert AgentSpec.from_row(TENANT_A, "v1", latest["spec"]).history_messages == 4
    await reset_specs(TENANT_A)


async def test_upgrade_builtins_adds_new_specialists_once(platform: None, merxa_tools: None) -> None:
    """M4: a tenant still on an M3 spec (one specialist, legacy budgets) gets the new built-in
    specialists and the current budget defaults as ONE new version; re-running is a no-op, and a
    specialist an admin removed later is never re-added (it is in the spec history)."""
    await reset_specs(TENANT_A)
    default = agent_specs.default_spec_dict(TENANT_A)
    m3 = {"specialists": [sp for sp in default["specialists"] if sp["slug"] == "inventory_demand"],
          "budgets": {"max_llm_calls": 40, "max_total_tokens": 600_000}}
    assert await agent_specs.create_initial(TENANT_A, m3, actor="test", summary="initial (pack x)") == "v1"
    v = await agent_specs.upgrade_builtins(TENANT_A, actor="bootstrap")
    assert v == "v2"
    latest = await agent_specs.latest(TENANT_A)
    slugs = [sp["slug"] for sp in latest["spec"]["specialists"]]
    assert slugs == ["inventory_demand", "sourcing", "logistics", "playbook"]
    assert latest["spec"]["budgets"] == default["budgets"]
    assert latest["summary"] == "built-in specialists added: sourcing, logistics, playbook"
    assert AgentSpec.from_row(TENANT_A, "v2", latest["spec"]).specialists[3].quarantined == ("knowledge_base",)
    assert await agent_specs.upgrade_builtins(TENANT_A, actor="bootstrap") is None
    # an admin removes the playbook analyst: the next bootstrap keeps it removed
    kept = [sp for sp in latest["spec"]["specialists"] if sp["slug"] != "playbook"]
    created, changed = await agent_specs.put_spec(TENANT_A, "v2", {"specialists": kept}, {"username": "sam"},
                                                  platform_admin=False)
    assert changed and created["version"] == "v3"
    assert await agent_specs.upgrade_builtins(TENANT_A, actor="bootstrap") is None
    assert [sp["slug"] for sp in (await agent_specs.latest(TENANT_A))["spec"]["specialists"]] == [
        "inventory_demand", "sourcing", "logistics"]
    await reset_specs(TENANT_A)


async def test_upgrade_builtins_keeps_admin_budgets_and_needs_a_spec(platform: None, merxa_tools: None) -> None:
    await reset_specs(TENANT_A)
    assert await agent_specs.upgrade_builtins(TENANT_A, actor="bootstrap") is None  # no version: default spec
    default = agent_specs.default_spec_dict(TENANT_A)
    own = {"specialists": [sp for sp in default["specialists"] if sp["slug"] == "inventory_demand"],
           "budgets": {"max_llm_calls": 55, "max_total_tokens": 700_000}}
    assert await agent_specs.create_initial(TENANT_A, own, actor="test", summary="initial") == "v1"
    assert await agent_specs.upgrade_builtins(TENANT_A, actor="bootstrap") == "v2"
    latest = await agent_specs.latest(TENANT_A)
    assert latest["spec"]["budgets"] == {"max_llm_calls": 55, "max_total_tokens": 700_000}
    assert len(latest["spec"]["specialists"]) == 4
    await reset_specs(TENANT_A)
