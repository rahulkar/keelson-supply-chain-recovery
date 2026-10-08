from __future__ import annotations

from pathlib import Path

import pytest

import run_live
from keelson_evals.cases import CaseError, check_names, is_soft, load_case, load_cases, validate_cases

EVALS = Path(__file__).resolve().parents[1]
CASES = EVALS / "cases"
REPO = EVALS.parent

REQUIRED_CASES = {
    "trim_delay_actual", "suez_whatif", "store_shortage_question", "adversarial_supplier_message",
    "adversarial_kb_doc", "followup_delay_doubles",
}


def test_shipped_cases_load_and_validate() -> None:
    cases = load_cases(CASES)
    assert {c.id for c in cases} >= REQUIRED_CASES
    assert validate_cases(cases, REPO) == []


def test_shipped_cases_cover_the_contract() -> None:
    by_id = {c.id: c for c in load_cases(CASES)}
    trim = by_id["trim_delay_actual"].turns[0].expect
    assert trim.affected_entities and trim.options and trim.recommended_lost_units and trim.numeric_grounding
    lat = trim.latency_s
    assert lat is not None and (lat.first_stage, lat.exposure, lat.options) == (2, 30, 180)
    suez = by_id["suez_whatif"]
    assert suez.turns[0].expect.case_mode == "what_if" and suez.turns[0].expect.exposure is not None
    assert {a.type: a.enabled for a in suez.turns[0].expect.actions}["apply"] is False
    assert suez.turns[1].action is not None and suez.turns[1].action.type == "promote"
    q = by_id["store_shortage_question"].turns[0]
    assert q.approach == "none" and q.expect.answer is not None and q.expect.answer.no_plan
    for adv, tool in (("adversarial_supplier_message", "list_supplier_messages"), ("adversarial_kb_doc", "kb.search")):
        c = by_id[adv]
        assert c.expect.guard_flagged is not None and c.expect.guard_flagged.source_tools == [tool]
        assert c.expect.no_write_tool_calls and c.expect.no_commit_without_human and c.expect.narrative_excludes
        assert c.expect.no_write_tools_bound and c.expect.no_untrusted_tool_calls
        assert c.teardown.reset_merxa  # injected text / probe change sets never outlive the case
        assert "conflict" in c.expect.committed_statuses
    assert by_id["adversarial_supplier_message"].setup.inject[0].kind == "supplier_message"
    assert by_id["adversarial_kb_doc"].setup.kb_upload is not None
    f = by_id["followup_delay_doubles"].turns[1].expect
    assert (f.plan_version_delta, f.kpis_changed, f.what_changed) == (1, True, True)


def test_dry_run_cli_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert run_live.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "OK: " in out and "trim_delay_actual" in out


def _write(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / f"{name}.yaml"
    p.write_text(body, encoding="utf-8")
    return p


MINIMAL = """
id: {id}
title: T
turns:
  - id: a
    message: hello
"""


def test_minimal_case_defaults(tmp_path: Path) -> None:
    case = load_case(_write(tmp_path, "mini", MINIMAL.format(id="mini")))
    assert case.setup.reset_merxa and case.expect.no_write_tool_calls and case.turns[0].approach == "confirm"
    assert case.turns[0].expect.run == "success"
    assert {"a.run", "a.no_placeholders", "case.no_write_tool_calls", "case.no_write_tools_bound"} <= check_names(case)


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        (MINIMAL.format(id="other"), "must equal the file name"),
        ("id: bad\ntitle: T\nturns: []\n", "turns"),
        ("id: bad\ntitle: T\nturns:\n  - id: a\n    message: m\n    action: {type: apply}\n", "exactly one"),
        ("id: bad\ntitle: T\nturns:\n  - id: a\n    action: {type: apply}\n", "first turn"),
        ("id: bad\ntitle: T\nturns:\n  - id: a\n    message: m\n    expect: {nope: 1}\n", "nope"),
        ("id: bad\ntitle: T\nturns:\n  - id: a\n    message: m\n  - id: a\n    message: n\n", "duplicate"),
        ("id: bad\ntitle: T\nturns:\n  - id: a\n    message: m\n    expect: {plan_version_delta: 1}\n",
         "previous turn"),
        ("id: bad\ntitle: T\nturns:\n  - id: a\n    message: m\nexpect:\n  narrative_code_limit:\n"
         "    - {pattern: '(', max: 1}\n", "does not compile"),
        ("id: bad\ntitle: T\nsetup:\n  inject:\n    - kind: supplier_message\n      payload: {subject: s}\n"
         "turns:\n  - id: a\n    message: m\n", "supplier_code"),
        ("id: bad\ntitle: T\nturns:\n  - id: a\n    message: m\n    expect:\n"
         "      recommended_lost_units: {baseline: do-nothing}\n", "max and/or max_ratio"),
        ("- just a list\n", "mapping"),
        ("id: bad\ntitle: T\nturns:\n  - id: a\n    message: m\n    expect:\n"
         "      exposure: {dc_codes: [DE1], weeks: [2026-46]}\n", "weeks"),
    ],
)
def test_invalid_case_files(tmp_path: Path, body: str, fragment: str) -> None:
    with pytest.raises(CaseError) as exc:
        load_case(_write(tmp_path, "bad", body))
    assert fragment in str(exc.value)


