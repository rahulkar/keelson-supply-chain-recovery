"""M5 Act 3 through the API, live (docs/contracts/95 §8): real models behind LiteLLM, Keycloak, Merxa, optimizer.

Story (one case, in order):

1. Demo reset (``ava``) and a fresh canonical-store snapshot.
2. Planner ``priya`` reports the trim delay, confirms the approach (and any values the plan relies
   on) and gets the options (the shared thin-slice helpers).
3. Option choice: the first non-guided option whose levers need four-eyes (alternative supplier /
   hybrid → ``po.create``), else the recommended one. Apply → the approval card names its quorum;
   a mixed option ("Apply N changes for me · Guide me through M") also gets its Guide checklist now.
4. Four-eyes: ``priya`` (the initiator, a planner) cannot decide; ``lena`` (compliance) approves
   first ("1 of 2"); ``marco`` approves → 412 connect_required → Connect Merxa (Keycloak form) →
   approve again → bulk apply → commit tracker verified. Merxa's change batch reads "Changed by
   Marco Rossi via Keelson (APR-…), co-approved by Lena Vogel". (A single-approver option skips Lena.)
5. Guide: one checklist step done in Merxa through REST as ``priya.shah`` (the deep-link form's
   save) → Merxa's webhook → the step is **verified** by read-back. Without a mixed option the
   planner first starts Guide on another option with transfer lines.
6. Second inject: Merxa ``lane_capacity`` cuts the sea-air lane the plan books (default
   CGP-UK1-SA) to 30 % → the routed ``signal`` run shows "New information: … plan updated (v2)" and,
   for the committed plan, a **delta change set** (booking cancels / smaller re-bookings /
   reschedules / extra transfers) that waits for people → approved per its quorum → applied.
7. Rollback of the delta: Undo (case action ``rollback``) → approval → Merxa ``rollback_change_set``
   → commit tracker ``rolled_back``, recovery plan v+1.
8. Demo reset.

Run inside the engine image like the offline M5 scripts (e2e/README.md), with model credits: the
engine's LLM record/replay (``LLM_REPLAY_MODE=record_or_replay``) records the first pass, so a rerun
of the same story costs nothing. Exit 0 pass, 1 expectation failed (``FAIL:``), 2 environment missing.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_m5_offline as m5  # noqa: E402
import api_thin_slice as ts  # noqa: E402
from api_thin_slice import KEELSON, MERXA, E2EFailure, expect, heading, say  # noqa: E402

FOUR_EYES_LEVERS = ("resource_alt_supplier", "resequence_production")
DEFAULT_LANE = "CGP-UK1-SA"


def blocks(case: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    return ts.blocks_by_kind(case).get(kind, [])


def choose_option(an: ts.Analysis) -> str:
    """Prefer an applicable option that needs four-eyes (po.create / resequencing), else the recommended."""
    for oid, opt in an.options.items():
        if oid == ts.DO_NOTHING or opt.get("guided_only"):
            continue
        if any(lv in FOUR_EYES_LEVERS for lv in opt.get("levers") or []):
            return oid
    return an.recommended


def decide(user: ts.KeelsonUser, interrupt_id: str, card: dict[str, Any], decision: str = "approve") -> httpx.Response:
    body: dict[str, Any] = {"decision": decision}
    if decision == "approve":
        body.update(plan_version=int(card["plan_version"]), change_set_hash=str(card["change_set_hash"]))
    return user.client.post(f"{KEELSON}/api/interrupts/{interrupt_id}/decision", json=body, headers=user.headers())


def approve_with_quorum(args: argparse.Namespace, case_id: str, card: dict[str, Any], after: int,
                        *, initiator: str = "priya") -> ts.RunResult:
    """Approve per the card's quorum (four-eyes: lena then marco); marco connects Merxa if needed."""
    iid = str(card["interrupt_id"])
    roles = list(card.get("required_roles") or [])
    expect(roles, f"approval card without roles: {card}")
    say(f"approval {iid}: needs {roles} ({ts.ascii_text(card.get('reason'), 160)})")
    if initiator == "priya":
        r = decide(ts.keelson_login("priya"), iid, card)
        expect(r.status_code == 403, f"the initiator / a planner must not decide: HTTP {r.status_code}")
    if "compliance" in roles:
        r = decide(ts.keelson_login("lena"), iid, card)
        expect(r.status_code == 202 and r.json().get("status") == "recorded", f"lena: HTTP {r.status_code} {r.text[:200]}")
        q = r.json().get("quorum") or {}
        expect(q.get("remaining") == 1 and q.get("missing_roles") == ["approver"], f"after lena: {q}")
        say("lena (compliance) approved: 1 of 2, waiting for the approver")
    marco = ts.keelson_login("marco")
    r = decide(marco, iid, card)
    if r.status_code == 412 and r.json().get("code") == "connect_required":
        say("marco: 412 connect_required -> Connect Merxa")
        ts.connect_merxa(marco, "marco.rossi", os.environ.get("DEMO_USER_PASSWORD", ""), f"/cases/{case_id}")
        r = decide(marco, iid, card)
    expect(r.status_code == 202 and r.json().get("status") == "resumed", f"marco approve: HTTP {r.status_code} {r.text[:300]}")
    return ts.follow_run(marco, case_id, str(r.json()["run_id"]), after, timeout_s=args.run_timeout)


