"""M8 mapping compiler + proposed mappings on the real stack, offline (docs/contracts/121).

Runs inside the engine image with the engine's own environment, like the M7 offline script
(``docker compose run --rm --no-deps -T -v <repo>/e2e:/e2e:ro engine-api python -u
/e2e/api_m8_mappings_offline.py``), against a stack started with ``MAPPING_PROPOSER=heuristic``
(the compose default): no model is ever called. HTTP through Caddy as a browser would; the
``/mcp-lite`` reads used to compute expectations go through the engine's own MCP gateway.

Steps:
1. Demo reset (``ava``).
2. Register and activate Merxa ``/mcp-lite`` for northwind-test (``merxa-lite``, reused from the M7
   offline script: tier ``[A]``, reads approved trusted, writes approved ``best_effort``); its
   mappings are left as they are (none after the reset).
3. Propose ``{kinds: [read, action], proposer: auto}``: the proposer answers ``heuristic`` with an
   ``llm_disabled`` note (rules only), shape-only profiles (no free text), a read draft that
   validates and covers location, item, po_line, inventory_position, demand_forecast and allocation
   with every required column, an action draft with ``transfer_order.create`` → ``create_transfer_order``
   and ``allocation.set_qty`` → ``update_item``; Merxa saw no write (allocation versions and the
   transfer-order count unchanged). List / get answer the same proposal.
4. Open the read draft as-is: ``PUT …/mappings/read {…, proposal_id}`` → a version with
   ``origin: proposal``; the proposal is ``accepted`` with that version.
5. Explain the pack's read mapping (``POST …/read/compile``): fewer distinct reads than uses (styles
   and stores are fetched once), the two blocking FK checks listed, SQL per column.
6. Install the pack mappings (``from-pack``, ``origin: pack``) and crawl through the worker: the
   snapshot is ``crawled``, not truncated, ``crawl.engine == duckdb`` with per-source stats, and the
   per-entity row counts equal Merxa's own ``list_items`` totals (the M7 assertion).
7. Disable ``merxa-lite``: ``GET …/staged`` describes the crawl's raw pages; a staged preview of an
   edited mapping maps the full staged data (row counts equal the crawl's, or capped at
   ``MAPPING_PREVIEW_MAX_ITEMS``) with no vendor call; a live preview answers 409.
8. Four-eyes on a proposed action mapping (demo-retail, which has a second admin): ``ava`` registers
   a second ``/mcp-lite`` connector there; ``sam`` (tenant-admin) proposes, saves the action draft
   with ``proposal_id`` → 202 change request ``connector_mapping`` (nothing applied), cannot approve
   it himself (403), ``ava`` rejects it with a reason; the connector is deleted again.
9. Clean-up: ``merxa-lite`` stays disabled; ``--skip-final-reset`` keeps the data, otherwise a final
   demo reset.

Exit 0 pass, 1 expectation failed (``FAIL:`` line), 2 environment missing.
"""

from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_m7_offline as m7  # noqa: E402
import api_thin_slice as ts  # noqa: E402
from api_m2_admin import admin, items_of, ok  # noqa: E402
from api_thin_slice import E2EFailure, expect, heading, say  # noqa: E402

NW = m7.NW
LITE = m7.LITE
DEMO = "demo-retail"
DEMO_LITE = "m8-lite-e2e"
PROPOSAL_ENTITIES = {"location", "item", "po_line", "inventory_position", "demand_forecast", "allocation"}
FREE_TEXT_PROBES = ("Hamburg", "Venlo")
"""Merxa DC names / cities with spaces never appear in a proposal (only shapes do)."""


def proposals_path(slug: str) -> str:
    return f"/connectors/{slug}/mappings/proposals"


def write_fingerprint() -> dict[str, Any]:
    """What a write call would change in Merxa: allocation versions (first page) + transfer orders."""
    allocations = m7.lite_items("allocation", size=200)
    return {"allocations": {a["code"]: a["version"] for a in allocations}, "transfer_orders": m7.lite_total("transfer_order")}


