"""Token diet (2026-10-07, offline, FakeLlm only): compacted tool results within a ~3k-token
budget with citable list summaries, smaller planner / analyst / critic / Strategist / structurer
briefs, the investigator turn cap, the critic REVISE gate (``high`` severity only), the Strategist
repair gate (core texts only) and per-run usage metering (``metadata.usage`` on the final event)."""

from __future__ import annotations

import json
import types as pytypes
from pathlib import Path
from typing import Any

import pytest
from fakes.core import FakeCore
from fakes.harness import PRIYA, Harness
from fakes.scripts import (
    critic_turn,
    intake_disruption,
    investigator_turns,
    m4_review_turns,
    planner_turn,
    recommendation,
    structurer_turn,
    user_text,
)
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from keelson_contracts.llm import Claim, Critique, Findings, InvestigationPlan
from keelson_contracts.runs import Actor
from keelson_contracts.scenario import DisruptionScenario

from keelson_engine.runtime.compose import UNVERIFIED_NOTE
from keelson_engine.runtime.context import RunContext, TaskBinding, bind_run, bind_task
from keelson_engine.runtime.jsonptr import JsonPointerError, resolve_pointer
from keelson_engine.runtime.llm import FakeTurn
from keelson_engine.runtime.plugins import (
    FINAL_TURN_NOTE,
    LIST_TOP_N,
    MAX_MODEL_RESULT_CHARS,
    MODEL_RESULT_TOKEN_BUDGET,
    approx_tokens,
    compact_for_model,
)
from keelson_engine.runtime.spec import AgentSpec, InvestigationDefaults
from keelson_engine.runtime.usage import UsageMeter, current_agent, record_cost, response_cost

DECIDER = {"sub": PRIYA["sub"], "username": PRIYA["username"], "name": PRIYA["name"]}
TEXT = "Trims from Meghna are 7 days late"


def _size(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str))


def _run(spec: AgentSpec | None = None) -> RunContext:
    return RunContext(tenant="demo-retail", case_id="c1", run_id="r1", kind="chat",
                      actor=Actor(sub="s", username="u", name="n", roles=[]), payload={"text": TEXT},
                      spec=spec or AgentSpec(tenant="demo-retail"), deps=FakeCore().deps(), invocation_id="i1")


# ───────────────────────────── realistic Merxa payloads ─────────────────────────────


