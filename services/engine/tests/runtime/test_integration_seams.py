"""Regressions for seams found while running the M1 thin slice end to end (engine integrator).

* the first stage only appeared after the intake LLM had answered (no stage for ~6-10 s);
* ``finish_run`` was called without the claiming worker's id, so E1's lease guard was inert;
* the investigator saw every tool result in full (500-row reads → ~50k prompt tokens per turn);
* the Strategist saw bare KPI placeholder names and used "units still at risk after the plan"
  as if it were the exposure before it ("recovers 2,245 units of the 0 units at risk").
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fakes.harness import PRIYA, Harness
from fakes.scripts import chat_flow_script
from keelson_contracts.agui import Stage

from keelson_engine.runtime.execute import initial_stage
from keelson_engine.runtime.nodes.planning import KPI_MEANINGS, KPI_UNITS, kpi_catalog, option_kpis
from keelson_engine.runtime.plugins import MAX_MODEL_RESULT_CHARS, compact_for_model
from keelson_engine.runtime.translator import Translator


class _Bus:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def append(self, **kwargs: Any) -> int:
        self.events.append(kwargs["event"])
        return len(self.events)


# ─────────────────────────────── first stage at run start ───────────────────────────────


@pytest.mark.parametrize(
    ("kind", "payload", "stage"),
    [
        ("chat", {"text": "hi"}, Stage.UNDERSTANDING),
        ("action", {"type": "apply", "option_id": "opt-A", "plan_version": 1}, Stage.PREPARING_CHANGES),
        ("resume", {"interrupt_id": "approach-c-1", "decision": "confirm"}, Stage.UNDERSTANDING),
        ("resume", {"interrupt_id": "approval-cs-1", "decision": "approve"}, Stage.AWAITING_APPROVAL),
        ("signal", {"signal_id": "s"}, Stage.UNDERSTANDING),
    ],
)
def test_initial_stage_per_run_kind(kind: str, payload: dict[str, Any], stage: Stage) -> None:
    assert initial_stage(kind, payload) == stage


async def test_translator_start_emits_stage_right_after_run_started() -> None:
    bus = _Bus()
    t = Translator(case_id="c1", run_id="r1", tenant="demo-retail", bus=bus)  # type: ignore[arg-type]
    await t.start(initial_stage=Stage.UNDERSTANDING)
    assert [e["type"] for e in bus.events] == ["RUN_STARTED", "STEP_STARTED"]
    assert bus.events[1]["stepName"] == "understanding"
    assert bus.events[0]["metadata"]["sc"]["stage"] == "understanding"


async def test_chat_run_shows_understanding_before_the_intake_answers(harness: Harness) -> None:
    h = harness
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    row = h.row("chat", {"text": "Trims from Meghna are 7 days late"})
    assert await h.run(row) == "succeeded"
    types = [(e["type"], e.get("stepName")) for e in h.events(row.id)]
    assert types[0] == ("RUN_STARTED", None)
    assert types[1] == ("STEP_STARTED", "understanding")
    first_subagent = next(i for i, (typ, _) in enumerate(types) if typ == "SUBAGENT_STARTED")
    assert first_subagent > 1
    # the intake node's own events do not re-enter the stage that is already shown
    stages = [s for typ, s in types if typ == "STEP_STARTED"]
    assert stages[0] == "understanding" and stages[1] != "understanding"
    assert all(a != b for a, b in zip(stages, stages[1:], strict=False))


# ─────────────────────────────── finish_run lease guard ───────────────────────────────


async def test_run_case_passes_the_claiming_worker_to_finish_run(harness: Harness) -> None:
    h = harness
    h.script = chat_flow_script(h.core.repo, delay_days=7)
    row = h.row("chat", {"text": "Trims from Meghna are 7 days late"})
    row.lease_owner = "worker-a"
    assert await h.run(row) == "succeeded"
    assert h.core.runs.finished[-1] | {"error": None} == {
        "run_id": row.id, "status": "succeeded", "invocation_id": f"e-{row.id}", "error": None,
        "worker_id": "worker-a",
    }


async def test_runs_adapter_forwards_worker_id_and_tolerates_ignored_finish() -> None:
    from keelson_engine.runtime.deps import _RunsAdapter

    calls: list[dict[str, Any]] = []

    class _Core:
        async def finish_run(self, run_id: str, status: str, **kw: Any) -> bool:
            calls.append({"run_id": run_id, "status": status, **kw})
            return False  # already final / leased by another worker

    await _RunsAdapter(_Core()).finish_run("r1", "succeeded", invocation_id="e-r1", worker_id="w1")
    assert calls == [{"run_id": "r1", "status": "succeeded", "invocation_id": "e-r1", "error": None,
                      "worker_id": "w1"}]


# ─────────────────────────────── model-facing tool results ───────────────────────────────


def _rows(n: int) -> list[dict[str, Any]]:
    return [{"location_code": "UK1", "sku": f"PUF-W-BLK-{i:03d}", "on_hand": 1000 + i, "atp": 900 + i}
            for i in range(n)]


def test_small_results_are_unchanged() -> None:
    payload = {"items": _rows(3), "next_cursor": None}
    assert compact_for_model(payload) == payload


def test_large_results_keep_a_pointer_stable_prefix_and_a_summary() -> None:
    payload = {"items": _rows(500), "next_cursor": "abc"}
    out = compact_for_model(payload)
    assert len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) <= MAX_MODEL_RESULT_CHARS
    shown = out["_summary"]["items"]["shown"]
    assert 0 < shown < 500 and out["_summary"]["items"]["total"] == 500
    assert out["items"] == payload["items"][:shown]  # /items/i still points at the stored evidence row
    assert out["next_cursor"] == "abc" and "narrower" in out["_note"]
    assert len(payload["items"]) == 500  # the evidence copy is untouched


def test_several_lists_are_cut_largest_first() -> None:
    payload = {"components": [{"code": "CMP-WAD-01"}], "open_po_lines": _rows(400), "styles": _rows(40)}
    out = compact_for_model(payload, max_chars=4000)
    assert out["components"] == payload["components"]
    assert "open_po_lines" in out["_summary"]
    assert len(json.dumps(out, ensure_ascii=False, separators=(",", ":"))) <= 4000


async def test_investigator_sees_compacted_results_while_evidence_keeps_everything(harness: Harness) -> None:
    from keelson_engine.runtime.plugins import EvidencePlugin
    from keelson_engine.runtime.toolsets import KEELSON_META

    class _Tool:
        name = "get_inventory"
        custom_metadata = {KEELSON_META: {"connector": "merxa"}}

    class _Ctx:
        agent_name = "inventory_demand_investigator"

    from keelson_contracts.runs import Actor

    from keelson_engine.runtime.context import RunContext, bind_run
    from keelson_engine.runtime.spec import AgentSpec

    ctx = RunContext(
        tenant="demo-retail", case_id=harness.case_id, run_id="run-x", kind="chat",
        actor=Actor.model_validate(PRIYA), payload={}, spec=AgentSpec(tenant="demo-retail"),
        deps=harness.core.deps(), invocation_id="e-run-x",
    )
    with bind_run(ctx):
        result = {"items": _rows(500), "next_cursor": None}
        out = await EvidencePlugin().after_tool_callback(
            tool=_Tool(), tool_args={"location_type": "dc"}, tool_context=_Ctx(), result=result  # type: ignore[arg-type]
        )
    assert out is not None and out["_evidence_id"].startswith("ev_")
    assert out["_summary"]["items"]["total"] == 500
    stored = await harness.core.repo.get_evidence(tenant="demo-retail", ids=[out["_evidence_id"]])
    assert len(stored[0]["result"]["items"]) == 500


# ─────────────────────────────── Strategist KPI catalogue ───────────────────────────────


def _option() -> dict[str, Any]:
    return {
        "id": "opt-A",
        "kpis_before": {"units_at_risk": 2245, "revenue_at_risk_cents": 7300600, "margin_at_risk_cents": 4910850},
        "kpis_after": {"units_at_risk": 0, "revenue_at_risk_cents": 0, "margin_at_risk_cents": 0,
                       "shortage_cluster_weeks": 0},
        "kpi_delta": {"units_recovered": 2245, "margin_recovered_cents": 4910850, "transfer_cost_cents": 110830,
                      "net_benefit_cents": 4800020, "transfers": 3},
        "actions": [{"payload": {"lines": [{"sku": "x", "qty": 2245}]}}],
    }


def test_option_kpis_include_the_exposure_before_the_option() -> None:
    kpis = option_kpis(_option())
    assert kpis["exposure_units_at_risk"] == 2245 and kpis["units_at_risk"] == 0
    assert kpis["exposure_revenue_at_risk_cents"] == 7300600
    assert all(key in KPI_UNITS for key in kpis)


def test_kpi_catalog_names_meanings_never_values() -> None:
    catalog = kpi_catalog(_option())
    by_ph = {c["placeholder"]: c["meaning"] for c in catalog}
    assert "AFTER" in by_ph["{{kpi:opt-A.units_at_risk}}"]
    assert "NOTHING" in by_ph["{{kpi:opt-A.exposure_units_at_risk}}"]
    assert set(KPI_MEANINGS) >= {p.split(".")[1].rstrip("}") for p in by_ph}
    text = json.dumps(catalog)
    for value in ("2245", "7300600", "4800020", "110830"):
        assert value not in text
