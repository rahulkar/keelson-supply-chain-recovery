"""Back-office agent specs, M4 additions: planner / critic prompt addenda (validated, exposed in the
catalog) and the semantic check of ``SpecialistSpec.quarantined`` sources (a connector source needs
an approved ``untrusted_text`` read tool on the bound connector; ``knowledge_base`` needs an existing
knowledge base)."""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
from sqlalchemy import text

from e1_browser import Browser
from e1_env import TENANT_A
from e1_litellm import FakeLiteLLM
from keelson_engine.core import agent_specs, kb
from keelson_engine.core.connector_admin import AdminError
from keelson_engine.core.db import system_session, tenant_session
from test_admin_agents import SPEC, _put, merxa_tools, people  # noqa: F401  (fixtures)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _specialists(spec: dict[str, Any], **quarantined: list[str]) -> list[dict[str, Any]]:
    """The spec's specialists with ``quarantined`` replaced for the given slugs."""
    out = []
    for sp in spec["specialists"]:
        sp = dict(sp)
        if sp["slug"] in quarantined:
            sp["quarantined"] = quarantined[sp["slug"]]
        out.append(sp)
    return out


async def _current(b: Browser) -> dict[str, Any]:
    return (await b.get(SPEC)).json()


async def test_planner_and_critic_addenda(people: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                          merxa_tools: None) -> None:
    sam = people["sam"]
    cur = await _current(sam)
    r = await _put(sam, cur["version"], {"prompts": {"planner": "Always consult the playbook analyst.",
                                                     "critic": "Be strict about stale as-of dates."}})
    assert r.status_code == 201, r.text
    spec = r.json()["spec"]
    assert spec["prompts"]["planner"] == "Always consult the playbook analyst."
    assert spec["prompts"]["critic"] == "Be strict about stale as-of dates."
    v = r.json()["version"]
    for role in ("planner", "critic"):
        too_long = await _put(sam, v, {"prompts": {role: "x" * 4001}})
        assert too_long.status_code == 422 and too_long.json()["code"] == "spec_invalid", too_long.text
    assert (await _put(sam, v, {"prompts": {"critic": "y" * 4000}})).status_code == 201
    v = (await _current(sam))["version"]
    # the other M4 roles stay without a slot (they read untrusted or user text)
    for role in ("guard", "extractor", "followup"):
        r = await _put(sam, v, {"prompts": {role: "be brief"}})
        assert r.status_code == 422 and r.json()["code"] == "spec_invalid", r.text

    cat = (await sam.get("/api/admin/agents/catalog")).json()
    roles = {r["role"]: r for r in cat["roles"]}
    assert roles["planner"]["display_name"] == "Planner" and roles["critic"]["display_name"] == "Critic"
    assert roles["planner"]["prompt_role"] == "planner" and roles["critic"]["prompt_role"] == "critic"
    assert roles["planner"]["prompt"] == "Always consult the playbook analyst."
    assert roles["critic"]["prompt"] == "y" * 4000
    assert roles["planner"]["alias"] == "reasoning-default" and roles["critic"]["alias"] == "critic-default"
    assert roles["planner"]["uses_tools"] is False and roles["critic"]["uses_tools"] is False
    assert roles["guard"]["prompt_role"] is None and roles["guard"]["prompt"] is None
    specialists = {s["slug"]: s for s in cat["specialists"]}
    assert specialists["sourcing"]["quarantined"] == ["supplier_messages"]
    assert specialists["playbook"]["quarantined"] == ["knowledge_base"]

    # clearing with null (merge patch) removes the addendum
    cleared = await _put(sam, v, {"prompts": {"planner": None, "critic": None}})
    assert cleared.status_code == 201, cleared.text
    assert cleared.json()["spec"]["prompts"]["planner"] is None