def _bom_where_used(lines: int = 540) -> dict[str, Any]:
    """Shaped like the live ``get_bom_where_used`` read (100 kB of JSON for the trim case)."""
    pos = [f"PO-BD-000{i}" for i in range(1, 7)]
    sizes = ["XS", "S", "M", "L", "XL"]
    return {
        "components": [{"code": "CMP-WAD-01", "name": "Polyester wadding 120 gsm"}],
        "factories": [{"code": "F-BD-01"}, {"code": "F-BD-02"}],
        "styles": [{"style_code": f"STY-{i:02d}", "qty_per_unit": 1} for i in range(15)],
        "open_po_lines": [
            {"qty": 30 + i % 17, "sku": f"PUF-W-BLK-{sizes[i % 5]}", "eta_dc": f"2026-10-{20 + i % 10:02d}",
             "status": "in_production" if i % 3 else "confirmed", "dest_dc": ("UK1", "DE1", "NL1")[i % 3],
             "line_no": i % 90 + 1, "po_code": pos[i // 90], "version": 1,
             "factory_code": "F-BD-01" if i % 2 else "F-BD-02"}
            for i in range(lines)
        ],
    }


def _old_compaction_size(payload: dict[str, Any]) -> int:
    """The M4 compaction kept the longest prefix that fits 6,000 characters (no summaries)."""
    return min(_size(payload), 6000)


def test_list_results_are_summarised_within_the_token_budget() -> None:
    payload = _bom_where_used()
    out = compact_for_model(payload)
    lines = payload["open_po_lines"]
    # top-N rows, pointer-stable, plus a summary over ALL rows
    assert out["open_po_lines"] == lines[:LIST_TOP_N] and out["styles"] == payload["styles"][:LIST_TOP_N]
    assert out["components"] == payload["components"] and out["factories"] == payload["factories"]
    summary = out["_summary"]["open_po_lines"]
    assert summary["total"] == 540 and summary["shown"] == LIST_TOP_N
    assert summary["sums"] == {"qty": sum(r["qty"] for r in lines)}  # line_no / version are not summed
    assert summary["counts"]["po_code"] == {f"PO-BD-000{i}": 90 for i in range(1, 7)}
    assert summary["counts"]["dest_dc"] == {"UK1": 180, "DE1": 180, "NL1": 180}
    assert "sku" in summary["counts"]  # five sizes: a short text column
    assert summary["range"]["eta_dc"] == {"min": "2026-10-20", "max": "2026-10-29"}
    assert out["_summary"]["styles"] == {"total": 15, "shown": LIST_TOP_N, "sums": {"qty_per_unit": 15}}
    assert "narrower" in out["_note"]
    # budget: ~3k tokens per result, and well under the old 6,000-character prefix
    assert _size(out) <= MAX_MODEL_RESULT_CHARS and approx_tokens(out) <= MODEL_RESULT_TOKEN_BUDGET
    assert _size(out) <= 0.6 * _old_compaction_size(payload), _size(out)
    assert _size(payload) > 30 * _size(out)
    assert len(lines) == 540  # the evidence copy is untouched


def test_every_summary_value_resolves_against_the_stored_evidence() -> None:
    """The model cites ``/_summary/...`` pointers; the stored row has no ``_summary`` key, so the
    pointer resolves virtually over the full list (the numbers rule stays enforceable)."""
    stored = _bom_where_used()
    shown = compact_for_model(stored)["_summary"]["open_po_lines"]
    assert resolve_pointer(stored, "/_summary/open_po_lines/total") == shown["total"]
    assert resolve_pointer(stored, "/_summary/open_po_lines/sums/qty") == shown["sums"]["qty"]
    assert resolve_pointer(stored, "/_summary/open_po_lines/counts/dest_dc/UK1") == 180
    assert resolve_pointer(stored, "/_summary/open_po_lines/range/eta_dc/max") == "2026-10-29"
    assert resolve_pointer(stored, "/summary/open_po_lines/total") == 540  # model spelling
    assert resolve_pointer(stored, "/_truncated/open_po_lines/total") == 540  # M4 key still resolves
    for bad in ("/_summary/open_po_lines/sums/line_no", "/_summary/open_po_lines/counts/dest_dc/FR1",
                "/_summary/open_po_lines/shown", "/_summary/components/sums/code", "/_summary/nope/total"):
        with pytest.raises(JsonPointerError):
            resolve_pointer(stored, bad)


def test_a_claim_citing_a_summary_total_passes_the_number_check() -> None:
    from keelson_engine.runtime.nodes.review import verify_findings

    rows = {"ev_bom": {"id": "ev_bom", "tool": "get_bom_where_used", "prov": "fact", "args": {},
                       "result": _bom_where_used()}}
    ptr = "/_summary/open_po_lines/sums/qty"
    findings = Findings(task_id="t2", status="ok", affected=[], gaps=[], scenario_updates=[], claims=[
        Claim(text=f"The delayed lines hold {{{{ev:ev_bom#{ptr}}}}}.", kind="fact", evidence_id="ev_bom",
              json_pointer=ptr, calc_id=None, confidence="high"),
    ])
    [out], report = verify_findings([findings], rows, _run())
    assert report["numeric"] == 1 and report["failed"] == 0 and out.claims[0].kind == "fact"


def test_nested_lists_keep_a_pointer_stable_prefix() -> None:
    schedule = {"factory_code": "F-BD-01", "from": "2026-10-01", "to": "2026-11-30", "lines": [
        {"code": f"FL-BD01-{c}", "orders": [{"seq": i, "code": f"MO-{i:06d}", "status": "running",
                                             "po_line_ref": f"PO-BD-0003#{i}"} for i in range(40)]}
        for c in "AB"]}
    out = compact_for_model(schedule)
    assert [len(line["orders"]) for line in out["lines"]] == [3, 3]
    assert out["lines"][1]["orders"][2] == resolve_pointer(schedule, "/lines/1/orders/2")
    assert "_note" in out and "lines" not in out["_summary"]  # nothing of the top-level list was cut
    assert _size(out) < _size(schedule) / 5


def test_a_cut_nested_list_shows_its_row_count_and_the_count_is_citable() -> None:
    """Review fix: a factory line with 40 orders showed 3 and nothing said 37 were hidden (an
    analyst could take the 3 for the whole sequence). Each cut nested list now has a ``_summary``
    entry under its path, and that total resolves against the stored evidence."""
    schedule = {"factory_code": "F-BD-01", "lines": [
        {"code": f"FL-BD01-{c}", "orders": [{"seq": i, "code": f"MO-{i:06d}", "qty": 10 + i, "status": "running"}
                                           for i in range(n)]}
        for c, n in (("A", 40), ("B", 2), ("C", 7))]}
    out = compact_for_model(schedule)
    assert out["_summary"]["lines/0/orders"] == {"total": 40, "shown": 3}
    assert out["_summary"]["lines/2/orders"] == {"total": 7, "shown": 3}
    assert "lines/1/orders" not in out["_summary"]  # two orders: nothing hidden
    assert resolve_pointer(schedule, "/_summary/lines/0/orders/total") == 40
    assert resolve_pointer(schedule, "/_summary/lines/2/orders/sums/qty") == sum(10 + i for i in range(7))
    assert resolve_pointer(schedule, "/_summary/lines~10~1orders/total") == 40  # escaped path key
    with pytest.raises(JsonPointerError):
        resolve_pointer(schedule, "/_summary/lines/9/orders/total")
    # a list inside a top-level object (one PO with its lines) gets the full summary
    po = {"po": {"po_code": "PO-BD-0001", "lines": [{"line_no": i, "qty": 5, "status": "open"} for i in range(25)]}}
    compact = compact_for_model(po)
    assert len(compact["po"]["lines"]) == LIST_TOP_N
    entry = compact["_summary"]["po/lines"]
    assert entry["total"] == 25 and entry["sums"] == {"qty": 125}
    assert resolve_pointer(po, "/_summary/po/lines/sums/qty") == 125
    from keelson_engine.runtime.nodes.review import verify_findings

    rows = {"ev_s": {"id": "ev_s", "tool": "get_factory_schedule", "prov": "fact", "args": {}, "result": schedule}}
    ptr = "/_summary/lines/0/orders/total"
    findings = Findings(task_id="t1", status="ok", affected=[], gaps=[], scenario_updates=[], claims=[
        Claim(text=f"Line A runs {{{{ev:ev_s#{ptr}}}}} orders.", kind="fact", evidence_id="ev_s", json_pointer=ptr,
              calc_id=None, confidence="high")])
    [verified], report = verify_findings([findings], rows, _run())
    assert report["failed"] == 0 and verified.claims[0].kind == "fact"


def test_nested_counts_stay_within_the_budget_when_rows_are_cut() -> None:
    wide = {"items": [{"sku": f"S-{i}", "note": "x" * 400, "lots": [{"lot": j} for j in range(9)]} for i in range(60)]}
    out = compact_for_model(wide)
    assert _size(out) <= MAX_MODEL_RESULT_CHARS
    shown = out["_summary"]["items"]["shown"]
    assert out["items"] == [{**r, "lots": r["lots"][:3]} for r in wide["items"][:shown]]
    nested = {k for k in out["_summary"] if k.startswith("items/")}
    assert nested == {f"items/{i}/lots" for i in range(shown)}  # counts only for rows still shown


def test_small_results_stay_unchanged_and_a_huge_row_list_still_fits() -> None:
    small = {"items": [{"sku": "A", "atp": 3}], "next_cursor": None}
    assert compact_for_model(small) == small
    wide = {"items": [{"sku": f"S-{i}", "note": "x" * 900} for i in range(50)]}
    out = compact_for_model(wide)
    assert _size(out) <= MAX_MODEL_RESULT_CHARS and out["_summary"]["items"]["total"] == 50
    assert out["items"] == wide["items"][: out["_summary"]["items"]["shown"]]


async def test_investigator_sees_the_summary_and_the_structurer_a_compact_index(harness: Harness) -> None:
    from keelson_engine.runtime.nodes.prompts import evidence_index_rows
    from keelson_engine.runtime.plugins import EvidencePlugin
    from keelson_engine.runtime.toolsets import KEELSON_META

    class _Tool:
        name = "get_bom_where_used"
        custom_metadata = {KEELSON_META: {"connector": "merxa"}}

    class _Ctx:
        agent_name = "sourcing_investigator"

    ctx = RunContext(
        tenant="demo-retail", case_id=harness.case_id, run_id="run-x", kind="chat",
        actor=Actor.model_validate(PRIYA), payload={}, spec=AgentSpec(tenant="demo-retail"),
        deps=harness.core.deps(), invocation_id="e-run-x",
    )
    args = {"component_code": "CMP-WAD-01", "po_codes": [f"PO-BD-000{i}" for i in range(1, 7)],
            "note": "n" * 200, "cursor": None}
    with bind_run(ctx), bind_task(TaskBinding(task_id="t2", specialist="sourcing", tools=())):
        out = await EvidencePlugin().after_tool_callback(
            tool=_Tool(), tool_args=args, tool_context=_Ctx(), result=_bom_where_used()  # type: ignore[arg-type]
        )
        index = evidence_index_rows(ctx, ["sourcing_investigator"])
    assert out is not None and out["_summary"]["open_po_lines"]["total"] == 540
    stored = await harness.core.repo.get_evidence(tenant="demo-retail", ids=[out["_evidence_id"]])
    assert len(stored[0]["result"]["open_po_lines"]) == 540 and "_summary" not in stored[0]["result"]
    [row] = index
    assert row["id"] == out["_evidence_id"] and row["tool"] == "get_bom_where_used" and "agent" not in row
    assert row["rows"] == {"components": 1, "factories": 2, "styles": 15, "open_po_lines": 540}
    assert row["args"]["po_codes"] == ["PO-BD-0001", "PO-BD-0002", "PO-BD-0003", "PO-BD-0004", "+2 more"]
    assert len(row["args"]["note"]) <= 60 and "cursor" not in row["args"]
    assert _size(index) < 450


# ───────────────────────────────────── briefs ─────────────────────────────────────


def _trim_scenario(lines: int = 540) -> DisruptionScenario:
    ids = [f"PO-BD-000{p}#{n}" for p in range(1, 7) for n in range(1, lines // 6 + 1)]
    matched = [{"code": "TRIM-BD-01", "type": "supplier", "label": "Meghna Trims Ltd"},
               {"code": "CMP-WAD-01", "type": "component", "label": "Polyester wadding 120 gsm"},
               {"code": "BD", "type": "region", "label": "Bangladesh"}] + [
        {"code": f"PO-BD-000{p}", "type": "po", "label": f"Purchase order PO-BD-000{p}"} for p in range(1, 7)]
    return DisruptionScenario.model_validate({
        "id": "scn-1", "tenant": "demo-retail", "mode": "actual", "event_family": "SUP", "event_type": "SUP.delay",
        "description": "Trim supplier Meghna Trims reports a delay for wadding and zips affecting Bangladesh POs.",
        "perturbations": [{"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": ids}, "days": 7,
                           "prov": "decision"}],
        "matched": matched,
        "assumptions": [
            {"key": "affected_receipts", "label": "Affected purchase-order lines", "prov": "fact",
             "value_text": f"{lines} open lines on PO-BD-0001, PO-BD-0002, PO-BD-0003, PO-BD-0004, PO-BD-0005, PO-BD-0006"},
            {"key": "delay_days", "label": "Delay", "value_text": "7 days", "prov": "decision"},
        ],
        "config_version": "default/p-1",
    })


def _old_scenario_brief(scenario: DisruptionScenario, task_id: str, version: int) -> dict[str, Any]:
    """The M4 brief: every matched entity and every perturbation target id, verbatim."""
    return {
        "task_id": task_id, "scenario_version": version, "mode": scenario.mode,
        "event": {"family": scenario.event_family, "type": scenario.event_type},
        "description": scenario.description,
        "matched": [m.model_dump(mode="json") for m in scenario.matched],
        "perturbations": [p.model_dump(mode="json", exclude_none=True) for p in scenario.perturbations],
        "assumptions": [{"label": a.label, "value": a.value_text, "prov": a.prov.value} for a in scenario.assumptions],
    }


def test_scenario_brief_summarises_large_targets() -> None:
    from keelson_engine.runtime.nodes.investigate import scenario_brief

    scenario = _trim_scenario()
    brief = scenario_brief(scenario, task_id="t1", scenario_version=1)
    [p] = brief["perturbations"]
    assert p["target"] == {"kinds": ["receipt"], "id_count": 540,
                           "ids_by_code": {f"PO-BD-000{i}": 90 for i in range(1, 7)}}
    assert p["days"] == 7 and p["prov"] == "decision"
    assert "supplier:TRIM-BD-01 Meghna Trims Ltd" in brief["matched"] and "scenario_version" not in brief
    old = _size(_old_scenario_brief(scenario, "t1", 1))
    assert old > 9_000 and _size(brief) < 1_500 and _size(brief) < old / 6
    # few ids stay verbatim
    small = scenario_brief(_trim_scenario(lines=6), task_id="t1", scenario_version=1)
    assert small["perturbations"][0]["target"]["ids"] == [f"PO-BD-000{p}#1" for p in range(1, 7)]


def test_scenario_brief_keeps_every_id_of_an_ordinary_target() -> None:
    """Review fix: a count cap of 12 hid most of a 30-SKU target from the analysts (``ids_sample``
    of 12). Targets are now summarised by size, so an ordinary target keeps every affected id."""
    from keelson_engine.runtime.nodes.investigate import MAX_BRIEF_TARGET_CHARS, scenario_brief

    base = _trim_scenario().model_dump(mode="json")
    skus = [f"PUF-W-BLK-{n:03d}" for n in range(30)]
    base["perturbations"][0]["target"] = {"kinds": ["sku"], "ids": skus}
    brief = scenario_brief(DisruptionScenario.model_validate(base), task_id="t1", scenario_version=1)
    assert brief["perturbations"][0]["target"] == {"kinds": ["sku"], "ids": skus}
    # a really long target of distinct codes: as many ids as fit, plus the count
    many = [f"PUF-W-BLK-{n:04d}" for n in range(400)]
    base["perturbations"][0]["target"] = {"kinds": ["sku"], "ids": many}
    target = scenario_brief(DisruptionScenario.model_validate(base), task_id="t1", scenario_version=1)[
        "perturbations"][0]["target"]
    assert target["id_count"] == 400 and target["ids_sample"] == many[: len(target["ids_sample"])]
    assert 12 < len(target["ids_sample"]) < 400 and _size(target["ids_sample"]) <= MAX_BRIEF_TARGET_CHARS
    # matched entities: listed while they fit, the rest counted
    base["matched"] = [{"code": f"S-{n:03d}", "type": "sku", "label": f"Sku {n}"} for n in range(40)]
    brief = scenario_brief(DisruptionScenario.model_validate(base), task_id="t1", scenario_version=1)
    assert len(brief["matched"]) == 40 and "matched_more" not in brief
    base["matched"] = [{"code": f"S-{n:03d}", "type": "sku", "label": "x" * 80} for n in range(60)]
    brief = scenario_brief(DisruptionScenario.model_validate(base), task_id="t1", scenario_version=1)
    assert len(brief["matched"]) + brief["matched_more"] == 60 and len(brief["matched"]) >= 24


def test_planner_brief_is_compact_and_keeps_the_user_request() -> None:
    from keelson_engine.runtime.nodes.investigate import _planner_brief

    spec = AgentSpec(tenant="demo-retail")
    scenario = _trim_scenario()
    text = _planner_brief(spec, scenario, 1, "Check our playbooks and the latest carrier rate updates")
    brief = json.loads(text)
    assert brief["user_request"] == "Check our playbooks and the latest carrier rate updates"
    assert "limits" not in brief and {s["name"] for s in brief["specialists"]} == {s.slug for s in spec.specialists}
    old = json.dumps({"scenario": {k: v for k, v in _old_scenario_brief(scenario, "plan", 1).items() if k != "task_id"},
                      "specialists": spec.planner_catalogue(),
                      "limits": {"max_tasks": 6, "max_parallel": 4},
                      "user_request": "Check our playbooks and the latest carrier rate updates"},
                     ensure_ascii=False, sort_keys=True)
    assert len(text) < len(old) / 3, (len(text), len(old))


def test_critic_brief_drops_plan_rationale_budget_and_tools() -> None:
    from keelson_engine.runtime.nodes.review import _critic_brief

    plan = InvestigationPlan.model_validate(planner_turn(
        ("t1", "inventory_demand", ["get_inventory", "get_demand_forecast"]),
        ("t2", "sourcing", ["get_bom_where_used"]), ("t3", "playbook", [], ["t1"])).json)
    findings = [Findings(task_id=t, status="ok", affected=[], gaps=[], scenario_updates=[], claims=[
        Claim(text=f"Claim {i} of {t}.", kind="inference", evidence_id=None, json_pointer=None, calc_id=None,
              confidence="medium") for i in range(30)]) for t in ("t1", "t2", "t3")]
    text = _critic_brief(plan, findings, {"numeric": 4, "failed": 0}, _run())
    brief = json.loads(text)
    assert brief["plan"] == {"tasks": [
        {"id": "t1", "specialist": "inventory_demand", "objective": "Check the inventory demand exposure"},
        {"id": "t2", "specialist": "sourcing", "objective": "Check the sourcing exposure"},
        {"id": "t3", "specialist": "playbook", "objective": "Check the playbook exposure", "depends_on": ["t1"]}]}
    assert all(len(f["claims"]) == 12 and "gaps" not in f and "affected" not in f for f in brief["findings"])
    assert "rationale" not in text and "get_inventory" not in text and "planning_date" not in brief
    old = json.dumps({"planning_date": None, "plan": plan.model_dump(mode="json"), "findings": [
        {"task_id": f.task_id, "status": f.status,
         "claims": [{"text": c.text, "kind": c.kind, "cited": c.is_cited, "confidence": c.confidence}
                    for c in f.claims][:20], "affected": [], "gaps": [], "scenario_updates": []} for f in findings],
        "number_check": {"numeric": 4, "failed": 0, "downgraded": None, "dropped_updates": None}}, sort_keys=True)
    assert len(text) < 0.6 * len(old), (len(text), len(old))


def _options() -> list[dict[str, Any]]:
    move = {"op": "transfer_order.create", "payload": {"lines": [{"sku": "S", "qty": 3}]}}
    split = {"op": "po_line.split", "target_ref": "PO-1#1", "payload": {"qty": 5}}
    out = []
    for oid, levers, actions in (("opt-A", ["reallocate_inventory"], [move]),
                                 ("opt-B", ["reallocate_inventory", "expedite_mode_shift"], [move, split]),
                                 ("opt-C", ["resource_alt_supplier", "resequence_production"], []),
                                 ("opt-D", ["reallocate_inventory", "expedite_mode_shift", "resource_alt_supplier"],
                                  [move, split]),
                                 ("do-nothing", [], [])):
        out.append({"id": oid, "title": oid, "levers": levers, "actions": actions, "fallback": not actions,
                    "kpi_delta": {"net_benefit_cents": 100, "units_recovered": 10},
                    "kpis_after": {"units_at_risk": 5, "revenue_at_risk_cents": 9, "margin_at_risk_cents": 4},
                    "kpis_before": {"units_at_risk": 9, "revenue_at_risk_cents": 19, "margin_at_risk_cents": 8},
                    "scorecard": {"incremental_cost_cents": 7, "fill_rate_bp": 9000, "shortage_days": 3,
                                  "first_breach_shift_days": 2, "changes": len(actions)}})
    return out


def test_strategist_brief_states_each_kpi_meaning_once_and_keeps_the_user_request() -> None:
    from keelson_engine.runtime.nodes.planning import KPI_MEANINGS, _recommender_brief, kpi_catalog

    options = _options()
    findings = [{"status": "ok", "claims": [{"text": "UK1 runs short in the peak weeks."}] * 3},
                {"status": "failed", "claims": [{"text": "ignored"}]}]

    class _Scn:
        event_type, description, mode = "SUP.delay", "trim delay", "actual"

    text = _recommender_brief(_Scn(), options, findings, request=TEXT)
    brief = json.loads(text)
    assert brief["user_request"] == TEXT and brief["findings"] == ["UK1 runs short in the peak weeks."]
    by_id = {o["id"]: o for o in brief["options"]}
    assert "{{kpi:opt-B.units_recovered}}" in by_id["opt-B"]["kpi_placeholders"]
    meaning = KPI_MEANINGS["units_recovered"]
    assert brief["kpi_meanings"]["units_recovered"] == meaning and text.count(meaning) == 1
    old = json.dumps({"options": [{"id": o["id"], "kpi_placeholders": kpi_catalog(o)} for o in options]},
                     ensure_ascii=False, sort_keys=True)
    new = json.dumps({"options": [{"id": o["id"], "kpi_placeholders": by_id[o["id"]]["kpi_placeholders"]}
                                  for o in options], "kpi_meanings": brief["kpi_meanings"]}, ensure_ascii=False)
    assert len(new) < 0.5 * len(old), (len(new), len(old))


def test_investigator_and_strategist_instructions_carry_no_message_history() -> None:
    from keelson_engine.runtime.nodes.prompts import investigator_instruction, recommender_instruction

    run = _run()
    run.case_title = "Trim delay"
    run.history = [{"role": "user", "text": "earlier message " * 30}, {"role": "assistant", "text": "long narrative " * 40}]
    with bind_run(run):
        inv = investigator_instruction("inventory_demand", "Inventory analyst")(None)  # type: ignore[arg-type]
        rec = recommender_instruction(None)  # type: ignore[arg-type]
    for text in (inv, rec):
        assert "Trim delay" in text and "earlier message" not in text and "long narrative" not in text
    assert "at most 5 turns" in inv and "_summary" in inv


# ───────────────────────────────── full runs (FakeLlm) ─────────────────────────────────


async def _run_to_end(h: Harness, row: Any) -> str:
    status = await h.run(row)
    if status == "paused":
        status = await h.run(h.resume_row(row, "confirm", [DECIDER]))
    return status


def _all_events(h: Harness) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for run_id in dict.fromkeys(e["run_id"] for e in h.core.bus.events if e["run_id"]):
        out += h.events(run_id)
    return out


async def test_critic_revise_without_a_high_issue_does_not_rerun_specialists(harness: Harness) -> None:
    h = harness
    h.script.add("intake", intake_disruption(delay_days=7))
    h.script.add("planner", planner_turn(("t1", "inventory_demand")))
    h.script.add("inventory_demand_investigator", *investigator_turns())
    h.script.add("inventory_demand_structurer", structurer_turn(h.core.repo))
    h.script.add("critic", critic_turn("REVISE", ["t1"], [
        {"task_id": "t1", "issue": "Could also check store stock.", "severity": "medium"},
        {"task_id": None, "issue": "Minor wording.", "severity": "low"}]))
    h.script.add("recommend", recommendation())
    assert await _run_to_end(h, h.row("chat", {"text": TEXT})) == "succeeded"
    assert [f["task_id"] for f in h.core.repo.findings] == [f"{h.case_id}:t1:r1"]  # no second round
    assert len(h.script.requests["inventory_demand_investigator"]) == 2
    [critique] = h.core.repo.evidence_by_tool("critic")
    assert critique["result"]["verdict"] == "REVISE"  # the original critique is kept for audit
    state = h.core.repo.evidence_by_tool("investigation_findings")[-1]["result"]
    assert state["task_revs"] == {"t1": 1}
    assert not any("second look" in str(e.get("value") or e.get("delta") or "") for e in _all_events(h))


def test_revise_is_warranted_only_for_high_issues_on_listed_tasks() -> None:
    from keelson_engine.runtime.nodes.review import revise_is_warranted

    def critique(*issues: tuple[str | None, str], revise: tuple[str, ...] = ("t1",)) -> Critique:
        return Critique(verdict="REVISE", revise_task_ids=list(revise),
                        issues=[{"task_id": t, "issue": "x", "severity": s} for t, s in issues])  # type: ignore[misc]

    assert revise_is_warranted(critique(("t1", "high")))
    assert revise_is_warranted(critique((None, "high")))  # about the whole investigation
    assert not revise_is_warranted(critique(("t1", "medium"), (None, "low")))
    assert not revise_is_warranted(critique(("t2", "high")))  # a task the critic did not ask to re-run
    assert not revise_is_warranted(critique())


async def test_investigator_turn_cap_switches_tools_off_on_the_last_turn(tmp_path: Path) -> None:
    h = await Harness(tmp=tmp_path, spec_overrides={
        "investigation": InvestigationDefaults(max_investigator_turns=3)}).setup()
    try:
        h.script.add("intake", intake_disruption(delay_days=7))
        h.script.add("planner", planner_turn(("t1", "inventory_demand")))
        seen: dict[str, Any] = {}

        def last_turn(request: LlmRequest) -> FakeTurn:
            seen["mode"] = request.config.tool_config.function_calling_config.mode  # type: ignore[union-attr]
            seen["note"] = FINAL_TURN_NOTE in str(request.config.system_instruction)
            return investigator_turns()[1](request)

        h.script.add("inventory_demand_investigator",
                     FakeTurn(function_calls=[("get_inventory", {"location_type": "dc"})]),
                     FakeTurn(function_calls=[("get_demand_forecast", {"weeks": ["2026-W45"]})]),
                     last_turn,
                     FakeTurn(function_calls=[("get_inventory", {})]))  # never reached
        h.script.add("inventory_demand_structurer", structurer_turn(h.core.repo))
        h.script.add("critic", critic_turn())
        h.script.add("recommend", recommendation())
        assert await _run_to_end(h, h.row("chat", {"text": TEXT})) == "succeeded"
        assert seen == {"mode": types.FunctionCallingConfigMode.NONE, "note": True}
        requests = h.script.requests["inventory_demand_investigator"]
        assert len(requests) == 3 and h.script.remaining("inventory_demand_investigator") == 1
        first = requests[0].config.tool_config
        assert first is None or first.function_calling_config is None or (
            first.function_calling_config.mode != types.FunctionCallingConfigMode.NONE)
    finally:
        await h.close()


async def test_investigator_turn_cap_safety_net_ends_the_task(tmp_path: Path) -> None:
    """A model that still asks for a tool on its tool-free last turn gets no further model call:
    the task ends with a fixed note and the structurer still runs."""
    h = await Harness(tmp=tmp_path, spec_overrides={
        "investigation": InvestigationDefaults(max_investigator_turns=2)}).setup()
    try:
        h.script.add("intake", intake_disruption(delay_days=7))
        h.script.add("planner", planner_turn(("t1", "inventory_demand")))
        h.script.add("inventory_demand_investigator",
                     FakeTurn(function_calls=[("get_inventory", {"location_type": "dc"})]),
                     FakeTurn(function_calls=[("get_demand_forecast", {"weeks": ["2026-W45"]})]))

        def structurer(request: LlmRequest) -> FakeTurn:
            assert "turn budget" in user_text(request)
            return structurer_turn(h.core.repo)(request)

        h.script.add("inventory_demand_structurer", structurer)
        h.script.add("critic", critic_turn())
        h.script.add("recommend", recommendation())
        assert await _run_to_end(h, h.row("chat", {"text": TEXT})) == "succeeded"
        assert len(h.script.requests["inventory_demand_investigator"]) == 2
        assert [f["status"] for f in h.core.repo.findings] == ["done"]
    finally:
        await h.close()


async def _chat_with(h: Harness, rec: FakeTurn, repair: FakeTurn | None = None) -> str:
    h.script.add("intake", intake_disruption(delay_days=7))
    m4_review_turns(h.script)
    h.script.add("inventory_demand_investigator", *investigator_turns())
    h.script.add("inventory_demand_structurer", structurer_turn(h.core.repo))
    h.script.add("recommend", rec)
    if repair is not None:
        h.script.add("recommender_repair", repair)
    row = h.row("chat", {"text": "Meghna trims 7 days late"})
    assert await h.run(row) == "succeeded"
    [text] = [e["delta"] for e in h.events(row.id) if e["type"] == "TEXT_MESSAGE_CONTENT"]
    return text


def test_repair_needed_only_for_core_texts() -> None:
    from keelson_engine.runtime.nodes.planning import repair_needed

    assert repair_needed({"headline": ["free number '7'"]})
    assert repair_needed({"bullet1": ["unknown placeholder {{kpi:x}}"]})
    assert not repair_needed({"risk0": ["free number '2'"], "why1": ["free number '3'"], "follow0": ["x"]})
    assert not repair_needed({})


async def test_a_free_number_in_a_risk_is_stripped_without_a_repair_turn(harness: Harness) -> None:
    turn = recommendation()
    data = dict(turn.json)  # type: ignore[arg-type]
    data["risks"] = ["Transit could slip by 3 days if the lane is congested.", "Watch the lane congestion."]
    text = await _chat_with(harness, FakeTurn(json=data))
    assert "recommender_repair" not in harness.script.requests  # no ~20-30 s repair turn
    assert "slip by 3 days" not in text and "Watch the lane congestion." in text and UNVERIFIED_NOTE in text
    assert "Rebalance stock from DE1 to NL1" in text


async def test_a_free_number_in_a_bullet_still_gets_one_repair_turn(harness: Harness) -> None:
    text = await _chat_with(harness, recommendation(free_number=True), recommendation())
    assert len(harness.script.requests["recommender_repair"]) == 1
    repair = json.loads(user_text(harness.script.requests["recommender_repair"][0]))
    assert "kpi_meanings" in repair and all(isinstance(p, str) for o in repair["options"] for p in o["kpi_placeholders"])
    assert UNVERIFIED_NOTE not in text


# ───────────────────────────────────── metering ─────────────────────────────────────


async def test_final_events_carry_per_agent_usage(harness: Harness) -> None:
    h = harness
    h.script.add("intake", intake_disruption(delay_days=7))
    h.script.add("planner", planner_turn(("t1", "inventory_demand"), ("t3", "logistics")))
    h.script.add("inventory_demand_investigator", *investigator_turns())
    h.script.add("inventory_demand_structurer", structurer_turn(h.core.repo))
    h.script.add("logistics_investigator", FakeTurn(text="No sailings affected."))
    h.script.add("logistics_structurer", FakeTurn(json={
        "task_id": "t3", "status": "ok", "claims": [], "affected": [], "gaps": [], "scenario_updates": []}))
    h.script.add("critic", critic_turn())
    h.script.add("recommend", recommendation())
    row = h.row("chat", {"text": TEXT})
    status = await h.run(row)
    finals = [e for e in h.events(row.id) if e["type"] in ("RUN_FINISHED", "RUN_ERROR")]
    usages = [finals[-1]["metadata"]["usage"]]
    if status == "paused":
        resume = h.resume_row(row, "confirm", [DECIDER])
        assert await h.run(resume) == "succeeded"
        usages.append([e for e in h.events(resume.id) if e["type"] == "RUN_FINISHED"][-1]["metadata"]["usage"])
    calls: dict[str, int] = {}
    for usage in usages:
        assert usage["trace_id"] and set(usage) == {"trace_id", "total", "agents"}
        for name, agent in usage["agents"].items():
            calls[name] = calls.get(name, 0) + agent["calls"]
            # FakeLlm: 100 tokens per turn, half prompt and half completion
            assert agent["prompt_tokens"] == 50 * agent["calls"] == agent["completion_tokens"]
            assert agent["reasoning_tokens"] == 0 and agent["cost_usd"] is None
        total = usage["total"]
        assert total["calls"] == sum(a["calls"] for a in usage["agents"].values())
        assert total["prompt_tokens"] == sum(a["prompt_tokens"] for a in usage["agents"].values())
    assert calls == {name: len(reqs) for name, reqs in h.script.requests.items()}
    assert calls["inventory_demand_investigator"] == 2 and calls["critic"] == 1 and calls["recommend"] == 1


def test_usage_meter_records_tokens_reasoning_latency_and_cost() -> None:
    meter = UsageMeter()
    usage = types.GenerateContentResponseUsageMetadata(
        prompt_token_count=1200, candidates_token_count=300, thoughts_token_count=200, total_token_count=1500,
        cached_content_token_count=100)
    meter.record("planner", usage, latency_ms=2500)
    meter.record("planner", None, latency_ms=10)
    meter.add_cost("planner", 0.0012)
    summary = meter.summary("run-1")
    assert summary["trace_id"] == "run-1"
    assert summary["agents"]["planner"] == {"calls": 2, "replayed": 0, "prompt_tokens": 1200,
                                            "completion_tokens": 300, "reasoning_tokens": 200, "cached_tokens": 100,
                                            "total_tokens": 1500, "latency_ms": 2510, "cost_usd": 0.0012}
    assert summary["total"]["prompt_tokens"] == 1200


async def test_usage_plugin_counts_replayed_calls_apart() -> None:
    """A call served by the record/replay layer reports zero tokens; the meter counts it as replayed."""
    from keelson_engine.runtime.plugins import UsagePlugin

    run = _run()
    plugin = UsagePlugin()
    ctx = pytypes.SimpleNamespace(agent_name="planner")
    replayed = LlmResponse(usage_metadata=types.GenerateContentResponseUsageMetadata(total_token_count=0),
                           custom_metadata={"keelson_replay": {"replayed": True, "cost_usd": 0.0}})
    live = LlmResponse(usage_metadata=types.GenerateContentResponseUsageMetadata(
        prompt_token_count=10, candidates_token_count=5, total_token_count=15))
    with bind_run(run):
        for response in (replayed, live):
            await plugin.before_model_callback(callback_context=ctx, llm_request=LlmRequest())  # type: ignore[arg-type]
            await plugin.after_model_callback(callback_context=ctx, llm_response=response)  # type: ignore[arg-type]
    row = run.usage.summary()["agents"]["planner"]
    assert (row["calls"], row["replayed"], row["total_tokens"]) == (2, 1, 15)


def test_litellm_cost_callback_attributes_cost_to_the_calling_agent() -> None:
    run = _run()
    proxy = pytypes.SimpleNamespace(_hidden_params={"additional_headers": {"llm_provider-x-litellm-response-cost": "0.0042"}})
    assert response_cost({}, proxy) == pytest.approx(0.0042)
    assert response_cost({"response_cost": 0.5}, pytypes.SimpleNamespace()) == 0.5
    assert response_cost({"response_cost": None}, pytypes.SimpleNamespace(_hidden_params={})) is None
    with bind_run(run):
        token = current_agent.set("critic")
        try:
            record_cost({}, proxy)
            record_cost({"response_cost": None}, pytypes.SimpleNamespace())  # no cost known: ignored
        finally:
            current_agent.reset(token)
    record_cost({}, proxy)  # outside a run: ignored
    assert run.usage.summary()["agents"]["critic"]["cost_usd"] == pytest.approx(0.0042)
    assert run.usage.summary()["agents"]["critic"]["calls"] == 0


def test_models_lock_uses_no_pro_models_and_the_diet_efforts() -> None:
    import yaml

    lock = yaml.safe_load((Path(__file__).resolve().parents[4] / "models.lock").read_text(encoding="utf-8"))
    aliases = lock["aliases"]
    assert all("-pro" not in a["model"] for a in aliases.values())
    chat = {k: a for k, a in aliases.items() if a["mode"] == "chat"}
    assert all("flash" in a["model"] for a in chat.values())
    efforts = {k: a["params"]["reasoning_effort"] for k, a in chat.items()}
    assert efforts == {"reasoning-default": "medium", "fast-default": "low", "extract-fast": "minimal",
                       "critic-default": "low", "guard-default": "minimal"}
    assert "lite" in aliases["extract-fast"]["model"] and "lite" in aliases["guard-default"]["model"]


def test_turn_cap_default_is_part_of_the_spec() -> None:
    spec = AgentSpec(tenant="demo-retail")
    assert spec.investigation.max_investigator_turns == 6
    assert AgentSpec.from_row("demo-retail", "v2", {"investigation": {"max_investigator_turns": 4}}).investigation.max_investigator_turns == 4
    with pytest.raises(ValueError):
        AgentSpec.from_row("demo-retail", "v2", {"investigation": {"max_investigator_turns": 1}})