# ───────────────────────────── 3. propose ─────────────────────────────


def propose(ava: ts.KeelsonUser) -> dict[str, Any]:
    from keelson_contracts.mapping import read_mapping_violations
    from keelson_contracts.mapping_proposal import MappingProposal

    heading("3. Propose read + action mappings for merxa-lite (rules only: MAPPING_PROPOSER=heuristic)")
    status = ok(admin(ava, "GET", "/mappings/proposer", tenant=NW), "proposer status")
    expect(status.get("enabled") is True, f"proposals are switched off: {status}")
    before = write_fingerprint()
    r = admin(ava, "POST", proposals_path(LITE), tenant=NW, body={"kinds": ["read", "action"], "proposer": "auto"},
              timeout=180.0)
    body = ok(r, "propose", 201)
    p = MappingProposal.model_validate(body)
    codes = {n.code for n in p.notes}
    expect(p.proposer == "heuristic", f"proposer {p.proposer} (a model was called?)")
    expect(codes & {"llm_disabled", "llm_unavailable"}, f"no note says the AI step did not run: {sorted(codes)}")
    expect(p.llm_calls == 0, f"llm_calls {p.llm_calls}")
    for probe in FREE_TEXT_PROBES:
        expect(probe not in r.text, f"free text {probe!r} leaked into the proposal")
    draft = p.read_draft()
    expect(draft is not None, "no complete read draft")
    assert draft is not None
    covered = set(draft.mapped_entities)
    expect(PROPOSAL_ENTITIES <= covered, f"read draft covers {sorted(covered)}")
    tools = {t["name"]: t for t in items_of(ok(admin(ava, "GET", f"/connectors/{LITE}/tools", tenant=NW), "tools"))}
    from keelson_contracts.connectors import ToolClassification

    classes = {n: ToolClassification.model_validate({k: t[k] for k in ("access", "content_trust", "egress", "idempotency")})
               for n, t in tools.items() if t.get("status") == "approved"}
    problems = read_mapping_violations(draft, tools=classes)
    expect(not problems, f"read draft violates the catalogue: {problems}")
    action = p.action_draft()
    expect(action is not None, "no action draft")
    assert action is not None
    ops = {o.op: o for o in action.ops}
    expect(ops.get("transfer_order.create") is not None and ops["transfer_order.create"].tool == "create_transfer_order",
           f"transfer_order.create -> {ops.get('transfer_order.create')}")
    expect(ops.get("allocation.set_qty") is not None and ops["allocation.set_qty"].tool == "update_item",
           f"allocation.set_qty -> {ops.get('allocation.set_qty')}")
    for op in ("transfer_order.create", "allocation.set_qty"):
        m = ops[op]
        expect(m.idempotency.value == "best_effort" and m.request_id_arg and m.readback is not None, f"{op}: {m}")
    after = write_fingerprint()
    expect(before == after, "Merxa changed while proposing (a write tool was called)")
    listed = items_of(ok(admin(ava, "GET", proposals_path(LITE), tenant=NW), "list proposals"))
    expect(listed and listed[0]["id"] == p.id and listed[0]["status"] == "ready", f"list {listed[:1]}")
    got = ok(admin(ava, "GET", f"{proposals_path(LITE)}/{p.id}", tenant=NW), "get proposal")
    expect(got["inputs_hash"] == p.inputs_hash, "get answers another proposal")
    say(f"proposal {p.id}: {len(p.read.entities) if p.read else 0} entity mappings "
        f"({len(draft.entities)} complete), {len(action.ops)} ops, notes {sorted(codes)}")
    return {"id": p.id, "entities": sorted(covered), "ops": sorted(ops), "notes": sorted(codes),
            "draft": draft.model_dump(mode="json")}


