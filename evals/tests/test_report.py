from __future__ import annotations

import json
from pathlib import Path

import pytest

from keelson_evals.assertions import Check
from keelson_evals.report import CaseResult, TurnResult, case_status, render_markdown, summarise, write_results


def _results() -> list[CaseResult]:
    ok = CaseResult("a", "A", ["golden"], case_id="c1", duration_s=100.0)
    ok.turns = [TurnResult("t", "message", ["r1"], {"first_stage": 1.0, "exposure": 10.0, "options": 90.0,
                                                    "total": 95.0}, 1, [Check("t.run", True, "ok")])]
    ok.metrics = {"entity_recall": 0.9, "numeric_grounding": 1.0, "tokens": 1000}
    bad = CaseResult("b", "B", ["adversarial"], case_id="c2")
    bad.turns = [TurnResult("t", "message", ["r2"], {"first_stage": 3.0, "exposure": None, "options": None,
                                                     "total": 20.0}, None,
                            [Check("t.latency_s", False, "first_stage: 3.0s > 2s", soft=True)])]
    bad.checks = [Check("case.guard_flagged", False, "0 quarantine flags")]
    bad.metrics = {"numeric_grounding": 0.5, "tokens": 3000}
    err = CaseResult("c", "C", error="create case: HTTP 500")
    return [ok, bad, err]


def test_case_status() -> None:
    assert case_status(None, [Check("x", True)]) == "pass"
    assert case_status(None, [Check("x", False, soft=True)]) == "pass"
    assert case_status(None, [Check("x", False)]) == "fail"
    assert case_status("boom", [Check("x", True)]) == "error"


def test_summarise() -> None:
    s = summarise(_results())
    assert (s["cases"], s["passed"], s["failed"], s["errors"]) == (3, 1, 1, 1)
    assert (s["checks"], s["checks_failed"], s["soft_failed"]) == (3, 1, 1)
    assert s["entity_recall_mean"] == 0.9 and s["numeric_grounding_mean"] == 0.75
    assert s["latency"]["first_stage_s"] == {"n": 2, "p50": 2.0, "max": 3.0}
    assert s["latency"]["options_s"] == {"n": 1, "p50": 90.0, "max": 90.0}
    assert s["tokens_total"] == 4000 and s["tokens_per_case_mean"] == 2000.0


def test_write_results_and_markdown(tmp_path: Path) -> None:
    results = _results()
    doc = {"schema": "keelson-evals/1", "started_at": "2026-10-07T10:00:00+00:00", "keelson_url": "http://k",
           "cases": [r.to_json() for r in results], "summary": summarise(results)}
    jp, mp = write_results(tmp_path / "results", doc, "20261007T100000Z")
    loaded = json.loads(jp.read_text(encoding="utf-8"))
    assert [c["status"] for c in loaded["cases"]] == ["pass", "fail", "error"]
    assert loaded["cases"][0]["turns"][0]["timings_s"]["options"] == 90.0
    md = mp.read_text(encoding="utf-8")
    assert "| `a` | PASS | 0.90 | 1.00 | 1.00s | 10.00s | 90.00s | 1000 |" in md
    assert "`b` FAIL `case.guard_flagged`: 0 quarantine flags" in md
    assert "`b` soft `t.latency_s`" in md
    assert "`c` ERROR: create case: HTTP 500" in md
    assert render_markdown(doc) == md


# ───────────────────────────── M8: keelson-evals/2 ─────────────────────────────


def test_v2_case_shape_and_integer_fields() -> None:
    from keelson_evals.report import SCHEMA

    ok = _results()[0]
    ok.turns[0].metrics.update({"trajectory": {"required": ["a:*"]}, "entities": {"golden": ["UK1"]},
                                "targets": {"latency_ms": {"first_stage": 2000}}, "trajectory_recall": 0.5})
    ok.turns[0].usage = {"runs": 1, "total": {"total_tokens": 10}, "agents": {}}
    ok.usage = {"runs": 1, "total": {"total_tokens": 10, "cost_microusd": 25}, "agents": {}}
    doc = ok.to_json()
    turn = doc["turns"][0]
    assert SCHEMA == "keelson-evals/2"
    assert turn["timings_ms"] == {"first_stage": 1000, "exposure": 10000, "options": 90000, "total": 95000}
    assert turn["trajectory"] == {"required": ["a:*"]} and turn["entities"] == {"golden": ["UK1"]}
    assert turn["targets"] == {"latency_ms": {"first_stage": 2000}}
    assert "trajectory" not in turn["metrics"] and turn["metrics"]["trajectory_recall_bp"] == 5000
    assert doc["metrics"]["entity_recall_bp"] == 9000 and doc["metrics"]["entity_recall"] == 0.9  # v1 field kept
    assert doc["duration_ms"] == 100_000 and doc["usage"]["total"]["total_tokens"] == 10
    s = summarise([ok])
    assert s["tokens_engine"] == 10 and s["cost_microusd_total"] == 25 and s["replay_misses"] == 0


def test_status_order_skipped_cancelled_error() -> None:
    assert case_status(None, [Check("x", False)], skipped="no embeddings", error_code="needs_live_embeddings") == "skipped"
    assert case_status("stopped", [Check("x", True)], error_code="cancelled") == "cancelled"
    assert case_status("turn report: LLM_REPLAY_MISS", [], error_code="replay_miss") == "error"
    miss = CaseResult("m", "M", error="turn a: the run ended with LLM_REPLAY_MISS", error_code="replay_miss")
    assert summarise([miss])["replay_misses"] == 1 and summarise([miss])["errors"] == 1


def test_v2_documents_convert_with_the_contracts_when_installed() -> None:
    contracts = pytest.importorskip("keelson_contracts.evals")
    results = _results()
    doc = {"schema": "keelson-evals/2", "started_at": "2026-10-07T10:00:00+00:00", "keelson_url": "http://k",
           "mode": "replay", "runner": "cli", "cases": [r.to_json() for r in results], "summary": summarise(results)}
    header, cases = contracts.results_doc_to_case_results(json.loads(json.dumps(doc)))
    assert header.mode == "replay" and [c.status for c in cases] == ["pass", "fail", "error"]
    assert cases[0].turns[0].timings.options_ms == 90_000 and cases[0].tokens_gateway == 1000
