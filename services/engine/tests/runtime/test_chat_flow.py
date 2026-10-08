"""Chat → approach pause → resume on a FRESH Runner → investigation → plan → composed answer."""

from __future__ import annotations

from typing import Any

from fakes.harness import MARCO, PRIYA, Harness
from fakes.scripts import chat_flow_script
from keelson_contracts.agui import AGENT_DISPLAY_NAMES
from keelson_contracts.blocks import parse_block

DECIDER_PRIYA = {"sub": PRIYA["sub"], "username": PRIYA["username"], "name": PRIYA["name"]}


def _types(events: list[dict[str, Any]]) -> list[str]:
    return [e["type"] for e in events]


async def test_chat_pause_then_resume_on_fresh_runner(harness: Harness) -> None:
    h = harness
    h.script = chat_flow_script(h.core.repo, delay_days=None)
    chat = h.row("chat", {"text": "Meghna Trims says the trims for our puffers are delayed"})

    assert await h.run(chat) == "paused"
    assert h.core.runs.status_of(chat.id) == "paused"

    # one approach interrupt, created before RUN_FINISHED{interrupt}
    [interrupt] = h.core.interrupts.created
    assert interrupt["kind"] == "approach"
    assert interrupt["interrupt_id"] == f"approach-{h.case_id}-1"
    assert interrupt["invocation_id"] == f"e-{chat.id}"
    assert interrupt["payload"]["session_id"] == chat.id and interrupt["session_id"] == chat.id
    assert interrupt["quorum_spec"]["roles"] == ["planner", "approver", "tenant-admin"]
    first = h.events(chat.id)
    assert first[0]["type"] == "RUN_STARTED" and first[-1]["type"] == "RUN_FINISHED"
    outcome = first[-1]["outcome"]
    assert outcome["type"] == "interrupt"
    assert outcome["interrupts"][0]["id"] == interrupt["interrupt_id"]
    assert outcome["interrupts"][0]["reason"] == "approach"
    assert "confirm" in outcome["interrupts"][0]["responseSchema"]["properties"]["decision"]["enum"]
    # the delay was not stated → assumption → the card asks; preliminary exposure shown first
    kinds = [b["kind"] for b in (e["content"] for e in first if e["type"] == "ACTIVITY_SNAPSHOT")]
    assert kinds == ["kpi_strip", "approach_card"]
    strip = next(e["content"] for e in first if e["type"] == "ACTIVITY_SNAPSHOT")
    assert all(item["preliminary"] for item in strip["props"]["items"])
    card = parse_block(next(e["content"] for e in first if e["type"] == "ACTIVITY_SNAPSHOT" and e["content"]["kind"] == "approach_card"))
    delay = next(a for a in card.props.assumptions if a.key == "delay_days")
    assert delay.prov.value == "assumption" and delay.value_text == "7 days"
    assert {m.code for m in card.props.matched.sample} >= {"TRIM-BD-01", "PO-BD-0001", "PO-BD-0002"}
    # only the intake and planner roles announced themselves in the first run (M4: the planner's
    # LLM call has its own row), and no LLM text became a TEXT_MESSAGE
    started = [e for e in first if e["type"] == "SUBAGENT_STARTED"]
    assert [e["name"] for e in started] == ["intake", "planner"]
    assert "TEXT_MESSAGE_START" not in _types(first)
    assert "REASONING_START" in _types(first)

    # resume on a brand-new Runner (fresh session service on the same DB) → full investigation
    fresh = h.new_cache()
    try:
        resume = h.resume_row(chat, "confirm", [DECIDER_PRIYA])
        assert await h.run(resume, cache=fresh) == "succeeded"
    finally:
        await fresh.close()
    second = h.events(resume.id)
    names = [e["name"] for e in second if e["type"] == "SUBAGENT_STARTED"]
    assert names == ["inventory_demand", "critic", "recommender"]
    assert set(names) <= set(AGENT_DISPLAY_NAMES)
    finished = [e["subagentRunId"] for e in second if e["type"] == "SUBAGENT_FINISHED"]
    assert len(finished) == 3 and all(r.startswith(f"e-{chat.id}:keelson@1/") for r in finished)  # + critic
    # replayed outputs from the first invocation are not re-announced
    assert "intake" not in names
    steps = [e["stepName"] for e in second if e["type"] == "STEP_STARTED"]
    assert steps == ["understanding", "checking_systems", "measuring_impact", "building_options", "ready_for_review"]
    tool_results = [e for e in second if e["type"] == "TOOL_CALL_RESULT"]
    assert {e["toolCallId"] for e in tool_results} and all(e["metadata"]["sc"]["evidenceRefs"] for e in tool_results)
    assert all("Merxa" in e["content"] for e in tool_results)
    # the confirmed scenario: assumptions became decisions
    scenario = (await h.core.repo.latest_scenario(tenant="demo-retail", case_id=h.case_id)).scenario
    assert not scenario.unconfirmed_assumptions
    # one plan with both options, and the review blocks
    plan = await h.core.repo.latest_plan(tenant="demo-retail", case_id=h.case_id)
    # M3: the default candidates A-D + do nothing; the stub connector declares no change-set ops (M1 ops).
    # M5: options with an applicable lever stay applicable (the other levers' lines are guided
    # steps); near-shore + resequence (ops Merxa cannot apply) are guided-only.
    assert plan["version"] == 1
    assert [o["id"] for o in plan["options"]] == ["opt-A", "opt-B", "opt-C", "opt-D", "do-nothing"]
    assert [o["guided_only"] for o in plan["options"]] == [False, False, True, False, False]
    assert plan["options"][0]["remediation"] == {"apply_lines": 1, "guide_lines": 0}
    assert plan["kpis"]["data_source"] == "mcp" and plan["kpis"]["snapshot_id"] is None
    snapshots = [e["content"] for e in second if e["type"] == "ACTIVITY_SNAPSHOT"]
    assert [b["kind"] for b in snapshots] == [
        "kpi_strip", "kpi_strip", "recommended_option", "scenario_compare", "network_map", "impact_graph",
        "stockout_timeline", "plan_diff", "evidence_pack", "narrative",
    ]
    for block in snapshots:
        parse_block(block)
    final_strip = snapshots[0]
    assert final_strip["id"] == strip["id"] and not any(i["preliminary"] for i in final_strip["props"]["items"])
    assert snapshots[1]["id"] == strip["id"]  # compose replaces the exposure strip (same block)
    assert {"units_recovered", "incremental_cost_cents"} <= {i["key"] for i in snapshots[1]["props"]["items"]}
    inline = {b["id"] for b in snapshots if b["placement"] == "inline" and b["kind"] != "narrative"}
    assert len(inline) <= 2
    rec = snapshots[2]["props"]
    assert rec["option"]["id"] == "opt-A" and rec["actions"][0] == {"type": "apply", "label": "Apply changes for me", "enabled": True, "disabled_reason": None}
    # exactly one composed TEXT_MESSAGE, with numbers filled deterministically
    texts = [e["delta"] for e in second if e["type"] == "TEXT_MESSAGE_CONTENT"]
    assert len(texts) == 1
    assert "{{" not in texts[0] and "€" in texts[0] and "units" in texts[0]
    assert h.core.repo.messages[-1]["text"] == texts[0]
    assert second[-1]["type"] == "RUN_FINISHED" and second[-1]["outcome"] == {"type": "success"}
    # LLM tools only ever ran with the reader principal
    assert {c.principal for c in h.mcp_calls} == {"reader"}
    assert h.optimizer.requests and {p for p, _ in h.optimizer.requests} == {"/v1/impact", "/v1/optimize"}
    assert MARCO["sub"] not in str(h.core.tokens.issued)
