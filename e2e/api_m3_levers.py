#!/usr/bin/env python3
"""M3 levers end to end: trim delay -> the sea-air (or near-shore) option -> human approval -> Merxa.

Runs like ``api_thin_slice.py`` (throwaway Python container on ``keelson_edge``, real Gemini via
LiteLLM, real Keycloak / engine / optimizer / Merxa) and reuses its HTTP helpers. See
e2e/README.md.

Story (``docs/contracts/80-m3-deterministic-core.md`` §3–§4):
 1. reset Merxa (golden seed), resync Keelson's canonical store, disconnect ``marco``'s Merxa link
 2. ``priya`` reports the trim delay; the analysis offers A–D + Do nothing with scorecards and the
    canvas blocks (same checks as the thin slice)
 3. pick the option to apply: ``opt-B`` (rebalance + targeted sea-air) when it buys sea-air, else
    the recommended option when it includes expedite, else a near-shore option (C / D)
 4. ``apply`` -> approval interrupt for a human ``approver`` (expedite / near-shore ops never
    auto-approve) -> ``marco`` connects Merxa and approves -> commit tracker verified
 5. Merxa read-back as ``merxa.admin``: the change set is applied in ONE set with Marco Rossi's
    attribution; sea-air = ``po_line.split`` items whose capacity bookings exist (an explicit
    ``capacity_booking.create`` item, or the booking Merxa's split books itself, with
    ``created_via == change_set:<code>``), the PO line shrank and a new sea-air line exists;
    near-shore = ``po.create`` + ``po_line.change_qty``/``po_line.cancel`` together, the new PO
    exists; audit rows carry the approval and ``via = Keelson``
 6. the engine resyncs the canonical store after the apply (stale mark / webhook, no admin call)

Exit status 0 = pass; 1 = an expectation failed; 2 = usage / environment error.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_thin_slice as ts  # noqa: E402
from api_thin_slice import MERXA, E2EFailure, ascii_text, expect, heading, say  # noqa: E402

EXPEDITE = "expedite_mode_shift"
NEARSHORE = "resource_alt_supplier"


def _premium(option: dict[str, Any], key: str) -> int:
    return int(((option.get("scorecard") or {}).get("cost_breakdown") or {}).get(key) or 0)


def choose_option(an: ts.Analysis) -> tuple[str, str]:
    """``(option_id, kind)`` with kind ``sea_air`` or ``nearshore``; applicable options only."""
    opts = an.options

    def usable(oid: str) -> bool:
        o = opts.get(oid) or {}
        return bool(o) and not o.get("guided_only") and int((o.get("scorecard") or {}).get("changes") or 0) > 0

    if usable("opt-B") and _premium(opts["opt-B"], "expedite_premium") > 0:
        return "opt-B", "sea_air"
    rec = opts.get(an.recommended) or {}
    if usable(an.recommended) and EXPEDITE in (rec.get("levers") or []) and _premium(rec, "expedite_premium") > 0:
        return an.recommended, "sea_air"
    for oid in ("opt-C", "opt-D"):
        if usable(oid) and _premium(opts[oid], "nearshore_premium") > 0:
            return oid, "nearshore"
    raise E2EFailure(
        "no applicable option buys sea-air or near-shore capacity: "
        + ", ".join(f"{k}={(v.get('scorecard') or {}).get('cost_breakdown')}" for k, v in sorted(opts.items()))
    )


def _all_pages(client: httpx.Client, path: str, **params: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    cursor: str | None = None
    while True:
        q = {"limit": 200, **params}
        if cursor:
            q["cursor"] = cursor
        r = client.get(f"{MERXA}{path}", params=q)
        expect(r.status_code == 200, f"merxa {path}: HTTP {r.status_code}")
        body = r.json()
        out.extend(body.get("items") or [])
        cursor = body.get("next_cursor")
        if not cursor:
            return out


def verify_merxa_levers(ext_code: str, kind: str) -> Counter[str]:
    """Read the applied change set back through Merxa REST (``merxa.admin``)."""
    client = ts.merxa_login("merxa.admin")
    r = client.get(f"{MERXA}/v1/change-sets/{ext_code}")
    expect(r.status_code == 200, f"merxa change set {ext_code}: HTTP {r.status_code}")
    cs = r.json()
    items = list(cs.get("items") or [])
    ops = Counter(str(i.get("op")) for i in items)
    say(f"merxa   : {cs['code']} status={cs['status']} items={len(items)} ops={dict(ops)}")
    say(f"merxa   : attribution '{ascii_text(cs.get('attribution_text'))}'")
    expect(cs["status"] == "applied", f"merxa change set status is {cs['status']!r}")
    attribution = str(cs.get("attribution_text") or "")
    expect("Marco Rossi" in attribution and "via Keelson" in attribution,
           f"attribution does not name Marco Rossi via Keelson: {attribution!r}")
    expect(all(i.get("status") == "applied" for i in items), "not every change set item is applied")
    created_via = f"change_set:{cs['code']}"
    creates: list[dict[str, Any]] = []
    reduced: list[dict[str, Any]] = []

    if kind == "sea_air":
        splits = [i for i in items if i.get("op") == "po_line.split"]
        expect(splits, f"sea-air option applied without po_line.split items ({dict(ops)})")
        bookings = [b for b in _all_pages(client, "/v1/capacity-bookings") if b.get("created_via") == created_via]
        explicit = [i for i in items if i.get("op") == "capacity_booking.create"]
        say(f"merxa   : {len(splits)} po_line.split, {len(explicit)} capacity_booking.create item(s); "
            f"{len(bookings)} capacity booking(s) created_via {created_via} "
            f"({sorted({(b['lane_code'], b['iso_week']) for b in bookings})[:6]})")
        # Merxa's po_line.split books the sea-air capacity itself (one confirmed booking per split);
        # an explicit capacity_booking.create item would add its own.
        expect(len(bookings) >= len(splits) + len(explicit), "missing capacity bookings for the sea-air splits")
        split_units = sum(int(i["payload"]["qty"]) for i in splits)
        booked = sum(int(b["qty_units"]) for b in bookings)
        expect(booked >= split_units, f"booked {booked} units < split {split_units} units")
        for item in splits[:5]:  # spot-check the PO lines
            po_code, line_no = str(item["target_ref"]).split("#", 1)
            po = client.get(f"{MERXA}/v1/purchase-orders/{po_code}").json()
            lines = {int(ln["line_no"]): ln for ln in po.get("lines") or []}
            expect(int(line_no) in lines, f"{item['target_ref']} vanished after the split")
            new = [ln for ln in lines.values()
                   if int(ln["line_no"]) > int(line_no) and ln.get("sku") == lines[int(line_no)].get("sku")]
            expect(new, f"{item['target_ref']}: no new line for the sea-air split")
        lanes = sorted({str(i["payload"]["lane_code"]) for i in splits})
        say(f"merxa   : sea-air {split_units} units on {lanes}; PO lines split as planned")
    else:
        creates = [i for i in items if i.get("op") == "po.create"]
        reduced = [i for i in items if i.get("op") in ("po_line.change_qty", "po_line.cancel")]
        expect(creates and reduced, f"near-shore option without po.create + po_line.change_qty/cancel ({dict(ops)})")

    audit = _all_pages(client, "/v1/audit-log", change_set_code=cs["code"])
    rows = [row for row in audit if row.get("approval_ref") == cs.get("approval_ref")]
    if kind == "nearshore":
        new_pos = sorted({str(row.get("entity_ref")) for row in rows
                          if row.get("entity_type") == "purchase_order" and row.get("action") == "create"})
        say(f"merxa   : {len(creates)} po.create + {len(reduced)} original line change(s); new POs {new_pos[:6]}")
        expect(len(new_pos) >= len(creates), f"{len(new_pos)} created POs in the audit trail, {len(creates)} po.create")
        for code in new_pos[:5]:
            po = client.get(f"{MERXA}/v1/purchase-orders/{code}")
            expect(po.status_code == 200 and po.json().get("lines"), f"near-shore PO {code} not readable")
    entities = Counter(str(row.get("entity_type")) for row in rows)
    say(f"merxa   : {len(rows)} audit row(s) for {cs['code']} by entity {dict(entities)}")
    expect(rows, "no audit rows for the applied change set")
    expect(all(row.get("via") == "Keelson" for row in rows), "audit rows not marked via Keelson")
    expect(any(row.get("actor_display") == "Marco Rossi" for row in rows), "audit rows do not name Marco Rossi")
    if kind == "sea_air":
        expect(entities.get("capacity_booking"), "no capacity_booking audit rows")
    return ops


def run_levers(args: argparse.Namespace) -> dict[str, float]:
    if not os.environ.get("DEMO_USER_PASSWORD", ""):
        print("DEMO_USER_PASSWORD is not set", file=sys.stderr)
        raise SystemExit(2)
    ts.reset_demo(args)
    an = ts.analyse(args, title="E2E M3 levers")
    an = ts.confirm_pending_values(args, an)  # M4: values refine_scenario left unconfirmed
    option_id, kind = choose_option(an)
    opt = an.options[option_id]
    say(f"chosen  : {option_id} '{opt.get('title')}' ({kind}; levers {opt.get('levers')}; "
        f"expedite premium {_premium(opt, 'expedite_premium') / 100:.2f}, "
        f"near-shore premium {_premium(opt, 'nearshore_premium') / 100:.2f})")
    approval = ts.request_apply(args, an, option_id)
    snap_before = ts.current_snapshot_id(ts.keelson_login("sam"))
    approved_ms = int(time.time() * 1000)
    ext_code = ts.approve_and_commit(args, an, approval)
    heading("Merxa: read back")
    verify_merxa_levers(ext_code, kind)
    if args.check_resync:
        heading("Keelson: resync after the apply")
        ts.verify_resync_after_apply(snap_before, ext_code, timeout_s=args.sync_timeout, started_after_ms=approved_ms)
    return ts.timings(an)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ts.add_common_args(parser)
    args = parser.parse_args()
    try:
        result = run_levers(args)
    except E2EFailure as exc:
        print(f"\nFAIL: {exc}", flush=True)
        return 1
    except httpx.HTTPError as exc:
        print(f"\nFAIL: HTTP error {type(exc).__name__}: {exc}", flush=True)
        return 1
    heading("PASS")
    for key, value in result.items():
        print(f"  {key:26s} {value:7.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
