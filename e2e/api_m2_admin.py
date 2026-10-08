#!/usr/bin/env python3
"""M2 back-office + multi-tenancy, end to end, through the real stack (docs/contracts/70 §1-5, §7).

Runs like ``api_thin_slice.py`` (throwaway Python container on ``keelson_edge``; real Gemini via
LiteLLM). It reuses that script's HTTP helpers (``import api_thin_slice``).

Story:
 1. sam (tenant-admin, demo-retail): read the catalog, PUT the agent spec (recommender prompt
    addendum + a different structurer alias) -> new version; stale ``base_version`` -> 409;
    the versions list shows it; finally restore the original version (so the demo is unchanged).
 2. ava (platform-admin): create tenant ``acme-outdoor`` from pack ``blank`` (idempotent on slug;
    no demo persona is added to it) -> listed, has a LiteLLM team (cross-tenant gateway view)
    and spec v1; sam gets 403 on the platform-admin routes.
 3. Merxa REST as ``merxa.admin``: mint an API key (show-once); ava registers ``/mcp-lite`` for
    ``northwind-test`` (static_header ``X-Merxa-Api-Key``), test -> tier [A], approves the read
    tools only (write tools stay unapproved), activates it and binds the read tools to the
    answer path.
 4. nora (northwind-test planner) asks "How many open purchase orders are there?" -> the answer
    path calls ``list_items`` on the lite connector, the answer cites the count Merxa reports,
    an ``evidence_pack`` block is present, no write tool is ever called.
 5. Tenancy: nora gets 404 on a demo-retail case / events / block data / interrupt / the merxa
    connector link, and priya 404 on nora's case; priya and nora get 403 on ``/api/admin/*``.
 6. Webhooks: the latest applied Merxa change set (``api_thin_slice.py`` creates one) shows up as
    ``change_set.applied`` in ``GET /api/admin/connectors/merxa/events`` (demo-retail).

Exit status 0 = pass; 1 = an expectation failed; 2 = usage / environment error.
Never prints secrets (the minted API key is held in memory only).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from typing import Any

import httpx

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api_thin_slice as ts  # noqa: E402
from api_thin_slice import KEELSON, MERXA, E2EFailure, expect, heading, say  # noqa: E402

NEW_TENANT = "acme-outdoor"
LITE_SLUG = "merxa-lite"
LITE_URL = f"{MERXA}/mcp-lite"
QUESTION = "How many open purchase orders are there?"
WRITE_TOOLS = {"update_item", "create_transfer_order"}
ADDENDUM = (
    "E2E guidance: when two options recover a similar number of units, prefer the option with "
    "fewer transfer lines."
)


def _json(r: httpx.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return {"raw": r.text[:300]}


def admin(user: ts.KeelsonUser, method: str, path: str, *, tenant: str | None = None, body: Any = None,
          timeout: float = 30.0) -> httpx.Response:
    params = {"tenant": tenant} if tenant else None
    headers = user.headers() if method != "GET" else {}
    return user.client.request(method, f"{KEELSON}/api/admin{path}", params=params, json=body, headers=headers,
                               timeout=timeout)


def ok(r: httpx.Response, what: str, *codes: int) -> Any:
    codes = codes or (200,)
    expect(r.status_code in codes, f"{what}: HTTP {r.status_code} {r.text[:400]}")
    return _json(r)


def items_of(body: Any) -> list[dict[str, Any]]:
    if isinstance(body, list):
        return body
    return list((body or {}).get("items") or [])


# ─────────────────────────────── 1. agent spec (sam) ───────────────────────────────


def agent_spec_edit() -> None:
    heading("Tenant admin (sam): edit the agent spec")
    sam = ts.keelson_login("sam")
    current = ok(admin(sam, "GET", "/agents/spec"), "GET spec")
    base = str(current["version"])
    spec = current["spec"]
    say(f"latest spec {base} by {current.get('created_by')}")
    catalog = ok(admin(sam, "GET", "/agents/catalog"), "GET catalog")
    aliases = [a if isinstance(a, str) else a.get("alias") for a in catalog.get("aliases") or catalog.get("available_aliases") or []]
    aliases = [a for a in aliases if a]
    say(f"catalog: {len(catalog.get('roles') or [])} roles, aliases {aliases}")
    cur_struct = (spec.get("models") or {}).get("structurer")
    choices = [a for a in aliases if a != cur_struct and a in ("critic-default", "fast-default", "reasoning-default")]
    expect(choices, f"no alternative structurer alias in {aliases} (current {cur_struct})")
    new_struct = choices[0]

    patch = {"prompts": {"recommender": ADDENDUM}, "models": {"structurer": new_struct}}
    r = admin(sam, "PUT", "/agents/spec", body={"base_version": base, "spec_patch": patch})
    created = ok(r, "PUT spec", 201)
    v_new = str(created["version"])
    expect(v_new != base, f"PUT spec did not create a new version ({v_new})")
    expect(created["spec"]["prompts"]["recommender"] == ADDENDUM, "addendum not stored")
    expect(created["spec"]["models"]["structurer"] == new_struct, "structurer alias not switched")
    say(f"new version {v_new}: structurer {cur_struct} -> {new_struct}, recommender addendum {len(ADDENDUM)} chars")

    stale = admin(sam, "PUT", "/agents/spec", body={"base_version": base, "spec_patch": {"history_messages": 7}})
    expect(stale.status_code == 409, f"stale base_version: expected 409, got {stale.status_code} {stale.text[:200]}")
    say("stale base_version -> 409")
    bad = admin(sam, "PUT", "/agents/spec", body={"base_version": v_new, "spec_patch": {"models": {"structurer": "no-such-alias"}}})
    expect(bad.status_code in (400, 422), f"unknown alias: expected 4xx, got {bad.status_code} {bad.text[:200]}")
    guarded = admin(sam, "PUT", "/agents/spec", body={
        "base_version": v_new,
        "spec_patch": {"policy": {"auto_max_units": int((spec.get("policy") or {}).get("auto_max_units", 500)) + 1}}})
    # M7 (110 §1): a guarded change by a tenant admin becomes a pending four-eyes change request
    expect(guarded.status_code == 202 and _json(guarded).get("status") == "pending",
           f"raising auto_max_units as tenant-admin: expected 202 change request, got {guarded.status_code} {guarded.text[:200]}")
    rej = admin(ts.keelson_login("ava"), "POST", f"/change-requests/{_json(guarded)['id']}/reject",
                body={"reason": "e2e: withdrawn"})
    expect(rej.status_code == 200, f"reject the change request: HTTP {rej.status_code} {rej.text[:200]}")
    say("unknown alias -> 4xx; raising auto_max_units -> 202 change request (rejected by ava)")

    versions = items_of(ok(admin(sam, "GET", "/agents/spec/versions"), "GET versions"))
    names = [str(v.get("version")) for v in versions]
    expect(v_new in names and base in names, f"versions list {names[:6]} misses {v_new}/{base}")
    full = ok(admin(sam, "GET", f"/agents/spec/versions/{v_new}"), "GET version")
    got = full.get("spec", full)
    expect((got.get("prompts") or {}).get("recommender") == ADDENDUM, "stored version lacks the addendum")

    restored = ok(admin(sam, "POST", "/agents/spec/restore", body={"version": base}), "restore", 201)
    expect(str(restored["version"]) not in (base, v_new), f"restore did not create a new version: {restored.get('version')}")
    expect((restored["spec"].get("models") or {}).get("structurer") == cur_struct, "restore did not bring the alias back")
    expect(not (restored["spec"].get("prompts") or {}).get("recommender"), "restore kept the addendum")
    say(f"restored {base} as {restored['version']} (demo spec unchanged)")

    audit = ok(admin(sam, "GET", "/audit", body=None), "GET audit")
    actions = [str(a.get("action")) for a in items_of(audit)[:20]]
    expect(any("spec" in a for a in actions), f"no agent spec action in the admin audit: {actions}")


# ─────────────────────────────── 2. tenants (ava) ───────────────────────────────


def create_tenant() -> None:
    heading("Platform admin (ava): create a tenant from a pack")
    ava = ts.keelson_login("ava")
    packs = [p.get("name") for p in items_of(ok(admin(ava, "GET", "/packs"), "GET packs"))]
    expect({"blank", "value-fashion-eu"} <= set(packs), f"packs {packs}")
    body = {"slug": NEW_TENANT, "name": "Acme Outdoor (e2e)", "pack": "blank", "admin_usernames": []}
    r = admin(ava, "POST", "/tenants", body=body, timeout=60.0)
    res = ok(r, "create tenant", 200, 201)
    say(f"tenant {NEW_TENANT}: HTTP {r.status_code} ({'created' if r.status_code == 201 else 'already existed'})")
    again = admin(ava, "POST", "/tenants", body=body, timeout=60.0)
    expect(again.status_code == 200, f"create tenant again (idempotent): HTTP {again.status_code} {again.text[:200]}")
    clash = admin(ava, "POST", "/tenants", body={**body, "pack": "value-fashion-eu"}, timeout=60.0)
    expect(clash.status_code == 409, f"same slug, other pack: expected 409, got {clash.status_code}")
    del res
    listed = items_of(ok(admin(ava, "GET", "/tenants"), "GET tenants"))
    slugs = [t.get("slug") for t in listed]
    expect(NEW_TENANT in slugs and "demo-retail" in slugs and "northwind-test" in slugs, f"tenants {slugs}")
    team = ok(admin(ava, "GET", "/gateway/tenant", tenant=NEW_TENANT), "gateway tenant (cross-tenant)")
    expect(team.get("team_alias"), f"no LiteLLM team for {NEW_TENANT}: {team}")
    expect(team.get("key_prefix"), f"no virtual key for {NEW_TENANT}: {team}")
    say(f"LiteLLM team {team.get('team_alias')} budget {team.get('budget_eur')} EUR, models {team.get('models')}")
    spec = ok(admin(ava, "GET", "/agents/spec", tenant=NEW_TENANT), "spec of the new tenant")
    expect(spec.get("version") == "v1" or str(spec.get("version")).startswith("v"), f"spec {spec.get('version')}")
    expect(list(spec["spec"].get("answer_tools") or []) == [], "blank pack should start with no answer tools")
    say(f"{NEW_TENANT} spec {spec['version']} (pack blank)")

    sam = ts.keelson_login("sam")
    for path in ("/tenants", "/packs"):
        r = admin(sam, "GET", path)
        expect(r.status_code == 403, f"sam GET /api/admin{path}: expected 403, got {r.status_code}")
    r = admin(sam, "GET", "/connectors", tenant=NEW_TENANT)
    expect(r.status_code == 404, f"sam with ?tenant={NEW_TENANT}: expected 404, got {r.status_code}")
    say("sam: 403 on platform routes, 404 on a foreign ?tenant=")


# ─────────────────────────────── 3. /mcp-lite for northwind-test ───────────────────────────────


def mint_merxa_key() -> str:
    client = ts.merxa_login("merxa.admin")
    r = client.post(
        f"{MERXA}/v1/api-keys",
        json={"name": f"keelson-northwind-e2e-{int(time.time())}", "scopes": ["catalog:read", "inventory:read", "po:read"]},
        headers={"Origin": MERXA, "Idempotency-Key": str(uuid.uuid4())},
    )
    expect(r.status_code == 201, f"mint Merxa API key: HTTP {r.status_code} {r.text[:200]}")
    body = r.json()
    key = str(body.get("key") or "")
    expect(key.startswith("mx_"), "minted key has an unexpected format")
    say(f"Merxa API key minted (prefix {body.get('prefix')}, scopes {body.get('scopes')})")
    # Keys of earlier runs belong to connectors this run replaces: revoke them.
    listed = client.get(f"{MERXA}/v1/api-keys")
    expect(listed.status_code == 200, f"list Merxa API keys: HTTP {listed.status_code}")
    stale = [k for k in items_of(listed.json()) if str(k.get("name", "")).startswith("keelson-northwind-e2e-")
             and not k.get("revoked_at") and k.get("id") != body.get("id")]
    for k in stale:
        r = client.post(f"{MERXA}/v1/api-keys/{k['id']}:revoke", headers={"Origin": MERXA})
        expect(r.status_code == 200, f"revoke old Merxa API key {k.get('prefix')}: HTTP {r.status_code} {r.text[:200]}")
    if stale:
        say(f"revoked {len(stale)} API key(s) of earlier runs")
    return key


def open_po_count() -> int:
    client = ts.merxa_login("merxa.admin")
    total = 0
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {"status": "open", "limit": 200}
        if cursor:
            params["cursor"] = cursor
        r = client.get(f"{MERXA}/v1/purchase-orders", params=params)
        expect(r.status_code == 200, f"merxa purchase orders: HTTP {r.status_code}")
        body = r.json()
        total += sum(1 for p in body.get("items") or [] if p.get("status") == "open")
        cursor = body.get("next_cursor")
        if not cursor:
            return total


def register_lite() -> list[str]:
    heading("Platform admin (ava): register Merxa /mcp-lite for northwind-test")
    ava = ts.keelson_login("ava")
    t = "northwind-test"
    existing = {c.get("slug"): c for c in items_of(ok(admin(ava, "GET", "/connectors", tenant=t), "list connectors"))}
    key = mint_merxa_key()
    planning = ok(admin(ava, "GET", "/agents/spec", tenant=t), "spec")["spec"].get("connector")
    rotated = LITE_SLUG in existing and planning == LITE_SLUG
    if rotated:
        # M7: the foreign-ERP live run plans on this connector (it cannot be deleted while bound):
        # rotate its API key in place instead of re-registering it
        ok(admin(ava, "PUT", f"/connectors/{LITE_SLUG}/secret", tenant=t, body={"secret": key}), "rotate the API key")
        say(f"{LITE_SLUG} is the planning connector (M7 foreign-ERP run): API key rotated in place")
    elif LITE_SLUG in existing:
        # A previous run registered it: unbind it from the answer path, then replace it (fresh key).
        spec = ok(admin(ava, "GET", "/agents/spec", tenant=t), "spec")
        if spec["spec"].get("answer_connector") == LITE_SLUG:
            ok(admin(ava, "PUT", "/agents/spec", tenant=t, body={
                "base_version": spec["version"], "spec_patch": {"answer_connector": "merxa", "answer_tools": []}}),
               "unbind lite connector", 200, 201)
        ok(admin(ava, "DELETE", f"/connectors/{LITE_SLUG}", tenant=t), "delete old lite connector", 204)
        say(f"removed the {LITE_SLUG} connector of a previous run")
    body = {
        "slug": LITE_SLUG, "name": "Merxa (mcp-lite, API key)", "mcp_url": LITE_URL, "trust_level": "third_party",
        "auth": {"kind": "static_header", "header_name": "X-Merxa-Api-Key", "secret": key},
    }
    if not rotated:
        created = ok(admin(ava, "POST", "/connectors", tenant=t, body=body), "register connector", 201)
        expect(created.get("status") == "draft", f"new connector status {created.get('status')}")
        expect(key not in json.dumps(created), "the API key is echoed back by the registry")

        pre = admin(ava, "POST", f"/connectors/{LITE_SLUG}/activate", tenant=t)
        expect(pre.status_code in (409, 422, 400), f"activate before a passing test: expected 4xx, got {pre.status_code}")

    test = ok(admin(ava, "POST", f"/connectors/{LITE_SLUG}/test", tenant=t, timeout=90.0), "test connection")
    for step in test.get("steps") or []:
        say(f"  test step {step.get('step'):12s} ok={step.get('ok')} {step.get('ms')} ms {ts.ascii_text(step.get('detail'), 90)}")
    expect(test.get("ok") is True, f"connection test failed: {test.get('steps')}")
    expect(list(test.get("tier") or []) == ["A"], f"tier {test.get('tier')}, expected ['A']")
    names = {str(x.get("name")): x for x in test.get("tools") or []}
    expect({"list_items", "get_item", *WRITE_TOOLS} <= set(names), f"tools {sorted(names)}")
    for w in WRITE_TOOLS:
        expect((names[w].get("suggested") or {}).get("access") in ("write", "destructive"),
               f"{w} suggested as {names[w].get('suggested')}")
    say(f"tier {test['tier']}; tools {sorted(names)}")

    tools = items_of(ok(admin(ava, "GET", f"/connectors/{LITE_SLUG}/tools", tenant=t), "tools"))
    reads = []
    for tool in tools:
        name = str(tool.get("name"))
        access = tool.get("access") or (tool.get("classification") or {}).get("access")
        if name in WRITE_TOOLS:
            expect(access != "read", f"write tool {name} classified read")
            continue
        if access == "read":
            ok(admin(ava, "PATCH", f"/connectors/{LITE_SLUG}/tools/{name}", tenant=t,
                     body={"status": "approved", "content_trust": "trusted_data"}), f"approve {name}")
            reads.append(name)
    expect({"list_items", "get_item"} <= set(reads), f"approved reads {reads}")
    # A tenant admin must not be able to reclassify a write tool as read (platform-admin only) -
    # covered by engine tests; here: write tools must stay unapproved.
    current = {c.get("slug"): c for c in items_of(ok(admin(ava, "GET", "/connectors", tenant=t), "list"))}[LITE_SLUG]
    if current.get("status") != "active":
        act = ok(admin(ava, "POST", f"/connectors/{LITE_SLUG}/activate", tenant=t), "activate")
        expect(act.get("status") == "active", f"activate -> {act}")
    tools = {str(x.get("name")): x for x in items_of(ok(admin(ava, "GET", f"/connectors/{LITE_SLUG}/tools", tenant=t), "tools"))}
    for w in WRITE_TOOLS:  # (the M7 foreign-ERP run approves them for its per-op commits)
        expect(rotated or tools[w].get("status") != "approved", f"write tool {w} is approved")
    say(f"approved read tools {sorted(reads)}; write tools stay {sorted({tools[w].get('status') for w in WRITE_TOOLS})}")

    spec = ok(admin(ava, "GET", "/agents/spec", tenant=t), "spec")
    bad = admin(ava, "PUT", "/agents/spec", tenant=t, body={
        "base_version": spec["version"], "spec_patch": {"answer_connector": LITE_SLUG, "answer_tools": ["update_item"]}})
    expect(bad.status_code in (400, 422), f"binding a write tool: expected 4xx, got {bad.status_code} {bad.text[:200]}")
    bound = ok(admin(ava, "PUT", "/agents/spec", tenant=t, body={
        "base_version": spec["version"], "spec_patch": {"answer_connector": LITE_SLUG, "answer_tools": sorted(reads)}}),
        "bind answer path", 201)
    say(f"northwind-test spec {bound['version']}: answer_connector={LITE_SLUG} answer_tools={sorted(reads)}")
    conns = {c.get("slug"): c for c in items_of(ok(admin(ava, "GET", "/connectors", tenant=t), "list connectors"))}
    expect(conns[LITE_SLUG].get("status") == "active" and list(conns[LITE_SLUG].get("tier") or []) == ["A"],
           f"connector list entry {conns[LITE_SLUG]}")
    # Platform-admin cross-tenant access is explicit (?tenant=) and audited in that tenant.
    audit = items_of(ok(admin(ava, "GET", "/audit", tenant=t), "audit (cross-tenant)"))
    actions = {str(a.get("action")) for a in audit}
    expect("admin.cross_tenant" in actions, f"no admin.cross_tenant audit rows in {t}: {sorted(actions)[:12]}")
    expect(any(str(a.get("action", "")).startswith("connector") for a in audit), f"connector changes not audited: {sorted(actions)[:12]}")
    say(f"{t} audit: {len(audit)} recent rows incl. admin.cross_tenant")
    return reads


def ask_nora(timeout_s: float) -> str:
    heading("northwind-test (nora): ask through the lite connector")
    expected = open_po_count()
    say(f"Merxa reports {expected} open purchase orders")
    nora = ts.keelson_login("nora")
    expect(nora.me["user"]["active_tenant"] == "northwind-test", f"nora's tenant {nora.me['user']['active_tenant']}")
    r = nora.client.post(f"{KEELSON}/api/cases", json={"title": "E2E open POs"}, headers=nora.headers())
    case_id = str(ok(r, "create case", 201)["id"])
    after = int(ts.get_case(nora, case_id).get("last_event_seq") or 0)
    t0 = time.monotonic()
    r = nora.client.post(f"{KEELSON}/api/cases/{case_id}/messages", json={"text": QUESTION}, headers=nora.headers(idem=True))
    run_id = str(ok(r, "post message", 202)["run_id"])
    res = ts.follow_run(nora, case_id, run_id, after, timeout_s=timeout_s)
    ts.expect_success(res, "answer run")
    expect(res.outcome.get("type") == "success", f"answer run ended with {res.outcome}")
    # The answer path is not a timeline subagent (no TOOL_CALL events, 30-engine-api-events.md);
    # its tool calls are visible as evidence (label = humanised tool name + arguments).
    calls = [str(e.get("toolCallName")) for e in res.events if e.get("type") == "TOOL_CALL_START"]
    expect(not WRITE_TOOLS & set(calls), f"a write tool reached the agent: {calls}")
    case = ts.get_case(nora, case_id)
    kinds = ts.blocks_by_kind(case)
    expect("evidence_pack" in kinds, f"no evidence_pack block (have {sorted(kinds)})")
    ev_items = (kinds["evidence_pack"][-1].get("props") or {}).get("items") or []
    expect(ev_items, "evidence_pack has no items")
    labels = [f"{i.get('source')}: {i.get('label')}" for i in ev_items]
    say(f"evidence ({time.monotonic() - t0:.1f}s): {[ts.ascii_text(x, 120) for x in labels]}")
    expect(any("list items" in str(i.get("label")) and "purchase_order" in str(i.get("label")) for i in ev_items),
           f"no list_items(purchase_order) evidence: {labels}")
    expect(all(i.get("source") != "Merxa" for i in ev_items if "list items" in str(i.get("label"))),
           "list_items evidence attributed to the Merxa platform connector instead of the lite connector")
    for w in WRITE_TOOLS:
        expect(not any(w.replace("_", " ") in str(i.get("label")) for i in ev_items), f"write tool {w} in evidence")
    assistant = [m for m in case.get("messages") or [] if m.get("role") == "assistant"]
    expect(assistant, "no assistant message")
    answer = str(assistant[-1].get("text") or "")
    say(f"answer: {ts.ascii_text(answer, 300)}")
    expect(not ts.PLACEHOLDER_RE.search(answer), "answer contains unfilled placeholders")
    numbers = {int(n) for n in re.findall(r"(?<![\w-])(\d+)(?![\w-])", answer)}
    expect(expected in numbers, f"the answer does not state the open PO count {expected} (numbers {sorted(numbers)})")
    say(f"evidence: {len(ev_items)} item(s); answer states {expected}")
    return case_id


# ─────────────────────────────── 5. tenancy negatives ───────────────────────────────


def tenancy_negatives(nora_case: str) -> None:
    heading("Tenancy: cross-tenant requests are refused")
    priya = ts.keelson_login("priya")
    cases = priya.client.get(f"{KEELSON}/api/cases").json()
    rows = cases if isinstance(cases, list) else cases.get("items") or []
    expect(rows, "priya has no demo-retail case to probe with (run api_thin_slice.py first)")
    demo_case = str(rows[0]["id"])
    detail = ts.get_case(priya, demo_case)
    block_id = next((str(b["id"]) for b in detail.get("blocks") or [] if b.get("id")), None)
    nora = ts.keelson_login("nora")
    probes = [
        ("GET", f"/api/cases/{demo_case}"),
        ("GET", f"/api/cases/{demo_case}/events?after=0"),
        ("GET", "/api/connectors/merxa/link"),
    ]
    if block_id:
        probes.append(("GET", f"/api/blocks/{block_id}/data"))
    for method, path in probes:
        if path.endswith("/events?after=0"):
            with nora.client.stream(method, f"{KEELSON}{path}", headers={"Accept": "text/event-stream"}) as resp:
                code = resp.status_code
        else:
            code = nora.client.request(method, f"{KEELSON}{path}").status_code
        expect(code == 404, f"nora {method} {path}: expected 404, got {code}")
    r = nora.client.post(f"{KEELSON}/api/cases/{demo_case}/messages", json={"text": "hello"}, headers=nora.headers(idem=True))
    expect(r.status_code == 404, f"nora posting into a demo-retail case: expected 404, got {r.status_code}")
    interrupts = [str(i["id"]) for i in detail.get("open_interrupts") or [] if i.get("id")]
    for iid in interrupts[:1]:
        r = nora.client.post(f"{KEELSON}/api/interrupts/{iid}/decision", json={"decision": "confirm"}, headers=nora.headers())
        expect(r.status_code == 404, f"nora deciding a demo-retail interrupt: expected 404, got {r.status_code}")
    r = priya.client.get(f"{KEELSON}/api/cases/{nora_case}")
    expect(r.status_code == 404, f"priya GET nora's case: expected 404, got {r.status_code}")
    say(f"nora: 404 on {len(probes) + 1} demo-retail probes; priya: 404 on nora's case")

    for user in (priya, nora):
        for path in ("/agents/spec", "/connectors", "/gateway/models", "/audit"):
            code = admin(user, "GET", path).status_code
            expect(code == 403, f"{user.username} GET /api/admin{path}: expected 403, got {code}")
        r = admin(user, "PUT", "/agents/spec", body={"base_version": "v1", "spec_patch": {}})
        expect(r.status_code == 403, f"{user.username} PUT spec: expected 403, got {r.status_code}")
    say("priya and nora: 403 on /api/admin/*")
    r = admin(nora, "GET", "/connectors", tenant="demo-retail")
    expect(r.status_code in (403, 404), f"nora ?tenant=demo-retail: got {r.status_code}")


# ─────────────────────────────── 6. webhook events ───────────────────────────────


def webhook_events(wait_s: float) -> None:
    heading("Webhooks: Merxa -> Keelson receiver")
    client = ts.merxa_login("merxa.admin")
    r = client.get(f"{MERXA}/v1/change-sets", params={"status": "applied", "limit": 50})
    expect(r.status_code == 200, f"merxa change sets: HTTP {r.status_code}")
    applied = [c for c in r.json().get("items") or [] if c.get("status") == "applied"]
    expect(applied, "no applied Merxa change set (run api_thin_slice.py first)")
    latest = max(applied, key=lambda c: str(c.get("applied_at") or c.get("updated_at") or c.get("created_at") or ""))
    code = str(latest["code"])
    say(f"latest applied change set {code}")
    sam = ts.keelson_login("sam")
    deadline = time.monotonic() + wait_s
    seen: list[str] = []
    while True:
        events = items_of(ok(admin(sam, "GET", "/connectors/merxa/events", body=None), "GET events"))
        seen = [str(e.get("type")) for e in events]
        hit = [e for e in events if e.get("type") == "change_set.applied" and code in json.dumps(e.get("payload"))]
        if hit:
            e = hit[0]
            say(f"event {e.get('event_id')} change_set.applied received {e.get('received_at')} ({len(events)} recent events)")
            break
        expect(time.monotonic() < deadline, f"no change_set.applied event for {code}; recent types {seen[:10]}")
        time.sleep(2)
    nora = ts.keelson_login("nora")
    code_n = admin(nora, "GET", "/connectors/merxa/events").status_code
    expect(code_n == 403, f"nora GET events: expected 403, got {code_n}")
    r = httpx.post(f"{KEELSON}/api/webhooks/demo-retail/merxa", content=b'{"type":"x"}',
                   headers={"webhook-id": "msg_e2e_forged", "webhook-timestamp": str(int(time.time())),
                            "webhook-signature": "v1,AAAA", "content-type": "application/json"})
    expect(r.status_code in (400, 401, 403), f"forged webhook: expected 4xx, got {r.status_code}")
    say(f"forged signature -> {r.status_code}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-timeout", type=float, default=300.0, help="seconds to wait for nora's run")
    parser.add_argument("--webhook-wait", type=float, default=60.0, help="seconds to wait for the webhook event")
    parser.add_argument("--skip-webhook", action="store_true", help="skip the webhook check (no applied change set)")
    args = parser.parse_args()
    if not os.environ.get("DEMO_USER_PASSWORD"):
        print("DEMO_USER_PASSWORD is not set", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    try:
        agent_spec_edit()
        create_tenant()
        register_lite()
        nora_case = ask_nora(args.run_timeout)
        tenancy_negatives(nora_case)
        if not args.skip_webhook:
            webhook_events(args.webhook_wait)
    except E2EFailure as exc:
        print(f"\nFAIL: {exc}", flush=True)
        return 1
    except httpx.HTTPError as exc:
        print(f"\nFAIL: HTTP error {type(exc).__name__}: {exc}", flush=True)
        return 1
    heading("PASS")
    print(f"  total_s {time.monotonic() - t0:7.1f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
