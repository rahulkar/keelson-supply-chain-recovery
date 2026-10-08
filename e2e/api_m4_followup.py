#!/usr/bin/env python3
"""M4 follow-ups — targeted re-planning through the API (docs/contracts/90 §6).

Runs like ``api_thin_slice.py`` (throwaway Python container on ``keelson_edge``, real Gemini via
LiteLLM; it imports that script's HTTP helpers). Story:

 1. reset the demo, then ``priya``'s trim-delay analysis (approach confirmed; values left
    unconfirmed are confirmed first) → plan vN
 2. "What if the delay doubles?" → RESIMULATE: no approach card, no specialist re-runs
    (investigations reused), plan vN+1 with changed KPIs and a "What changed" line on the
    narrative (``narrative@1.what_changed``)
 3. "Why not air freight?" → EXPLAIN: an answer from the plan's options and scorecards, no new
    plan version, no options / scenario-compare blocks and no specialists (the optimizer is not
    re-run: an option already uses sea-air)

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
from api_thin_slice import KEELSON, E2EFailure, ascii_text, expect, heading, say  # noqa: E402

RESIMULATE = "What if the delay doubles?"
EXPLAIN = "Why not air freight?"
PLAN_BLOCKS = ("recommended_option", "scenario_compare", "plan_diff")


def _final_kpis(case: dict[str, Any]) -> dict[str, Any]:
    strips = [b for b in ts.blocks_by_kind(case).get("kpi_strip") or []
              if not any(i.get("preliminary") for i in b["props"]["items"])]
    expect(strips, "no final kpi_strip")
    return {str(i["key"]): i.get("value") for i in strips[-1]["props"]["items"]}


def _rec(case: dict[str, Any]) -> dict[str, Any]:
    return dict(ts.blocks_by_kind(case)["recommended_option"][-1]["props"])


def followup(args: argparse.Namespace, an: ts.Analysis, text: str) -> ts.RunResult:
    priya = ts.keelson_login("priya")
    after = int(ts.get_case(priya, an.case_id).get("last_event_seq") or an.last_seq)
    ts.CLOCK.marks.pop("followup_posted", None)
    ts.CLOCK.mark("followup_posted")
    r = priya.client.post(f"{KEELSON}/api/cases/{an.case_id}/messages", json={"text": text},
                          headers=priya.headers(idem=True))
    expect(r.status_code == 202, f"post follow-up: HTTP {r.status_code} {r.text[:200]}")
    res = ts.follow_run(priya, an.case_id, str(r.json()["run_id"]), after, timeout_s=args.run_timeout)
    ts.expect_success(res, f"follow-up run ({text!r})")
    expect(res.outcome.get("type") == "success",
           f"follow-up {text!r} paused ({[i.get('reason') for i in res.interrupts]}); expected no approach card")
    an.last_seq = max(an.last_seq, res.last_seq)
    return res


def resimulate(args: argparse.Namespace, an: ts.Analysis) -> dict[str, float]:
    heading(f"Planner: {RESIMULATE!r} (RESIMULATE)")
    priya = ts.keelson_login("priya")
    before = ts.get_case(priya, an.case_id)
    kpis_before, rec_before = _final_kpis(before), _rec(before)
    t0 = ts.CLOCK.now()
    res = followup(args, an, RESIMULATE)
    took = ts.CLOCK.now() - t0
    expect("approach_card" not in res.blocks, "the RESIMULATE follow-up showed an approach card")
    rerun = [a for a in ts.agents_started(res) if a in ts.SPECIALISTS]
    expect(not rerun, f"RESIMULATE re-ran specialists {rerun}; investigations should be reused")
    expect("recommended_option" in res.blocks, f"no new recommended_option (blocks {sorted(res.blocks)})")
    case = ts.get_case(priya, an.case_id)
    rec = _rec(case)
    expect(int(rec["plan_version"]) == int(rec_before["plan_version"]) + 1,
           f"plan v{rec['plan_version']} after RESIMULATE (was v{rec_before['plan_version']})")
    kpis = _final_kpis(case)
    say(f"kpis before {kpis_before}")
    say(f"kpis after  {kpis}")
    expect(kpis != kpis_before, "the exposure KPIs did not change when the delay doubled")
    narratives = ts.blocks_by_kind(case).get("narrative") or []
    changed = str((narratives[-1]["props"] if narratives else {}).get("what_changed") or "")
    say(f"what changed: {ascii_text(changed, 300)}")
    expect(changed.startswith(f"Plan v{rec_before['plan_version']} → v{rec['plan_version']}"),
           f"no 'What changed' line on the narrative: {changed!r}")
    expect(not ts.PLACEHOLDER_RE.search(changed), "what_changed has placeholders")
    ts.check_m3_analysis(priya, case)
    an.plan_version = int(rec["plan_version"])
    return {"resimulate_s": took}


def explain(args: argparse.Namespace, an: ts.Analysis) -> dict[str, float]:
    heading(f"Planner: {EXPLAIN!r} (EXPLAIN)")
    priya = ts.keelson_login("priya")
    before = ts.get_case(priya, an.case_id)
    rec_before = _rec(before)
    options = {str(o["id"]): o for o in ts.blocks_by_kind(before)["scenario_compare"][-1]["props"]["options"]}
    air = [oid for oid, o in options.items() if "expedite_mode_shift" in (o.get("levers") or [])]
    say(f"options using sea-air / air freight: {air}")
    t0 = ts.CLOCK.now()
    res = followup(args, an, EXPLAIN)
    took = ts.CLOCK.now() - t0
    expect(not (set(PLAN_BLOCKS) & set(res.blocks)), f"EXPLAIN emitted plan blocks {sorted(res.blocks)}")
    expect("approach_card" not in res.blocks, "the EXPLAIN follow-up showed an approach card")
    rerun = [a for a in ts.agents_started(res) if a in ts.SPECIALISTS]
    expect(not rerun, f"EXPLAIN ran specialists {rerun}")
    case = ts.get_case(priya, an.case_id)
    expect(int(_rec(case)["plan_version"]) == int(rec_before["plan_version"]),
           "EXPLAIN created a new plan version (the optimizer re-ran)")
    answer = res.text.strip()
    say(f"answer: {ascii_text(answer, 400)}")
    expect(answer, "EXPLAIN produced no answer text")
    expect(not ts.PLACEHOLDER_RE.search(answer), "the explanation has placeholders")
    if air:
        titles = [str(options[oid].get("title") or "") for oid in air]
        rec_title = str(rec_before["option"]["title"])
        expect(any(t and t in answer for t in [*titles, rec_title]) or "sea-air" in answer.lower(),
               f"the explanation names neither the sea-air options {titles} nor the recommendation")
    return {"explain_s": took}


def run_followups(args: argparse.Namespace) -> dict[str, float]:
    if not os.environ.get("DEMO_USER_PASSWORD", ""):
        print("DEMO_USER_PASSWORD is not set", file=sys.stderr)
        raise SystemExit(2)
    ts.reset_demo(args)
    an = ts.analyse(args, title="E2E follow-ups")
    an = ts.confirm_pending_values(args, an)
    out = {"options_s": ts.CLOCK.marks.get("options", 0) - an.t_msg}
    out.update(resimulate(args, an))
    out.update(explain(args, an))
    out["total_s"] = ts.CLOCK.now()
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ts.add_common_args(parser)
    args = parser.parse_args()
    try:
        result = run_followups(args)
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
