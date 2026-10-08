"""M7 live generic proof (docs/contracts/110 §4): the trim-delay story on a **foreign ERP**.

northwind-test plans on Merxa ``/mcp-lite`` only (tier A: plain CRUD tools, no snapshot export, no
change sets): crawl sync → advisory options → the mapped transfer / allocation lines are applied
**per op** after a human approval (one ``create_transfer_order`` / ``update_item`` call per line,
read back) → everything else becomes guided steps. Real Gemini (Flash) behind LiteLLM: run it from
``scripts/verify-live.sh`` (step ``m7-foreign-erp``), never while credits are depleted. Runs inside
the engine image like the M5/M6 scripts (it reuses ``api_m7_offline.py``'s set-up, which reads
Merxa through the engine's own MCP gateway).

Steps:
1. Demo reset (ava; the Merxa reset revokes earlier API keys).
2. Foreign ERP (ava, cross-tenant): merxa-lite registered (or its API key rotated in place when the
   agent spec already plans on it), tools classified, activated, pack ``foreign-erp-lite`` mappings
   installed, crawl sync → ``crawled`` snapshot (row counts = Merxa's own).
3. Agent spec (ava, platform admin): planning connector, answer path and the inventory specialist
   on merxa-lite. nora gets ``approver`` next to ``planner`` (Users API; restored at the end) —
   northwind-test has no approver persona.
4. nora reports the trim delay → approach card confirmed → options. Every plan block is labelled
   **advisory** (``recommended_option`` / ``scenario_compare`` / ``narrative`` carry ``advisory``
   with ``reason: crawled_snapshot`` and an as-of range; the narrative says why).
5. Apply the recommended option: the mapped lines form a per-op change set (dry run renders one call
   per line, nothing executed) → approval card (nora approves; or a policy auto-approval) → apply →
   ``commit_tracker`` verified; unmapped lines → a guided checklist (``remediation_checklist``).
6. Read back in Merxa (``/mcp-lite``): every applied transfer order exists (``created_via
   mcp-lite``) and every allocation carries its new quantity; the commit ledger says ``verified``.

The northwind-test spec keeps planning on merxa-lite afterwards (the foreign-ERP configuration);
the next run (or ``api_m7_offline.py``) rotates the API key in place. nora's roles are restored.
Exit 0 pass, 1 expectation failed, 2 environment missing.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_m7_offline as m7  # noqa: E402
import api_thin_slice as ts  # noqa: E402
from api_m2_admin import admin, items_of, ok  # noqa: E402
from api_thin_slice import KEELSON, E2EFailure, expect, heading, say  # noqa: E402

NW, LITE = m7.NW, m7.LITE
PROMPT = ts.PROMPT  # the M1 trim-delay story (Meghna Trims, 7 days)
ADVISORY_RE = ("paging", "advisory")


def bind_spec(ava: ts.KeelsonUser) -> str:
    heading("3. northwind-test plans on the foreign ERP")
    cur = ok(admin(ava, "GET", "/agents/spec", tenant=NW), "spec")
    spec = cur["spec"]
    specialists = []
    for sp in spec.get("specialists") or []:
        sp = dict(sp)
        if sp.get("slug") == "inventory_demand":
            sp.update(connector=LITE, tools=list(m7.READ_TOOLS))
        specialists.append(sp)
    patch = {"connector": LITE, "answer_connector": LITE, "answer_tools": list(m7.READ_TOOLS),
             "specialists": specialists}
    if (spec.get("connector"), spec.get("answer_connector")) == (LITE, LITE) and spec.get("specialists") == specialists:
        say(f"spec {cur['version']} already plans on {LITE}")
        return str(cur["version"])
    r = ok(admin(ava, "PUT", "/agents/spec", tenant=NW, body={"base_version": cur["version"], "spec_patch": patch}),
           "bind the foreign ERP", 201)
    say(f"spec {r['version']}: connector={LITE}, answer path + inventory specialist on {LITE}")
    return str(r["version"])


def nora_roles(ava: ts.KeelsonUser, roles: list[str]) -> None:
    users = items_of(ok(admin(ava, "GET", "/users", tenant=NW), "northwind users"))
    nora = next((u for u in users if u.get("username") == "nora"), None)
    expect(nora is not None, "nora is not a northwind-test user")
    ok(admin(ava, "PATCH", f"/users/{nora['id']}", tenant=NW, body={"roles": roles}), f"nora roles {roles}")
    say(f"nora roles → {roles}")


def advisory_of(blk: dict[str, Any]) -> dict[str, Any]:
    return dict((blk.get("props") or {}).get("advisory") or {})


def analyse(args: argparse.Namespace) -> dict[str, Any]:
    heading("4. nora reports the trim delay (foreign ERP, crawled data)")
    nora = ts.keelson_login("nora")
    r = nora.client.post(f"{KEELSON}/api/cases", json={"title": "M7 live: trim delay on the foreign ERP"},
                         headers=nora.headers())
    expect(r.status_code == 201, f"create case: HTTP {r.status_code} {r.text[:200]}")
    case_id = str(r.json()["id"])
    after = int(ts.get_case(nora, case_id).get("last_event_seq") or 0)
    r = nora.client.post(f"{KEELSON}/api/cases/{case_id}/messages", json={"text": PROMPT}, headers=nora.headers(idem=True))
    expect(r.status_code == 202, f"post message: HTTP {r.status_code} {r.text[:200]}")
    res = ts.follow_run(nora, case_id, str(r.json()["run_id"]), after, timeout_s=args.run_timeout)
    ts.expect_success(res, "chat run")
    if res.outcome.get("type") == "interrupt":
        approach = [i for i in res.interrupts if i.get("reason") == "approach"]
        expect(approach, f"unexpected interrupt(s): {res.interrupts}")
        r = nora.client.post(f"{KEELSON}/api/interrupts/{approach[0]['id']}/decision", json={"decision": "confirm"},
                             headers=nora.headers())
        expect(r.status_code == 202, f"confirm approach: HTTP {r.status_code} {r.text[:300]}")
        res = ts.follow_run(nora, case_id, str(r.json()["run_id"]), res.last_seq, timeout_s=args.run_timeout)
        ts.expect_success(res, "approach resume run")
    expect(res.outcome.get("type") == "success", f"analysis ended with {res.outcome}")
    case = ts.get_case(nora, case_id)
    kinds = ts.blocks_by_kind(case)
    for kind in ("recommended_option", "narrative"):
        expect(kind in kinds, f"missing {kind} block (have {sorted(kinds)})")
    rec_blk = kinds["recommended_option"][-1]
    adv = advisory_of(rec_blk)
    expect(adv.get("reason") == "crawled_snapshot" and (adv.get("as_of_range") or {}).get("from"),
           f"the recommended option is not labelled advisory: {adv}")
    if "scenario_compare" in kinds:
        expect(advisory_of(kinds["scenario_compare"][-1]).get("reason") == "crawled_snapshot", "scenario_compare not advisory")
    narrative = kinds["narrative"][-1]
    expect(advisory_of(narrative).get("reason") == "crawled_snapshot", "narrative not advisory")
    text = str((narrative.get("props") or {}).get("markdown") or "").lower()
    expect(any(w in text for w in ADVISORY_RE), f"the narrative does not say the options are advisory: {text[:300]}")
    rec = rec_blk["props"]
    say(f"plan v{rec['plan_version']}: recommended {rec['option']['id']} '{rec['option']['title']}' (advisory, "
        f"as of {adv['as_of_range'].get('from')} … {adv['as_of_range'].get('to')})")
    return {"case_id": case_id, "plan_version": int(rec["plan_version"]), "option_id": str(rec["option"]["id"]),
            "rec": rec, "last_seq": int(case.get("last_event_seq") or res.last_seq)}


def confirm_values(args: argparse.Namespace, an: dict[str, Any]) -> dict[str, Any]:
    pending = list(an["rec"].get("unconfirmed") or [])
    if not pending:
        return an
    heading("4b. nora confirms the values the plan relies on")
    nora = ts.keelson_login("nora")
    r = nora.client.post(f"{KEELSON}/api/cases/{an['case_id']}/actions", headers=nora.headers(idem=True), json={
        "type": "confirm_assumptions", "option_id": an["option_id"], "plan_version": an["plan_version"],
        "assumption_keys": [str(u["key"]) for u in pending]})
    expect(r.status_code == 202, f"confirm_assumptions: HTTP {r.status_code} {r.text[:300]}")
    res = ts.follow_run(nora, an["case_id"], str(r.json()["run_id"]), an["last_seq"], timeout_s=args.run_timeout)
    ts.expect_success(res, "confirm run")
    case = ts.get_case(nora, an["case_id"])
    rec = ts.blocks_by_kind(case)["recommended_option"][-1]["props"]
    expect(not rec.get("unconfirmed"), f"values still unconfirmed: {rec.get('unconfirmed')}")
    return {**an, "plan_version": int(rec["plan_version"]), "option_id": str(rec["option"]["id"]), "rec": rec,
            "last_seq": int(case.get("last_event_seq") or res.last_seq)}


def apply_option(args: argparse.Namespace, an: dict[str, Any]) -> dict[str, Any]:
    heading(f"5. Apply {an['option_id']}: mapped lines per op, the rest guided")
    expect(an["option_id"] != ts.DO_NOTHING, "the recommendation is to do nothing: no per-op apply to check")
    nora = ts.keelson_login("nora")
    r = nora.client.post(f"{KEELSON}/api/cases/{an['case_id']}/actions", headers=nora.headers(idem=True),
                         json={"type": "apply", "option_id": an["option_id"], "plan_version": an["plan_version"]})
    expect(r.status_code == 202, f"apply action: HTTP {r.status_code} {r.text[:300]}")
    res = ts.follow_run(nora, an["case_id"], str(r.json()["run_id"]), an["last_seq"], timeout_s=args.run_timeout)
    ts.expect_success(res, "apply run")
    diff = res.blocks.get("plan_diff") or {}
    say(f"dry run: {ts.ascii_text(json.dumps((diff.get('props') or {}).get('totals')), 200)}")
    approved_by = "policy"
    if res.outcome.get("type") == "interrupt":
        approvals = [i for i in res.interrupts if i.get("reason") == "approval"]
        expect(approvals, f"unexpected interrupt(s) {res.interrupts}")
        card = (res.blocks.get("approval_card") or {}).get("props") or {}
        body = {"decision": "approve", "plan_version": int(card["plan_version"]), "change_set_hash": card["change_set_hash"]}
        r = nora.client.post(f"{KEELSON}/api/interrupts/{approvals[0]['id']}/decision", json=body, headers=nora.headers())
        expect(r.status_code == 202 and r.json().get("status") == "resumed", f"approve: HTTP {r.status_code} {r.text[:300]}")
        res = ts.follow_run(nora, an["case_id"], str(r.json()["run_id"]), res.last_seq, timeout_s=args.run_timeout)
        ts.expect_success(res, "commit run")
        approved_by = "nora"
    expect(res.outcome.get("type") == "success", f"commit run ended with {res.outcome}")
    case = ts.get_case(nora, an["case_id"])
    kinds = ts.blocks_by_kind(case)
    trackers = [b["props"] for b in kinds.get("commit_tracker") or []]
    expect(trackers, "no commit_tracker block")
    expect(trackers[-1].get("status") == "verified", f"commit_tracker ended {trackers[-1].get('status')}")
    guided = kinds.get("remediation_checklist") or []
    say(f"approved by {approved_by}; commit_tracker verified ({trackers[-1].get('applied')} lines); "
        f"{len(guided)} guided checklist(s)")
    return {"case_id": an["case_id"], "approved_by": approved_by, "guided": len(guided)}


def read_back(case_id: str) -> dict[str, Any]:
    heading("6. Read back in Merxa (/mcp-lite) and the commit ledger")
    rows = m7.db(_change_sets(case_id))
    per_op = [r for r in rows if (r.get("preview") or {}).get("mode") == "per_op"]
    expect(per_op, f"no per-op change set on {LITE}: {[(r['connector'], r['status']) for r in rows]}")
    checked = 0
    for row in per_op:
        expect(row["status"] == "verified", f"change set {row['id']} is {row['status']}")
        for item in row["items"] or []:
            p = item.get("payload") or {}
            if item["op"] == "transfer_order.create":
                found = m7.lite_items("transfer_order", {"from_dc": p["from_dc"], "to_dc": p["to_dc"],
                                                         "ship_date": p["ship_date"], "created_via": "mcp-lite"})
                expect(found, f"transfer order {p['from_dc']}→{p['to_dc']} {p['ship_date']} not in Merxa")
            elif item["op"] == "allocation.set_qty":
                got = m7.lite_call("get_item", {"type": "allocation", "key": item["target_ref"]})["item"]
                expect(int(got["qty"]) == int(p["qty"]), f"allocation {item['target_ref']} qty {got['qty']} != {p['qty']}")
            checked += 1
        ledger = (row.get("result") or {}).get("ledger") or {}
        expect(ledger and all((v.get("entry") or {}).get("state") == "verified" for v in ledger.values()),
               f"ledger of {row['id']}: {ledger}")
    say(f"{checked} per-op line(s) read back in Merxa; ledgers verified")
    return {"per_op_change_sets": len(per_op), "lines": checked}


async def _change_sets(case_id: str) -> list[dict[str, Any]]:
    from keelson_engine.core.db import tenant_session
    from sqlalchemy import text

    async with tenant_session(NW) as s:
        res = await s.execute(text("SELECT id, connector, status, items, preview, result FROM change_set "
                                   "WHERE case_id = :c ORDER BY created_at"), {"c": case_id})
        return [dict(m) for m in res.mappings().all()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--run-timeout", type=float, default=600.0)
    parser.add_argument("--skip-reset", action="store_true")
    parser.add_argument("--prepare-only", action="store_true",
                        help="steps 1-3 only (no model call): check the set-up offline")
    args = parser.parse_args()
    m7.core_init()
    results: dict[str, Any] = {}
    ava: ts.KeelsonUser | None = None
    try:
        if not args.skip_reset:
            results["reset"] = m7.demo_reset()
        ava = ts.keelson_login("ava")
        m7.register_lite(ava)
        results["mappings"] = m7.install_mappings(ava)
        results["crawl"] = m7.crawl(ava)
        results["spec"] = bind_spec(ava)
        nora_roles(ava, ["planner", "approver"])
        if args.prepare_only:
            say("--prepare-only: stopping before the first model call")
            raise SystemExit(0)
        an = confirm_values(args, analyse(args))
        results["apply"] = apply_option(args, an)
        results["read_back"] = read_back(an["case_id"])
    except E2EFailure as exc:
        ts.print_usage_report()
        print(f"FAIL: {exc}", flush=True)
        return 1
    except httpx.HTTPError as exc:
        print(f"FAIL: HTTP error {type(exc).__name__}: {exc}", flush=True)
        return 1
    finally:
        if ava is not None:
            try:
                nora_roles(ts.keelson_login("ava"), ["planner"])
            except (E2EFailure, httpx.HTTPError) as exc:
                print(f"WARN: could not restore nora's roles: {exc}", flush=True)
    ts.print_usage_report()
    heading("Results")
    print(json.dumps(results, indent=2, default=str))
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
