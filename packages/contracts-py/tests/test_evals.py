"""M8 evals contracts (``docs/contracts/128-m8-evals-ui.md``): the results file format (v1 files of
the M4–M7 CLI, v2 of the M8 runner) and its conversion to integer units, the untrusted-input caps,
the run request rules, thresholds and the document identity used to dedupe imports."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from keelson_contracts.evals import (
    CONFIRM_LIVE_TEXT,
    DEFAULT_EVAL_THRESHOLDS,
    EVAL_METRICS,
    MAX_CASE_RESULT_BYTES,
    MAX_CHECKS,
    MAX_DOC_CASES,
    METRIC_OPS,
    EvalBudget,
    EvalCaseResult,
    EvalDocError,
    EvalLatencyStat,
    EvalLatencySummary,
    EvalRunCreate,
    EvalRunEventIn,
    EvalRunSummary,
    EvalSuiteInfo,
    EvalThreshold,
    breach_count,
    case_result_from_doc,
    case_result_to_doc,
    clean_text,
    eval_run_create_error,
    evaluate_thresholds,
    metric_value,
    ratio_to_bp,
    results_doc_header,
    results_doc_sha256,
    results_doc_to_case_results,
    seconds_to_ms,
)

FIXTURES = Path(__file__).resolve().parents[3] / "evals" / "tests" / "fixtures"
V1_A = FIXTURES / "results-v1-20261007T080848Z.json"
V1_B = FIXTURES / "results-v1-20261007T085022Z.json"
V2 = FIXTURES / "results-v2.json"


def _load(path: Path) -> dict[str, Any]:
    if not path.is_file():
        pytest.skip(f"repository fixture {path.name} not available")
    return dict(json.loads(path.read_text(encoding="utf-8")))


def _case(**over: Any) -> dict[str, Any]:
    case: dict[str, Any] = {
        "id": "trim_delay_actual", "title": "Trim delay", "tags": ["golden"], "status": "fail",
        "case_id": "01a11569-0577-726c-8a60-8a74c4e20152", "error": None, "duration_s": 226.21,
        "turns": [{
            "id": "report", "kind": "message", "run_ids": ["r1"],
            "timings_s": {"first_stage": 0.65, "exposure": 20.97, "options": 226.0, "total": 226.21},
            "plan_version": 1, "skipped": None,
            "checks": [{"name": "report.run", "ok": True, "detail": "ok", "soft": False},
                       {"name": "report.numeric_grounding", "ok": False, "detail": "20/22", "soft": False}],
            "metrics": {"numeric_grounding": 0.9091, "entity_recall": 0.9, "narrative_numbers": 22,
                        "agents_started": ["planner", "sourcing"], "tool_calls": 29, "unverified_note": False,
                        "options": ["opt-A", "do-nothing"], "recommended": {"recommended": "opt-A", "ratio": 0.1}},
        }],
        "checks": [{"name": "case.guard_flagged", "ok": True, "detail": "1 flag", "soft": False}],
        "metrics": {"tool_calls": 29, "guard_flagged": 1, "tokens": 424634, "entity_recall": 0.9,
                    "numeric_grounding": 0.9091},
    }
    case.update(over)
    return case


# ───────────────────────────── units ─────────────────────────────


@pytest.mark.parametrize(("value", "bp"), [(0.9, 9000), (0.9091, 9091), (1, 10_000), (0.0, 0), (0.33335, 3334),
                                           (1.5, None), (-0.1, None), ("x", None), (True, None), (None, None),
                                           (float("nan"), None)])
def test_ratio_to_bp_is_exact_and_half_up(value: Any, bp: int | None) -> None:
    assert ratio_to_bp(value) == bp


@pytest.mark.parametrize(("value", "ms"), [(226.21, 226_210), (0.65, 650), (2.0005, 2001), (0, 0), (-1, None),
                                           (float("inf"), None)])
def test_seconds_to_ms(value: Any, ms: int | None) -> None:
    assert seconds_to_ms(value) == ms


def test_clean_text_strips_controls_and_bidi_and_truncates() -> None:
    assert clean_text("a\x00b‮c⁦d\ne", 100) == "abcd\ne"
    assert clean_text("x" * 10, 5) == "xxxx…"
    assert clean_text("  spaced \r\n", 20) == "spaced"


# ───────────────────────────── conversion ─────────────────────────────


def test_v1_case_converts_to_integer_units() -> None:
    r = case_result_from_doc(_case())
    assert r.duration_ms == 226_210 and r.status == "fail"
    turn = r.turns[0]
    assert turn.timings.first_stage_ms == 650 and turn.timings.options_ms == 226_000
    assert turn.numeric_grounding_bp == 9091 and turn.entity_recall_bp == 9000
    assert turn.agents_started == ["planner", "sourcing"] and turn.tool_calls == 29
    # other metrics are kept as short scalars / text (never floats)
    assert turn.extra == {"unverified_note": False, "options": ["opt-A", "do-nothing"],
                          "recommended": '{"ratio":0.1,"recommended":"opt-A"}'}
    assert r.entity_recall_bp == 9000 and r.numeric_grounding_bp == 9091
    assert r.tokens_gateway == 424634 and r.usage is None and r.tokens == 424634  # gateway tokens are flagged
    assert r.keelson_case_id == "01a11569-0577-726c-8a60-8a74c4e20152"
    assert len(r.all_checks()) == 3


def test_the_committed_v1_files_and_the_v2_fixture_validate() -> None:
    header_a, cases_a = results_doc_to_case_results(_load(V1_A))
    assert header_a.doc_schema == "keelson-evals/1" and header_a.mode == "unknown" and header_a.runner == "cli"
    assert header_a.target == "http://keelson.localhost"
    assert [c.status for c in cases_a] == ["fail", "fail", "fail", "fail", "error", "fail"]
    assert cases_a[0].turns[0].timings.options_ms == 223_090
    header_b, cases_b = results_doc_to_case_results(_load(V1_B))
    assert {c.case_id: c.status for c in cases_b} == {"adversarial_kb_doc": "pass", "adversarial_supplier_message": "error"}
    assert cases_b[0].turns[0].timings.total_ms == 226_210 and cases_b[0].duration_ms == 327_100

    header, cases = results_doc_to_case_results(_load(V2))
    assert header.doc_schema == "keelson-evals/2" and header.mode == "replay" and header.runner == "cli"
    assert header.suite_hash is not None
    first = cases[0]
    assert first.usage is not None and first.usage.total.total_tokens == 42_000 and first.usage.total.cost_microusd == 12_300
    assert first.tokens_gateway == 400 and first.tokens == 42_000  # exact usage wins over the gateway delta
    assert first.turns[0].trajectory is not None and first.turns[0].entities is not None
    assert first.turns[0].budgets is not None and first.turns[0].budgets.first_stage_ms == 2000
    assert any(c.error_code == "replay_miss" and c.status == "error" for c in cases)


def test_export_round_trip_is_identity() -> None:
    for path in (V1_A, V1_B, V2):
        _header, cases = results_doc_to_case_results(_load(path))
        for c in cases:
            again = case_result_from_doc(json.loads(json.dumps(case_result_to_doc(c))))
            assert again == c, c.case_id


def test_status_is_recomputed_only_when_missing_or_unknown() -> None:
    assert case_result_from_doc(_case(status="passed")).status == "pass"
    assert case_result_from_doc(_case(status="??")).status == "fail"  # a hard check failed
    assert case_result_from_doc(_case(status=None, error="boom")).status == "error"
    ok = _case(status=None)
    ok["turns"][0]["checks"][1]["soft"] = True
    assert case_result_from_doc(ok).status == "pass"


def test_untrusted_case_text_is_sanitised_and_capped() -> None:
    bad = _case(title="Trim‮ delay\x00", tags=["a" * 60, "golden", 3])
    bad["turns"][0]["checks"] = [{"name": f"c{i}", "ok": True, "detail": "x" * 5000} for i in range(MAX_CHECKS + 50)]
    bad["turns"][0]["checks"].append({"name": "", "ok": False})  # unusable entries are dropped
    bad["turns"] *= 30
    bad["case_id"] = "javascript:alert(1)"
    r = case_result_from_doc(bad)
    assert r.title == "Trim delay"
    assert r.tags[0].endswith("…") and len(r.tags[0]) == 40 and r.tags[1] == "golden" and len(r.tags) == 2
    assert len(r.turns) == 20 and all(len(t.checks) == MAX_CHECKS for t in r.turns)
    assert r.keelson_case_id is None  # never something a link could be built from
    size = len(json.dumps(r.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    assert size <= MAX_CASE_RESULT_BYTES
    assert max(len(c.detail) for t in r.turns for c in t.checks) < 2000  # details were shortened to fit


def test_invalid_cases_and_documents_are_refused() -> None:
    with pytest.raises(EvalDocError, match="valid id"):
        case_result_from_doc(_case(id="../etc/passwd"))
    with pytest.raises(EvalDocError, match="not an object"):
        case_result_from_doc(["x"])
    good = {"schema": "keelson-evals/1", "started_at": "2026-10-07T08:08:48+00:00", "cases": [_case()]}
    with pytest.raises(EvalDocError, match="unsupported results schema"):
        results_doc_to_case_results({**good, "schema": "keelson-evals/9"})
    with pytest.raises(EvalDocError, match="started_at"):
        results_doc_to_case_results({**good, "started_at": "2026-10-07T08:08:48"})  # no time zone
    with pytest.raises(EvalDocError, match="no cases"):
        results_doc_to_case_results({**good, "cases": []})
    with pytest.raises(EvalDocError, match=f"more than {MAX_DOC_CASES}"):
        results_doc_to_case_results({**good, "cases": [_case(id=f"c{i}") for i in range(MAX_DOC_CASES + 1)]})
    with pytest.raises(EvalDocError, match="repeat"):
        results_doc_to_case_results({**good, "cases": [_case(), _case()]})
    with pytest.raises(EvalDocError, match="not a JSON object"):
        results_doc_header([good])


def test_header_labels_never_become_links_or_unknown_modes() -> None:
    h = results_doc_header({"schema": "keelson-evals/2", "started_at": "2026-10-07T08:08:48Z", "mode": "live!!",
                            "runner": "x", "suite_hash": "nothex", "keelson_url": "javascript:‮alert(1)"})
    assert h.mode == "unknown" and h.runner == "unknown" and h.suite_hash is None
    assert h.target == "javascript:alert(1)"  # a label only: never fetched or linked by the engine / UI


def test_doc_identity_ignores_layout_schema_and_labels() -> None:
    doc = _load(V1_B)
    header, cases = results_doc_to_case_results(doc)
    sha = results_doc_sha256(header, cases)
    relabelled = copy.deepcopy(doc)
    relabelled["keelson_url"] = "http://elsewhere.test"
    relabelled["merxa_url"] = None
    reserialised = json.loads(json.dumps(relabelled, indent=None))
    assert results_doc_sha256(*results_doc_to_case_results(reserialised)) == sha
    exported = {"schema": "keelson-evals/2", "started_at": doc["started_at"], "finished_at": doc["finished_at"],
                "mode": "unknown", "cases": [case_result_to_doc(c) for c in cases]}
    assert results_doc_sha256(*results_doc_to_case_results(exported)) == sha
    changed = copy.deepcopy(doc)
    changed["cases"][0]["status"] = "fail"
    assert results_doc_sha256(*results_doc_to_case_results(changed)) != sha


# ───────────────────────────── requests ─────────────────────────────

CAPS = EvalBudget(max_tokens=3_000_000, max_cost_microusd=5_000_000)


def test_run_create_shape() -> None:
    req = EvalRunCreate.model_validate({"acknowledge_reset": True})
    assert req.mode == "replay" and req.case_ids is None and eval_run_create_error(req, CAPS) is None
    with pytest.raises(ValidationError):
        EvalRunCreate.model_validate({})  # replay without acknowledging the Merxa reset
    with pytest.raises(ValidationError):
        EvalRunCreate.model_validate({"acknowledge_reset": False})
    with pytest.raises(ValidationError):
        EvalRunCreate.model_validate({"acknowledge_reset": True, "case_ids": ["a", "a"]})
    with pytest.raises(ValidationError):
        EvalRunCreate.model_validate({"acknowledge_reset": True, "case_ids": []})
    with pytest.raises(ValidationError):
        EvalRunCreate.model_validate({"acknowledge_reset": True, "mode": "off"})
    with pytest.raises(ValidationError):
        EvalRunCreate.model_validate({"acknowledge_reset": True, "extra": 1})


def test_live_runs_need_the_typed_confirmation_and_a_budget_within_the_caps() -> None:
    base = {"acknowledge_reset": True, "mode": "record_or_replay"}
    budget = {"max_tokens": 100_000, "max_cost_microusd": 200_000}
    assert eval_run_create_error(EvalRunCreate.model_validate({**base, "budget": budget}), CAPS)[0] == "confirm_required"  # type: ignore[index]
    assert eval_run_create_error(EvalRunCreate.model_validate({**base, "confirm_live": "run live", "budget": budget}),
                                 CAPS)[0] == "confirm_required"  # type: ignore[index]
    assert eval_run_create_error(EvalRunCreate.model_validate({**base, "confirm_live": CONFIRM_LIVE_TEXT}),
                                 CAPS)[0] == "budget_required"  # type: ignore[index]
    too_high = {"max_tokens": 3_000_001, "max_cost_microusd": 1}
    assert eval_run_create_error(EvalRunCreate.model_validate({**base, "confirm_live": CONFIRM_LIVE_TEXT,
                                                               "budget": too_high}), CAPS)[0] == "budget_too_high"  # type: ignore[index]
    ok = EvalRunCreate.model_validate({**base, "mode": "record", "confirm_live": CONFIRM_LIVE_TEXT, "budget": budget})
    assert eval_run_create_error(ok, CAPS) is None
    with pytest.raises(ValidationError):
        EvalBudget.model_validate({"max_tokens": 0, "max_cost_microusd": 1})
    with pytest.raises(ValidationError):
        EvalBudget.model_validate({"max_tokens": 1.5, "max_cost_microusd": 1})


def test_runner_events_are_small_scalars() -> None:
    EvalRunEventIn.model_validate({"type": "turn_finished", "case_id": "trim_delay_actual", "turn_id": "report",
                                   "data": {"checks_failed": 2, "final_code": "LLM_REPLAY_MISS"}})
    with pytest.raises(ValidationError):
        EvalRunEventIn.model_validate({"type": "turn_finished", "data": {"nested": {"x": 1}}})
    with pytest.raises(ValidationError):
        EvalRunEventIn.model_validate({"type": "turn_finished", "data": {f"k{i}": i for i in range(21)}})
    with pytest.raises(ValidationError):
        EvalRunEventIn.model_validate({"type": "shell"})


def test_suite_ids_are_unique() -> None:
    case = {"id": "a", "title": "A", "tags": [], "turns": 1, "turn_kinds": ["message"],
            "needs_live_embeddings": False, "injects": False}
    EvalSuiteInfo.model_validate({"suite_hash": "0" * 64, "cases": [case]})
    with pytest.raises(ValidationError, match="unique"):
        EvalSuiteInfo.model_validate({"suite_hash": "0" * 64, "cases": [case, case]})


# ───────────────────────────── thresholds ─────────────────────────────


def _summary(**over: Any) -> EvalRunSummary:
    stat = EvalLatencyStat(n=0, p50_ms=None, max_ms=None)
    data: dict[str, Any] = {
        "cases": 4, "passed": 3, "failed": 1, "errors": 0, "skipped": 0, "cancelled": 0, "checks": 40,
        "checks_failed": 1, "soft_failed": 0, "pass_rate_bp": 7500, "adversarial_cases": 1,
        "adversarial_pass_rate_bp": 10_000, "entity_recall_mean_bp": 9000, "numeric_grounding_mean_bp": 9500,
        "numeric_grounding_min_bp": 9091, "trajectory_recall_mean_bp": None,
        "latency": EvalLatencySummary(first_stage=EvalLatencyStat(n=4, p50_ms=500, max_ms=1590), exposure=stat,
                                      options=EvalLatencyStat(n=2, p50_ms=200_000, max_ms=223_090), total=stat),
        "tokens_total": 1_000_000, "tokens_per_case_mean": 250_000, "tokens_source": "engine",
        "cost_microusd_total": None, "cost_per_case_mean_microusd": None, "llm_calls_live": 10,
        "llm_calls_replayed": 0, "replay_misses": 0,
    }
    data.update(over)
    return EvalRunSummary.model_validate(data)


def test_threshold_outcomes_and_modes() -> None:
    s = _summary()
    live = {r.metric: r.outcome for r in evaluate_thresholds(s, DEFAULT_EVAL_THRESHOLDS, "record_or_replay")}
    assert live == {
        "pass_rate_bp": "breach", "adversarial_pass_rate_bp": "ok", "entity_recall_mean_bp": "ok",
        "numeric_grounding_min_bp": "breach", "trajectory_recall_mean_bp": "not_measured",
        "first_stage_p50_ms": "ok", "exposure_max_ms": "not_measured", "options_max_ms": "breach",
        "tokens_per_case_mean": "ok", "cost_per_case_mean_microusd": "not_measured",
    }
    replay = {r.metric: r.outcome for r in evaluate_thresholds(s, DEFAULT_EVAL_THRESHOLDS, "replay")}
    # latency, tokens and cost targets only mean something for live runs
    assert {m for m, o in replay.items() if o == "not_applicable"} == {
        "first_stage_p50_ms", "exposure_max_ms", "options_max_ms", "tokens_per_case_mean",
        "cost_per_case_mean_microusd"}
    assert breach_count(evaluate_thresholds(s, DEFAULT_EVAL_THRESHOLDS, "replay")) == 2
    assert breach_count(evaluate_thresholds(s, DEFAULT_EVAL_THRESHOLDS, "unknown")) == 3  # imported CLI runs: live
    assert [metric_value(s, m) for m in ("options_max_ms", "first_stage_p50_ms")] == [223_090, 500]
    with pytest.raises(ValueError):
        metric_value(s, "nope")


def test_default_thresholds_cover_every_metric_in_the_right_direction() -> None:
    assert [t.metric for t in DEFAULT_EVAL_THRESHOLDS] == list(EVAL_METRICS)
    assert all(t.op == METRIC_OPS[t.metric] for t in DEFAULT_EVAL_THRESHOLDS)
    with pytest.raises(ValidationError, match="min target"):
        EvalThreshold(metric="pass_rate_bp", op="max", value=1, modes=["replay"])
    with pytest.raises(ValidationError, match="unique"):
        EvalThreshold(metric="pass_rate_bp", op="min", value=1, modes=["replay", "replay"])


def test_case_result_models_reject_floats_and_unknown_fields() -> None:
    r = case_result_from_doc(_case()).model_dump(mode="json")
    with pytest.raises(ValidationError):
        EvalCaseResult.model_validate({**r, "duration_ms": 1.5})
    with pytest.raises(ValidationError):
        EvalCaseResult.model_validate({**r, "surprise": True})
    bad = copy.deepcopy(r)
    bad["turns"][0]["entity_recall_bp"] = 10_001
    with pytest.raises(ValidationError):
        EvalCaseResult.model_validate(bad)
