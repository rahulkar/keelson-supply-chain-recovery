"""``refine_scenario`` (``90`` §5): fact-backed updates apply, inference-backed ones become
unconfirmed assumptions (Apply disabled until ``confirm_assumptions``), malformed / unknown /
conflicting updates are dropped; plus the shared scenario value edits."""

from __future__ import annotations

import datetime as dt
from typing import Any

import pytest
from fakes.core import FakeCore
from keelson_contracts.common import Provenance
from keelson_contracts.scenario import DisruptionScenario
from test_m4_framing_support import run_ctx, run_node

from keelson_engine.runtime.deps import ToolInfo
from keelson_engine.runtime.nodes.refine import (
    confirm_scenario_keys,
    evidence_is_trusted,
    refine,
    refine_scenario,
    scenario_value,
    set_scenario_value,
    update_supported,
)
from keelson_contracts.llm import ScenarioUpdate

RECEIPTS = ["PO-BD-0001#1", "PO-BD-0001#2"]


def scenario(**kw: Any) -> DisruptionScenario:
    perturbations = [
        {"op": "lead_time_shift", "target": {"kinds": ["receipt"], "ids": RECEIPTS}, "days": 7, "p10": 5, "p90": 14,
         "prov": "assumption"},
        {"op": "capacity_scale", "target": {"kinds": ["port"], "ids": ["DEHAM"]}, "factor_bp": 3000, "prov": "assumption",
         "window": {"start": "2026-10-05", "end": "2026-10-11"}},
    ]
    return DisruptionScenario.model_validate({
        "id": "scn-1", "tenant": "demo-retail", "mode": "actual", "event_family": "SUP", "event_type": "SUP.delay",
        "description": "d", "perturbations": kw.get("perturbations", perturbations), "matched": [],
        "assumptions": kw.get("assumptions", [
            {"key": "delay_days", "label": "Delay", "value_text": "7 days", "prov": "assumption"},
            {"key": "affected_receipts", "label": "Affected purchase-order lines", "value_text": "2 open lines on PO-BD-0001", "prov": "fact"},
            {"key": "duration_days", "label": "Duration", "value_text": "7 days", "prov": "assumption"},
        ]),
        "config_version": "default/p-1",
    })


def ev(eid: str, tool: str, result: Any, *, prov: str = "fact", connector: str = "merxa", **extra: Any) -> dict[str, Any]:
    return {"id": eid, "tool": tool, "connector": connector, "prov": prov, "result": result, "args": {}, **extra}


def finding(task: str, *updates: dict[str, Any], status: str = "ok") -> dict[str, Any]:
    return {"task_id": task, "status": status, "claims": [], "affected": [], "gaps": [],
            "scenario_updates": [{"value_int": None, "value_ids": None, "json_pointer": "", **u} for u in updates]}


PO = ev("ev_po1", "get_purchase_order", {"code": "PO-BD-0001", "delay_days": 10,
                                         "lines": [{"po_code": "PO-BD-0001", "line_no": 1}, {"po_code": "PO-BD-0001", "line_no": 3}]})
MSG = ev("ev_msg1", "list_supplier_messages", {"items": [{"text": "delayed 12 days", "delay_days": 12}]})
WRAP = ev("ev_wrap1", "read_supplier_messages_structured", {"items": [{"delay_days": 9}]})
UNTRUSTED = {("merxa", "list_supplier_messages")}


def test_fact_backed_update_applies_as_fact() -> None:
    r = refine(scenario(), [finding("t1", {"key": "delay_days", "value_int": 10, "evidence_id": "ev_po1",
                                           "json_pointer": "/delay_days"})], {"ev_po1": PO}, UNTRUSTED)
    [o] = r.outcomes
    assert o.outcome == "fact"
    lts = r.scenario.perturbations[0]
    assert (lts.days, lts.p10, lts.p90, lts.prov) == (10, None, None, Provenance.FACT)
    a = next(a for a in r.scenario.assumptions if a.key == "delay_days")
    assert (a.prov, a.unconfirmed, a.source_evidence_ids, a.value_text) == (Provenance.FACT, False, ["ev_po1"], "10 days")
    assert not r.scenario.pending_confirmation
    assert r.scenario.perturbations[1] == scenario().perturbations[1]  # untouched


