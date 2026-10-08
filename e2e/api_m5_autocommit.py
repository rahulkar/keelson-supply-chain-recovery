"""M5 auto-commit and policy refusals on the real stack (docs/contracts/95 §1, §8) — no model call.

The commit path is deterministic (routing → compile → dry-run → policy → apply → reconcile, with
templated messages), so this script seeds each case's scenario and plan v1 through the engine's
core functions instead of asking the agents for them, and then drives everything else through the
public API exactly like the web app (case actions, SSE, Merxa REST). It therefore runs with or
without model credits; ``verify-live.sh`` runs it next to the live Act 3 story.

1. Demo reset (``ava``) and a fresh canonical-store snapshot.
2. **Auto-commit**: planner ``priya`` applies a small transfer-only option (1 line, ≤ 500 units)
   → no approval card; Merxa applies it as the ``keelson-autocommit`` principal; the change batch
   reads "Applied via Keelson auto-policy auto-small-transfer@<policy>, initiated by Priya Shah".
3. **Refusals** (no change set reaches Merxa, no approval card): a what-if case; a plan that relies
   on an unconfirmed (inferred) value.
4. **Signal origin never auto**: Merxa's ``lane_capacity`` inject on a lane the committed plan
   depends on → the routed ``signal`` run re-plans and compiles a delta, which waits for people
   (approval card with roles, nothing applied), however small the change.
5. Demo reset again (``--skip-final-reset`` keeps the cases for inspection).

Run it inside the engine image (``docker compose run engine-api``), see e2e/README.md.
Exit 0 pass, 1 expectation failed (``FAIL:`` line), 2 environment missing.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_m5_offline as m5  # noqa: E402
import api_thin_slice as ts  # noqa: E402
from api_thin_slice import KEELSON, MERXA, E2EFailure, expect, heading, say  # noqa: E402

TENANT = m5.TENANT
LANE = "CGP-UK1-SA"
RUN_TIMEOUT_S = 300.0


def scenario(*, mode: str = "actual", prov: str = "decision") -> dict[str, Any]:
    return {
        "id": f"scn-e2e-{uuid.uuid4().hex[:8]}", "tenant": TENANT, "mode": mode, "event_family": "SUP",
        "event_type": "SUP.delay", "description": "Trims from Meghna Trims are delayed (e2e seed)",
        "perturbations": [], "matched": [],
        "assumptions": [{"key": "delay_days", "label": "Delay", "value_text": "7 days", "prov": prov}],
        "config_version": "default/p-2",
    }


def option(oid: str, actions: list[dict[str, Any]], *, levers: tuple[str, ...] = ("reallocate_inventory",),
           covers_lane: str | None = None) -> dict[str, Any]:
    """``covers_lane``: the transfer covers a shortfall of receipts arriving on that inbound lane, so
    the plan depends on it (the decision names it; that is what routes a capacity signal here)."""
    acts = [{**a, "seq": i} for i, a in enumerate(actions, start=1)]
    units = sum(int(x["qty"]) for a in actions for x in (a.get("payload") or {}).get("lines") or [])
    decisions = []
    if covers_lane and acts:
        p = acts[0]["payload"]
        decisions.append({"lever": "reallocate_inventory", "from_dc": p["from_dc"], "to_dc": p["to_dc"],
                          "sku": p["lines"][0]["sku"], "qty": units, "covers_inbound_lane": covers_lane,
                          "lane_code": covers_lane})
    return {
        "id": oid, "title": f"Move {units} units DE1 → NL1" if units else "Do nothing", "status": "OPTIMAL",
        "levers": list(levers), "guided_only": False, "guided_reason": None,
        "kpis_before": {"units_at_risk": 900}, "kpis_after": {"units_at_risk": 900 - units},
        "kpi_delta": {"net_benefit_cents": 20_000, "transfer_cost_cents": units * 35}, "actions": acts,
        "decisions": decisions, "warnings": [], "fallback": False,
        "scorecard": {"incremental_cost_cents": units * 35, "changes": len(acts)},
    }


def seed_case(user: ts.KeelsonUser, title: str, transfer: dict[str, Any], *, mode: str = "actual",
              prov: str = "decision", lanes: list[str] | None = None) -> str:
    from keelson_engine.core import repo

    case_id = m5.create_case(user, title)
    if mode != "actual":
        m5.db(repo.set_case_mode(case_id, mode, tenant=TENANT))
    m5.db(repo.save_scenario(case_id, scenario(mode=mode, prov=prov), tenant=TENANT))
    deps = {"lanes": lanes or [], "by_option": {"opt-A": {"lanes": lanes or []}}, "committed_lanes": []}
    kpis = {"before": {"units_at_risk": 900}, "scenario_version": 1, "data_source": "scn", "dependencies": deps}
    opt = option("opt-A", [transfer], covers_lane=(lanes or [None])[0])
    m5.db(repo.save_plan(case_id, 1, [opt, option("do-nothing", [], levers=())], "opt-A", kpis, tenant=TENANT))
    return case_id


def apply(user: ts.KeelsonUser, case_id: str) -> ts.RunResult:
    after = int(ts.get_case(user, case_id).get("last_event_seq") or 0)
    r = user.client.post(f"{KEELSON}/api/cases/{case_id}/actions",
                         json={"type": "apply", "option_id": "opt-A", "plan_version": 1}, headers=user.headers(idem=True))
    expect(r.status_code == 202, f"apply: HTTP {r.status_code} {r.text[:300]}")
    return ts.follow_run(user, case_id, str(r.json()["run_id"]), after, timeout_s=RUN_TIMEOUT_S)


def merxa_change_sets() -> dict[str, dict[str, Any]]:
    admin = ts.merxa_login("merxa.admin")
    r = admin.get(f"{MERXA}/v1/change-sets", params={"limit": 200})
    expect(r.status_code == 200, f"Merxa change sets: HTTP {r.status_code}")
    return {str(c["code"]): c for c in r.json()["items"]}


def last_assistant(user: ts.KeelsonUser, case_id: str) -> str:
    msgs = [m for m in ts.get_case(user, case_id).get("messages") or [] if m.get("role") == "assistant"]
    return str(msgs[-1]["text"]) if msgs else ""


def pick_transfer() -> dict[str, Any]:
    mx = ts.merxa_login("priya.shah")
    r = mx.get(f"{MERXA}/v1/inventory-positions", params={"location_type": "dc", "location_code": "DE1", "limit": 200})
    expect(r.status_code == 200, f"Merxa inventory: HTTP {r.status_code}")
    items = sorted(r.json()["items"], key=lambda x: (-int(x["atp"]), x["sku"]))
    expect(items and int(items[0]["atp"]) >= 100, "no DE1 stock for a small transfer")
    today = str(mx.get(f"{MERXA}/v1/capacity-bookings/CB-0001").json()["today"])
    ship = time.strftime("%Y-%m-%d", time.gmtime(time.mktime(time.strptime(today, "%Y-%m-%d")) + 3 * 86400))
    return {"op": "transfer_order.create", "target_ref": None, "expected_version": None,
            "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": ship,
                        "lines": [{"sku": str(items[0]["sku"]), "qty": 60}]}}


def check_autocommit(transfer: dict[str, Any]) -> dict[str, Any]:
    heading("2. Auto-commit: small transfer-only change set started by a planner")
    priya = ts.keelson_login("priya")
    case_id = seed_case(priya, "M5 e2e: auto-commit", transfer, lanes=[LANE])
    before = merxa_change_sets()
    res = apply(priya, case_id)
    ts.expect_success(res, "apply run")
    expect(res.outcome.get("type") == "success" and not res.interrupts,
           f"auto-commit must not pause for approval: {res.outcome}")
    expect("approval_card" not in res.blocks, "auto-commit showed an approval card")
    tracker = (res.blocks.get("commit_tracker") or {}).get("props") or {}
    expect(tracker.get("status") == "verified", f"commit tracker {tracker.get('status')}")
    code = str(tracker.get("ext_code"))
    after = merxa_change_sets()
    expect(code in after and code not in before, f"Merxa change set {code} not new")
    cs = ts.merxa_login("merxa.admin").get(f"{MERXA}/v1/change-sets/{code}").json()
    attribution = str(cs.get("attribution_text") or "")
    expect(cs.get("status") == "applied", f"Merxa change set {code} status {cs.get('status')}")
    expect(attribution.startswith("Applied via Keelson auto-policy auto-small-transfer@")
           and attribution.endswith("initiated by Priya Shah"), f"attribution {attribution!r}")
    expect(str(cs.get("approved_by")).startswith("policy:auto-small-transfer@") and cs.get("initiated_by") == "Priya Shah",
           f"receipt claims approved_by={cs.get('approved_by')} initiated_by={cs.get('initiated_by')}")
    expect(cs.get("applied_via_client") == "keelson-autocommit", f"applied via {cs.get('applied_via_client')}")
    say(f"case {case_id}: applied as {code} without approval by {cs.get('applied_via_client')} "
        f"(approved_by {cs.get('approved_by')}); '{attribution}'")
    case = ts.get_case(priya, case_id)
    expect(case["case"]["status"] == "committed", f"case status {case['case']['status']}")
    return {"case_id": case_id, "change_set": code, "attribution": attribution}


def check_refusals(transfer: dict[str, Any]) -> dict[str, Any]:
    heading("3. Refusals: what-if case, unconfirmed value")
    priya = ts.keelson_login("priya")
    out: dict[str, Any] = {}
    for label, kwargs, needle in (("what-if", {"mode": "what_if"}, "what-if"),
                                  ("unconfirmed", {"prov": "inference"}, "unconfirmed")):
        case_id = seed_case(priya, f"M5 e2e: refuse {label}", transfer, **kwargs)
        before = merxa_change_sets()
        res = apply(priya, case_id)
        ts.expect_success(res, f"{label} apply run")
        expect(not res.interrupts and "approval_card" not in res.blocks and "commit_tracker" not in res.blocks,
               f"{label}: the refusal must not reach approval or apply ({sorted(res.blocks)})")
        text = last_assistant(priya, case_id)
        expect(needle in text.lower(), f"{label}: refusal text {text[:200]!r}")
        after = merxa_change_sets()
        applied = [c for c, v in after.items() if c not in before and v.get("status") == "applied"]
        expect(not applied, f"{label}: Merxa change sets applied {applied}")
        say(f"{label}: refused — {ts.ascii_text(text, 140)}")
        out[label] = case_id
    return out


def check_signal_never_auto(case_id: str) -> dict[str, Any]:
    heading("4. Signal origin: the re-plan delta waits for people")
    priya = ts.keelson_login("priya")
    after = int(ts.get_case(priya, case_id).get("last_event_seq") or 0)
    before = merxa_change_sets()
    admin = ts.merxa_login("merxa.admin")
    r = admin.post(f"{MERXA}/v1/admin/scenario:inject", headers={"Origin": MERXA, "Idempotency-Key": str(uuid.uuid4())},
                   json={"kind": "lane_capacity", "payload": {"lane_code": LANE, "factor_bp": 3000, "weeks": 3}})
    expect(r.status_code == 200, f"lane_capacity inject: HTTP {r.status_code} {r.text[:200]}")
    run_id: str | None = None
    for _seq, ev in ts.sse_events(priya, case_id, after, time.monotonic() + 120):
        if ev.get("type") == "RUN_STARTED" and ev.get("runId"):
            run_id = str(ev["runId"])
            break
    expect(run_id, "no signal run reached the case")
    run = m5.sql("SELECT kind, actor FROM run WHERE id = :i", i=run_id)[0]
    expect(run["kind"] == "signal" and run["actor"].get("roles") == [], f"routed run {run}")
    res = ts.follow_run(priya, case_id, str(run_id), after, timeout_s=RUN_TIMEOUT_S)
    expect(res.final.get("type") == "RUN_FINISHED", f"signal run ended {res.final.get('type')} {res.final.get('code')}")
    narr = (res.blocks.get("narrative") or {}).get("props") or {}
    banner = str((narr.get("banner") or {}).get("text") or "")
    expect(banner.startswith("New information") and "plan updated (v2)" in banner, f"banner {banner!r}")
    expect("commit_tracker" not in res.blocks, "a signal run applied changes without people")
    card = (res.blocks.get("approval_card") or {}).get("props")
    if res.outcome.get("type") == "interrupt":
        expect(card and card.get("required_roles"), f"approval card without roles: {card}")
        say(f"delta compiled: approval needed from {card['required_roles']} ({ts.ascii_text(card.get('reason'), 140)})")
    else:
        say(f"no delta to apply (the applied change still fits): {ts.ascii_text(last_assistant(priya, case_id), 160)}")
    after_cs = merxa_change_sets()
    applied = [c for c, v in after_cs.items() if v.get("status") == "applied" and before.get(c, {}).get("status") != "applied"]
    expect(not applied, f"Merxa change sets applied during the signal run: {applied}")
    say(f"banner: {banner}")
    return {"signal_run": run_id, "outcome": res.outcome.get("type"), "roles": (card or {}).get("required_roles")}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-final-reset", action="store_true")
    parser.add_argument("--sync-timeout", type=float, default=300.0)
    args = parser.parse_args()
    m5.core_init()
    results: dict[str, Any] = {}
    try:
        heading("1. Demo reset + fresh snapshot")
        results["reset_s"] = m5.demo_reset(keep_consents=False)["wall_s"]
        ts.keelson_resync("after the demo reset", timeout_s=args.sync_timeout)
        transfer = pick_transfer()
        say(f"transfer: {json.dumps(transfer['payload'])}")
        auto = check_autocommit(transfer)
        results["autocommit"] = auto
        results["refusals"] = check_refusals(transfer)
        results["signal"] = check_signal_never_auto(auto["case_id"])
        if not args.skip_final_reset:
            heading("5. Demo reset")
            results["final_reset_s"] = m5.demo_reset(keep_consents=False)["wall_s"]
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    heading("Results")
    print(json.dumps(results, indent=2, default=str))
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
