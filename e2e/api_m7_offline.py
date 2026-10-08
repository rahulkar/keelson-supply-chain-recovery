"""M7 real-stack checks that never call a model (docs/contracts/110 §1, §2, §4, §5, §7), offline.

Runs inside the engine image with the engine's own environment, like the M5/M6 offline scripts
(``docker compose run --rm --no-deps -T -v D:/primark/e2e:/e2e:ro engine-api python -u
/e2e/api_m7_offline.py``): HTTP through Caddy as a browser would (Keelson, Merxa), plus the
engine's own runtime functions for the per-op commit harness (step 5). No chat is ever posted and
no run is queued, so the worker never reaches a model.

Steps:
1. Demo reset (``ava``): every step ok, including the ``signal_hub`` step.
2. Generic Tier-A proof, northwind-test (``ava`` cross-tenant, real Merxa ``/mcp-lite``): mint a
   Merxa API key (``changes:apply`` + the read scopes; earlier e2e keys revoked), register
   ``merxa-lite`` (``static_header`` ``X-Merxa-Api-Key``; the key is never echoed), test → tier
   ``[A]``, approve ``list_items`` / ``get_item`` as trusted read tools and ``update_item`` /
   ``create_transfer_order`` as ``best_effort`` write tools (never bound to an agent), activate,
   install pack ``foreign-erp-lite``'s read + action mappings (``from-pack``; a second install is
   unchanged), read-mapping test (one page per source), then the **crawl sync**: snapshot
   ``consistency: crawled`` with an ``as_of_range``, not truncated, and per-entity row counts equal
   to what Merxa itself reports (``list_items`` totals; locations = DCs + stores + distinct clusters).
3. Action-mapping dry-run (``nora``'s tenant through ``ava``): ``transfer_order.create`` and
   ``allocation.set_qty`` render the exact ``create_transfer_order`` / ``update_item`` calls (never
   executed: the allocation's version is unchanged afterwards) and run their read-backs;
   ``po_line.reschedule`` is unmapped → a guided reason.
4. Four-eyes (demo-retail): sam raises the auto caps → 202 change request (nothing applied); sam
   cannot approve his own request (403); ava approves → applied (new spec version, auto caps
   raised). A second request whose base moved (sam edits the spec meanwhile) → 409 ``stale_base``
   and stays pending; ava rejects it with a reason. sam restores the caps (lowering is not guarded).
5. Per-op commit harness (northwind-test; **in-process, documented test-only harness**: no approver
   persona exists in northwind-test and a plan needs a model, so the engine's own runtime functions
   run the approved-change-set path a worker would run after a human approval): a seeded case +
   plan option with two mapped lines (``transfer_order.create`` + ``allocation.set_qty``) and one
   unmapped line (``po_line.reschedule``) → the capability table routes the unmapped op to a guided
   checklist with generated instructions; the mapped lines compile into a per-op change set whose
   dry run renders the calls (``preview.mode = per_op``, ``calls_hash``); the seeded approval applies
   them **one call per line** through the commit ledger (``create_transfer_order`` / ``update_item``
   with per-line request ids, apply principal), reconcile reads them back → ``verified``; Merxa
   holds the new transfer order and the allocation's new quantity; a second apply sends nothing.
6. Users (demo-retail, real Keycloak): sam invites a user (temporary password never returned;
   required action ``UPDATE_PASSWORD``, tenant attribute), sets roles (planner + approver), granting
   ``tenant-admin`` → 202 change request (ava rejects it), disables the user; the user is deleted
   from Keycloak at the end (also on failure).
7. Signals admin round-trip (demo-retail ↔ Signal Hub): sources (toggle one subscription and
   restore it), watch regions (add a radius region around a network node, read it back, restore),
   alert rules (re-save the current set): every write is pushed to the hub (``hub_synced``).
8. Clean-up: the lite connector is disabled (its demo-scoped API key dies with the next Merxa
   reset); ``--skip-final-reset`` keeps the data, otherwise a final demo reset.

Exit 0 pass, 1 expectation failed (``FAIL:`` line), 2 environment missing.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import json
import random
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_thin_slice as ts  # noqa: E402
from api_m2_admin import admin, items_of, ok  # noqa: E402
from api_thin_slice import KEELSON, MERXA, E2EFailure, expect, heading, say  # noqa: E402

TENANT = "demo-retail"
NW = "northwind-test"
LITE = "merxa-lite"
LITE_URL = f"{MERXA}/mcp-lite"
PACK = "foreign-erp-lite"
KEY_PREFIX = "keelson-m7-e2e-"
READ_TOOLS = ("list_items", "get_item")
WRITE_TOOLS = ("update_item", "create_transfer_order")
CRAWL_TIMEOUT_S = 1200.0
E2E_USER_PREFIX = "e2e-m7-"

# ───────────────────────────────── engine core ─────────────────────────────────

_LOOP = asyncio.new_event_loop()


def db(coro: Any) -> Any:
    return _LOOP.run_until_complete(coro)


def core_init() -> None:
    try:
        from keelson_engine.core.db import init_db
    except ImportError as exc:  # not in the engine image
        print(f"ENV: run this inside the engine image ({exc})", file=sys.stderr)
        raise SystemExit(2) from exc
    db(init_db())


def lite_call(tool: str, args: dict[str, Any], *, principal: str = "reader") -> dict[str, Any]:
    """One /mcp-lite call through the engine's own MCP gateway (the registered connector's key)."""
    from keelson_engine.runtime.nodes.per_op import connector_call

    return dict(db(connector_call(tenant=NW, connector=LITE, principal=principal, tool=tool, args=args)))


def lite_total(type_: str, filters: dict[str, Any] | None = None) -> int:
    args: dict[str, Any] = {"type": type_, "page_size": 1}
    if filters:
        args["filter_json"] = json.dumps(filters)
    return int(lite_call("list_items", args)["total"])


def lite_items(type_: str, filters: dict[str, Any] | None = None, *, page: int = 1, size: int = 200) -> list[dict[str, Any]]:
    args: dict[str, Any] = {"type": type_, "page_size": size, "page": page}
    if filters:
        args["filter_json"] = json.dumps(filters)
    return list(lite_call("list_items", args)["items"])


def wait_until(what: str, fn: Any, *, timeout_s: float, every_s: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise E2EFailure(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(every_s)


# ───────────────────────────────── 1. demo reset ─────────────────────────────────


def demo_reset() -> dict[str, Any]:
    heading("1. Demo reset (incl. the Signal Hub)")
    ava = ts.keelson_login("ava")
    started = time.monotonic()
    r = ava.client.post(f"{KEELSON}/api/demo/reset", json={}, headers=ava.headers(), timeout=180.0)
    wall = time.monotonic() - started
    expect(r.status_code == 200, f"demo reset: HTTP {r.status_code} {r.text[:300]}")
    log = r.json()
    for s in log["steps"]:
        say(f"  {s['step']:<22} {s['status']:<8} {s['duration_ms']:>6} ms  {ts.ascii_text(s.get('detail') or '', 70)}")
    steps = {s["step"]: s for s in log["steps"]}
    expect(log["ok"] is True, f"demo reset failed steps: {[s for s, v in steps.items() if v['status'] == 'failed']}")
    expect(steps.get("signal_hub", {}).get("status") == "ok", f"signal_hub step: {steps.get('signal_hub')}")
    expect(wall < 60.0, f"demo reset took {wall:.1f} s")
    say(f"demo reset ok in {wall:.1f} s")
    return {"wall_s": round(wall, 1), "steps": list(steps)}


# ─────────────────────────────── 2. Tier-A proof: register + crawl ───────────────────────────────


def mint_key() -> str:
    client = ts.merxa_login("merxa.admin")
    scopes = ["catalog:read", "inventory:read", "po:read", "orders:read", "changes:apply"]
    r = client.post(f"{MERXA}/v1/api-keys", json={"name": f"{KEY_PREFIX}{int(time.time())}", "scopes": scopes},
                    headers={"Origin": MERXA, "Idempotency-Key": str(uuid.uuid4())})
    expect(r.status_code == 201, f"mint Merxa API key: HTTP {r.status_code} {r.text[:200]}")
    body = r.json()
    key = str(body.get("key") or "")
    expect(key.startswith("mx_"), "minted key has an unexpected format")
    listed = client.get(f"{MERXA}/v1/api-keys")
    expect(listed.status_code == 200, f"list Merxa API keys: HTTP {listed.status_code}")
    stale = [k for k in items_of(listed.json()) if str(k.get("name", "")).startswith(KEY_PREFIX)
             and not k.get("revoked_at") and k.get("id") != body.get("id")]
    for k in stale:
        rv = client.post(f"{MERXA}/v1/api-keys/{k['id']}:revoke", headers={"Origin": MERXA})
        expect(rv.status_code == 200, f"revoke old key: HTTP {rv.status_code}")
    say(f"Merxa API key minted (prefix {body.get('prefix')}, scopes {body.get('scopes')}); {len(stale)} old revoked")
    return key


def bound_connectors(spec: dict[str, Any]) -> set[str]:
    """Connectors the agent spec plans, answers or investigates with (a bound connector is never deleted)."""
    out = {str(spec.get("connector") or ""), str(spec.get("answer_connector") or "")}
    out |= {str(s.get("connector") or "") for s in spec.get("specialists") or [] if isinstance(s, dict) and s.get("tools")}
    return out - {""}


def register_lite(ava: ts.KeelsonUser) -> None:
    heading("2a. Register Merxa /mcp-lite for northwind-test (static header) and classify its tools")
    existing = {c.get("slug"): c for c in items_of(ok(admin(ava, "GET", "/connectors", tenant=NW), "list connectors"))}
    key = mint_key()
    spec = ok(admin(ava, "GET", "/agents/spec", tenant=NW), "spec")
    if LITE in existing and LITE in bound_connectors(spec["spec"]):
        # a live run (api_m7_foreign_erp.py) plans on it: keep it, rotate its API key in place
        rot = ok(admin(ava, "PUT", f"/connectors/{LITE}/secret", tenant=NW, body={"secret": key}), "rotate the API key")
        expect(key not in json.dumps(rot), "the API key is echoed back by the rotation")
        say("merxa-lite is bound by the agent spec: API key rotated in place")
    else:
        if LITE in existing:
            ok(admin(ava, "DELETE", f"/connectors/{LITE}", tenant=NW), "delete the previous lite connector", 204)
            say("removed the merxa-lite connector of a previous run")
        body = {"slug": LITE, "name": "Merxa (mcp-lite, API key)", "mcp_url": LITE_URL, "trust_level": "third_party",
                "auth": {"kind": "static_header", "header_name": "X-Merxa-Api-Key", "secret": key}}
        created = ok(admin(ava, "POST", "/connectors", tenant=NW, body=body), "register connector", 201)
        expect(created.get("status") == "draft", f"new connector status {created.get('status')}")
        expect(key not in json.dumps(created), "the API key is echoed back by the registry")
    test = ok(admin(ava, "POST", f"/connectors/{LITE}/test", tenant=NW, timeout=90.0), "test connection")
    expect(test.get("ok") is True, f"connection test failed: {test.get('steps')}")
    expect(list(test.get("tier") or []) == ["A"], f"tier {test.get('tier')}, expected ['A']")
    expect(key not in json.dumps(test), "the API key appears in the test log")
    names = {str(x.get("name")) for x in test.get("tools") or []}
    expect(set(READ_TOOLS) | set(WRITE_TOOLS) <= names, f"tools {sorted(names)}")
    for name in READ_TOOLS:
        ok(admin(ava, "PATCH", f"/connectors/{LITE}/tools/{name}", tenant=NW,
                 body={"status": "approved", "access": "read", "content_trust": "trusted_data"}), f"approve {name}")
    for name in WRITE_TOOLS:
        ok(admin(ava, "PATCH", f"/connectors/{LITE}/tools/{name}", tenant=NW,
                 body={"status": "approved", "access": "write", "content_trust": "trusted_data",
                       "idempotency": "best_effort"}), f"approve write tool {name}")
    status = {c.get("slug"): c for c in items_of(ok(admin(ava, "GET", "/connectors", tenant=NW), "list"))}[LITE].get("status")
    if status != "active":
        act = ok(admin(ava, "POST", f"/connectors/{LITE}/activate", tenant=NW), "activate")
        expect(act.get("status") == "active", f"activate -> {act}")
    tools = {str(t.get("name")): t for t in items_of(ok(admin(ava, "GET", f"/connectors/{LITE}/tools", tenant=NW), "tools"))}
    for name in WRITE_TOOLS:
        b = tools[name].get("binding") or (tools[name].get("classification") or {}).get("binding")
        expect(b != "auto", f"write tool {name} must not be auto-bound ({b})")
    say(f"tier [A]; reads {list(READ_TOOLS)} approved (trusted), writes {list(WRITE_TOOLS)} approved (best_effort)")


def install_mappings(ava: ts.KeelsonUser) -> dict[str, Any]:
    heading("2b. Install the foreign-erp-lite mappings and test the read mapping")
    first = ok(admin(ava, "POST", f"/connectors/{LITE}/mappings/from-pack", tenant=NW, body={"pack": PACK}),
               "install pack mappings")
    expect(set(first["mappings"]) == {"read", "action"}, f"installed {first['mappings']}")
    again = ok(admin(ava, "POST", f"/connectors/{LITE}/mappings/from-pack", tenant=NW, body={"pack": PACK}),
               "install pack mappings again")
    expect(all(v["unchanged"] for v in again["mappings"].values()), f"a second install changed {again['mappings']}")
    for kind in ("read", "action"):
        m = ok(admin(ava, "GET", f"/connectors/{LITE}/mappings/{kind}", tenant=NW), f"GET {kind} mapping")
        expect(m.get("version") and not m.get("problems"), f"{kind} mapping problems: {m.get('problems')}")
    stale = admin(ava, "PUT", f"/connectors/{LITE}/mappings/read", tenant=NW,
                  body={"base_version": None, "mapping": {"entities": []}})
    expect(stale.status_code in (409, 422), f"a PUT on a stale base must be refused: HTTP {stale.status_code}")
    caps = ok(admin(ava, "GET", f"/connectors/{LITE}/capabilities", tenant=NW), "capabilities")
    say(f"mappings read v{first['mappings']['read']['version']} / action v{first['mappings']['action']['version']}; "
        f"capabilities {ts.ascii_text(json.dumps(caps), 200)}")
    test = ok(admin(ava, "POST", f"/connectors/{LITE}/mappings/read/test", tenant=NW, body={"max_pages": 1},
                    timeout=120.0), "read mapping test")
    entities = test.get("entities") or test.get("items") or []
    rows = {str(e.get("entity")): e for e in (entities if isinstance(entities, list) else [])}
    expect(rows, f"read test returned no entities: {ts.ascii_text(json.dumps(test), 300)}")
    errors = {k: v.get("error") for k, v in rows.items() if v.get("error")}
    expect(not errors, f"read test errors: {errors}")
    say("read test (1 page per source): " + ", ".join(f"{k}={v.get('rows')}" for k, v in sorted(rows.items())))
    return {"read_version": first["mappings"]["read"]["version"], "action_version": first["mappings"]["action"]["version"]}


def merxa_counts() -> dict[str, int]:
    """What Merxa itself reports through /mcp-lite (list_items totals)."""
    stores = []
    page = 1
    while True:
        batch = lite_items("store", page=page)
        stores += batch
        if len(batch) < 200:
            break
        page += 1
    clusters = {s.get("cluster_code") for s in stores if s.get("cluster_code")}
    return {
        "location": lite_total("dc") + len(stores) + len(clusters),
        "item": lite_total("style") + lite_total("sku"),
        "po_line": lite_total("po_line"),
        "inventory_position": lite_total("inventory"),
        "demand_forecast": lite_total("forecast"),
        "allocation": lite_total("allocation"),
    }


def crawl(ava: ts.KeelsonUser) -> dict[str, Any]:
    heading("2c. Tier-A crawl sync (worker) → crawled snapshot")
    before = ok(admin(ava, "GET", f"/connectors/{LITE}/crawl", tenant=NW), "crawl status")
    prev_snapshot = before.get("snapshot_id")
    q = ok(admin(ava, "POST", f"/connectors/{LITE}/crawl", tenant=NW), "queue crawl", 202)
    expect(q.get("status") == "queued", f"crawl queued: {q}")
    started = time.monotonic()
    last: dict[str, Any] = {}

    def done() -> dict[str, Any] | None:
        nonlocal last
        cur = ok(admin(ava, "GET", f"/connectors/{LITE}/crawl", tenant=NW), "crawl status")
        if cur != last and cur.get("status") != last.get("status"):
            say(f"  crawl {cur.get('status')} ({time.monotonic() - started:.0f} s)")
        last = cur
        if cur.get("status") == "failed":
            raise E2EFailure(f"crawl failed: {cur.get('error')}")
        if cur.get("status") in ("succeeded", "partial") and cur.get("snapshot_id") and cur["snapshot_id"] != prev_snapshot:
            return cur
        return None

    status = wait_until("the crawl sync", done, timeout_s=CRAWL_TIMEOUT_S, every_s=5.0)
    wall = time.monotonic() - started
    expect(status["status"] == "succeeded", f"crawl status {status['status']} (truncated?) {status.get('crawl')}")
    expect(status["consistency"] == "crawled", f"snapshot consistency {status['consistency']}")
    rng = status.get("as_of_range") or {}
    expect(rng.get("from") and rng.get("to") and str(rng["from"]) <= str(rng["to"]), f"as_of_range {rng}")
    crawl_info = status.get("crawl") or {}
    expect(not crawl_info.get("truncated"), f"crawl truncated: {crawl_info}")
    got = {k: int(v) for k, v in (status.get("entities") or {}).items()}
    want = merxa_counts()
    say(f"crawled in {wall:.0f} s: pages {crawl_info.get('pages')}, rows seen {crawl_info.get('rows_seen')}")
    for entity in sorted(want):
        say(f"  {entity:<20} crawled {got.get(entity, 0):>7}   Merxa {want[entity]:>7}")
    mismatched = {e: (got.get(e, 0), n) for e, n in want.items() if got.get(e, 0) != n}
    expect(not mismatched, f"crawled row counts differ from Merxa: {mismatched}")
    extra = sorted(set(got) - set(want))
    expect(not extra, f"crawled entities not in the read mapping: {extra}")
    snaps = ok(admin(ava, "GET", f"/connectors/{LITE}/snapshots?limit=3", tenant=NW), "snapshots")
    cur = snaps.get("current") or {}
    label = cur.get("label") or {}
    expect(label.get("consistency") == "crawled" or cur.get("consistency") == "crawled", f"current snapshot {cur}")
    say(f"snapshot {status['snapshot_id']} crawled, as of {rng.get('from')} … {rng.get('to')}")
    return {"wall_s": round(wall), "rows": got, "pages": crawl_info.get("pages"), "snapshot_id": status["snapshot_id"]}


# ─────────────────────────────── 3. action-mapping dry-run ───────────────────────────────


def pick_allocation() -> dict[str, Any]:
    # allocations hold 1-4 units: one with 3 can go down by one (dry-run render, harness apply)
    items = lite_items("allocation", {"status": "planned", "qty": 3}, size=50)
    expect(items, "no allocation in Merxa to work with")
    return items[len(items) // 2]


def dry_run(ava: ts.KeelsonUser) -> dict[str, Any]:
    heading("3. Action mapping dry-run (rendered, never executed) + read-back")
    alloc = pick_allocation()
    key, version, qty = str(alloc["key"]), int(alloc["version"]), int(alloc["qty"])
    r = ok(admin(ava, "POST", f"/connectors/{LITE}/mappings/action/dry-run", tenant=NW, body={
        "op": "allocation.set_qty", "target_ref": key, "expected_version": version, "payload": {"qty": qty - 1}}),
        "dry-run allocation.set_qty")
    expect(r["mapped"] is True and r["executed"] is False, f"dry-run {r}")
    call = r["call"]
    expect(call["tool"] == "update_item" and call["args"]["type"] == "allocation" and call["args"]["key"] == key
           and call["args"]["expected_version"] == version and json.loads(call["args"]["fields_json"]) == {"qty": qty - 1},
           f"rendered update_item {call}")
    rb = r.get("readback") or {}
    expect(rb.get("executed") is True and rb.get("ok") is True and rb.get("matched") is False,
           f"read-back of the unchanged allocation must run and not match: {rb}")
    after = lite_call("get_item", {"type": "allocation", "key": key})["item"]
    expect(int(after["version"]) == version and int(after["qty"]) == qty, f"the dry-run changed Merxa: {after}")
    say(f"allocation.set_qty → update_item(allocation {key}, qty {qty - 1}, expected_version {version}); "
        f"read-back ran ({rb.get('items_found')} item, no match); Merxa unchanged")
    t = ok(admin(ava, "POST", f"/connectors/{LITE}/mappings/action/dry-run", tenant=NW, body={
        "op": "transfer_order.create",
        "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": "2027-03-01", "lines": [{"sku": "X-1", "qty": 3}]}}),
        "dry-run transfer_order.create")
    expect(t["mapped"] is True and t["call"]["tool"] == "create_transfer_order"
           and json.loads(t["call"]["args"]["lines_json"]) == [{"sku": "X-1", "qty": 3}]
           and t["request_id_arg"] == "request_id" and t["idempotency"] == "best_effort", f"rendered TO {t}")
    expect((t.get("readback") or {}).get("executed") is True, f"TO read-back {t.get('readback')}")
    g = ok(admin(ava, "POST", f"/connectors/{LITE}/mappings/action/dry-run", tenant=NW, body={
        "op": "po_line.reschedule", "target_ref": "PO-1#1", "expected_version": 1, "payload": {"eta_dc": "2027-03-01"}}),
        "dry-run unmapped op")
    expect(g["mapped"] is False and g["guided_reason"], f"unmapped op {g}")
    say(f"transfer_order.create → create_transfer_order (request_id arg, best_effort); po_line.reschedule → guided "
        f"({ts.ascii_text(g['guided_reason'], 80)})")
    return {"allocation": key}


# ─────────────────────────────── 4. four-eyes ───────────────────────────────


def four_eyes() -> dict[str, Any]:
    heading("4. Four-eyes: guarded spec change → change request → second admin")
    sam, ava = ts.keelson_login("sam"), ts.keelson_login("ava")
    spec = ok(admin(sam, "GET", "/agents/spec"), "spec")
    policy = spec["spec"]["policy"]
    lines, units = int(policy["auto_max_lines"]), int(policy["auto_max_units"])
    r = admin(sam, "PUT", "/agents/spec", body={"base_version": spec["version"],
                                                "spec_patch": {"policy": {"auto_max_lines": lines + 3}}})
    cr = ok(r, "sam raises the auto cap", 202)
    expect(cr["status"] == "pending" and cr["kind"] == "agent_spec" and cr["base_version"] == spec["version"], f"CR {cr}")
    expect(ok(admin(sam, "GET", "/agents/spec"), "spec")["version"] == spec["version"], "the guarded change was applied")
    queue = ok(admin(sam, "GET", "/change-requests?status=pending"), "pending queue")
    expect(cr["id"] in [x["id"] for x in queue["items"]] and queue["pending"] >= 1, "request not in the pending queue")
    own = admin(sam, "POST", f"/change-requests/{cr['id']}/approve", body={})
    expect(own.status_code == 403, f"self-approval must be refused: HTTP {own.status_code} {own.text[:200]}")
    priya = ts.keelson_login("priya")
    expect(admin(priya, "POST", f"/change-requests/{cr['id']}/approve", body={}).status_code == 403,
           "a planner must not decide")
    done = ok(admin(ava, "POST", f"/change-requests/{cr['id']}/approve", body={"reason": "e2e: reviewed"}),
              "ava approves")
    expect(done["status"] == "applied" and done["approver"]["username"] == "ava", f"approved CR {done}")
    after = ok(admin(sam, "GET", "/agents/spec"), "spec")
    expect(after["version"] != spec["version"] and int(after["spec"]["policy"]["auto_max_lines"]) == lines + 3,
           f"applied spec {after['version']} {after['spec']['policy']}")
    say(f"sam → 202 {cr['id'][:8]} (pending; self-approval 403, planner 403); ava approved → {after['version']}")

    r2 = admin(sam, "PUT", "/agents/spec", body={"base_version": after["version"],
                                                 "spec_patch": {"policy": {"auto_max_units": units + 100}}})
    cr2 = ok(r2, "sam raises the unit cap", 202)
    moved = ok(admin(sam, "PUT", "/agents/spec", body={
        "base_version": after["version"],
        "spec_patch": {"prompts": {"recommender": "E2E M7: prefer fewer transfer lines."}}}),
        "sam edits the spec meanwhile", 201)
    stale = admin(ava, "POST", f"/change-requests/{cr2['id']}/approve", body={})
    expect(stale.status_code == 409 and stale.json().get("code") == "stale_base",
           f"stale base must be 409: HTTP {stale.status_code} {stale.text[:200]}")
    expect(ok(admin(ava, "GET", f"/change-requests/{cr2['id']}"), "CR2")["status"] == "pending", "stale CR left pending")
    no_reason = admin(ava, "POST", f"/change-requests/{cr2['id']}/reject", body={})
    expect(no_reason.status_code in (400, 422), f"a reject needs a reason: HTTP {no_reason.status_code}")
    rej = ok(admin(ava, "POST", f"/change-requests/{cr2['id']}/reject", body={"reason": "e2e: base moved"}), "reject")
    expect(rej["status"] == "rejected", f"rejected CR {rej}")
    say(f"second request on base {after['version']} → base moved to {moved['version']} → 409 stale_base; rejected")
    restore = ok(admin(sam, "PUT", "/agents/spec", body={
        "base_version": moved["version"],
        "spec_patch": {"policy": {"auto_max_lines": lines}, "prompts": {"recommender": spec["spec"].get("prompts", {}).get("recommender", "")}}}),
        "sam restores the caps (not guarded)", 201)
    expect(int(restore["spec"]["policy"]["auto_max_lines"]) == lines, "restore failed")
    audit = items_of(ok(admin(ava, "GET", "/audit?limit=200"), "audit"))
    acts = {str(a.get("action")) for a in audit}
    expect(any(a.startswith("change_request") for a in acts), f"change requests not audited: {sorted(acts)[:20]}")
    say(f"sam lowered the cap again → {restore['version']}; audit has the change-request rows")
    return {"applied": cr["id"], "stale": cr2["id"]}


# ─────────────────────────────── 5. per-op commit harness ───────────────────────────────


def merxa_today() -> dt.date:
    mx = ts.merxa_login("priya.shah")
    r = mx.get(f"{MERXA}/v1/capacity-bookings/CB-0001")
    expect(r.status_code == 200, f"Merxa sim today: HTTP {r.status_code}")
    return dt.date.fromisoformat(str(r.json()["today"]))


def per_op_harness() -> dict[str, Any]:
    heading("5. Per-op commit (test-only in-process harness): mapped lines applied, unmapped guided")
    from keelson_contracts.runs import Actor

    from keelson_engine.core import repo as core_repo
    from keelson_engine.core.agent_specs import latest_version_name
    from keelson_engine.runtime.candidates import capability_table
    from keelson_engine.runtime.commit import compile_changeset, stored_items
    from keelson_engine.runtime.context import RunContext
    from keelson_engine.runtime.deps import get_deps
    from keelson_engine.runtime.guide import build_checklist, checklist_id_for
    from keelson_engine.runtime.nodes.per_op import apply_per_op, dry_run_per_op, verify_per_op
    from keelson_engine.runtime.remediation import partition_actions
    from keelson_engine.runtime.runner_cache import default_spec_loader

    nora = ts.keelson_login("nora")
    r = nora.client.post(f"{KEELSON}/api/cases", json={"title": "M7 e2e: per-op commit on the foreign ERP"},
                         headers=nora.headers())
    expect(r.status_code == 201, f"nora creates a case: HTTP {r.status_code} {r.text[:200]}")
    case_id = str(r.json()["id"])

    # inputs from Merxa itself: DE1 stock, a free ship date, an allocation, an open PO line
    stock = [i for i in lite_items("inventory", {"location_type": "dc", "location_code": "DE1"}, size=200)
             if int(i["on_hand"]) - int(i["allocated"]) - int(i["reserved"]) >= 30]
    expect(stock, "no DE1 stock to transfer")
    sku = str(stock[0]["sku_code"])
    today = merxa_today()
    rnd = random.Random()
    for _ in range(20):
        ship = (today + dt.timedelta(days=rnd.randint(40, 400))).isoformat()
        if not lite_items("transfer_order", {"from_dc": "DE1", "to_dc": "NL1", "ship_date": ship}, size=5):
            break
    else:
        raise E2EFailure("no free DE1→NL1 ship date found")
    alloc = pick_allocation()
    a_key, a_ver, a_qty = str(alloc["key"]), int(alloc["version"]), int(alloc["qty"])
    po_line = lite_items("po_line", {"status": "open"}, size=5)
    expect(po_line, "no open PO line")
    po_ref, po_ver = str(po_line[0]["key"]), int(po_line[0]["version"])
    actions = [
        {"seq": 1, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
         "payload": {"from_dc": "DE1", "to_dc": "NL1", "ship_date": ship, "lines": [{"sku": sku, "qty": 12}]}},
        {"seq": 2, "op": "allocation.set_qty", "target_ref": a_key, "expected_version": a_ver,
         "payload": {"qty": a_qty - 1}},
        {"seq": 3, "op": "po_line.reschedule", "target_ref": po_ref, "expected_version": po_ver,
         "payload": {"eta_dc": (today + dt.timedelta(days=60)).isoformat()}},
    ]
    option = {"id": "opt-e2e", "label": "E2E per-op", "actions": actions}
    db(core_repo.save_plan(case_id, 1, [option, {"id": "do-nothing", "label": "Do nothing", "actions": []}],
                           "opt-e2e", {}, tenant=NW))

    deps = get_deps()
    spec = db(default_spec_loader(NW, db(latest_version_name(NW))))
    actor = Actor(sub=str(nora.me["user"]["sub"]), username="nora", name="Nora Test", roles=["planner"])
    run = RunContext(tenant=NW, case_id=case_id, run_id=f"e2e-m7-{uuid.uuid4().hex[:10]}", kind="action",
                     actor=actor, payload={}, spec=spec, deps=deps, invocation_id=f"e2e-{uuid.uuid4().hex[:10]}",
                     case_title="M7 e2e per-op")
    table = db(capability_table(deps, tenant=NW, connectors=[LITE], vendor="Merxa lite"))
    caps = table.get(LITE)
    expect(caps is not None and caps.per_op, f"{LITE} is not a per-op connector: {caps}")
    part = partition_actions(actions, table)
    expect([a["op"] for a in part.apply.get(LITE, [])] == ["transfer_order.create", "allocation.set_qty"],
           f"apply lines {part.apply}")
    guided = [a for v in part.guide.values() for a in v]
    expect([a["op"] for a in guided] == ["po_line.reschedule"], f"guide lines {part.guide}")

    # the unmapped op → a guided checklist with generated instructions (what compile_changeset builds)
    checklist_id = checklist_id_for(tenant=NW, case_id=case_id, plan_version=1, option_id="opt-e2e",
                                    connector=LITE, nonce=run.invocation_id)
    built = db(build_checklist(deps, tenant=NW, case_id=case_id, run_id=run.run_id, connector=LITE, vendor="Merxa lite",
                               actions=guided, checklist_id=checklist_id, plan_version=1, option_id="opt-e2e",
                               tools=set(caps.tools), config={}, sop=False, kb_ids=None))
    expect(len(built.steps) == 1 and (built.steps[0].get("instructions_md") or "").strip(), f"guide steps {built.steps}")
    db(deps.checklists.create_checklist(tenant=NW, case_id=case_id, checklist_id=checklist_id, connector=LITE,
                                        plan_version=1, option_id="opt-e2e", steps=built.steps, meta=built.meta))
    r = nora.client.get(f"{KEELSON}/api/cases/{case_id}/checklists")
    expect(r.status_code == 200 and any(c["id"] == checklist_id for c in r.json()["items"]),
           f"nora sees the guided checklist: HTTP {r.status_code}")
    say(f"po_line.reschedule {po_ref} → guided step: {ts.ascii_text(built.steps[0].get('title'), 80)}")

    lines = [dict(a) for a in part.apply[LITE]]
    for seq, item in enumerate(lines, start=1):
        item["seq"] = seq
    draft = compile_changeset(tenant=NW, change_set_id="draft", connector=LITE, actions=lines)
    cs_id = db(deps.repo.create_change_set(tenant=NW, case_id=case_id, plan_version=1, option_id="opt-e2e",
                                           connector=LITE, items=stored_items(draft, lines), hash=draft.hash))

    def load() -> tuple[dict[str, Any], Any]:
        row = db(deps.repo.get_change_set(tenant=NW, change_set_id=cs_id))
        cs = compile_changeset(tenant=NW, change_set_id=cs_id, connector=LITE, actions=list(row["items"]),
                               revision=int(row.get("revision") or 1))
        expect(cs.hash == row["hash"], "stored change-set hash mismatch")
        return row, cs

    row, cs = load()

    async def _dry() -> dict[str, Any]:
        outcome: dict[str, Any] = {}
        async for item in dry_run_per_op(run, row, cs, system="Merxa lite", plan_version=1, option_id="opt-e2e",
                                         problem=None, multi=False):
            if isinstance(item, dict) and "ok" in item and "totals" in item:
                outcome = item
        return outcome

    outcome = db(_dry())
    expect(outcome.get("ok") is True, f"per-op dry run: {outcome}")
    row, cs = load()
    preview = row.get("preview") or {}
    expect(row["status"] == "validated" and preview.get("mode") == "per_op" and preview.get("calls_hash"), f"preview {preview}")
    calls = preview["calls"]
    expect([c["tool"] for c in calls] == ["create_transfer_order", "update_item"], f"rendered calls {calls}")
    expect(all(c["args"].get("request_id") for c in calls), f"per-line request ids missing: {calls}")
    expect(lite_items("transfer_order", {"from_dc": "DE1", "to_dc": "NL1", "ship_date": ship}, size=5) == [],
           "the dry run created a transfer order")
    say(f"change set {cs_id[:8]}: dry run rendered {len(calls)} calls (per_op, calls_hash {preview['calls_hash'][:10]}…)")

    # the seeded approval (what the approval interrupt records for a human decision)
    db(deps.repo.update_change_set(tenant=NW, change_set_id=cs_id, status="applying"))
    row, cs = load()
    keep = {"requirement": {"kind": "e2e_harness"}, "approved_by": "user:e2e-harness"}
    summary = db(apply_per_op(run, row, cs, principal="autocommit", token=None, keep=keep))
    expect(summary["status"] == "applied" and summary["applied"] == 2 and not summary["guided_seqs"],
           f"per-op apply summary {summary}")
    row, cs = load()
    ledger = (row.get("result") or {}).get("ledger") or {}
    expect(set(ledger) == {"1", "2"}, f"ledger lines {list(ledger)}")
    verified = db(verify_per_op(run, row, cs))
    expect(verified["row_status"] == "verified" and verified["verified"] == 2, f"reconcile {verified}")
    entries = db(_ledger_rows(cs_id))
    states = {(e["line_seq"], e["state"]) for e in entries}
    expect({(1, "verified"), (2, "verified")} <= states, f"commit_ledger rows {entries}")
    expect({e["tool"] for e in entries} == {"create_transfer_order", "update_item"}
           and {e["idempotency"] for e in entries} == {"best_effort"}, f"ledger tools {entries}")
    tos = lite_items("transfer_order", {"from_dc": "DE1", "to_dc": "NL1", "ship_date": ship, "created_via": "mcp-lite"})
    expect(len(tos) == 1, f"Merxa transfer orders for {ship}: {tos}")
    a_now = lite_call("get_item", {"type": "allocation", "key": a_key})["item"]
    expect(int(a_now["qty"]) == a_qty - 1 and int(a_now["version"]) == a_ver + 1, f"allocation now {a_now}")
    say(f"applied per op: {tos[0]['code']} (DE1→NL1 {sku} × 12, ship {ship}); allocation {a_key} qty {a_qty}→{a_qty - 1}; "
        "both read back → verified")

    again = db(apply_per_op(run, row, cs, principal="autocommit", token=None, keep=keep))
    expect(again["applied"] == 2, f"second apply {again}")
    tos2 = lite_items("transfer_order", {"from_dc": "DE1", "to_dc": "NL1", "ship_date": ship, "created_via": "mcp-lite"})
    a2 = lite_call("get_item", {"type": "allocation", "key": a_key})["item"]
    expect(len(tos2) == 1 and int(a2["version"]) == a_ver + 1, "a second apply sent a line again")
    say("a second apply skips the settled lines (nothing sent twice)")
    return {"case_id": case_id, "change_set_id": cs_id, "transfer_order": tos[0]["code"], "allocation": a_key,
            "guided": po_ref}


async def _ledger_rows(cs_id: str) -> list[dict[str, Any]]:
    from keelson_engine.core.db import tenant_session
    from sqlalchemy import text

    async with tenant_session(NW) as s:
        res = await s.execute(text("SELECT line_seq, state, tool, idempotency, attempts, request_id FROM commit_ledger "
                                   "WHERE change_set_id = :c ORDER BY line_seq"), {"c": cs_id})
        return [dict(m) for m in res.mappings().all()]


# ─────────────────────────────── 6. users ───────────────────────────────


def _kc_delete_user(user_id: str) -> None:
    from keelson_engine.core.keycloak_admin import KeycloakAdmin

    async def go() -> None:
        kc = KeycloakAdmin()
        token = await kc._token()  # noqa: SLF001 - e2e clean-up (the API only disables)
        await kc._req("DELETE", f"/admin/realms/keelson/users/{user_id}", token)  # noqa: SLF001

    db(go())


def _kc_user(user_id: str) -> dict[str, Any] | None:
    from keelson_engine.core.keycloak_admin import KeycloakAdmin

    return db(KeycloakAdmin().get_user("keelson", user_id))


def _keys_like(value: Any, needle: str) -> set[str]:
    """Every JSON object key (any depth) that contains ``needle``."""
    found: set[str] = set()
    if isinstance(value, dict):
        for k, v in value.items():
            if needle in str(k).lower():
                found.add(str(k))
            found |= _keys_like(v, needle)
    elif isinstance(value, list):
        for v in value:
            found |= _keys_like(v, needle)
    return found


def users_api(created: list[str]) -> dict[str, Any]:
    heading("6. Users API (real Keycloak)")
    sam, ava = ts.keelson_login("sam"), ts.keelson_login("ava")
    username = f"{E2E_USER_PREFIX}{uuid.uuid4().hex[:6]}"
    body = {"username": username, "email": f"{username}@demo-retail.test", "name": "E2E Invitee",
            "roles": ["planner"]}
    r = admin(sam, "POST", "/users", body=body)
    out = ok(r, "invite a user", 201)
    uid = str(out["user"]["id"])
    created.append(uid)
    expect(not _keys_like(out, "password"), f"a password field is returned: {sorted(_keys_like(out, 'password'))}")
    expect(out["user"]["roles"] == ["planner"] and out["user"]["tenants"] == [TENANT] and out["user"]["enabled"] is True,
           f"created user {out['user']}")
    expect(out.get("setup") and out["setup"].get("instructions"), f"no set-up instructions: {out.get('setup')}")
    kc = _kc_user(uid) or {}
    expect("UPDATE_PASSWORD" in (kc.get("requiredActions") or []), f"required actions {kc.get('requiredActions')}")
    expect((kc.get("attributes") or {}).get("tenant") == [TENANT], f"tenant attribute {kc.get('attributes')}")
    listed = {u["username"]: u for u in items_of(ok(admin(sam, "GET", "/users"), "list users"))}
    expect(username in listed and "priya" in listed, f"users {sorted(listed)[:10]}")
    say(f"invited {username} (planner; temporary password never returned; UPDATE_PASSWORD; tenant {TENANT})")
    p = ok(admin(sam, "PATCH", f"/users/{uid}", body={"roles": ["planner", "approver"]}), "set roles")
    expect(sorted(p["roles"]) == ["approver", "planner"], f"roles {p['roles']}")
    g = admin(sam, "PATCH", f"/users/{uid}", body={"roles": ["planner", "tenant-admin"]})
    gcr = ok(g, "grant tenant-admin", 202)
    expect(gcr["kind"] == "user_roles" and gcr["status"] == "pending", f"grant CR {gcr}")
    ok(admin(ava, "POST", f"/change-requests/{gcr['id']}/reject", body={"reason": "e2e: not needed"}), "reject grant")
    pf = admin(sam, "PATCH", f"/users/{uid}", body={"roles": ["platform-admin"]})
    expect(pf.status_code in (403, 422), f"a tenant-admin granting platform-admin: HTTP {pf.status_code}")
    d = ok(admin(sam, "DELETE", f"/users/{uid}"), "disable user")
    expect(d.get("enabled") is False or (d.get("user") or {}).get("enabled") is False, f"disabled {d}")
    expect((_kc_user(uid) or {}).get("enabled") is False, "Keycloak user still enabled")
    say("roles → planner+approver; tenant-admin grant → 202 (rejected); platform-admin → refused; disabled")
    return {"user": username}


# ─────────────────────────────── 7. signals admin ───────────────────────────────


def signals_admin() -> dict[str, Any]:
    heading("7. Signals admin round-trip (engine ↔ Signal Hub)")
    sam = ts.keelson_login("sam")
    src = ok(admin(sam, "GET", "/signals/sources"), "sources")
    expect(src.get("hub_error") is None and src["items"], f"sources {src}")
    s0 = src["items"][0]
    sid, was = str(s0["id"]), bool(s0["enabled"])
    expect(s0.get("licence"), f"source {sid} shows no licence")
    t = ok(admin(sam, "PATCH", f"/signals/sources/{sid}", body={"enabled": not was}), "toggle source")
    expect(t["enabled"] is (not was) and t["hub_synced"] is True, f"toggle {t}")
    back = ok(admin(sam, "PATCH", f"/signals/sources/{sid}", body={"enabled": was}), "restore source")
    expect(back["enabled"] is was and back["hub_synced"] is True, f"restore {back}")
    bad = admin(sam, "PATCH", f"/signals/sources/{sid}", body={"cadence_s": 5})
    expect(bad.status_code == 422, f"cadence below range: HTTP {bad.status_code}")
    say(f"source {sid} ({s0.get('licence')}): enabled {was}→{not was}→{was}, pushed to the hub; bad cadence 422")

    regions = ok(admin(sam, "GET", "/signals/watch-regions"), "watch regions")
    original = regions["items"]
    nodes = ok(admin(sam, "GET", "/signals/network"), "network").get("nodes") or {}
    ntype = "port" if nodes.get("port") else next((k for k, v in nodes.items() if v), None)
    expect(ntype, f"no network nodes pushed to the hub: {nodes}")
    code = nodes[ntype][0]
    new = {"id": "e2e-m7-radius", "name": "E2E radius", "kind": "radius", "node": {"type": ntype, "code": code},
           "radius_km": 150, "enabled": True}
    put = ok(admin(sam, "PUT", "/signals/watch-regions", body={"regions": [*original, new]}), "PUT watch regions")
    expect(put["hub_synced"] is True, f"watch regions not pushed: {put.get('hub_error')}")
    got = {r["id"]: r for r in ok(admin(sam, "GET", "/signals/watch-regions"), "watch regions")["items"]}
    expect(got.get("e2e-m7-radius", {}).get("node_code") == code and got["e2e-m7-radius"]["radius_km"] == 150,
           f"region read back {got.get('e2e-m7-radius')}")
    restored = ok(admin(sam, "PUT", "/signals/watch-regions", body={"regions": original}), "restore regions")
    expect(restored["hub_synced"] is True and len(restored["items"]) == len(original), "regions not restored")
    say(f"watch region 150 km around {ntype} {code}: stored, pushed, read back, removed")

    rules = ok(admin(sam, "GET", "/signals/alert-rules"), "alert rules")
    body_rules = rules["rules"] if rules.get("custom") else []
    saved = ok(admin(sam, "PUT", "/signals/alert-rules", body={"rules": body_rules}), "re-save alert rules")
    expect(saved["hub_synced"] is True and saved["rules"] == rules["rules"], f"alert rules {saved}")
    say(f"alert rules ({'custom' if rules.get('custom') else 'defaults'}, {len(rules['rules'])}): re-saved and pushed")
    return {"source": sid, "region_node": f"{ntype}:{code}"}


# ─────────────────────────────── main ───────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-final-reset", action="store_true")
    parser.add_argument("--skip-crawl", action="store_true", help="reuse the current crawled snapshot")
    args = parser.parse_args()
    core_init()
    results: dict[str, Any] = {}
    created_users: list[str] = []
    ava: ts.KeelsonUser | None = None
    try:
        results["reset"] = demo_reset()
        ava = ts.keelson_login("ava")
        register_lite(ava)
        results["mappings"] = install_mappings(ava)
        if not args.skip_crawl:
            results["crawl"] = crawl(ava)
        results["dry_run"] = dry_run(ava)
        results["four_eyes"] = four_eyes()
        results["per_op"] = per_op_harness()
        results["users"] = users_api(created_users)
        results["signals"] = signals_admin()
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    finally:
        for uid in created_users:
            try:
                _kc_delete_user(uid)
                say(f"clean-up: Keycloak user {uid[:8]} deleted")
            except Exception as exc:  # noqa: BLE001 - best-effort clean-up
                print(f"WARN: could not delete the e2e user {uid}: {exc}", flush=True)
        if ava is not None:
            try:
                admin(ava, "POST", f"/connectors/{LITE}/disable", tenant=NW)
                say("clean-up: merxa-lite disabled (no hourly crawls with a key the next Merxa reset revokes)")
            except httpx.HTTPError as exc:
                print(f"WARN: could not disable {LITE}: {exc}", flush=True)
    if not args.skip_final_reset:
        try:
            results["final_reset"] = demo_reset()
        except E2EFailure as exc:
            print(f"FAIL: {exc}", flush=True)
            return 1
    heading("Results")
    print(json.dumps(results, indent=2, default=str))
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