@pytest.mark.parametrize(
    ("row", "pointer", "why"),
    [(MSG, "/items/0/delay_days", "untrusted tool"), (WRAP, "/items/0/delay_days", "quarantine wrapper"),
     (PO, "/code", "value not supported by the evidence"),
     (ev("ev_inf", "get_purchase_order", {"delay_days": 12}, prov="inference"), "/delay_days", "inference evidence"),
     (ev("ev_tag", "get_purchase_order", {"delay_days": 12}, content_trust="untrusted_text"), "/delay_days", "tagged row")],
)
def test_inference_backed_updates_become_unconfirmed_assumptions(row: dict[str, Any], pointer: str, why: str) -> None:
    value = 12 if row is not PO else 11
    r = refine(scenario(), [finding("t1", {"key": "delay_days", "value_int": value, "evidence_id": row["id"],
                                           "json_pointer": pointer})], {row["id"]: row}, UNTRUSTED)
    assert [o.outcome for o in r.outcomes] == ["inference"], why
    a = next(a for a in r.scenario.assumptions if a.key == "delay_days")
    assert (a.prov, a.unconfirmed, a.source_evidence_ids) == (Provenance.INFERENCE, True, [row["id"]])
    assert "inferred from the investigation; was 7 days" in a.value_text
    assert r.scenario.perturbations[0].days == value and r.scenario.perturbations[0].prov == Provenance.INFERENCE
    assert [p.key for p in r.scenario.pending_confirmation] == ["delay_days"]


def test_receipt_updates_check_every_id_against_the_evidence() -> None:
    add = {"key": "add_receipts", "value_ids": ["PO-BD-0001#3"], "evidence_id": "ev_po1", "json_pointer": "/lines"}
    r = refine(scenario(), [finding("t1", add)], {"ev_po1": PO}, UNTRUSTED)
    assert r.outcomes[0].outcome == "fact"
    assert r.scenario.perturbations[0].target.ids == ["PO-BD-0001#1", "PO-BD-0001#2", "PO-BD-0001#3"]
    assert next(a for a in r.scenario.assumptions if a.key == "affected_receipts").value_text.startswith("3 open lines")
    bad = {**add, "value_ids": ["PO-BD-0001#3", "PO-BD-0001#9"]}
    r2 = refine(scenario(), [finding("t1", bad)], {"ev_po1": PO}, UNTRUSTED)
    assert r2.outcomes[0].outcome == "inference"
    rm = {"key": "remove_receipts", "value_ids": RECEIPTS, "evidence_id": "ev_po1", "json_pointer": "/lines"}
    r3 = refine(scenario(), [finding("t1", rm)], {"ev_po1": PO}, UNTRUSTED)
    assert r3.outcomes[0].outcome == "dropped" and "not applicable" in r3.outcomes[0].reason  # would empty the target


def test_duration_update_moves_the_window_end() -> None:
    row = ev("ev_ev1", "list_sailings", {"items": [{"closure_days": 14}]})
    r = refine(scenario(), [finding("t1", {"key": "duration_days", "value_int": 14, "evidence_id": "ev_ev1",
                                           "json_pointer": "/items/0/closure_days"})], {"ev_ev1": row}, UNTRUSTED)
    cap = r.scenario.perturbations[1]
    assert (cap.window.start, cap.window.end) == (dt.date(2026, 10, 5), dt.date(2026, 10, 18))
    assert r.scenario.perturbations[0].window is None


def test_malformed_unknown_and_conflicting_updates_are_dropped() -> None:
    findings = [
        finding("t1", {"key": "delay_days", "value_int": -3, "evidence_id": "ev_po1", "json_pointer": "/delay_days"},
                {"key": "delay_days", "value_int": 10, "evidence_id": "ev_nope", "json_pointer": "/delay_days"},
                {"key": "delay_days", "value_int": 10, "evidence_id": "not-an-id", "json_pointer": "/delay_days"},
                {"key": "demand_change_pct", "value_int": 10, "evidence_id": "ev_po1", "json_pointer": "/delay_days"}),
        finding("t2", {"key": "delay_days", "value_int": 12, "evidence_id": "ev_msg1", "json_pointer": "/items/0/delay_days"}),
        finding("t3", {"key": "delay_days", "value_int": 10, "evidence_id": "ev_po1", "json_pointer": "/delay_days"}),
        finding("t4", {"key": "delay_days", "value_int": 10, "evidence_id": "ev_po1", "json_pointer": "/delay_days"},
                status="failed"),
    ]
    r = refine(scenario(), findings, {"ev_po1": PO, "ev_msg1": MSG}, UNTRUSTED)
    by = {(o.task_id, o.evidence_id, o.outcome, o.reason.split(":")[0]) for o in r.outcomes}
    assert ("t1", "ev_po1", "dropped", "malformed") in by
    assert ("t1", "ev_nope", "dropped", "unknown evidence") in by
    assert ("t1", "not-an-id", "dropped", "malformed") in by
    assert ("t1", "ev_po1", "dropped", "not applicable to this scenario") in by  # no demand perturbation
    assert ("t2", "ev_msg1", "dropped", "conflict") in by  # the fact-backed t3 wins over the supplier message
    assert ("t3", "ev_po1", "fact", "") in by
    assert all(o.task_id != "t4" for o in r.outcomes)  # failed findings carry no updates
    assert r.scenario.perturbations[0].days == 10 and not r.scenario.pending_confirmation


