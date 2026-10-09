"""M8 CP-SAT integer stage in engine planning (``docs/contracts/125-m8-optimizer-cpsat.md`` §7), offline
(FakeLlm, the Merxa and optimizer stubs): the tenant's ``planning.solver_integer_stage`` reaches every
optimizer request (planning and re-plan share :func:`solver_options`), ``repair`` requests, option
dicts and compare props stay byte-identical, derived alternate sources carry the sourcing terms,
and ``cpsat`` options carry a solver summary that becomes ``CompareOption.integer``."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest
from fakes.harness import Harness
from fakes.merxa_stub import MerxaStub
from fakes.optimizer_stub import OptimizerStub
from fakes.scripts import chat_flow_script
from keelson_contracts.blocks import CompareInteger, parse_block
from keelson_contracts.optimizer import (
    LEVER_OPS,
    BomLine,
    InboundLane,
    OptimizeRequest,
    PoLine,
    SolverStage,
)
from pydantic import ValidationError

from keelson_engine.runtime.nodes.planning import option_from, solver_options, solver_summary
from keelson_engine.runtime.problem_scn import _alt_sources
from keelson_engine.runtime.spec import AgentSpec, AltSourcingDefaults, PlanningDefaults
from keelson_engine.runtime.visuals import compare_integer, scenario_compare_props

ALL_OPS = tuple(sorted({op for ops in LEVER_OPS.values() for op in ops}))


# ───────────────────────────── spec and solver options ─────────────────────────────


def test_defaults_keep_the_m7_requests_and_spec_dumps() -> None:
    planning = PlanningDefaults()
    assert planning.solver_integer_stage == "repair"
    assert "integer_stage" not in solver_options(planning).model_dump(mode="json")
    dumped = planning.model_dump(mode="json")
    assert "solver_integer_stage" not in dumped
    assert not {"order_multiple_units", "moq_basis", "share_factory_capacity", "max_orders_per_source"} & set(
        dumped["alt_sourcing"]
    )
    cpsat = PlanningDefaults(solver_integer_stage="cpsat", alt_sourcing=AltSourcingDefaults(order_multiple_units=6))
    assert solver_options(cpsat).model_dump(mode="json") == {"time_limit_s": 5, "seed": 7, "integer_stage": "cpsat"}
    again = PlanningDefaults.model_validate(cpsat.model_dump(mode="json"))
    assert again == cpsat and again.model_dump(mode="json")["alt_sourcing"]["order_multiple_units"] == 6


@pytest.mark.parametrize(
    "bad",
    [
        {"solver_integer_stage": "mip"},
        {"alt_sourcing": {"order_multiple_units": 0}},
        {"alt_sourcing": {"moq_basis": "sku"}},
        {"alt_sourcing": {"max_orders_per_source": 0}},
    ],
)
def test_bad_planning_values_are_rejected(bad: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        PlanningDefaults.model_validate(bad)


# ───────────────────────────── derived alternate sources ─────────────────────────────


def _derive(cfg: AltSourcingDefaults) -> list[Any]:
    lanes = [
        InboundLane(code=code, origin="F-TR-01", dest_dc=dc, mode="road", transit_days=5, cost_per_unit_cents=30,
                    weekly_capacity_units=5000)
        for code, dc in (("IST-DE1-RD", "DE1"), ("IST-NL1-RD", "NL1"))
    ]  # fmt: skip
    po = PoLine(ref="PO-1#1", po_code="PO-1", supplier_code="SUP-1", factory_code="F-BD-01", dest_dc="DE1",
                sku="PUF-01-M", qty=500, eta=dt.date(2026, 11, 2), ex_factory=dt.date(2026, 10, 1), mode="sea")
    bom = [BomLine(style_code="PUF-01", component_code="ZIP-1", supplier_code="TRIM-1", factory_code="F-TR-01")]
    return _alt_sources(
        {}, planning=PlanningDefaults(alt_sourcing=cfg), bom=bom, po_lines=[po], styles_in_scope={"PUF-01"},
        sku_codes={"PUF-01-M"}, unit_cost={"PUF-01-M": 2000}, sku_style={"PUF-01-M": "PUF-01"}, inbound=lanes,
        port_country={}, factory_country={}, parent_supplier={"F-TR-01": "CMT-TR-01"},
        factory_daily={"F-TR-01": 400},
    )  # fmt: skip


def test_derived_sources_carry_the_m8_terms() -> None:
    plain = _derive(AltSourcingDefaults())
    assert [s.lane_code for s in plain] == ["IST-DE1-RD", "IST-NL1-RD"]
    for s in plain:
        assert (s.order_multiple_units, s.moq_basis, s.capacity_group, s.max_orders) == (1, "po", None, None)
        dumped = s.model_dump(mode="json")
        assert not {"order_multiple_units", "moq_basis", "capacity_group", "max_orders"} & set(dumped)
    terms = _derive(AltSourcingDefaults(order_multiple_units=12, moq_basis="style", share_factory_capacity=True,
                                        max_orders_per_source=2))  # fmt: skip
    for s in terms:
        assert (s.order_multiple_units, s.moq_basis, s.capacity_group, s.max_orders) == (12, "style", "F-TR-01", 2)
        assert s.weekly_capacity_units == 2400 and s.moq_effective == 300  # ceil(300 / 12) * 12


# ───────────────────────────── option dicts and compare props ─────────────────────────────


def _response(stage: str, levers: list[str]) -> Any:
    """A stub response for a minimal request (the stub echoes stages for ``cpsat``)."""
    req = OptimizeRequest.model_validate({
        "data": _tiny_problem(),
        "perturbations": [],
        "plan_spec": {"id": "opt-C", "title": "C", "levers": [{"code": c} for c in levers]},
        "solver": {"time_limit_s": 5, "seed": 7, **({"integer_stage": stage} if stage != "repair" else {})},
    })  # fmt: skip
    return OptimizerStub().optimize(req)


def _tiny_problem() -> dict[str, Any]:
    return {
        "tenant": "demo-retail",
        "as_of": "2026-10-05",
        "horizon": {"start_week": "2026-W41", "weeks": 2},
        "dcs": [{"code": "UK1"}],
        "clusters": [{"code": "UK1-A", "dc_code": "UK1"}],
        "skus": [{"code": "TOP-M", "style_code": "TOP", "retail_price_cents": 3000, "unit_cost_cents": 1000}],
        "inventory": [],
        "receipts": [],
        "demand": [{"cluster_code": "UK1-A", "sku": "TOP-M", "iso_week": "2026-W41", "units": 10}],
    }


def test_option_dicts_and_compare_props_unchanged_in_repair_mode() -> None:
    resp = _response("repair", ["resource_alt_supplier"])
    assert resp.solver.stages == []
    option = option_from(resp, "C", levers=["resource_alt_supplier"])
    assert "solver" not in option
    props = scenario_compare_props([option], recommended_id="opt-C")
    assert props.options[0].integer is None
    assert "integer" not in props.model_dump(mode="json")["options"][0]


def test_cpsat_option_summary_becomes_the_compare_note() -> None:
    resp = _response("cpsat", ["resource_alt_supplier"])
    option = option_from(resp, "C", levers=["resource_alt_supplier"])
    assert option["solver"] == {"integer_stage": "cpsat", "integer_method": "cpsat_optimal", "gap_bp": 0,
                                "fallback_reason": None, "resequence_methods": None}  # fmt: skip
    props = scenario_compare_props([option], recommended_id="opt-C")
    assert props.options[0].integer == CompareInteger(method="cpsat_optimal", gap_bp=0)
    # an option without re-sourcing runs no integer stage: no note
    other = option_from(_response("cpsat", ["reallocate_inventory"]), "A", levers=["reallocate_inventory"])
    assert other["solver"]["integer_method"] is None
    assert scenario_compare_props([other], recommended_id=None).options[0].integer is None


@pytest.mark.parametrize(
    ("summary", "expected"),
    [
        ({"integer_method": "cpsat_feasible", "gap_bp": 40}, CompareInteger(method="cpsat_feasible", gap_bp=40)),
        ({"integer_method": "cpsat_feasible", "gap_bp": 99_999}, CompareInteger(method="cpsat_feasible", gap_bp=10_000)),
        ({"integer_method": "lp_rounding", "fallback_reason": "time_limit_no_solution"},
         CompareInteger(method="lp_rounding", fallback_reason="time_limit_no_solution")),
        ({"integer_method": "lp_rounding", "fallback_reason": "made-up"}, CompareInteger(method="lp_rounding")),
        ({"integer_method": None}, None),
        ({"integer_method": "magic"}, None),
        (None, None),
        ("not-a-dict", None),
    ],
)  # fmt: skip
def test_compare_integer_mapping(summary: Any, expected: CompareInteger | None) -> None:
    assert compare_integer(summary) == expected


def test_solver_summary_from_real_params() -> None:
    resp = _response("cpsat", ["resource_alt_supplier"])
    solver = resp.solver.model_copy(update={
        "params": {"integer_stage": "cpsat", "integer_method": "lp_rounding", "integer_fallback": "no_improvement",
                   "resequence_methods": "FL-VN01-A:cpsat_v2_feasible"},
        "stages": [SolverStage(name="integer", solver="CP-SAT", status="FEASIBLE", used=False,
                               fallback_reason="no_improvement")],
    })  # fmt: skip
    summary = solver_summary(resp.model_copy(update={"solver": solver}))
    assert summary == {"integer_stage": "cpsat", "integer_method": "lp_rounding", "gap_bp": None,
                       "fallback_reason": "no_improvement", "resequence_methods": "FL-VN01-A:cpsat_v2_feasible"}  # fmt: skip


# ───────────────────────────── the planning flow ─────────────────────────────


async def _plan(tmp_path: Path, stage: str | None) -> Harness:
    h = await Harness(tmp=tmp_path, merxa=MerxaStub(supported_ops=ALL_OPS)).setup()  # type: ignore[arg-type]
    planning = {"as_of": dt.date(2026, 10, 5)} | ({"solver_integer_stage": stage} if stage else {})
    h.spec = lambda tenant: AgentSpec(tenant=tenant, planning=PlanningDefaults(**planning))  # type: ignore[method-assign]
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    assert await h.run(h.row("chat", {"text": "Trims from Meghna are 7 days late"})) == "succeeded"
    return h


async def test_cpsat_spec_reaches_every_optimize_request(tmp_path: Path) -> None:
    h = await _plan(tmp_path, "cpsat")
    try:
        bodies = [body for path, body in h.optimizer.requests if path == "/v1/optimize"]
        assert bodies and all(body["solver"]["integer_stage"] == "cpsat" for body in bodies)
        plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
        assert any("resource_alt_supplier" in o["levers"] for o in plan["options"])
        for o in plan["options"]:
            if o["id"] == "do-nothing":
                assert "solver" not in o  # the stub records no stage for an empty plan
                continue
            assert o["solver"]["integer_stage"] == "cpsat"
            alt = "resource_alt_supplier" in o["levers"]
            assert (o["solver"]["integer_method"] == "cpsat_optimal") is alt
        compare = parse_block(h.core.repo.blocks_of("scenario_compare")[-1])
        by_id = {o["id"]: o for o in plan["options"]}
        for co in compare.props.options:
            alt = "resource_alt_supplier" in by_id[co.id]["levers"]
            assert (co.integer is not None) is alt
    finally:
        await h.close()


async def test_default_spec_requests_carry_no_integer_stage(tmp_path: Path) -> None:
    h = await _plan(tmp_path, None)
    try:
        bodies = [body for path, body in h.optimizer.requests if path == "/v1/optimize"]
        assert bodies and all("integer_stage" not in body["solver"] for body in bodies)
        plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
        assert all("solver" not in o for o in plan["options"])
        compare = h.core.repo.blocks_of("scenario_compare")[-1]
        assert all("integer" not in o for o in compare["props"]["options"])
    finally:
        await h.close()
