"""Domain repo helpers used by the runtime (tenant-scoped, ambient tenant via use_tenant)."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from e1_env import TENANT_A, TENANT_B
from keelson_contracts.changeset import change_set_hash
from keelson_engine.core import repo
from keelson_engine.core.context import NoTenantBound, use_tenant
from keelson_engine.core.db import tenant_session

pytestmark = pytest.mark.asyncio(loop_scope="session")

SCENARIO = {
    "id": "scn-1",
    "tenant": TENANT_A,
    "mode": "actual",
    "event_family": "SUP",
    "event_type": "trim_delay",
    "description": "Trims 7 days late",
    "perturbations": [],
    "matched": [],
    "assumptions": [],
    "config_version": "default",
}


async def test_ambient_tenant_and_messages(platform: None) -> None:
    with pytest.raises(NoTenantBound):
        await repo.list_cases()
    with use_tenant(TENANT_A):
        case_id = await repo.create_case(None, created_by={"sub": "s", "name": "Sam"})
        mid = await repo.add_message(case_id, "assistant", "Here is the plan", "run-1")
        assert mid
        await repo.set_case_mode(case_id, "what_if")
        await repo.set_case_status(case_id, "awaiting_approval")
        case = await repo.get_case(case_id)
        assert case is not None and case["mode"] == "what_if" and case["status"] == "awaiting_approval"
        assert case["title"] == "New case" and case["last_message_preview"] == "Here is the plan"
        msgs = await repo.list_messages(case_id)
        assert [(m["role"], m["text"], m["run_id"]) for m in msgs] == [("assistant", "Here is the plan", "run-1")]
        with pytest.raises(ValueError):
            await repo.set_case_mode(case_id, "fantasy")
    with pytest.raises(repo.CaseNotFound):
        await repo.set_case_status(case_id, "x", tenant=TENANT_B)


async def test_scenarios_plans_evidence_calc(platform: None) -> None:
    case_id = await repo.create_case("repo", created_by="u", tenant=TENANT_A)
    v1 = await repo.save_scenario(case_id, SCENARIO, tenant=TENANT_A)
    v2 = await repo.save_scenario(case_id, SCENARIO, tenant=TENANT_A)
    assert (v1, v2) == (1, 2)
    latest = await repo.latest_scenario(case_id, tenant=TENANT_A)
    assert latest is not None and latest.version == 2 and latest.scenario.id == "scn-1"
    assert latest.fingerprint == latest.scenario.fingerprint()
    assert await repo.latest_scenario(case_id, tenant=TENANT_B) is None

    plan_id = await repo.save_plan(case_id, 1, [{"id": "opt-A", "title": "Rebalance"}, {"id": "opt-0"}], "opt-A",
                                   {"opt-A": {"units_recovered": 400}}, tenant=TENANT_A)
    plan = await repo.latest_plan(case_id, tenant=TENANT_A)
    assert plan is not None and plan.id == plan_id and plan.version == 1 and plan.option("opt-A") is not None
    await repo.set_plan_status(case_id, 1, "committed", tenant=TENANT_A)
    assert (await repo.latest_plan(case_id, tenant=TENANT_A)).status == "committed"  # type: ignore[union-attr]

    eid = await repo.save_evidence(case_id, "run-1", "merxa", "get_inventory", {"location_codes": ["UK1"]},
                                   {"items": [{"sku": "PUF-W-BLK-M", "atp": 12}]}, "fact", tenant=TENANT_A)
    assert eid.startswith("ev_")
    got = await repo.get_evidence([eid, "ev_missing"], tenant=TENANT_A)
    assert len(got) == 1 and got[0]["result"]["items"][0]["atp"] == 12
    assert await repo.get_evidence([eid], tenant=TENANT_B) == []
    assert [e["id"] for e in await repo.list_evidence(case_id, run_id="run-1", tenant=TENANT_A)] == [eid]

    await repo.save_calc(case_id, "run-1", "calc_1", "sum", {"over": eid}, {"value": 12}, tenant=TENANT_A)
    calc = await repo.get_calc("calc_1", tenant=TENANT_A)
    assert calc is not None and calc["result"] == {"value": 12}
    await repo.upsert_investigation_task(case_id, "task-inv-r1", "inventory_demand", run_id="run-1",
                                         status="ok", tenant=TENANT_A)
    fid = await repo.save_finding(case_id, "task-inv-r1", "run-1", {"status": "ok", "claims": []}, tenant=TENANT_A)
    assert fid > 0


async def test_change_sets_and_ledger(platform: None) -> None:
    case_id = await repo.create_case("cs", created_by="u", tenant=TENANT_A)
    await repo.save_plan(case_id, 3, [{"id": "opt-A"}], "opt-A", {}, tenant=TENANT_A)
    items = [{"seq": 1, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
              "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-19",
                          "lines": [{"sku": "PUF-W-BLK-M", "qty": 400}]}}]
    h = change_set_hash(items)
    cs_id = await repo.create_change_set(case_id, 3, "opt-A", "merxa", items, h, tenant=TENANT_A)
    await repo.update_change_set(cs_id, status="validated", ext_change_set_id="ext-1", ext_code="CS-0001",
                                 preview={"totals": {"lines": 1}}, tenant=TENANT_A)
    cs = await repo.get_change_set(cs_id, tenant=TENANT_A)
    assert cs is not None and cs["hash"] == h and cs["status"] == "validated" and cs["ext_code"] == "CS-0001"
    assert cs["plan_version"] == 3 and cs["plan_id"] is not None and cs["items"] == items
    assert cs["preview"] == {"totals": {"lines": 1}}
    with pytest.raises(ValueError):
        await repo.update_change_set(cs_id, tenant_id="x", tenant=TENANT_A)
    assert await repo.get_change_set(cs_id, tenant=TENANT_B) is None
    await repo.ledger_mark(cs_id, 1, "attempted", tenant=TENANT_A)
    await repo.ledger_mark(cs_id, 1, "succeeded", "TO-000123", tenant=TENANT_A)
    await repo.ledger_mark(cs_id, 1, "verified", tenant=TENANT_A)
    ledger = await repo.list_ledger(cs_id, tenant=TENANT_A)
    assert [(r["line_seq"], r["state"], r["ext_ref"]) for r in ledger] == [(1, "verified", "TO-000123")]


async def test_agent_spec_and_audit(platform: None) -> None:
    async with tenant_session(TENANT_A) as s:
        await s.execute(
            text("INSERT INTO agent_spec (tenant, version, spec, created_by) VALUES (:t, 'v-test', '{\"a\": 1}', 'sam') "
                 "ON CONFLICT DO NOTHING"),
            {"t": TENANT_A},
        )
    spec = await repo.get_agent_spec("v-test", tenant=TENANT_A)
    assert spec is not None and spec["spec"] == {"a": 1}
    assert await repo.get_agent_spec("v-test", tenant=TENANT_B) is None
    await repo.admin_audit("system", "test.str_actor", "x", tenant=TENANT_A)
    await repo.admin_audit({"sub": "p", "username": "ava"}, "test.platform", "y", platform=True)
    async with tenant_session(TENANT_A) as s:
        rows = (await s.execute(text(
            "SELECT action, actor_username FROM admin_audit WHERE action IN ('test.str_actor', 'test.platform')"
        ))).all()
    assert [tuple(r) for r in rows] == [("test.str_actor", "system")]  # platform rows are invisible to tenants
