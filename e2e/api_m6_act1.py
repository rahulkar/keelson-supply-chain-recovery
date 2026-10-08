"""M6 Act 1, live (docs/contracts/100 §10): typhoon pack → alert → open case → signal run → approach card.

Needs model credits (the signal run's planner is a live call; reruns are replayed for free with the
compose default ``LLM_REPLAY_MODE=record_or_replay``). Runs inside the engine image like the M5
scripts (``docker compose run engine-api``, see e2e/README.md); ``scripts/verify-live.sh`` runs it
as step ``m6-act1`` (twice).

1. Demo reset (``ava``) and wait for the post-reset scn sync to push the tenant network to the hub.
2. Presenter starts ``typhoon-hcmc`` (advisory 1, Orange) → the alert reaches the inbox with the
   VNSGN / F-VN-01 / SUP-VN-01 exposure.
3. ``priya`` opens the case from the alert → one case + its ``signal`` run.
4. Follow the run on the case stream: it frames the event without a model (alert_origin card), the
   planner writes the approach, and the run pauses at the approach card — ``source: alert``, never
   auto-confirmed, no headline or alert title in the planner's checks.
5. (``--confirm``) confirm the approach and follow the investigation to the options (the
   ``external_intel`` specialist reads the hub through the quarantine).
6. Advance to advisory 3 (Red, ``alert_and_draft_case``): the alert re-notifies, the case stays the
   only one (the open case is reused, nothing is auto-opened twice).
7. Demo reset (unless ``--skip-final-reset``).

Exit 0 pass, 1 expectation failed (``FAIL:`` line), 2 environment missing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_m6_offline as m6  # noqa: E402
import api_thin_slice as ts  # noqa: E402
from api_thin_slice import KEELSON, E2EFailure, expect, heading, say  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-timeout", type=float, default=420.0)
    parser.add_argument("--confirm", action="store_true", help="also confirm the approach and wait for options")
    parser.add_argument("--skip-final-reset", action="store_true")
    args = parser.parse_args()
    m6.core_init()
    results: dict[str, Any] = {}
    try:
        heading("1. Reset + network push")
        results["reset"] = m6.check_reset_and_network()

        heading("2. Typhoon advisory 1 → alert")
        ava, priya = ts.keelson_login("ava"), ts.keelson_login("priya")
        m6.pack_action(ava, "start")
        alert = m6.wait_until("the typhoon alert", lambda: m6.alert_of_pack(priya), timeout_s=30)
        detail = m6.get_alert(priya, alert["id"])
        m6.check_alert_detail(detail, 0)
        say(f"alert {alert['id']}: {alert['severity']} · {alert['why']!r}")

        heading("3. Open case")
        t0 = time.monotonic()
        r = priya.client.post(
            f"{KEELSON}/api/alerts/{alert['id']}/open-case", json={}, headers=priya.headers(idem=True)
        )
        expect(r.status_code == 200 and r.json()["created"] is True, f"open-case: HTTP {r.status_code} {r.text[:300]}")
        oc = r.json()
        say(f"case {oc['case_id']}, signal run {oc['run_id']}")

        heading("4. Signal run → approach card")
        res = ts.follow_run(priya, oc["case_id"], str(oc["run_id"]), 0, timeout_s=args.run_timeout)
        results["approach_s"] = round(time.monotonic() - t0, 1)
        expect(res.outcome.get("type") == "interrupt", f"the signal run must pause at the approach: {res.outcome}")
        approach = [i for i in res.interrupts if i.get("reason") == "approach"]
        expect(approach and "approach_card" in res.blocks, f"interrupts {res.interrupts}, blocks {sorted(res.blocks)}")
        props = ts.check_approach_card(res.blocks["approach_card"])
        expect((props.get("event") or {}).get("source") == "alert", f"approach event source {props.get('event')}")
        case = ts.get_case(priya, oc["case_id"])
        kinds = ts.blocks_by_kind(case)
        expect("alert_origin" in kinds, f"no alert_origin card (have {sorted(kinds)})")
        origin = kinds["alert_origin"][-1]["props"]
        expect(origin.get("alert_id") == alert["id"], f"alert_origin {origin}")
        checks = json.dumps(props.get("checks") or [])
        expect(alert["title"] not in checks, "the alert title (untrusted) reached the planner's checks")
        say(f"approach card after {results['approach_s']} s (source alert, alert_origin card)")

        if args.confirm:
            heading("5. Confirm the approach → options")
            r = priya.client.post(
                f"{KEELSON}/api/interrupts/{approach[0]['id']}/decision",
                json={"decision": "confirm"},
                headers=priya.headers(),
            )
            expect(r.status_code == 202, f"confirm: HTTP {r.status_code} {r.text[:300]}")
            res2 = ts.follow_run(
                priya, oc["case_id"], str(r.json()["run_id"]), res.last_seq, timeout_s=args.run_timeout
            )
            expect(res2.outcome.get("type") in ("success", "interrupt"), f"investigation ended {res2.outcome}")
            agents = ts.agents_started(res, res2)
            say(f"agents consulted: {agents}")
            expect("external_intel" in agents, f"external_intel did not run: {agents}")
            kinds = ts.blocks_by_kind(ts.get_case(priya, oc["case_id"]))
            expect("recommended_option" in kinds, f"no options (blocks {sorted(kinds)})")
            results["agents"] = agents

        heading("6. Advisory 3 (Red): re-notify, still one case")
        for _ in range(2):
            m6.pack_action(ava, "advance")
        cur = m6.wait_until(
            "the critical alert",
            lambda: (d := m6.get_alert(priya, alert["id"]))["alert"]["severity"] == "critical" and d,
            timeout_s=30,
        )
        expect(cur["alert"]["case_id"] == oc["case_id"], f"alert case {cur['alert']['case_id']}")
        time.sleep(8)  # the engine's alert watcher polls every 5 s
        cases = m6.sql("SELECT case_id FROM alert_case WHERE alert_id = CAST(:a AS uuid)", a=alert["id"])
        expect(len(cases) == 1, f"alert_case rows {cases}")
        say("Red advisory: alert critical, the same case, nothing auto-opened twice")

        if not args.skip_final_reset:
            results["final_reset"] = m6.check_final_reset(alert["id"])
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    heading("Results")
    print(json.dumps(results, indent=2, default=str))
    ts.print_usage_report()
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
