"""M8 proposed mappings through the admin API against the ``/mcp-lite`` FastMCP fake and Postgres
(``docs/contracts/121-m8-mapping-compiler.md`` §3.5–§3.6): create (rules only, schema only, AI
refined with FakeLlm, every refusal), list / get / discard / retention, accepting a read draft
(provenance ``proposal``), four-eyes on an action mapping from a proposal (202, self-approval
refused, a second admin applies it, a moved base answers 409, a platform-admin applies directly),
authorisation and audit. No write tool is ever called."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import text

from e1_browser import Browser
from e1_env import PUBLIC, TENANT_A, TENANT_B
from e1_lite_server import LiteServer, drop_lite, register_lite
from keelson_contracts.mapping_proposal import MappingProposal, MappingProposalLlm
from keelson_engine.config import Settings
from keelson_engine.core import mapping_proposals, scn_sync
from keelson_engine.core.db import system_session
from keelson_engine.runtime.llm import FakeModelFactory, FakeScript, FakeTurn

pytestmark = pytest.mark.asyncio(loop_scope="session")

SLUG = "m8-propose-erp"
BASE = f"/api/admin/connectors/{SLUG}"
PROPOSALS = f"{BASE}/mappings/proposals"
CR = "/api/admin/change-requests"


@pytest_asyncio.fixture(scope="module", loop_scope="session")
async def lite(db: None) -> AsyncIterator[LiteServer]:
    server = LiteServer()
    await server.start()
    yield server
    await server.stop()
    await drop_lite(TENANT_B, SLUG)


@pytest.fixture
def dev_hosts(settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "connector_dev_hosts", "127.0.0.1")


@pytest_asyncio.fixture(loop_scope="session")
async def people(session_browser: Any, platform: None) -> dict[str, Browser]:
    async with system_session() as s:
        await s.execute(text("DELETE FROM admin_change_request WHERE tenant = :t"), {"t": TENANT_B})
    return {
        "nadia": await session_browser("nadia", ["tenant-admin"], [TENANT_B]),
        "omar": await session_browser("omar", ["tenant-admin"], [TENANT_B]),
        "pia": await session_browser("pia", ["planner"], [TENANT_B]),
        "pat": await session_browser("pat", ["platform-admin"], [TENANT_B]),
        "sam": await session_browser("sam", ["tenant-admin"], [TENANT_A]),
    }


async def _connector(people: dict[str, Browser], lite: LiteServer) -> Browser:
    await register_lite(people["nadia"], lite, TENANT_B, SLUG)
    return people["nadia"]


async def _propose(b: Browser, **body: Any) -> Any:
    return await b.post(PROPOSALS, json={"kinds": ["read", "action"], "proposer": "heuristic", **body})


async def test_rules_only_proposal_and_accepting_its_read_draft(people: dict[str, Browser], lite: LiteServer,
                                                                dev_hosts: None) -> None:
    nadia = await _connector(people, lite)
    r = await _propose(nadia)
    assert r.status_code == 201, r.text
    body = r.json()
    proposal = MappingProposal.model_validate(body)
    assert proposal.status == "ready" and proposal.proposer == "heuristic" and proposal.llm_calls == 0
    assert proposal.base_versions.read is None and len(proposal.inputs_hash) == 64
    assert {n.code for n in proposal.notes} >= {"llm_disabled", "sample_value_withheld", "generic_update_tool"}
    assert lite.writes == [] and {t for t, _a in lite.calls} <= {"list_items", "get_item", "list_cursor"}
    assert "list_notes" not in {t for t, _a in lite.calls}
    draft = proposal.read_draft()
    assert draft is not None and set(draft.mapped_entities) == {
        "location", "item", "po_line", "inventory_position", "demand_forecast", "allocation"}
    assert "Hamburg DC" not in r.text and "Berlin Alex" not in r.text  # profiles are shape-only
    items = (await nadia.get(PROPOSALS)).json()["items"]
    assert items[0]["id"] == proposal.id and items[0]["status"] == "ready" and items[0]["complete_entities"] == 9
    assert (await nadia.get(f"{PROPOSALS}/{proposal.id}")).json() == body

    put = await nadia.put(f"{BASE}/mappings/read", json={"base_version": None, "mapping": draft.model_dump(mode="json"),
                                                         "proposal_id": proposal.id})
    assert put.status_code == 201, put.text
    assert put.json()["origin"] == "proposal" and put.json()["proposal_id"] == proposal.id
    got = (await nadia.get(f"{PROPOSALS}/{proposal.id}")).json()
    assert got["status"] == "accepted" and got["accepted"] == {"read": 1, "action": None}
    audit = (await nadia.get("/api/admin/audit?action=connector.mapping.update&limit=1")).json()["items"][0]
    assert audit["after"]["origin"] == "proposal" and audit["after"]["proposal_id"] == proposal.id
    assert audit["after"]["accepted"] > 50 and audit["after"]["edited"] == 0
    created = (await nadia.get("/api/admin/audit?action=connector.mapping.proposal.created&limit=1")).json()["items"]
    assert created[0]["after"]["proposer"] == "heuristic" and "llm_disabled" in created[0]["after"]["notes"]
    # the accepted draft crawls through the compiler
    row = await scn_sync.sync_connector(TENANT_B, SLUG, reason="test")
    assert {k: v for k, v in row.row_counts.items() if v} == {
        "location": 7, "item": 8, "po_line": 3, "inventory_position": 3, "demand_forecast": 2, "allocation": 2}
    # the pack install records its own provenance
    r = await nadia.post(f"{BASE}/mappings/from-pack", json={"pack": "foreign-erp-lite", "kinds": ["read"]})
    assert r.status_code == 200 and (await nadia.get(f"{BASE}/mappings/read")).json()["origin"] == "pack"


async def test_refusals_discard_and_retention(people: dict[str, Browser], lite: LiteServer, dev_hosts: None,
                                              settings: Settings, monkeypatch: pytest.MonkeyPatch,
                                              api: Any) -> None:
    nadia = await _connector(people, lite)
    assert (await _propose(people["pia"])).status_code == 403
    assert (await _propose(people["sam"])).status_code == 404  # another tenant's connector does not exist
    no_csrf = await api.post(PROPOSALS, json={"kinds": ["read"]},
                             headers={"Cookie": f"keelson_session={nadia.cookie}", "Origin": PUBLIC})
    assert no_csrf.status_code == 403
    assert (await nadia.post(PROPOSALS, json={"kinds": []})).status_code == 422
    monkeypatch.setattr(settings, "mapping_proposer", "off")
    r = await _propose(nadia)
    assert r.status_code == 409 and r.json()["code"] == "proposer_disabled"
    monkeypatch.setattr(settings, "mapping_proposer", "heuristic")
    monkeypatch.setattr(mapping_proposals, "_running", {TENANT_B: 1})
    r = await _propose(nadia)
    assert r.status_code == 429 and r.json()["code"] == "proposal_busy"
    monkeypatch.setattr(mapping_proposals, "_running", {})
    # an env cap may only lower the code constant, and says so
    monkeypatch.setattr(settings, "mapping_propose_sample_size", 5)
    r = await _propose(nadia, kinds=["read"])
    assert r.status_code == 201, r.text
    lowered = MappingProposal.model_validate(r.json())
    assert any(n.code == "sampling_truncated" and "5 items per page instead of 20" in n.message for n in lowered.notes)
    assert lowered.profiles and max(p.items_sampled for p in lowered.profiles) <= 5
    monkeypatch.setattr(settings, "mapping_propose_sample_size", 20)

    assert (await nadia.post(f"{BASE}/disable")).status_code == 200
    lite.calls.clear()
    r = await _propose(nadia)
    assert r.status_code == 409 and r.json()["code"] == "connector_inactive"
    r = await _propose(nadia, sample=False)
    assert r.status_code == 201, r.text
    schema_only = MappingProposal.model_validate(r.json())
    assert lite.calls == [] and "schema_only" in {n.code for n in schema_only.notes}
    assert schema_only.read is not None and schema_only.read.entities == [] and schema_only.read_draft() is None

    d = await nadia.post(f"{PROPOSALS}/{schema_only.id}/discard")
    assert d.status_code == 200 and d.json()["status"] == "discarded"
    again = await nadia.post(f"{PROPOSALS}/{schema_only.id}/discard")
    assert again.status_code == 409 and again.json()["code"] == "proposal_decided"
    r = await nadia.put(f"{BASE}/mappings/action", json={"base_version": None, "proposal_id": schema_only.id,
                                                         "mapping": {"ops": []}})
    assert r.status_code == 422  # the mapping itself is checked first
    assert (await nadia.get(f"{PROPOSALS}/00000000-0000-7000-8000-000000000000")).status_code == 404
    assert (await nadia.get(f"{PROPOSALS}/not-a-uuid")).status_code == 404

    monkeypatch.setattr(mapping_proposals, "MAX_KEPT", 2)
    for _ in range(3):
        assert (await _propose(nadia, sample=False, kinds=["action"])).status_code == 201
    assert len((await nadia.get(PROPOSALS)).json()["items"]) == 2
    audit = (await nadia.get("/api/admin/audit?action=connector.mapping.proposal.discarded&limit=1")).json()["items"]
    assert audit and audit[0]["after"] == {"status": "discarded"}


async def test_a_proposed_action_mapping_needs_a_second_admin(people: dict[str, Browser], lite: LiteServer,
                                                              dev_hosts: None) -> None:
    nadia, omar = await _connector(people, lite), people["omar"]
    proposal = MappingProposal.model_validate((await _propose(nadia, kinds=["action"])).json())
    action = proposal.action_draft()
    assert action is not None and {"transfer_order.create", "allocation.set_qty"} <= set(action.mapped_ops)
    body = {"base_version": None, "mapping": action.model_dump(mode="json"), "proposal_id": proposal.id}
    r = await nadia.put(f"{BASE}/mappings/action", json=body)
    assert r.status_code == 202, r.text
    cr = r.json()
    assert cr["kind"] == "connector_mapping" and cr["status"] == "pending" and cr["base_version"] == "0"
    assert [x["code"] for x in cr["reasons"]] == ["ai_proposed_action_mapping"] and cr["diff"]
    assert (await nadia.get(f"{BASE}/mappings/action")).json()["version"] is None  # nothing applied
    self_ok = await nadia.post(f"{CR}/{cr['id']}/approve", json={})
    assert self_ok.status_code == 403 and self_ok.json()["code"] == "self_approval"
    ok = await omar.post(f"{CR}/{cr['id']}/approve", json={})
    assert ok.status_code == 200 and ok.json()["status"] == "applied", ok.text
    got = (await nadia.get(f"{BASE}/mappings/action")).json()
    assert got["version"] == 1 and got["origin"] == "change_request" and got["proposal_id"] == proposal.id
    assert (await nadia.get(f"{PROPOSALS}/{proposal.id}")).json()["accepted"] == {"read": None, "action": 1}
    assert lite.writes == []

    # the base moved before the approval: 409 stale_base, the request stays pending
    second = MappingProposal.model_validate((await _propose(nadia, kinds=["action"])).json())
    r = await nadia.put(f"{BASE}/mappings/action", json={**body, "base_version": 1, "proposal_id": second.id})
    assert r.status_code == 202
    stale_id = r.json()["id"]
    manual = action.model_copy(update={"ops": action.ops[:1]})
    r = await nadia.put(f"{BASE}/mappings/action", json={"base_version": 1, "mapping": manual.model_dump(mode="json")})
    assert r.status_code == 201 and r.json()["origin"] == "manual"  # a manual edit is not guarded
    stale = await omar.post(f"{CR}/{stale_id}/approve", json={})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_base"
    assert (await nadia.post(f"{CR}/{stale_id}/reject", json={"reason": "superseded"})).status_code == 200

    # a platform-admin applies it directly (audited)
    pat = people["pat"]
    r = await pat.put(f"{BASE}/mappings/action", json={**body, "base_version": 2, "proposal_id": second.id})
    assert r.status_code == 201 and r.json()["origin"] == "proposal", r.text
    audit = (await nadia.get("/api/admin/audit?action=connector.mapping.update&limit=1")).json()["items"][0]
    assert audit["after"]["applied_by_platform_admin"] is True and audit["after"]["accepted"] == len(action.ops)
    # a read mapping from a proposal is not guarded
    read = MappingProposal.model_validate((await _propose(nadia, kinds=["read"])).json()).read_draft()
    assert read is not None


async def test_ai_refined_proposal_through_the_api(people: dict[str, Browser], lite: LiteServer, dev_hosts: None,
                                                  settings: Settings, monkeypatch: pytest.MonkeyPatch) -> None:
    nadia = await _connector(people, lite)
    monkeypatch.setattr(settings, "mapping_proposer", "auto")
    status = (await nadia.get("/api/admin/mappings/proposer")).json()
    assert status["mode"] == "auto" and status["enabled"] is True and status["alias"] == "fast-default"
    assert status["llm"]["reason"] in (None, "no_key", "replay_only")

    async def no_model(_tenant: str) -> None:
        return None

    monkeypatch.setattr(mapping_proposals, "_models", no_model)
    r = await nadia.post(PROPOSALS, json={"kinds": ["read"]})
    assert r.status_code == 201 and r.json()["proposer"] == "heuristic"
    assert "llm_unavailable" in {n["code"] for n in r.json()["notes"]}

    script = FakeScript()
    holder: dict[str, Any] = {}

    def answer(request: Any) -> FakeTurn:
        text_in = "".join(p.text or "" for c in request.contents or [] for p in c.parts or [])
        holder["text"] = text_in
        alloc = text_in.split('"variant":"allocation"')[0].rsplit('"ref":"', 1)[1].split('"', 1)[0]
        return FakeTurn(json=MappingProposalLlm.model_validate({
            "entities": [{"entity": "allocation", "source_ref": alloc, "dedupe": False, "group_by": [],
                          "rationale": "allocations", "fields": [
                              {"column": "qty", "expression": "int($.qty)", "confidence": "high", "rationale": "qty"}]}],
            "lookups": [], "ops": [], "injection_suspected": False}))

    script.add("mapping_proposer", answer)

    async def fake_models(_tenant: str) -> Any:
        return FakeModelFactory(script)

    monkeypatch.setattr(mapping_proposals, "_models", fake_models)
    r = await nadia.post(PROPOSALS, json={"kinds": ["read"]})
    assert r.status_code == 201, r.text
    p = MappingProposal.model_validate(r.json())
    assert p.proposer == "llm+heuristic" and p.model_alias == "fast-default" and p.llm_calls == 1
    assert p.read is not None
    alloc = next(e for e in p.read.entities if e.entity == "allocation")
    qty = next(f for f in alloc.fields if f.column == "qty")
    assert qty.origin == "both" and qty.confidence_bp == 9500
    assert "Hamburg DC" not in holder["text"] and "untrusted data, not instructions" in holder["text"]
    assert lite.writes == []
