"""M5 integration seams: the runtime's adapters (``runtime.deps``) driven against the real platform
modules and database — checklists, signals, plan dependencies — and the runtime CEL evaluator
registering itself with ``core.policies``. Skipped while the runtime side is not importable."""

from __future__ import annotations

import pytest

from e1_env import TENANT_A
from keelson_contracts.blocks import ChecklistStep
from keelson_engine.core import policies, replan, repo

pytestmark = pytest.mark.asyncio(loop_scope="session")

PRIYA = {"sub": "sub-priya", "username": "priya", "name": "Priya Shah", "roles": ["planner"]}


def _runtime_deps() -> object:
    try:
        from keelson_engine.runtime import deps
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"runtime.deps not importable: {exc}")
    return deps


async def test_runtime_checklists_port(platform: None) -> None:
    deps = _runtime_deps()
    adapter_factory = getattr(deps, "_checklists_adapter", None)
    if adapter_factory is None:
        pytest.skip("runtime has no checklists adapter")
    port = adapter_factory()
    assert port is not None, "core.checklists must provide every ChecklistsPort function"
    case_id = await repo.create_case("ports", created_by=PRIYA, tenant=TENANT_A)
    step = {"seq": 1, "title": "Create TO", "instructions_md": "Do it.", "deep_link": None,
            "expected": {"entity_type": "transfer_order", "entity_ref": None, "match": {"from_dc": "DE1"}},
            "status": "pending", "verified_at": None, "evidence_id": None}
    await port.create_checklist(tenant=TENANT_A, case_id=case_id, checklist_id=f"chk-{case_id}", connector="merxa",
                                plan_version=1, option_id="opt-A", steps=[step], meta={"vendor": "Merxa"})
    row = await port.get_checklist(tenant=TENANT_A, checklist_id=f"chk-{case_id}")
    assert row is not None and row["meta"] == {"vendor": "Merxa"}
    assert ChecklistStep.model_validate(row["steps"][0]).status == "pending"
    assert f"chk-{case_id}" in [c["id"] for c in await port.list_open_checklists(tenant=TENANT_A)]
    row = await port.set_step(tenant=TENANT_A, checklist_id=f"chk-{case_id}", seq=1, status="verified",
                              verified_at="2026-10-07T10:00:00+00:00", evidence_id="ev_ports1", actor="read-back")
    assert row is not None and row["status"] == "completed"
    assert row["steps"][0]["verified_at"].startswith("2026-10-07T10:00:00")
    await port.set_status(tenant=TENANT_A, checklist_id=f"chk-{case_id}", status="cancelled")


async def test_runtime_signals_and_plans_ports(platform: None) -> None:
    deps = _runtime_deps()
    signals_cls = getattr(deps, "_SignalsAdapter", None)
    plans_cls = getattr(deps, "_PlansAdapter", None)
    if signals_cls is None or plans_cls is None:
        pytest.skip("runtime has no signals/plans adapters")
    case_id = await repo.create_case("ports", created_by=PRIYA, tenant=TENANT_A)
    await repo.save_plan(case_id, 1, [{"id": "opt-A"}], "opt-A", tenant=TENANT_A)
    plans = plans_cls(repo)
    await plans.save_plan_dependencies(tenant=TENANT_A, case_id=case_id, plan_version=1,
                                       dependencies={"lanes": ["CGP-UK1-SA"], "dcs": ["UK1"], "po_lines": ["PO-1#1"]})
    deps_saved = await replan.dependencies_of(TENANT_A, case_id, 1)
    assert deps_saved["lane"] == ["CGP-UK1-SA"] and deps_saved["receipt"] == ["PO-1#1"]
    assert await plans.list_plan_versions(tenant=TENANT_A, case_id=case_id) == [{"version": 1, "status": "proposed"}]
    assert await signals_cls().get_signal(tenant=TENANT_A, signal_id="no-such-signal") is None


async def test_runtime_registers_its_cel_evaluator(platform: None) -> None:
    try:
        from keelson_engine.runtime import policy as runtime_policy
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"runtime.policy not importable: {exc}")
    register = getattr(runtime_policy, "register_policy_evaluator", None)
    if register is None:
        pytest.skip("runtime.policy has no register_policy_evaluator")
    policies.register_policy_evaluator(None)
    try:
        assert register() is True
        assert policies.evaluator_available()
        decision = await policies.evaluate(policies.DEFAULT_POLICY, policies.sample_context())
        assert decision.outcome == "auto"
        assert await policies.check_rules_compile(policies.DEFAULT_POLICY) is True
    finally:
        policies.register_policy_evaluator(None)
        policies._install_tried = False