def accept_read(ava: ts.KeelsonUser, proposal: dict[str, Any]) -> dict[str, Any]:
    heading("4. Open the read draft as-is and save it with its proposal id")
    current = ok(admin(ava, "GET", f"/connectors/{LITE}/mappings/read", tenant=NW), "GET read mapping")
    base = current.get("version")
    saved = ok(admin(ava, "PUT", f"/connectors/{LITE}/mappings/read", tenant=NW,
                     body={"base_version": base, "mapping": proposal["draft"], "proposal_id": proposal["id"]}),
               "save the read draft", 200, 201)
    expect(saved.get("origin") == "proposal" and saved.get("proposal_id") == proposal["id"], f"saved {saved}")
    p = ok(admin(ava, "GET", f"{proposals_path(LITE)}/{proposal['id']}", tenant=NW), "get proposal")
    expect(p["status"] == "accepted" and (p.get("accepted") or {}).get("read") == saved["version"], f"proposal {p}")
    say(f"read mapping v{saved['version']} saved from the proposal (origin proposal)")
    return {"version": saved["version"]}


# ───────────────────────────── 5. explain ─────────────────────────────


def explain(ava: ts.KeelsonUser) -> dict[str, Any]:
    from keelson_engine.core.packs import load_pack, load_pack_mappings

    heading("5. Explain the pack's read mapping (compile, nothing called)")
    pack = load_pack_mappings(load_pack(m7.PACK))["read"]
    plan = ok(admin(ava, "POST", f"/connectors/{LITE}/mappings/read/compile", tenant=NW,
                    body={"mapping": {k: v for k, v in pack.items() if k != "kind"}}), "compile")
    uses = sum(len(s["used_by"]) for s in plan["sources"])
    expect(plan["engine"] == "duckdb" and len(plan["sources"]) < uses,
           f"{len(plan['sources'])} reads for {uses} uses (shared reads must be fetched once)")
    blocking = {c["label"] for c in plan["checks"] if c["code"] == "fk" and c["severity"] == "error"}
    expect(blocking == {"po_line.sku_code → item(sku)", "inventory_position.location → location"},
           f"blocking FK checks {blocking}")
    expect(all(c["sql"] for e in plan["entities"] for c in e["columns"]), "a column has no SQL")
    say(f"{len(plan['sources'])} distinct reads for {uses} uses; {len(plan['checks'])} checks")
    return {"reads": len(plan["sources"]), "uses": uses}


# ───────────────────────────── 6. crawl (compiler) ─────────────────────────────


def crawl_compiled(ava: ts.KeelsonUser) -> dict[str, Any]:
    heading("6. Install the pack mappings and crawl through the DuckDB compiler")
    installed = m7.install_mappings(ava)
    read = ok(admin(ava, "GET", f"/connectors/{LITE}/mappings/read", tenant=NW), "GET read mapping")
    expect(read.get("origin") == "pack", f"pack install origin {read.get('origin')}")
    result = m7.crawl(ava)
    status = ok(admin(ava, "GET", f"/connectors/{LITE}/crawl", tenant=NW), "crawl status")
    info = status.get("crawl") or {}
    expect(info.get("engine") == "duckdb", f"crawl engine {info.get('engine')}")
    sources = info.get("sources") or []
    expect(sources and all(s.get("items", 0) >= 0 and s.get("pages", 0) >= 1 for s in sources), f"sources {sources}")
    expect(len(info.get("mapping_hash") or "") == 64 and info.get("mapping_version") == installed["read_version"],
           f"mapping version / hash {info.get('mapping_version')} {info.get('mapping_hash')}")
    say(f"compiled crawl: {len(sources)} distinct reads, {info.get('pages')} pages, checks {len(info.get('checks') or [])}")
    return {**result, "sources": len(sources)}


# ───────────────────────────── 7. staged preview ─────────────────────────────


