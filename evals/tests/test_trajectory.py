"""(M8) Tool trajectory in any order, structured entities / targets and exact usage aggregation."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from keelson_evals import assertions as A
from keelson_evals.cases import Case, TrajectoryExpect, check_names
from keelson_evals.evaluate import TurnObservation, evaluate_turn, merge_metrics

CALLS = [("inventory_demand", "get_inventory"), ("sourcing", "list_purchase_orders"),
         ("sourcing", "list_purchase_orders"), ("logistics", "read_events_structured")]


def test_required_patterns_match_in_any_order_with_wildcards() -> None:
    m = A.trajectory_match(list(reversed(CALLS)), ["sourcing:*", "inventory_demand:get_*", "playbook:*"])
    assert m["matched"] == ["sourcing:*", "inventory_demand:get_*"] and m["missing"] == ["playbook:*"]
    assert m["recall"] == pytest.approx(2 / 3)
    assert m["calls"][0] == {"agent": "sourcing", "tool": "list_purchase_orders", "n": 2}
    assert m["total_calls"] == 4 and m["forbidden_hits"] == []


def test_forbidden_patterns_and_no_requirement() -> None:
    calls = [*CALLS, ("sourcing", "apply_change_set"), ("answer", "list_supplier_messages")]
    m = A.trajectory_match(calls, [], ["*:apply_*", "*:list_supplier_messages", "x:*"])
    assert m["recall"] is None
    assert m["forbidden_hits"] == ["answer:list_supplier_messages", "sourcing:apply_change_set"]


def test_patterns_are_literal_except_star() -> None:
    # regex characters in a pattern are literal; '*' is the only wildcard and matches the whole name
    m = A.trajectory_match([("a", "list.po"), ("a", "listxpo")], ["a:list.po"], ["a:list?po"])
    assert m["matched"] == ["a:list.po"] and m["forbidden_hits"] == []
    assert A.trajectory_match([("ab", "x")], ["a:*"])["missing"] == ["a:*"]


def test_calls_are_capped_but_counted() -> None:
    calls = [(f"a{i}", "t") for i in range(150)]
    m = A.trajectory_match(calls, ["*:t"])
    assert len(m["calls"]) == A.MAX_TRAJECTORY_CALLS and m["total_calls"] == 150 and m["recall"] == 1.0


def test_trajectory_expect_validation() -> None:
    TrajectoryExpect(required=["sourcing:*"])
    TrajectoryExpect(max_calls=0)
    with pytest.raises(ValidationError, match="required, forbidden"):
        TrajectoryExpect()
    for bad in ("sourcing", "a:b:c", "a b:c", ":x"):
        with pytest.raises(ValidationError):
            TrajectoryExpect(required=[bad])


def _case(expect: dict[str, Any]) -> Case:
    return Case.model_validate({"id": "c", "title": "c", "turns": [{"id": "t", "message": "m", "expect": expect}],
                                "soft": []})


def _obs(calls: list[tuple[str, str]]) -> TurnObservation:
    events = [{"type": "TOOL_CALL_START", "toolCallName": tool, "metadata": {"sc": {"agent": agent}}}
              for agent, tool in calls]
    return TurnObservation(turn_id="t", final={"type": "RUN_FINISHED", "outcome": {"type": "success"}},
                           events=events)


def test_trajectory_check_and_metrics() -> None:
    case = _case({"trajectory": {"required": ["sourcing:*", "playbook:*"], "min_recall": 0.5,
                                 "forbidden": ["*:apply_*"], "max_calls": 10}})
    assert "t.trajectory" in check_names(case)
    checks, metrics = evaluate_turn(case, case.turns[0], _obs(CALLS), None, ["m"])
    traj = next(c for c in checks if c.name == "t.trajectory")
    assert traj.ok, traj.detail
    assert metrics["trajectory_recall"] == 0.5
    assert metrics["trajectory"]["recall_bp"] == 5000 and metrics["trajectory"]["min_recall_bp"] == 5000
    assert metrics["trajectory"]["missing"] == ["playbook:*"]

    checks, _ = evaluate_turn(case, case.turns[0], _obs([*CALLS, ("sourcing", "apply_change_set")] * 3), None, ["m"])
    traj = next(c for c in checks if c.name == "t.trajectory")
    assert not traj.ok and "forbidden calls ['sourcing:apply_change_set']" in traj.detail and "15 tool calls > 10" in traj.detail


def test_structured_entities_and_targets() -> None:
    case = _case({"affected_entities": {"golden": ["UK1", "NL1", "UK1"], "min_recall": 0.5},
                  "numeric_grounding": {"min": 0.95}, "latency_s": {"first_stage": 2, "options": 180}})
    obs = _obs([])
    obs.blocks = {"kpi_strip": {"kind": "kpi_strip", "props": {"items": [{"key": "dc", "value_text": "UK1"}]}},
                  "narrative": {"kind": "narrative", "props": {"markdown": "UK1 is short."}}}
    obs.timings = {"first_stage": 1.5, "exposure": None, "options": 100.0, "total": 120.0}
    _checks, metrics = evaluate_turn(case, case.turns[0], obs, None, ["m"])
    assert metrics["entities"] == {"golden": ["UK1", "NL1"], "missing": ["NL1"], "recall_bp": 5000, "min_recall_bp": 5000}
    assert metrics["targets"] == {"latency_ms": {"first_stage": 2000, "exposure": None, "options": 180_000,
                                                 "total": None}, "numeric_grounding_min_bp": 9500}


def test_merge_metrics_takes_the_minimum_trajectory_recall() -> None:
    merged = merge_metrics([{"trajectory_recall": 1.0}, {"trajectory_recall": 0.5}, {}], {})
    assert merged["trajectory_recall"] == 0.5
    assert "trajectory_recall" not in merge_metrics([{}], {})


def _usage(calls: int, tokens: int, cost: float | None, agent: str = "planner") -> dict[str, Any]:
    row = {"calls": calls, "replayed": 1, "prompt_tokens": tokens - 10, "completion_tokens": 10,
           "reasoning_tokens": 3, "cached_tokens": 5, "total_tokens": tokens, "latency_ms": 1200,
           "cost_usd": cost}
    return {"trace_id": "x", "total": row, "agents": {agent: row}}


def test_usage_total_sums_runs_exactly() -> None:
    total = A.usage_total([_usage(2, 1000, 0.0012345), None, {"no": "usage"}, _usage(1, 500, None, "critic")])
    assert total is not None and total["runs"] == 2
    assert total["total"] == {"calls": 3, "replayed": 2, "prompt_tokens": 1480, "completion_tokens": 20,
                              "reasoning_tokens": 6, "total_tokens": 1500, "latency_ms": 2400, "cost_microusd": 1235}
    assert sorted(total["agents"]) == ["critic", "planner"] and total["agents"]["critic"]["cost_microusd"] is None
    assert A.usage_total([]) is None and A.usage_total([None]) is None