async def test_catalog_fills_m4_aliases_for_an_older_stored_version(people: dict[str, Browser],
                                                                   gateway_ready: FakeLiteLLM,
                                                                   merxa_tools: None) -> None:
    """A version stored before M4 has no planner / critic alias: the catalog shows the effective
    defaults, so the admin page's Planner and Critic cards are never blank."""
    sam = people["sam"]
    default = agent_specs.default_spec_dict(TENANT_A)
    m3 = {k: v for k, v in default.items() if k not in ("investigation",)}
    m3["models"] = {k: v for k, v in default["models"].items()
                    if k not in ("planner", "critic", "guard", "extractor", "followup")}
    m3["specialists"] = [sp for sp in default["specialists"] if sp["slug"] == "inventory_demand"]
    for sp in m3["specialists"]:
        sp.pop("quarantined", None)
    cur = await _current(sam)
    async with tenant_session(TENANT_A) as s:
        seq = int((await s.execute(text("SELECT COALESCE(max(seq), 0) FROM agent_spec"))).scalar_one()) + 1
        await s.execute(text(
            "INSERT INTO agent_spec (tenant, version, seq, spec, created_by, summary, base_version) "
            "VALUES (:t, :v, :seq, CAST(:spec AS jsonb), 'test', 'm3 row', :b)"),
            {"t": TENANT_A, "v": f"v{seq}", "seq": seq, "spec": json.dumps(m3), "b": cur["version"]})
    cat = (await sam.get("/api/admin/agents/catalog")).json()
    roles = {r["role"]: r for r in cat["roles"]}
    assert roles["planner"]["alias"] == "reasoning-default" and roles["critic"]["alias"] == "critic-default"
    assert roles["planner"]["prompt_role"] == "planner"


async def test_quarantined_connector_sources(people: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                             merxa_tools: None) -> None:
    sam = people["sam"]
    cur = await _current(sam)
    spec = cur["spec"]
    # removing a source is never checked
    r = await _put(sam, cur["version"], {"specialists": _specialists(spec, sourcing=[], logistics=[])})
    assert r.status_code == 201, r.text
    v, spec = r.json()["version"], r.json()["spec"]

    # list_events is not in this connector's snapshot
    r = await _put(sam, v, {"specialists": _specialists(spec, logistics=["events"])})
    assert r.status_code == 422, r.text
    body = r.json()
    assert body["code"] == "quarantine_source_invalid"
    assert "list_events" in body["message"] and "does not exist" in body["message"]
    assert body["field"] == "specialists.logistics.quarantined"

    # list_supplier_messages: approved + read + untrusted_text → accepted
    ok = await _put(sam, v, {"specialists": _specialists(spec, sourcing=["supplier_messages"])})
    assert ok.status_code == 201, ok.text
    assert ok.json()["spec"]["specialists"][1]["quarantined"] == ["supplier_messages"]
    v_ok, spec_ok = ok.json()["version"], ok.json()["spec"]
    # unchanged sources are not re-checked on an unrelated edit
    assert (await _put(sam, v_ok, {"history_messages": 6})).status_code == 201
    v = (await _current(sam))["version"]

    removed = await _put(sam, v, {"specialists": _specialists(spec_ok, sourcing=[])})
    assert removed.status_code == 201, removed.text
    v, spec = removed.json()["version"], removed.json()["spec"]
    try:
        # reclassified as trusted data: no longer a quarantine source
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("UPDATE connector_tool SET content_trust = 'trusted_data' "
                                 "WHERE connector = 'merxa' AND name = 'list_supplier_messages'"))
        r = await _put(sam, v, {"specialists": _specialists(spec, sourcing=["supplier_messages"])})
        assert r.status_code == 422 and r.json()["code"] == "quarantine_source_invalid", r.text
        assert "untrusted_text" in r.json()["message"] and "trusted_data" in r.json()["message"]
        # untrusted but not approved
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("UPDATE connector_tool SET content_trust = 'untrusted_text', status = 'disabled' "
                                 "WHERE connector = 'merxa' AND name = 'list_supplier_messages'"))
        r = await _put(sam, v, {"specialists": _specialists(spec, sourcing=["supplier_messages"])})
        assert r.status_code == 422 and r.json()["code"] == "quarantine_source_invalid", r.text
        assert "disabled" in r.json()["message"]
    finally:
        async with tenant_session(TENANT_A) as s:
            await s.execute(text("UPDATE connector_tool SET content_trust = 'untrusted_text', status = 'approved' "
                                 "WHERE connector = 'merxa' AND name = 'list_supplier_messages'"))

    # moving a specialist to another connector re-checks all its sources
    moved = [dict(sp, connector="nope") if sp["slug"] == "sourcing" else sp
             for sp in _specialists(spec, sourcing=["supplier_messages"])]
    r = await _put(sam, v, {"specialists": moved})
    assert r.status_code == 422 and r.json()["code"] == "unknown_connector", r.text