def staged_preview(ava: ts.KeelsonUser, crawled: dict[str, Any]) -> dict[str, Any]:
    from keelson_engine.core.packs import load_pack, load_pack_mappings

    heading("7. Disable merxa-lite; preview an edited mapping against the staged crawl (no vendor call)")
    ok(admin(ava, "POST", f"/connectors/{LITE}/disable", tenant=NW), "disable merxa-lite")
    staged = ok(admin(ava, "GET", f"/connectors/{LITE}/staged", tenant=NW), "staged set")
    expect(staged["status"] == "complete" and staged["pages"] >= len(staged["sources"]) > 0, f"staged {staged}")
    edited = copy.deepcopy(load_pack_mappings(load_pack(m7.PACK))["read"])
    edited.pop("kind", None)
    edited["entities"][0]["fields"]["name"] = "upper($.name)"
    preview = ok(admin(ava, "POST", f"/connectors/{LITE}/mappings/read/preview", tenant=NW,
                       body={"source": "staged", "mapping": edited}, timeout=300.0), "staged preview")
    expect(preview["source"] == "staged" and preview["staged"]["snapshot_id"] == staged["snapshot_id"],
           f"preview of {preview.get('staged')}")
    errors = {e["where"]: e["error"] for e in preview["entities"] if e.get("error")}
    expect(not errors, f"staged preview errors: {errors}")
    rows: dict[str, int] = {}
    for e in preview["entities"]:
        rows[e["entity"]] = rows.get(e["entity"], 0) + int(e["rows"])
    if not preview["capped"]:
        expect(preview["ok"] is True, f"staged preview not ok: {preview.get('error')} {preview.get('checks')}")
        expect(rows == {k: v for k, v in crawled["rows"].items() if v},
               f"staged preview rows {rows} != crawled {crawled['rows']}")
    else:
        say("staged preview capped at MAPPING_PREVIEW_MAX_ITEMS: row counts are not compared")
    live = admin(ava, "POST", f"/connectors/{LITE}/mappings/read/preview", tenant=NW, body={"source": "live"})
    expect(live.status_code == 409 and live.json().get("code") == "connector_inactive", f"live preview HTTP {live.status_code}")
    say(f"staged preview of {staged['items']} raw records: rows {rows} (capped {preview['capped']}); live → 409")
    return {"rows": rows, "capped": preview["capped"]}


# ───────────────────────────── 8. four-eyes on a proposed action mapping ─────────────────────────────


def register_demo_lite(ava: ts.KeelsonUser) -> None:
    existing = {c.get("slug") for c in items_of(ok(admin(ava, "GET", "/connectors"), "list connectors"))}
    if DEMO_LITE in existing:
        ok(admin(ava, "DELETE", f"/connectors/{DEMO_LITE}"), "delete the previous e2e connector", 200, 204)
    key = m7.mint_key()
    body = {"slug": DEMO_LITE, "name": "Merxa (mcp-lite, M8 e2e)", "mcp_url": m7.LITE_URL, "trust_level": "third_party",
            "auth": {"kind": "static_header", "header_name": "X-Merxa-Api-Key", "secret": key}}
    ok(admin(ava, "POST", "/connectors", body=body), "register the demo-retail lite connector", 201)
    test = ok(admin(ava, "POST", f"/connectors/{DEMO_LITE}/test", timeout=90.0), "test connection")
    expect(test.get("ok") is True, f"connection test failed: {test.get('steps')}")
    for name in m7.READ_TOOLS:
        ok(admin(ava, "PATCH", f"/connectors/{DEMO_LITE}/tools/{name}",
                 body={"status": "approved", "access": "read", "content_trust": "trusted_data"}), f"approve {name}")
    for name in m7.WRITE_TOOLS:
        ok(admin(ava, "PATCH", f"/connectors/{DEMO_LITE}/tools/{name}",
                 body={"status": "approved", "access": "write", "content_trust": "trusted_data",
                       "idempotency": "best_effort"}), f"approve write tool {name}")
    act = ok(admin(ava, "POST", f"/connectors/{DEMO_LITE}/activate"), "activate")
    expect(act.get("status") == "active", f"activate -> {act}")