def tracker_of(res: ts.RunResult) -> dict[str, Any]:
    return dict((res.blocks.get("commit_tracker") or {}).get("props") or {})


def merxa_change_set(code: str) -> dict[str, Any]:
    r = ts.merxa_login("merxa.admin").get(f"{MERXA}/v1/change-sets/{code}")
    expect(r.status_code == 200, f"Merxa change set {code}: HTTP {r.status_code}")
    return dict(r.json())


def do_guide_step(case_id: str, priya: ts.KeelsonUser) -> dict[str, Any]:
    """Carry out one checklist step in Merxa (REST, as priya.shah) and wait for the read-back."""
    items = priya.client.get(f"{KEELSON}/api/cases/{case_id}/checklists").json()["items"]
    expect(items, "no guide checklist on the case")
    mx = ts.merxa_login("priya.shah")
    for cl in items:
        for step in cl["props"]["steps"]:
            if step["status"] not in ("pending", "done"):
                continue
            exp = step["expected"]
            match = exp.get("match") or {}
            etype, ref = exp.get("entity_type"), exp.get("entity_ref")
            if etype == "transfer_order":
                today = str(mx.get(f"{MERXA}/v1/capacity-bookings/CB-0001").json()["today"])
                ship = time.strftime("%Y-%m-%d", time.gmtime(time.mktime(time.strptime(today, "%Y-%m-%d")) + 2 * 86400))
                r = mx.post(f"{MERXA}/v1/transfer-orders", headers=m5.merxa_headers(),
                            json={"from_dc": match["from_dc"], "to_dc": match["to_dc"], "ship_date": ship,
                                  "lines": match["lines"]})
            elif etype == "allocation" and ref and "qty" in match:
                cur = mx.get(f"{MERXA}/v1/allocations/{ref}")
                r = mx.patch(f"{MERXA}/v1/allocations/{ref}", json={"qty": int(match["qty"])},
                             headers={**m5.merxa_headers(), "If-Match": cur.headers.get("ETag", "")})
            elif etype == "po_line" and ref and "eta_dc" in match:
                po, _, n = str(ref).rpartition("#")
                lines = mx.get(f"{MERXA}/v1/purchase-orders/{po}").json().get("lines") or []
                version = next((ln.get("version") for ln in lines if str(ln.get("line_no")) == n), 1)
                r = mx.patch(f"{MERXA}/v1/purchase-orders/{po}/lines/{n}", json={"eta_dc": match["eta_dc"]},
                             headers={**m5.merxa_headers(), "If-Match": f'"{version}"'})
            else:
                continue
            expect(r.status_code in (200, 201), f"Merxa REST for step {step['seq']} ({etype}): HTTP {r.status_code} "
                                                f"{r.text[:300]}")
            say(f"step {step['seq']} '{ts.ascii_text(step['title'], 80)}' done in Merxa as priya.shah "
                f"(link {ts.ascii_text(step.get('deep_link'), 120)})")
            cid, seq = cl["id"], step["seq"]

            def verified(cid: str = cid, seq: int = seq) -> dict[str, Any] | None:
                got = next(c for c in priya.client.get(f"{KEELSON}/api/cases/{case_id}/checklists").json()["items"]
                           if c["id"] == cid)
                st = next(s for s in got["props"]["steps"] if s["seq"] == seq)
                return st if st["status"] == "verified" else None

            st = m5.wait_until(f"step {seq} verified by read-back", verified, timeout_s=120)
            say(f"step {seq} verified by read-back (evidence {st['evidence_id']})")
            return {"checklist_id": cid, "seq": seq, "entity_type": etype}
    raise E2EFailure("no checklist step this script can carry out through Merxa REST")