async def test_quarantined_knowledge_base(people: dict[str, Browser], gateway_ready: FakeLiteLLM,
                                          merxa_tools: None, monkeypatch: pytest.MonkeyPatch) -> None:
    sam = people["sam"]
    cur = await _current(sam)
    spec = cur["spec"]
    created = await kb.create_kb(TENANT_A, f"m4p spec kb {uuid.uuid4().hex[:8]}", created_by="test")
    try:
        v = cur["version"]
        unknown = str(uuid.uuid4())
        for ids in ([unknown], ["not-a-uuid"]):
            r = await _put(sam, v, {"investigation": {"kb_ids": ids}})
            assert r.status_code == 422, r.text
            assert r.json()["code"] == "quarantine_source_invalid" and r.json()["field"] == "investigation.kb_ids"
            assert "do not exist" in r.json()["message"]
        r = await _put(sam, v, {"investigation": {"kb_ids": []}})  # the playbook analyst would search nothing
        assert r.status_code == 422 and r.json()["code"] == "quarantine_source_invalid", r.text
        ok = await _put(sam, v, {"investigation": {"kb_ids": [created["id"]]}})
        assert ok.status_code == 201, ok.text
        assert ok.json()["spec"]["investigation"]["kb_ids"] == [created["id"]]
        v = ok.json()["version"]
        back = await _put(sam, v, {"investigation": {"kb_ids": None}})
        assert back.status_code == 201, back.text
        v, spec = back.json()["version"], back.json()["spec"]

        # without any playbook source an empty list is harmless
        no_kb = await _put(sam, v, {"specialists": _specialists(spec, playbook=[])})
        assert no_kb.status_code == 201, no_kb.text
        v, spec = no_kb.json()["version"], no_kb.json()["spec"]
        assert (await _put(sam, v, {"investigation": {"kb_ids": []}})).status_code == 201
        v = (await _current(sam))["version"]
        r = await _put(sam, v, {"specialists": _specialists(spec, playbook=["knowledge_base"])})
        assert r.status_code == 422 and r.json()["code"] == "quarantine_source_invalid", r.text
        # adding the source with kb_ids = null (all) needs at least one knowledge base
        added = await _put(sam, v, {"specialists": _specialists(spec, playbook=["knowledge_base"]),
                                    "investigation": {"kb_ids": None}})
        assert added.status_code == 201, added.text
        v = added.json()["version"]
        removed = await _put(sam, v, {"specialists": _specialists(added.json()["spec"], playbook=[])})
        assert removed.status_code == 201, removed.text
        base = removed.json()

        async def no_kbs(tenant: str) -> list[dict[str, Any]]:
            return []

        monkeypatch.setattr(kb, "list_kbs", no_kbs)
        with pytest.raises(AdminError) as exc:
            await agent_specs.put_spec(TENANT_A, base["version"],
                                       {"specialists": _specialists(base["spec"], playbook=["knowledge_base"])},
                                       {"username": "sam"}, platform_admin=False)
        assert exc.value.status == 422 and exc.value.code == "quarantine_source_invalid"
        assert "Knowledge page" in exc.value.message
        assert exc.value.extra["field"] == "specialists.playbook.quarantined"
    finally:
        async with system_session() as s:
            await s.execute(text("DELETE FROM kb WHERE id = CAST(:k AS uuid)"), {"k": created["id"]})
