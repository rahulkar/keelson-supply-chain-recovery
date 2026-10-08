#!/usr/bin/env python3
"""M4 Act 2 — the Suez what-if, end to end through the API (docs/contracts/90 §8).

Runs like ``api_thin_slice.py`` (throwaway Python container on ``keelson_edge``, real Gemini via
LiteLLM; it imports that script's HTTP helpers). Story:

 1. reset the demo (Merxa golden seed → scn resync → marco's Merxa link disconnected)
 2. ``priya`` asks "What if the Suez Canal closes for 3 weeks?" → the approach card carries the
    what-if badge (``mode: what_if``) and the planner's checks → confirm → options compared;
    the case is ``what_if``. Before confirming, a typed **Edit** (``edits: [{key, value_int}]``,
    as the web sends it) lengthens the closure by 7 days and the re-framed card must show it
 3. the recommended option: **Apply disabled** ("What-if case: promote it to an actual case
    first."), **Promote** offered; an ``apply`` sent straight to the API is refused by the
    server (no approval card, no change set)
 4. action ``promote`` → the case becomes ``actual``, plan v+1 re-simulated on the current
    snapshot, Promote gone, Apply enabled (values left unconfirmed are confirmed first)
 5. apply the recommended option → human approval (approver) → marco connects Merxa and
    approves → commit tracker verified → Merxa read-back: ONE applied change set attributed to
    Marco Rossi via Keelson, every item applied, audit rows via Keelson

Exit status 0 = pass; 1 = an expectation failed; 2 = usage / environment error.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_thin_slice as ts  # noqa: E402
from api_thin_slice import KEELSON, MERXA, E2EFailure, ascii_text, expect, heading, say  # noqa: E402

PROMPT = "What if the Suez Canal closes for 3 weeks?"
PROMOTE_REASON = "promote"


def _rec(case: dict[str, Any]) -> dict[str, Any]:
    kinds = ts.blocks_by_kind(case)
    expect("recommended_option" in kinds, f"no recommended_option block (have {sorted(kinds)})")
    return dict(kinds["recommended_option"][-1]["props"])


def _action(rec: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((a for a in rec.get("actions") or [] if a.get("type") == kind), None)


def check_whatif_plan(an: ts.Analysis) -> None:
    heading("Check the what-if plan")
    priya = ts.keelson_login("priya")
    case = ts.get_case(priya, an.case_id)
    expect(case["case"].get("mode") == "what_if", f"case mode {case['case'].get('mode')!r}, expected what_if")
    expect(an.approach.get("mode") == "what_if", "the approach card has no what-if badge (mode != what_if)")
    rec = _rec(case)
    apply = _action(rec, "apply") or {}
    promote = _action(rec, "promote") or {}
    say(f"recommended {rec['option']['id']} what_if={rec.get('what_if')} apply.enabled={apply.get('enabled')} "
        f"reason={ascii_text(apply.get('disabled_reason'))!r} promote.enabled={promote.get('enabled')}")
    expect(rec.get("what_if") is True, "recommended_option is not marked what_if")
    expect(apply and apply.get("enabled") is False, f"Apply should be disabled on a what-if case: {apply}")
    expect(PROMOTE_REASON in str(apply.get("disabled_reason") or "").lower(),
           f"the Apply reason does not mention promote: {apply.get('disabled_reason')!r}")
    expect(promote and promote.get("enabled") is True, f"Promote is not offered: {rec.get('actions')}")
    timeline = ts.blocks_by_kind(case).get("stockout_timeline") or []
    expect(timeline, "no stockout_timeline block")
    data = ts.fetch_block_data(priya, timeline[-1])
    short_rows = [r for r in data.get("rows") or [] if any(c.get("status") == "short" for c in r.get("cells") or [])]
    weeks = sorted({c["week"] for r in short_rows for c in r["cells"] if c.get("status") == "short"})
    say(f"timeline: {len(short_rows)} row(s) short, weeks {weeks[:6]}")
    expect(short_rows, "the Suez closure shows no shortage on the timeline")


def apply_is_refused(args: argparse.Namespace, an: ts.Analysis) -> None:
    """The server refuses Apply on a what-if case too, not only the disabled button (90 §8)."""
    heading("Planner: Apply on the what-if case is refused")
    priya = ts.keelson_login("priya")
    r = priya.client.post(
        f"{KEELSON}/api/cases/{an.case_id}/actions",
        json={"type": "apply", "option_id": an.recommended, "plan_version": an.plan_version},
        headers=priya.headers(idem=True),
    )
    expect(r.status_code == 202, f"apply action: HTTP {r.status_code} {r.text[:300]}")
    res = ts.follow_run(priya, an.case_id, str(r.json()["run_id"]), an.last_seq, timeout_s=args.run_timeout)
    ts.expect_success(res, "refused apply run")
    expect(res.outcome.get("type") == "success", f"the refused apply paused: {res.outcome}")
    expect("approval_card" not in res.blocks and "commit_tracker" not in res.blocks,
           f"a what-if apply produced {sorted(res.blocks)}")
    expect("promote" in res.text.lower(), f"the refusal does not ask to promote: {ascii_text(res.text, 300)!r}")
    say(f"refused: {ascii_text(res.text, 200)}")
    an.last_seq = max(an.last_seq, res.last_seq)


def promote(args: argparse.Namespace, an: ts.Analysis) -> ts.Analysis:
    heading("Planner: promote to an actual case")
    priya = ts.keelson_login("priya")
    ts.CLOCK.mark("promote_requested")
    r = priya.client.post(
        f"{KEELSON}/api/cases/{an.case_id}/actions",
        json={"type": "promote", "option_id": an.recommended, "plan_version": an.plan_version},
        headers=priya.headers(idem=True),
    )
    expect(r.status_code == 202, f"promote action: HTTP {r.status_code} {r.text[:300]}")
    res = ts.follow_run(priya, an.case_id, str(r.json()["run_id"]), an.last_seq, timeout_s=args.run_timeout)
    ts.expect_success(res, "promote run")
    expect(res.outcome.get("type") == "success", f"promote run ended with {res.outcome}")
    ts.CLOCK.mark("promoted")
    rerun = [a for a in ts.agents_started(res) if a in ts.SPECIALISTS]
    expect(not rerun, f"promote re-ran specialists {rerun} (only impact + simulate should run)")
    case = ts.get_case(priya, an.case_id)
    expect(case["case"].get("mode") == "actual", f"case mode after promote {case['case'].get('mode')!r}")
    rec = _rec(case)
    expect(int(rec["plan_version"]) == an.plan_version + 1, f"plan v{rec['plan_version']} after promote")
    expect(not rec.get("what_if"), "recommended_option still marked what_if after promote")
    expect(_action(rec, "promote") is None, "Promote still offered after promote")
    options = ts.check_m3_analysis(priya, case)
    say(f"plan v{rec['plan_version']}: recommended {rec['option']['id']} '{rec['option']['title']}' (actual)")
    return ts.Analysis(an.case_id, int(rec["plan_version"]), case, options, str(rec["option"]["id"]), an.t_msg,
                       int(case.get("last_event_seq") or res.last_seq), an.agents, an.approach)


def choose_apply_option(an: ts.Analysis) -> str:
    """The recommended option when it changes something; else the best option with changes."""
    rec = an.options.get(an.recommended) or {}
    if an.recommended != ts.DO_NOTHING and (rec.get("scorecard") or {}).get("changes"):
        return an.recommended
    ranked = sorted(
        (o for oid, o in an.options.items() if oid != ts.DO_NOTHING and (o.get("scorecard") or {}).get("changes")
         and not o.get("guided_only")),
        key=lambda o: -int((o.get("scorecard") or {}).get("net_benefit_cents") or 0),
    )
    expect(ranked, "no option with changes to apply after promote")
    return str(ranked[0]["id"])


def verify_change_set(ext_code: str) -> None:
    heading("Merxa: read back")
    client = ts.merxa_login("merxa.admin")
    r = client.get(f"{MERXA}/v1/change-sets/{ext_code}")
    expect(r.status_code == 200, f"merxa change set {ext_code}: HTTP {r.status_code}")
    cs = r.json()
    items = cs.get("items") or []
    say(f"merxa   : {cs['code']} status={cs['status']} items={len(items)} ops={sorted({i.get('op') for i in items})}")
    say(f"merxa   : attribution '{ascii_text(cs.get('attribution_text'))}'")
    expect(cs["status"] == "applied", f"merxa change set status is {cs['status']!r}")
    attribution = str(cs.get("attribution_text") or "")
    expect("Marco Rossi" in attribution and "via Keelson" in attribution, f"attribution {attribution!r}")
    expect(items and all(i.get("status") == "applied" for i in items), "not every change set item is applied")
    r = client.get(f"{MERXA}/v1/audit-log", params={"change_set_code": cs["code"], "limit": 200})
    expect(r.status_code == 200, f"merxa audit log: HTTP {r.status_code}")
    rows = [row for row in r.json().get("items") or [] if row.get("approval_ref") == cs.get("approval_ref")]
    say(f"merxa   : {len(rows)} audit row(s) via {sorted({str(x.get('via')) for x in rows})}")
    expect(rows and all(row.get("via") == "Keelson" for row in rows), "audit rows missing or not via Keelson")


def run_whatif(args: argparse.Namespace) -> dict[str, float]:
    if not os.environ.get("DEMO_USER_PASSWORD", ""):
        print("DEMO_USER_PASSWORD is not set", file=sys.stderr)
        raise SystemExit(2)
    ts.reset_demo(args)
    an = ts.analyse(args, title="E2E Suez what-if", prompt=PROMPT, mode="what_if", typed_edit=args.typed_edit)
    ts.check_m4_investigation(an, min_specialists=args.min_specialists)
    check_whatif_plan(an)
    apply_is_refused(args, an)
    an = promote(args, an)
    an = ts.confirm_pending_values(args, an)
    option_id = choose_apply_option(an)
    approval = ts.request_apply(args, an, option_id)
    ext_code = ts.approve_and_commit(args, an, approval)
    verify_change_set(ext_code)
    m = ts.CLOCK.marks
    edit_s = m["edit_done"] - m["edit_requested"] if "edit_done" in m else 0.0
    return {
        "first_stage_s": m.get("first_stage", 0) - an.t_msg,
        "exposure_s": m.get("exposure", 0) - an.t_msg,
        "options_s": m.get("options", 0) - an.t_msg,
        "typed_edit_s": edit_s,
        "options_excl_edit_s": m.get("options", 0) - an.t_msg - edit_s,
        "analysis_total_s": m["analysis_done"] - an.t_msg,
        "promote_s": m["promoted"] - m["promote_requested"],
        "apply_to_approval_card_s": m["approval_requested"] - m["apply_requested"],
        "commit_s": m["verified"] - m["approved"],
        "total_s": ts.CLOCK.now(),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ts.add_common_args(parser)
    parser.add_argument("--min-specialists", type=int, default=2,
                        help="specialist agents the plan must consult (default 2: a closure needs logistics + inventory)")
    parser.add_argument("--no-typed-edit", dest="typed_edit", action="store_false",
                        help="confirm the first approach card as is (default: change one value with a typed Edit first)")
    args = parser.parse_args()
    try:
        result = run_whatif(args)
    except E2EFailure as exc:
        print(f"\nFAIL: {exc}", flush=True)
        return 1
    except ts.httpx.HTTPError as exc:
        print(f"\nFAIL: HTTP error {type(exc).__name__}: {exc}", flush=True)
        return 1
    heading("PASS")
    for key, value in result.items():
        print(f"  {key:26s} {value:7.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