def test_an_inference_repeating_a_decision_changes_nothing() -> None:
    decided = set_scenario_value(scenario(), "delay_days", value_int=12, prov=Provenance.DECISION)
    assert decided is not None
    r = refine(decided, [finding("t2", {"key": "delay_days", "value_int": 12, "evidence_id": "ev_msg1",
                                        "json_pointer": "/items/0/delay_days"})], {"ev_msg1": MSG}, UNTRUSTED)
    assert [o.reason for o in r.outcomes] == ["no change"] and r.scenario == decided


def test_trust_and_support_helpers() -> None:
    assert evidence_is_trusted(PO, UNTRUSTED)
    assert not evidence_is_trusted(MSG, UNTRUSTED)
    assert not evidence_is_trusted(WRAP, set())
    assert not evidence_is_trusted({**PO, "connector": "other"}, {("other", "*")})
    u = ScenarioUpdate(key="delay_days", value_int=10, value_ids=None, evidence_id="ev_po1", json_pointer="/delay_days")
    assert update_supported(u, PO["result"])
    assert not update_supported(u.model_copy(update={"json_pointer": "/missing"}), PO["result"])
    assert not update_supported(u, {"delay_days": True})


def test_confirm_scenario_keys_turns_inference_into_decision_and_revalidates() -> None:
    r = refine(scenario(), [finding("t2", {"key": "delay_days", "value_int": 12, "evidence_id": "ev_msg1",
                                           "json_pointer": "/items/0/delay_days"})], {"ev_msg1": MSG}, UNTRUSTED)
    confirmed = confirm_scenario_keys(r.scenario, ["delay_days", "unknown"])
    a = next(a for a in confirmed.assumptions if a.key == "delay_days")
    assert (a.prov, a.unconfirmed) == (Provenance.DECISION, False)
    assert confirmed.perturbations[0].prov == Provenance.DECISION
    assert confirmed.perturbations[1].prov == Provenance.ASSUMPTION  # other values keep their provenance
    assert not confirmed.pending_confirmation
    assert confirmed.fingerprint() != r.scenario.fingerprint()
    DisruptionScenario.model_validate(confirmed.model_dump(mode="json"))  # still a valid stored scenario
    assert confirm_scenario_keys(r.scenario, []) is r.scenario


def test_scenario_value_edits() -> None:
    s = scenario()
    assert scenario_value(s, "delay_days") == 7 and scenario_value(s, "duration_days") == 7
    assert scenario_value(s, "capacity_pct") == 30 and scenario_value(s, "demand_change_pct") is None
    assert set_scenario_value(s, "demand_change_pct", value_int=10, prov=Provenance.DECISION) is None
    cap = set_scenario_value(s, "capacity_pct", value_int=0, prov=Provenance.DECISION)
    assert cap.perturbations[1].factor_bp == 0
    assert next(a for a in cap.assumptions if a.key == "capacity_pct").value_text == "Closed (0% of normal capacity)"
    start = set_scenario_value(s, "start_date", value_date=dt.date(2026, 10, 12), prov=Provenance.DECISION)
    assert (start.perturbations[1].window.start, start.perturbations[1].window.end) == (dt.date(2026, 10, 12), dt.date(2026, 10, 18))
    with pytest.raises(ValueError):
        set_scenario_value(s, "colour", value_int=1, prov=Provenance.DECISION)