def test_validate_cases_offline_problems(tmp_path: Path) -> None:
    body = """
id: v
title: T
setup:
  kb_upload:
    kb_name: temp
    files: [missing/doc.md, evals/run_live.py]
turns:
  - id: a
    message: m
soft: [a.latency_s, a.run, case.kb_*]
"""
    case = load_case(_write(tmp_path, "v", body))
    problems = validate_cases([case], REPO)
    assert any("missing/doc.md not found" in p for p in problems)
    assert any("run_live.py is not .md/.txt/.pdf" in p for p in problems)
    assert any("'a.latency_s' matches no check" in p for p in problems)
    assert not any("'a.run'" in p or "case.kb_*" in p for p in problems)


def test_is_soft_patterns(tmp_path: Path) -> None:
    case = load_case(_write(tmp_path, "s", MINIMAL.format(id="s") + "soft: [a.agent:*, case.guard_flagged]\n"))
    assert is_soft(case, "a.agent:sourcing") and is_soft(case, "case.guard_flagged")
    assert not is_soft(case, "a.run") and not is_soft(case, "case.guard_flagged_x")


def test_load_cases_filters_and_rejects_unknown() -> None:
    assert [c.id for c in load_cases(CASES, ["suez_whatif"])] == ["suez_whatif"]
    with pytest.raises(CaseError, match="unknown case ids"):
        load_cases(CASES, ["nope"])


def test_load_cases_empty_dir(tmp_path: Path) -> None:
    with pytest.raises(CaseError, match="no case files"):
        load_cases(tmp_path)


def test_dry_run_cli_reports_invalid(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write(tmp_path, "bad", MINIMAL.format(id="other"))
    assert run_live.main(["--dry-run", "--cases-dir", str(tmp_path)]) == 1
    assert "INVALID" in capsys.readouterr().out


def test_signal_turn_schema() -> None:
    import pytest
    from keelson_evals.cases import Case

    base = {"id": "s", "title": "s", "turns": [{"id": "a", "message": "hi"}]}
    ok = Case.model_validate({**base, "turns": [*base["turns"], {"id": "b", "signal": {"inject": {
        "kind": "lane_capacity", "payload": {"lane_code": "CGP-UK1-SA", "factor_bp": 3000, "weeks": 3}}}}]})
    assert ok.turns[1].kind == "signal" and ok.turns[1].signal is not None
    with pytest.raises(ValueError, match="lane_capacity payload needs"):
        Case.model_validate({**base, "turns": [*base["turns"], {"id": "b", "signal": {"inject": {
            "kind": "lane_capacity", "payload": {"lane_code": "X", "factor_bp": "30%", "weeks": 3}}}}]})
    with pytest.raises(ValueError, match="exactly one of"):
        Case.model_validate({**base, "turns": [{"id": "a", "message": "hi", "signal": {"inject": {
            "kind": "trim_delay"}}}]})
    with pytest.raises(ValueError, match="first turn must be a message"):
        Case.model_validate({**base, "turns": [{"id": "b", "signal": {"inject": {"kind": "trim_delay"}}}]})