def run_story(args: argparse.Namespace) -> dict[str, Any]:
    out: dict[str, Any] = {}
    heading("1. Demo reset")
    out["reset_s"] = m5.demo_reset(keep_consents=False)["wall_s"]
    ts.keelson_resync("after the demo reset", timeout_s=args.sync_timeout)

    an = ts.analyse(args, title="M5 Act 3")
    an = ts.confirm_pending_values(args, an)
    priya = ts.keelson_login("priya")
    oid = choose_option(an)
    say(f"option {oid} ({ts.ascii_text(an.options[oid].get('title'))}; levers {an.options[oid].get('levers')})")

    heading("3. Apply (mixed apply + guide when the option has guided lines)")
    after = int(ts.get_case(priya, an.case_id).get("last_event_seq") or 0)
    r = priya.client.post(f"{KEELSON}/api/cases/{an.case_id}/actions",
                          json={"type": "apply", "option_id": oid, "plan_version": an.plan_version},
                          headers=priya.headers(idem=True))
    expect(r.status_code == 202, f"apply: HTTP {r.status_code} {r.text[:300]}")
    res = ts.follow_run(priya, an.case_id, str(r.json()["run_id"]), after, timeout_s=args.run_timeout)
    ts.expect_success(res, "apply run")
    mixed = "remediation_checklist" in res.blocks
    card = (res.blocks.get("approval_card") or {}).get("props")
    expect(card, f"expected an approval card (outcome {res.outcome})")
    out["four_eyes"] = "compliance" in (card.get("required_roles") or [])
    out["mixed"] = mixed

    heading("4. Approval (four-eyes when the policy asks) → bulk apply")
    after = int(ts.get_case(priya, an.case_id).get("last_event_seq") or 0)
    res = approve_with_quorum(args, an.case_id, card, after)
    ts.expect_success(res, "commit run")
    tracker = tracker_of(res)
    expect(tracker.get("status") == "verified", f"commit tracker {tracker.get('status')}")
    cs = merxa_change_set(str(tracker["ext_code"]))
    attribution = str(cs.get("attribution_text") or "")
    expect("Changed by Marco Rossi via Keelson (APR-" in attribution, f"attribution {attribution!r}")
    if out["four_eyes"]:
        expect("co-approved by Lena Vogel" in attribution, f"co-approval missing: {attribution!r}")
    say(f"Merxa {cs['code']} applied: {attribution}")
    out["v1_change_set"] = cs["code"]

    heading("5. Guide step done in Merxa → verified by read-back")
    if not mixed:
        other = next((o for o, v in an.options.items() if o not in (oid, ts.DO_NOTHING) and not v.get("guided_only")
                      and "reallocate_inventory" in (v.get("levers") or [])), None)
        expect(other, "no option to guide")
        after = int(ts.get_case(priya, an.case_id).get("last_event_seq") or 0)
        r = priya.client.post(f"{KEELSON}/api/cases/{an.case_id}/actions",
                              json={"type": "guide", "option_id": other, "plan_version": an.plan_version},
                              headers=priya.headers(idem=True))
        expect(r.status_code == 202, f"guide: HTTP {r.status_code} {r.text[:300]}")
        g = ts.follow_run(priya, an.case_id, str(r.json()["run_id"]), after, timeout_s=args.run_timeout)
        ts.expect_success(g, "guide run")
        say(f"no mixed option: guided {other} instead")
    out["guide"] = do_guide_step(an.case_id, priya)

    heading("6. Second inject: sea-air capacity cut → re-plan v2 + delta")
    booked = [i for i in cs.get("items") or [] if i.get("op") == "capacity_booking.create"]
    lane = str(((booked[0].get("payload") or {}).get("lane_code")) if booked else DEFAULT_LANE)
    after = int(ts.get_case(priya, an.case_id).get("last_event_seq") or 0)
    r = ts.merxa_login("merxa.admin").post(
        f"{MERXA}/v1/admin/scenario:inject", headers={"Origin": MERXA, "Idempotency-Key": str(uuid.uuid4())},
        json={"kind": "lane_capacity", "payload": {"lane_code": lane, "factor_bp": 3000, "weeks": 3}})
    expect(r.status_code == 200, f"lane_capacity inject: HTTP {r.status_code} {r.text[:300]}")
    run_id = None
    for _seq, ev in ts.sse_events(priya, an.case_id, after, time.monotonic() + 120):
        if ev.get("type") == "RUN_STARTED" and ev.get("runId"):
            run_id = str(ev["runId"])
            break
    expect(run_id, "the re-plan signal never reached the case")
    sig = ts.follow_run(priya, an.case_id, str(run_id), after, timeout_s=args.run_timeout)
    ts.expect_success(sig, "signal run")
    banner = str((((sig.blocks.get("narrative") or {}).get("props") or {}).get("banner") or {}).get("text") or "")
    expect(banner.startswith("New information") and "plan updated" in banner, f"banner {banner!r}")
    expect("commit_tracker" not in sig.blocks, "the signal run applied changes without people")
    say(f"banner: {banner}")
    out["banner"] = banner
    delta_card = (sig.blocks.get("approval_card") or {}).get("props")
    if delta_card is None:
        say("the applied changes still fit: no delta to approve (rollback step uses the v1 change set)")
        undo_target = str(tracker["change_set_id"])
    else:
        diff = (sig.blocks.get("plan_diff") or {}).get("props") or {}
        say(f"delta: {len(diff.get('rows') or [])} line(s); {ts.ascii_text(delta_card.get('reason'), 160)}")
        after = int(ts.get_case(priya, an.case_id).get("last_event_seq") or 0)
        dres = approve_with_quorum(args, an.case_id, delta_card, after, initiator="signal")
        ts.expect_success(dres, "delta commit run")
        dtracker = tracker_of(dres)
        expect(dtracker.get("status") == "verified", f"delta commit tracker {dtracker.get('status')}")
        out["delta_change_set"] = dtracker.get("ext_code")
        undo_target = str(dtracker["change_set_id"])

    heading("7. Rollback (Undo) with approval")
    after = int(ts.get_case(priya, an.case_id).get("last_event_seq") or 0)
    r = priya.client.post(f"{KEELSON}/api/cases/{an.case_id}/actions",
                          json={"type": "rollback", "change_set_id": undo_target}, headers=priya.headers(idem=True))
    expect(r.status_code == 202, f"rollback: HTTP {r.status_code} {r.text[:300]}")
    rb = ts.follow_run(priya, an.case_id, str(r.json()["run_id"]), after, timeout_s=args.run_timeout)
    ts.expect_success(rb, "rollback run")
    rb_card = (rb.blocks.get("approval_card") or {}).get("props")
    expect(rb_card, f"a rollback always needs people (outcome {rb.outcome})")
    after = int(ts.get_case(priya, an.case_id).get("last_event_seq") or 0)
    rres = approve_with_quorum(args, an.case_id, rb_card, after)
    ts.expect_success(rres, "rollback commit run")
    rtracker = tracker_of(rres)
    expect(rtracker.get("status") == "rolled_back", f"after undo: tracker {rtracker.get('status')} "
                                                     f"conflicts {rtracker.get('conflicts')}")
    msgs = [m["text"] for m in ts.get_case(priya, an.case_id)["messages"] if m["role"] == "assistant"]
    expect(any("Rolled back by Marco Rossi via Keelson (APR-RB-" in t for t in msgs[-3:]), f"rollback text {msgs[-1:]}")
    say(f"rolled back: {ts.ascii_text(msgs[-1], 200)}")
    out["rolled_back"] = undo_target

    heading("8. Demo reset")
    out["final_reset_s"] = m5.demo_reset(keep_consents=False)["wall_s"]
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ts.add_common_args(parser)
    args = parser.parse_args()
    if not os.environ.get("DEMO_USER_PASSWORD"):
        print("ENV: DEMO_USER_PASSWORD is required", file=sys.stderr)
        return 2
    try:
        result = run_story(args)
    except E2EFailure as exc:
        ts.print_usage_report()
        print(f"\nFAIL: {exc}", flush=True)
        return 1
    except httpx.HTTPError as exc:
        ts.print_usage_report()
        print(f"\nFAIL: HTTP error {type(exc).__name__}: {exc}", flush=True)
        return 1
    ts.print_usage_report()
    heading("PASS")
    print(json.dumps(result, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