async def test_refine_scenario_node_saves_a_version_and_passes_findings_through() -> None:
    core = FakeCore()
    core.connectors.tools = [ToolInfo(name="list_supplier_messages", title=None, description="", input_schema={},
                                      output_schema=None, access="read", content_trust="untrusted_text",
                                      status="approved", definition_hash="h", annotations={})]
    run = run_ctx(core)
    await core.repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario())
    msg = await core.repo.save_evidence(tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector="merxa",
                                        tool="list_supplier_messages", args={}, result=MSG["result"], prov="fact")
    po = await core.repo.save_evidence(tenant=run.tenant, case_id=run.case_id, run_id=run.run_id, connector="merxa",
                                       tool="get_purchase_order", args={}, result=PO["result"], prov="fact")
    findings = [finding("t1", {"key": "duration_days", "value_int": 9, "evidence_id": msg, "json_pointer": "/items/0/delay_days"}),
                finding("t2", {"key": "add_receipts", "value_ids": ["PO-BD-0001#3"], "evidence_id": po, "json_pointer": "/lines"})]
    events, out, _state = await run_node(refine_scenario, {"findings": findings, "verdict": "PASS"}, run)
    assert out["scenario_version"] == 2 and out["verdict"] == "PASS" and out["findings"] == findings
    assert [o["key"] for o in out["refined"]["applied"]] == ["add_receipts"]
    assert [o["key"] for o in out["refined"]["pending"]] == ["duration_days"]
    saved = (await core.repo.latest_scenario(tenant=run.tenant, case_id=run.case_id)).scenario
    assert [a.key for a in saved.pending_confirmation] == ["duration_days"]
    labels = [e.custom_metadata["sc_progress"]["label"] for e in events if e.partial and e.custom_metadata]
    assert labels and "please confirm before applying" in labels[0]
    # nothing to refine → same version, no progress label
    _events, out2, _ = await run_node(refine_scenario, {"findings": []}, run)
    assert out2["scenario_version"] == 2 and out2["refined"] == {"applied": [], "pending": [], "dropped": []}


# ─────────────── review regressions: no laundering of untrusted values into facts ───────────────

LINES = ev("ev_lines", "list_purchase_orders", {"items": [{"po_code": "PO-BD-0001", "line_no": 14, "qty": 0,
                                                            "delay_days": 3}]})


@pytest.mark.parametrize(("key", "value", "pointer"), [
    ("delay_days", 14, "/items/0/line_no"),  # an equal integer in an unrelated field
    ("delay_days", 0, "/items/0/qty"),
    ("demand_change_pct", 14, "/items/0/line_no"),
])
def test_an_equal_integer_in_an_unrelated_field_is_not_fact_backing(key: str, value: int, pointer: str) -> None:
    u = ScenarioUpdate(key=key, value_int=value, value_ids=None, evidence_id="ev_lines", json_pointer=pointer)  # type: ignore[arg-type]
    assert not update_supported(u, LINES["result"])
    r = refine(scenario(), [finding("t1", u.model_dump())], {"ev_lines": LINES}, UNTRUSTED)
    if key == "delay_days":
        assert [o.outcome for o in r.outcomes] == ["inference"]
        assert [a.key for a in r.scenario.pending_confirmation] == ["delay_days"]


def test_a_named_field_still_backs_the_update() -> None:
    u = ScenarioUpdate(key="delay_days", value_int=3, value_ids=None, evidence_id="ev_lines",
                       json_pointer="/items/0/delay_days")
    assert update_supported(u, LINES["result"])
    # the field name is read from the last non-index token, unescaped
    assert update_supported(u.model_copy(update={"json_pointer": "/x~1delay/0"}), {"x/delay": [3]})


def test_removing_receipts_always_needs_confirmation() -> None:
    rm = {"key": "remove_receipts", "value_ids": ["PO-BD-0001#1"], "evidence_id": "ev_po1", "json_pointer": "/lines"}
    r = refine(scenario(), [finding("t1", rm)], {"ev_po1": PO}, UNTRUSTED)
    assert [o.outcome for o in r.outcomes] == ["inference"]
    assert r.scenario.perturbations[0].target.ids == ["PO-BD-0001#2"]
    assert [a.key for a in r.scenario.pending_confirmation] == ["affected_receipts"]


def test_quarantine_marked_rows_are_never_trusted() -> None:
    raw = ev("ev_raw", "some_tool", {"delay_days": 10}, args={"content_trust": "untrusted_text"})
    extraction = ev("ev_ext", "some_tool", {"delay_days": 10}, args={"content_trust": "untrusted_extraction"})
    assert not evidence_is_trusted(raw, set()) and not evidence_is_trusted(extraction, set())


async def test_evidence_of_another_case_never_backs_an_update() -> None:
    core = FakeCore()
    run = run_ctx(core)
    await core.repo.save_scenario(tenant=run.tenant, case_id=run.case_id, scenario=scenario())
    other = await core.repo.save_evidence(tenant=run.tenant, case_id="case-other", run_id="r-x", connector="merxa",
                                          tool="get_purchase_order", args={}, result=PO["result"], prov="fact")
    findings = [finding("t1", {"key": "delay_days", "value_int": 10, "evidence_id": other, "json_pointer": "/delay_days"})]
    _events, out, _ = await run_node(refine_scenario, {"findings": findings}, run)
    assert [(o["evidence_id"], o["reason"]) for o in out["refined"]["dropped"]] == [(other, "unknown evidence")]
    assert out["scenario_version"] == 1
