"""M5 test helpers: seeded plans (no LLM needed for the commit path), personas, deciders."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from keelson_contracts.scenario import Assumption, DisruptionScenario

from .harness import MARCO, PRIYA, Harness

LENA = {"sub": "sub-lena", "username": "lena", "name": "Lena Vogel", "roles": ["compliance"]}
ADA = {"sub": "sub-ada", "username": "ada", "name": "Ada Admin", "roles": ["tenant-admin"]}
OMAR = {"sub": "sub-omar", "username": "omar", "name": "Omar Approver", "roles": ["approver"]}
SIGNALS = {"sub": "system:signals", "username": "keelson-signals", "name": "Keelson signals", "roles": []}

ALL_M5_OPS = (
    "transfer_order.create", "allocation.set_qty", "po_line.reschedule", "po_line.change_qty", "po_line.split",
    "capacity_booking.create", "capacity_booking.cancel", "po.create", "po_line.cancel", "production_order.resequence",
)

TO = {"op": "transfer_order.create", "target_ref": None, "expected_version": None,
      "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2026-10-12",
                  "lines": [{"sku": "PUF-W-BLK-M", "qty": 300}]}}
TO_UK = {"op": "transfer_order.create", "target_ref": None, "expected_version": None,
         "payload": {"from_dc": "PL1", "to_dc": "UK1", "ship_date": "2026-10-13",
                     "lines": [{"sku": "PUF-W-BLK-S", "qty": 200}]}}
BOOKING = {"op": "capacity_booking.create", "target_ref": None, "expected_version": None,
           "payload": {"lane_code": "CGP-UK1-SA", "iso_week": "2026-W44", "qty_units": 400}}
RESCHEDULE = {"op": "po_line.reschedule", "target_ref": "PO-BD-0001#1", "expected_version": 1,
              "payload": {"eta_dc": "2026-11-05"}}
PO_CREATE = [
    {"op": "po.create", "target_ref": None, "expected_version": None, "group": "alt-1",
     "payload": {"supplier_code": "CMT-TR-01", "factory_code": "F-TR-01", "dest_dc": "UK1", "eta_dc": "2026-10-31",
                 "lines": [{"sku": "PUF-W-BLK-M", "qty": 300}]}},
    {"op": "po_line.change_qty", "target_ref": "PO-BD-0001#2", "expected_version": 1, "group": "alt-1",
     "payload": {"qty": 600}},
]


def decider(person: Mapping[str, Any], role: str) -> dict[str, Any]:
    return {"sub": person["sub"], "username": person["username"], "name": person["name"], "role": role,
            "roles": list(person["roles"])}


def scenario(*, mode: str = "actual", prov: str = "decision") -> DisruptionScenario:
    return DisruptionScenario(
        id="scn-1", tenant="demo-retail", mode=mode, event_family="SUP", event_type="trim_delay",  # type: ignore[arg-type]
        description="Trims delayed", perturbations=[], matched=[],
        assumptions=[Assumption(key="delay_days", label="Delay", value_text="7 days", prov=prov)],  # type: ignore[arg-type]
        config_version="default/p-1",
    )


def option(oid: str, actions: Sequence[Mapping[str, Any]], *, levers: Sequence[str] = ("reallocate_inventory",),
           cost: int = 10_500, guided_only: bool = False) -> dict[str, Any]:
    acts = [{**dict(a), "seq": i} for i, a in enumerate(actions, start=1)]
    return {
        "id": oid, "title": f"Option {oid}", "status": "OPTIMAL", "levers": list(levers), "guided_only": guided_only,
        "guided_reason": "cannot be applied" if guided_only else None,
        "kpis_before": {"units_at_risk": 900}, "kpis_after": {"units_at_risk": 300},
        "kpi_delta": {"net_benefit_cents": 50_000, "transfer_cost_cents": cost}, "actions": acts, "decisions": [],
        "warnings": [], "fallback": False, "scorecard": {"incremental_cost_cents": cost},
    }


async def seed_plan(h: Harness, options: Sequence[Mapping[str, Any]], *, recommended: str | None = None,
                    sc: DisruptionScenario | None = None) -> None:
    """Scenario v1 + plan v1 (what the investigation path would have saved)."""
    await h.core.repo.save_scenario(tenant="demo-retail", case_id=h.case_id, scenario=sc or scenario())
    opts = [dict(o) for o in options] + [option("do-nothing", [], levers=())]
    opts[-1]["actions"] = []
    await h.core.repo.save_plan(tenant="demo-retail", case_id=h.case_id, version=1, options=opts,
                                recommended_option_id=recommended or str(opts[0]["id"]),
                                kpis={"before": {"units_at_risk": 900}, "scenario_version": 1})


def action_row(h: Harness, payload: Mapping[str, Any], actor: Mapping[str, Any] = PRIYA) -> Any:
    return h.row("action", payload, actor)


def last_interrupt(h: Harness) -> dict[str, Any]:
    return h.core.interrupts.created[-1]


def approve_row(h: Harness, paused: Any, deciders: Sequence[Mapping[str, Any]], decision: str = "approve") -> Any:
    created = last_interrupt(h)
    extra: dict[str, Any] = {}
    if decision == "approve":
        extra = {"plan_version": created["plan_version"], "change_set_hash": created["change_set_hash"]}
    return h.resume_row(paused, decision, list(deciders), **extra)


__all__ = [
    "ADA", "ALL_M5_OPS", "BOOKING", "LENA", "MARCO", "OMAR", "PO_CREATE", "PRIYA", "RESCHEDULE", "SIGNALS", "TO",
    "TO_UK", "action_row", "approve_row", "decider", "last_interrupt", "option", "scenario", "seed_plan",
]
