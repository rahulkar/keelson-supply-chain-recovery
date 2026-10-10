"""M8 Evals UI: the pure run-level computations (``core/evals_summary.py``) on the two real CLI
result files of 2026-10-07 and on synthetic case results, plus the mode availability rules."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest
from keelson_contracts.evals import (
    DEFAULT_EVAL_THRESHOLDS,
    EvalCaseResult,
    EvalCounters,
    EvalProgress,
    EvalRun,
    EvalRunnerInfo,
    case_result_from_doc,
    evaluate_thresholds,
    results_doc_to_case_results,
)

from keelson_engine.core import evals_summary as S
from keelson_engine.core.evals import mode_availability

FIXTURES = Path(__file__).resolve().parents[4] / "evals" / "tests" / "fixtures"
T0 = dt.datetime(2026, 10, 7, 8, 8, 48, tzinfo=dt.UTC)


def _fixture(name: str) -> list[EvalCaseResult]:
    path = FIXTURES / name
    if not path.is_file():
        pytest.skip(f"repository fixture {name} not available")
    return results_doc_to_case_results(json.loads(path.read_text(encoding="utf-8")))[1]


def _run(run_id: str, cases: list[EvalCaseResult], mode: str = "unknown") -> EvalRun:
    summary = S.summarise(cases)
    outcomes = evaluate_thresholds(summary, DEFAULT_EVAL_THRESHOLDS, mode)
    return EvalRun(
        id=run_id, tenant="demo-retail", source="import", mode=mode, status="succeeded",  # type: ignore[arg-type]
        case_ids=[c.case_id for c in cases], requested_by=None, note=None, budget=None,
        counters=EvalCounters(tokens_live=0, tokens_replayed=0, llm_calls_live=0, llm_calls_replayed=0,
                              replay_misses=0, cost_microusd=0),
        cancel_requested=False, suite_hash=None, summary=summary, thresholds=list(DEFAULT_EVAL_THRESHOLDS),
        threshold_results=outcomes, breaches=sum(1 for o in outcomes if o.outcome == "breach"),
        doc_schema="keelson-evals/1", file_name=None, target=None, error=None, created_at=T0, started_at=T0,
        finished_at=T0, progress=EvalProgress(cases_done=len(cases), cases_total=len(cases), current_case=None,
                                              current_turn=None),
    )


def _case(case_id: str, status: str, **over: Any) -> EvalCaseResult:
    raw: dict[str, Any] = {"id": case_id, "title": case_id, "tags": over.pop("tags", []), "status": status,
                           "duration_s": 10, "turns": over.pop("turns", []), "checks": over.pop("checks", []),
                           "metrics": over.pop("metrics", {})}
    raw.update(over)
    return case_result_from_doc(raw)


def _turn(first: float | None = None, options: float | None = None) -> dict[str, Any]:
    return {"id": "t", "kind": "message", "timings_s": {"first_stage": first, "options": options}, "checks": []}


# ───────────────────────────── summary ─────────────────────────────


def test_summary_of_the_real_v1_run() -> None:
    s = S.summarise(_fixture("results-v1-20261007T080848Z.json"))
    assert (s.cases, s.passed, s.failed, s.errors, s.skipped) == (6, 0, 5, 1, 0)
    assert (s.checks, s.checks_failed, s.soft_failed) == (126, 46, 2)  # the CLI's own Markdown: 80/126, soft 2
    assert s.pass_rate_bp == 0 and s.adversarial_cases == 2 and s.adversarial_pass_rate_bp == 0
    assert s.numeric_grounding_min_bp == 9091 and s.numeric_grounding_mean_bp == 9111  # (9091 + 9130) / 2
    assert s.entity_recall_mean_bp == 0
    assert s.latency.first_stage.max_ms == 1590 and s.latency.options.max_ms == 223_090
    assert s.tokens_source == "gateway" and s.tokens_total == sum(
        c.tokens_gateway or 0 for c in _fixture("results-v1-20261007T080848Z.json"))
    assert s.cost_microusd_total is None and s.llm_calls_live == 0


def test_summary_rates_skip_unevaluated_cases_and_use_exact_usage() -> None:
    usage = {"runs": 1, "total": {"calls": 3, "replayed": 1, "total_tokens": 900, "cost_microusd": 1500},
             "agents": {}}
    cases = [
        _case("a", "pass", tags=["adversarial"], usage=usage, turns=[_turn(0.4, 100)]),
        _case("b", "fail", usage=usage, turns=[_turn(0.6, 300)], metrics={"entity_recall": 0.5}),
        _case("c", "skipped", error_code="needs_live_embeddings"),
        _case("d", "cancelled", error_code="cancelled", error="stopped"),
        _case("e", "error", error_code="replay_miss", error="miss", tags=["adversarial"], metrics={"tokens": 100}),
    ]
    s = S.summarise(cases)
    assert s.pass_rate_bp == 3333 and s.adversarial_pass_rate_bp == 5000  # 1/3 evaluated, 1/2 adversarial
    assert (s.skipped, s.cancelled, s.errors, s.replay_misses) == (1, 1, 1, 1)
    assert s.latency.first_stage.p50_ms == 500 and s.latency.options.max_ms == 300_000  # median of 400 and 600
    assert s.tokens_source == "mixed" and s.tokens_total == 1900 and s.tokens_per_case_mean == 633
    assert s.cost_microusd_total == 3000 and s.cost_per_case_mean_microusd == 1500
    assert (s.llm_calls_live, s.llm_calls_replayed) == (4, 2)
    assert s.entity_recall_mean_bp == 5000
    empty = S.summarise([])
    assert empty.cases == 0 and empty.pass_rate_bp is None and empty.tokens_source is None


def test_summary_survives_inconsistent_untrusted_usage() -> None:
    """An imported file / runner report claiming more replayed calls than calls must not crash the
    summary (an import would answer 500, a finish could never complete and the lease reaper would
    fail on every read)."""
    usage = {"runs": 1, "total": {"calls": 1, "replayed": 5, "total_tokens": 10}, "agents": {}}
    s = S.summarise([_case("a", "pass", usage=usage)])
    assert s.llm_calls_live == 0 and s.llm_calls_replayed <= 1


def test_case_columns() -> None:
    usage = {"runs": 1, "total": {"calls": 1, "total_tokens": 42, "cost_microusd": 7}, "agents": {}}
    c = _case("x", "fail", usage=usage, turns=[_turn(None, None), _turn(0.25, 12)],
              checks=[{"name": "case.a", "ok": False}, {"name": "case.b", "ok": False, "soft": True},
                      {"name": "case.c", "ok": True}])
    cols = S.case_columns(c)
    assert (cols["checks_total"], cols["checks_failed"], cols["soft_failed"]) == (3, 1, 1)
    assert cols["failed_checks"] == ["case.a"] and cols["first_stage_ms"] == 250 and cols["options_ms"] == 12_000
    assert cols["tokens"] == 42 and cols["cost_microusd"] == 7


# ───────────────────────────── compare ─────────────────────────────


def test_compare_the_two_real_runs() -> None:
    base_cases = _fixture("results-v1-20261007T080848Z.json")
    head_cases = _fixture("results-v1-20261007T085022Z.json")
    base = _run("0199c1a2-7b3c-7d4e-8f50-0000000000a1", base_cases)
    head = _run("0199c1a2-7b3c-7d4e-8f50-0000000000a2", head_cases)
    cmp = S.compare_runs(base, base_cases, head, head_cases)
    changes = {c.case_id: c.change for c in cmp.cases}
    assert changes == {
        "adversarial_kb_doc": "fixed",                  # fail -> pass
        "adversarial_supplier_message": "changed",      # fail -> error
        "followup_delay_doubles": "removed", "store_shortage_question": "removed", "suez_whatif": "removed",
        "trim_delay_actual": "removed",
    }
    assert [c.case_id for c in cmp.cases][:2] == ["adversarial_kb_doc", "adversarial_supplier_message"]
    kb = next(c for c in cmp.cases if c.case_id == "adversarial_kb_doc")
    assert "case.guard_flagged" in kb.checks_newly_passed and "report.numeric_grounding" in kb.checks_newly_passed
    assert kb.checks_newly_failed == []
    assert kb.metric_deltas["numeric_grounding_bp"].better is True
    pr = cmp.summary_deltas["pass_rate_bp"]
    assert (pr.base, pr.head, pr.delta, pr.better) == (0, 5000, 5000, True)
    opts = cmp.summary_deltas["options_max_ms"]
    assert opts.base == 223_090 and opts.head == 226_000 and opts.better is False  # slower is worse


def test_regressions_and_new_cases() -> None:
    base_cases = [_case("a", "pass"), _case("b", "fail", checks=[{"name": "case.x", "ok": False}])]
    head_cases = [_case("a", "fail", checks=[{"name": "case.y", "ok": False}]), _case("b", "fail"), _case("n", "pass")]
    cmp = S.compare_runs(_run("0199c1a2-7b3c-7d4e-8f50-0000000000b1", base_cases), base_cases,
                         _run("0199c1a2-7b3c-7d4e-8f50-0000000000b2", head_cases), head_cases)
    by = {c.case_id: c for c in cmp.cases}
    assert by["a"].change == "regressed" and by["a"].checks_newly_failed == ["case.y"]
    assert by["b"].change == "unchanged" and by["b"].checks_newly_passed == ["case.x"]
    assert by["n"].change == "new" and by["n"].base_status is None and by["n"].metric_deltas == {}


def test_metric_delta_direction() -> None:
    assert S.metric_delta(None, 5, higher_is_better=True).delta is None
    assert S.metric_delta(5, 5, higher_is_better=True).better is None
    assert S.metric_delta(5, 3, higher_is_better=False).better is True


# ───────────────────────────── trends ─────────────────────────────


def test_trends_shape() -> None:
    a_cases = _fixture("results-v1-20261007T080848Z.json")
    b_cases = _fixture("results-v1-20261007T085022Z.json")
    runs = [_run("0199c1a2-7b3c-7d4e-8f50-0000000000c1", a_cases), _run("0199c1a2-7b3c-7d4e-8f50-0000000000c2", b_cases)]
    rows = {r.id: [(c.case_id, c.title, c.status) for c in cs] for r, cs in zip(runs, (a_cases, b_cases), strict=True)}
    t = S.trends(runs, rows, DEFAULT_EVAL_THRESHOLDS)
    assert [p.metrics["pass_rate_bp"] for p in t.runs] == [0, 5000]
    strip = {c.case_id: c.statuses for c in t.cases}
    assert strip["adversarial_kb_doc"] == ["fail", "pass"] and strip["trim_delay_actual"] == ["fail", None]
    assert t.runs[0].tokens_source == "gateway" and len(t.thresholds) == len(DEFAULT_EVAL_THRESHOLDS)


# ───────────────────────────── availability ─────────────────────────────


def _runner(configured: bool = True, online: bool = True) -> EvalRunnerInfo:
    return EvalRunnerInfo(configured=configured, online=online, runner_id="r" if online else None, version=None,
                          last_seen_at=None, suite_hash=None)


@pytest.mark.parametrize(("runner", "demo", "live", "process", "expected"), [
    (_runner(), True, True, "record_or_replay", {"replay": None, "record_or_replay": None, "record": None}),
    (_runner(configured=False), True, True, "off", dict.fromkeys(("replay", "record_or_replay", "record"),
                                                                 "runner_not_configured")),
    (_runner(online=False), True, True, "off", dict.fromkeys(("replay", "record_or_replay", "record"),
                                                             "runner_offline")),
    (_runner(), False, True, "off", dict.fromkeys(("replay", "record_or_replay", "record"), "demo_mode_off")),
    (_runner(), True, True, "replay", {"replay": None, "record_or_replay": "replay_only", "record": "replay_only"}),
    (_runner(), True, False, "record_or_replay", {"replay": None, "record_or_replay": "live_disabled",
                                                  "record": "live_disabled"}),
])
def test_mode_availability(runner: EvalRunnerInfo, demo: bool, live: bool, process: str,
                           expected: dict[str, str | None]) -> None:
    got = mode_availability(runner, demo_mode=demo, live_enabled=live, process_mode=process)
    assert {m.mode: m.reason for m in got} == expected
    assert all(m.available == (m.reason is None) for m in got)
    assert all(m.message for m in got if not m.available)