def four_eyes(ava: ts.KeelsonUser) -> dict[str, Any]:
    from keelson_contracts.mapping_proposal import MappingProposal

    heading("8. A proposed action mapping needs a second admin (demo-retail)")
    register_demo_lite(ava)
    sam = ts.keelson_login("sam")
    p = MappingProposal.model_validate(ok(admin(sam, "POST", proposals_path(DEMO_LITE),
                                                body={"kinds": ["action"], "proposer": "heuristic"}, timeout=180.0),
                                          "sam proposes", 201))
    action = p.action_draft()
    expect(action is not None, "no action draft")
    assert action is not None
    current = ok(admin(sam, "GET", f"/connectors/{DEMO_LITE}/mappings/action"), "GET action mapping")
    r = admin(sam, "PUT", f"/connectors/{DEMO_LITE}/mappings/action",
              body={"base_version": current.get("version"), "mapping": action.model_dump(mode="json"), "proposal_id": p.id})
    cr = ok(r, "save the action draft", 202)
    expect(cr["kind"] == "connector_mapping" and cr["status"] == "pending", f"change request {cr}")
    expect([x["code"] for x in cr["reasons"]] == ["ai_proposed_action_mapping"], f"reasons {cr['reasons']}")
    still = ok(admin(sam, "GET", f"/connectors/{DEMO_LITE}/mappings/action"), "GET action mapping")
    expect(still.get("version") == current.get("version"), "the action mapping changed before any approval")
    self_ok = admin(sam, "POST", f"/change-requests/{cr['id']}/approve", body={})
    expect(self_ok.status_code == 403 and self_ok.json().get("code") == "self_approval",
           f"self-approval: HTTP {self_ok.status_code} {self_ok.text[:200]}")
    rejected = ok(admin(ava, "POST", f"/change-requests/{cr['id']}/reject",
                        body={"reason": "e2e: reviewed, not applied"}), "ava rejects")
    expect(rejected["status"] == "rejected", f"reject -> {rejected['status']}")
    say(f"change request {cr['id'][:8]}: pending → self-approval refused → rejected by ava")
    return {"change_request": cr["id"], "ops": action.mapped_ops}


# ───────────────────────────── main ─────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-final-reset", action="store_true")
    args = parser.parse_args()
    m7.core_init()
    results: dict[str, Any] = {}
    ava: ts.KeelsonUser | None = None
    try:
        results["reset"] = m7.demo_reset()
        ava = ts.keelson_login("ava")
        m7.register_lite(ava)
        proposal = propose(ava)
        results["proposal"] = {k: v for k, v in proposal.items() if k != "draft"}
        results["accepted"] = accept_read(ava, proposal)
        results["explain"] = explain(ava)
        results["crawl"] = crawl_compiled(ava)
        results["staged"] = staged_preview(ava, results["crawl"])
        results["four_eyes"] = four_eyes(ava)
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    finally:
        if ava is not None:
            for path, tenant, what in ((f"/connectors/{LITE}/disable", NW, "merxa-lite disabled"),
                                       (f"/connectors/{DEMO_LITE}", None, f"{DEMO_LITE} deleted")):
                try:
                    r = admin(ava, "DELETE" if path.endswith(DEMO_LITE) else "POST", path, tenant=tenant)
                    if r.status_code in (200, 204):
                        say(f"clean-up: {what}")
                except httpx.HTTPError as exc:
                    print(f"WARN: clean-up ({what}) failed: {exc}", flush=True)
    if not args.skip_final_reset:
        try:
            results["final_reset"] = m7.demo_reset()
        except E2EFailure as exc:
            print(f"FAIL: {exc}", flush=True)
            return 1
    heading("Results")
    print(json.dumps(results, indent=2, default=str))
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
