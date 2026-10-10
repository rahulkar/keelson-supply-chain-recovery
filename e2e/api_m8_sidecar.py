"""M8 adapter sidecar: the generic tier A·B·C·W proof on the real stack, offline (docs/contracts/122 §11).

Runs inside the engine image with the engine's own environment, like the M5–M7 offline scripts, with the
compose profile ``adapters`` up (``fernhill-sidecar`` behind ``http://fernhill.localhost``)::

    docker compose --profile adapters up -d fernhill-sidecar
    set -a; . <(grep -E '^FERNHILL_(API|ADMIN)_KEY=' .env | tr -d '\\r'); set +a
    docker compose run --rm --no-deps -T -e LOG_LEVEL=warning -e FERNHILL_API_KEY -e FERNHILL_ADMIN_KEY \
      -v D:/keelson-supply-chain-recovery/e2e:/e2e:ro \
      engine-api python -u /e2e/api_m8_sidecar.py

The sidecar's keys (``FERNHILL_API_KEY``, ``FERNHILL_ADMIN_KEY``) are never given to the engine
services: the script takes them from its own environment (``-e NAME`` passes them by name, values never
on the command line), else from the dotenv file named by ``--env-file`` (default ``/run/keelson.env``;
the engine image runs as uid 10001, which cannot read the owner-only ``.env`` that ``scripts/init``
writes, so mounting it fails with "Permission denied"). HTTP goes
through Caddy like a browser (Keelson admin API as ``ava``; the sidecar's ``/v1/admin/*`` with the
admin key), and the commit harness (step 7) runs the engine's own runtime functions in-process — the
same building blocks the commit nodes run after an approval (northwind-test has no approver persona
and a plan needs a model). No chat is ever posted and no run is queued: no model is called.

Steps:
1. Fernhill reset (``POST /v1/admin/reset``, admin key; without it 401); ``/mcp`` without a key → 401.
2. Register ``fernhill`` on northwind-test (``ava`` cross-tenant): ``static_header`` ``X-Adapter-Key``,
   third party; the key is never echoed (create, test log).
3. Connection test → ok, tier ``["A","B","C","W"]``, the 13 sidecar tools; the engine's suggested
   classification matches the sidecar's annotations (reads trusted/read/internal; create/add/validate
   write + idempotency enforced; apply/rollback destructive + enforced).
4. Approve every tool as suggested (no agent binding), activate → ``active`` (internal egress: no
   change request); the commit capability table says ``change_set`` mode with the sidecar's two ops
   read from the ``add_change_set_items`` op enum, apply idempotent.
5. Tier-B sync (worker) → a new current snapshot, ``consistency: consistent``, row counts equal to the
   sidecar's own ``/v1/admin/stats`` (unmapped entities empty), the sidecar watermark.
6. ``poll_changes`` → False (nothing changed since the snapshot's watermark).
7. Commit harness (northwind-test, nora's case): a plan option with ``transfer_order.create``
   (FH-RTM → FH-LDS, two SKUs) and ``allocation.set_qty`` (``expected_version`` from the synced
   snapshot) → engine change set ``H`` → dry run as the preparer: ``preview_hash == H``; a receipt
   minted for **Merxa's** audience is refused (``invalid_receipt``, reason ``wrong_audience``) and
   leaves the set validated; the policy receipt through ``_credentials`` / ``_receipt_target``
   (``aud = http://fernhill.localhost/mcp``, ``ext_iss`` the connector origin, ``ext_sub
   connector:fernhill``) applies it; reconcile → ``verified`` with exactly one transfer order
   ``created_via change_set:<code>``; the CSV rows read back (reserved / in-transit / allocation qty
   and version); applying again with the same idempotency key replays the identical body and still
   one transfer order.
8. Tier C: ``scn.changes_since(<step-5 watermark>)`` lists the applied rows (``requires_sync``);
   ``poll_changes`` marks the connector stale and the worker resyncs → the new snapshot holds the new
   allocation qty/version, the new transfer-order lines and FH-RTM's reserved units.
9. Rollback with a user receipt (nora) → ``rolled_back`` with a compensating change set; the
   business columns are restored and the created transfer order is gone.
10. Clean-up (also on failure): Fernhill reset, the connector disabled (``--keep`` keeps both).

Exit 0 pass, 1 expectation failed (``FAIL:`` line), 2 environment missing.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_thin_slice as ts  # noqa: E402
from api_m2_admin import admin, items_of, ok  # noqa: E402
from api_thin_slice import E2EFailure, expect, heading, say  # noqa: E402

NW = "northwind-test"
SLUG = "fernhill"
FERNHILL = os.environ.get("FERNHILL_URL", "http://fernhill.localhost").rstrip("/")
FERNHILL_MCP = f"{FERNHILL}/mcp"
KEY_HEADER = "X-Adapter-Key"
ADMIN_HEADER = "X-Admin-Key"
DEFAULT_ENV_FILE = "/run/keelson.env"

READ_TOOLS = ("get_system_info", "scn.snapshot_open", "scn.snapshot_export", "scn.changes_since", "get_change_set",
              "get_audit_trail", "get_operator_guide", "list_transfer_orders")
PREVIEW_TOOLS = ("create_change_set", "add_change_set_items", "validate_change_set")
APPLY_TOOLS = ("apply_change_set", "rollback_change_set")
OPS = {"transfer_order.create", "allocation.set_qty"}
MAPPED = ("location", "item", "po_line", "inventory_position", "demand_forecast", "size_curve", "allocation",
          "transfer_order_line")

FROM_DC, TO_DC, SHIP_DATE = "FH-RTM", "FH-LDS", "2026-10-26"
TO_LINES = ({"sku": "FH-PARKA-NVY-M", "qty": 60}, {"sku": "FH-PARKA-NVY-L", "qty": 40})
ALLOCATION_DELTA = -2
SYNC_TIMEOUT_S = 240.0
RESYNC_TIMEOUT_S = 300.0

# ───────────────────────────────── environment ─────────────────────────────────

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


def _dotenv(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        out[key.strip()] = value
    return out


def fernhill_keys(env_file: str) -> tuple[str, str]:
    """``(api key, admin key)`` from the environment, else the dotenv file; exit 2 when missing."""
    found = {k: os.environ.get(k, "") for k in ("FERNHILL_API_KEY", "FERNHILL_ADMIN_KEY")}
    if not all(found.values()) and Path(env_file).is_file():
        dotenv = _dotenv(Path(env_file))
        found = {k: v or dotenv.get(k, "") for k, v in found.items()}
    missing = [k for k, v in found.items() if not v]
    if missing:
        print(f"ENV: {missing} not set (environment or {env_file}); run scripts/init and pass them with -e (see e2e/README.md)", file=sys.stderr)
        raise SystemExit(2)
    return found["FERNHILL_API_KEY"], found["FERNHILL_ADMIN_KEY"]


class Fernhill:
    """The sidecar's admin routes (``/v1/admin/*``, ``X-Admin-Key``) through Caddy."""

    def __init__(self, admin_key: str) -> None:
        self.client = httpx.Client(base_url=FERNHILL, timeout=httpx.Timeout(60.0, connect=10.0),
                                   follow_redirects=False, trust_env=False)
        self._headers = {ADMIN_HEADER: admin_key}

    def ready(self) -> bool:
        try:
            return self.client.get("/readyz").status_code == 200
        except httpx.HTTPError:
            return False

    def reset(self) -> dict[str, Any]:
        return ok(self.client.post("/v1/admin/reset", headers=self._headers), "Fernhill reset")

    def stats(self) -> dict[str, Any]:
        return ok(self.client.get("/v1/admin/stats", headers=self._headers), "Fernhill stats")

    def rows(self, table: str, **filters: str) -> list[dict[str, Any]]:
        r = self.client.get(f"/v1/admin/rows/{table}", params=filters, headers=self._headers)
        return list(ok(r, f"Fernhill rows {table} {filters}")["items"])

    def row(self, table: str, **filters: str) -> dict[str, Any]:
        rows = self.rows(table, **filters)
        expect(len(rows) == 1, f"Fernhill {table} {filters}: {len(rows)} rows")
        return rows[0]


def wait_until(what: str, fn: Any, *, timeout_s: float, every_s: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise E2EFailure(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(every_s)


def connector_call(principal: str, tool: str, args: dict[str, Any]) -> dict[str, Any]:
    """One tool call through the engine's own MCP gateway (the registered connector's key, egress)."""
    from keelson_engine.runtime.nodes.per_op import connector_call as call

    return dict(db(call(tenant=NW, connector=SLUG, principal=principal, tool=tool, args=args)))


def jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    return dict(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))))


# ───────────────────────────────── 1. reset ─────────────────────────────────


def reset(fh: Fernhill) -> dict[str, Any]:
    heading("1. Fernhill reset; the admin routes and /mcp refuse callers without a key")
    anonymous = fh.client.post("/v1/admin/reset")
    expect(anonymous.status_code == 401, f"reset without the admin key: HTTP {anonymous.status_code}")
    mcp = fh.client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"Accept": "application/json, text/event-stream"})
    expect(mcp.status_code == 401, f"/mcp without a key: HTTP {mcp.status_code}")
    out = fh.reset()
    expect(out.get("status") == "reset" and str(out.get("watermark", "")).startswith("j:"), f"reset {out}")
    stats = fh.stats()
    expect(stats["backend"] == "csv" and stats["change_sets"] == 0, f"stats after reset {stats}")
    say(f"reset → watermark {out['watermark']}; {sum(stats['tables'].values())} rows in {len(stats['tables'])} "
        "CSV tables; anonymous reset and /mcp → 401")
    return {"watermark": out["watermark"], "tables": stats["tables"]}


# ─────────────────────────────── 2–4. register, test, approve, activate ───────────────────────────────


def register(ava: ts.KeelsonUser, api_key: str) -> dict[str, Any]:
    heading("2. Register Fernhill on northwind-test (static header X-Adapter-Key)")
    existing = {c.get("slug") for c in items_of(ok(admin(ava, "GET", "/connectors", tenant=NW), "list connectors"))}
    if SLUG in existing:
        ok(admin(ava, "DELETE", f"/connectors/{SLUG}", tenant=NW), "delete the previous fernhill connector", 204)
        say("removed the fernhill connector of a previous run")
    body = {"slug": SLUG, "name": "Fernhill ERP (adapter sidecar)", "mcp_url": FERNHILL_MCP,
            "trust_level": "third_party",
            "auth": {"kind": "static_header", "header_name": KEY_HEADER, "secret": api_key}}
    created = ok(admin(ava, "POST", "/connectors", tenant=NW, body=body), "register connector", 201)
    expect(created.get("status") == "draft", f"new connector status {created.get('status')}")
    expect(api_key not in json.dumps(created), "the adapter key is echoed back by the registry")
    say(f"fernhill registered as draft ({FERNHILL_MCP}); key not echoed")

    heading("3. Connection test → tiers A·B·C·W; suggested classification from the sidecar's annotations")
    test = ok(admin(ava, "POST", f"/connectors/{SLUG}/test", tenant=NW, timeout=90.0), "test connection")
    expect(test.get("ok") is True, f"connection test failed: {test.get('steps')}")
    expect(list(test.get("tier") or []) == ["A", "B", "C", "W"], f"tier {test.get('tier')}")
    expect(api_key not in json.dumps(test), "the adapter key appears in the test log")
    tools = tool_rows(ava)
    expect(set(tools) == {*READ_TOOLS, *PREVIEW_TOOLS, *APPLY_TOOLS}, f"tools {sorted(tools)}")
    want = {**{n: ("read", "none") for n in READ_TOOLS}, **{n: ("write", "enforced") for n in PREVIEW_TOOLS},
            **{n: ("destructive", "enforced") for n in APPLY_TOOLS}}
    for name, (access, idem) in want.items():
        s = tools[name].get("suggested") or {}
        expect(s.get("access") == access and s.get("content_trust") == "trusted_data" and s.get("egress") == "internal",
               f"{name} suggested {s}")
        if idem == "enforced":
            expect(s.get("idempotency") == "enforced", f"{name} idempotency suggestion {s}")
    say(f"tier {test['tier']}; {len(tools)} tools; suggestions: reads trusted/internal, "
        "create/add/validate write + enforced, apply/rollback destructive + enforced")

    heading("4. Approve the tools as suggested, activate; commit capabilities")
    for name, t in tools.items():
        s = t["suggested"]
        ok(admin(ava, "PATCH", f"/connectors/{SLUG}/tools/{name}", tenant=NW, body={
            "status": "approved", "access": s["access"], "content_trust": s["content_trust"], "egress": s["egress"],
            "idempotency": s["idempotency"]}), f"approve {name}")
    act = admin(ava, "POST", f"/connectors/{SLUG}/activate", tenant=NW)
    expect(act.status_code == 200 and act.json().get("status") == "active",
           f"activate → HTTP {act.status_code} {act.text[:300]} (internal egress needs no change request)")
    after = tool_rows(ava)
    for name in (*PREVIEW_TOOLS, *APPLY_TOOLS):
        expect(after[name].get("binding") != "auto", f"write tool {name} must not be auto-bound")
    caps_json = ok(admin(ava, "GET", f"/connectors/{SLUG}/capabilities", tenant=NW), "capabilities")
    caps = commit_capabilities()
    expect(caps.commit_mode == "change_set" and caps.can_apply and caps.apply_idempotent, f"capabilities {caps}")
    expect(set(caps.supported_ops) == OPS and caps.ops_source == "tool_schema",
           f"ops {sorted(caps.supported_ops)} from {caps.ops_source}")
    say(f"{len(after)} tools approved, active; commit mode change_set, ops {sorted(caps.supported_ops)} "
        f"(from the op enum); capabilities {ts.ascii_text(json.dumps(caps_json), 160)}")
    return {"tier": test["tier"], "tools": sorted(tools)}


def tool_rows(ava: ts.KeelsonUser) -> dict[str, dict[str, Any]]:
    listed = ok(admin(ava, "GET", f"/connectors/{SLUG}/tools", tenant=NW), "connector tools")
    return {str(t["name"]): t for t in items_of(listed)}


def commit_capabilities() -> Any:
    from keelson_engine.runtime.candidates import capability_table
    from keelson_engine.runtime.deps import get_deps

    table = db(capability_table(get_deps(), tenant=NW, connectors=[SLUG], vendor="Fernhill ERP"))
    caps = table.get(SLUG)
    expect(caps is not None, "fernhill is missing from the capability table")
    return caps


# ─────────────────────────────── 5./8. sync ───────────────────────────────


def snapshots(ava: ts.KeelsonUser) -> dict[str, Any]:
    return ok(admin(ava, "GET", f"/connectors/{SLUG}/snapshots?limit=5", tenant=NW), "snapshots")


def wait_for_snapshot(ava: ts.KeelsonUser, prev_id: str | None, *, timeout_s: float, why: str) -> dict[str, Any]:
    started = time.monotonic()
    seen = {str(s.get("id")) for s in snapshots(ava).get("items") or []}

    def done() -> dict[str, Any] | None:
        snaps = snapshots(ava)
        failed = [s for s in snaps.get("items") or [] if s.get("status") == "failed" and str(s.get("id")) not in seen]
        if failed:
            raise E2EFailure(f"sync failed: {failed[0].get('error')}")
        cur = snaps.get("current") or {}
        if cur.get("id") and cur["id"] != prev_id and cur.get("status") == "ready":
            return cur
        return None

    cur = wait_until(why, done, timeout_s=timeout_s, every_s=3.0)
    say(f"  snapshot {cur['id']} ready after {time.monotonic() - started:.0f} s ({cur['rows']} rows, "
        f"watermark {cur['watermark']})")
    return cur


def sync(ava: ts.KeelsonUser, fh: Fernhill) -> dict[str, Any]:
    heading("5. Tier-B sync in the worker → consistent snapshot equal to the sidecar's counts")
    prev = (snapshots(ava).get("current") or {}).get("id")
    q = ok(admin(ava, "POST", f"/connectors/{SLUG}/sync", tenant=NW), "queue sync", 202)
    expect(q.get("status") == "queued", f"sync queued: {q}")
    cur = wait_for_snapshot(ava, prev, timeout_s=SYNC_TIMEOUT_S, why="the tier-B sync")
    stats = fh.stats()
    expect(cur["consistency"] == "consistent" and cur.get("as_of_range") is None, f"snapshot label {cur}")
    expect(cur["watermark"] == stats["watermark"], f"snapshot watermark {cur['watermark']} != {stats['watermark']}")
    counts = {k: int(v) for k, v in (cur.get("row_counts") or {}).items()}
    want = {e: int(stats["entities"].get(e, 0)) for e in MAPPED}
    mismatched = {e: (counts.get(e, 0), n) for e, n in want.items() if counts.get(e, 0) != n}
    expect(not mismatched, f"synced row counts differ from Fernhill's: {mismatched}")
    extra = {e: n for e, n in counts.items() if e not in MAPPED and n}
    expect(not extra, f"unmapped entities with rows: {extra}")
    for entity in MAPPED:
        say(f"  {entity:<20} synced {counts.get(entity, 0):>5}   Fernhill {want[entity]:>5}")
    return cur


def poll(expected: bool) -> bool:
    from keelson_engine.core import scn_sync

    changed = bool(db(scn_sync.poll_changes(NW, SLUG)))
    expect(changed is expected, f"poll_changes → {changed}, expected {expected}")
    return changed


async def _scn_rows(table: str, snapshot_id: str, where: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    from keelson_engine.core.db import tenant_session
    from sqlalchemy import text

    async with tenant_session(NW) as s:
        res = await s.execute(text(f"SELECT * FROM {table} WHERE tenant = :t AND snapshot_id = :sid AND {where}"),
                              {"t": NW, "sid": snapshot_id, **params})
        return [dict(m) for m in res.mappings().all()]


def scn_rows(table: str, snapshot_id: str, where: str = "true", **params: Any) -> list[dict[str, Any]]:
    return list(db(_scn_rows(table, snapshot_id, where, params)))


# ─────────────────────────────── 7. commit harness ───────────────────────────────


class Harness:
    """The approved-change-set path a worker runs (commit nodes), from the same building blocks."""

    def __init__(self, fh: Fernhill, snapshot: dict[str, Any]) -> None:
        self.fh = fh
        self.snapshot = snapshot
        self.out: dict[str, Any] = {}

    def setup(self) -> None:
        from keelson_contracts.runs import Actor

        from keelson_engine.core import repo as core_repo
        from keelson_engine.core.agent_specs import latest_version_name
        from keelson_engine.runtime.commit import compile_changeset, stored_items
        from keelson_engine.runtime.context import RunContext
        from keelson_engine.runtime.deps import get_deps
        from keelson_engine.runtime.runner_cache import default_spec_loader

        nora = ts.keelson_login("nora")
        r = nora.client.post(f"{ts.KEELSON}/api/cases", json={"title": "M8 e2e: adapter sidecar commit"},
                             headers=nora.headers())
        expect(r.status_code == 201, f"nora creates a case: HTTP {r.status_code} {r.text[:200]}")
        self.case_id = str(r.json()["id"])
        alloc = scn_rows("scn_allocation", self.snapshot["id"], "status = 'planned' AND qty >= 3 ORDER BY code LIMIT 1")
        expect(alloc, "no planned allocation in the synced snapshot")
        self.alloc = alloc[0]
        self.alloc_qty = int(self.alloc["qty"]) + ALLOCATION_DELTA
        self.actions = [
            {"seq": 1, "op": "transfer_order.create", "target_ref": None, "expected_version": None,
             "payload": {"from_dc": FROM_DC, "to_dc": TO_DC, "ship_date": SHIP_DATE,
                         "lines": [dict(x) for x in TO_LINES]}},
            {"seq": 2, "op": "allocation.set_qty", "target_ref": str(self.alloc["code"]),
             "expected_version": int(self.alloc["src_version"]), "payload": {"qty": self.alloc_qty}},
        ]
        option = {"id": "opt-e2e", "label": "E2E sidecar", "actions": self.actions}
        db(core_repo.save_plan(self.case_id, 1, [option, {"id": "do-nothing", "label": "Do nothing", "actions": []}],
                               "opt-e2e", {}, tenant=NW))
        self.deps = get_deps()
        spec = db(default_spec_loader(NW, db(latest_version_name(NW))))
        self.actor = Actor(sub=str(nora.me["user"]["sub"]), username="nora", name="Nora Test", roles=["planner"])
        self.run = RunContext(tenant=NW, case_id=self.case_id, run_id=f"e2e-m8-{uuid.uuid4().hex[:10]}", kind="action",
                              actor=self.actor, payload={}, spec=spec, deps=self.deps,
                              invocation_id=f"e2e-{uuid.uuid4().hex[:10]}", case_title="M8 e2e adapter sidecar")
        draft = compile_changeset(tenant=NW, change_set_id="draft", connector=SLUG, actions=self.actions)
        self.cs_id = db(self.deps.repo.create_change_set(tenant=NW, case_id=self.case_id, plan_version=1,
                                                         option_id="opt-e2e", connector=SLUG,
                                                         items=stored_items(draft, self.actions), hash=draft.hash))
        self.before = {
            "from": {x["sku"]: self.fh.row("stock", site=FROM_DC, sku=x["sku"]) for x in TO_LINES},
            "to": {x["sku"]: self.fh.row("stock", site=TO_DC, sku=x["sku"]) for x in TO_LINES},
            "alloc": self.fh.row("store_allocations", code=str(self.alloc["code"])),
            "transfers": len(self.fh.rows("transfers")),
        }
        lines = ", ".join(f"{x['sku']} x {x['qty']}" for x in TO_LINES)
        say(f"case {self.case_id[:8]}; change set {self.cs_id[:8]}: TO {FROM_DC}→{TO_DC} {lines}; allocation "
            f"{self.alloc['code']} {self.alloc['qty']}→{self.alloc_qty} (v{self.alloc['src_version']})")

    def load(self) -> tuple[dict[str, Any], Any]:
        from keelson_engine.runtime.commit import compile_changeset

        row = db(self.deps.repo.get_change_set(tenant=NW, change_set_id=self.cs_id))
        cs = compile_changeset(tenant=NW, change_set_id=self.cs_id, connector=SLUG, actions=list(row["items"]),
                               revision=int(row.get("revision") or 1))
        expect(cs.hash == row["hash"], "stored change-set hash mismatch")
        return row, cs

    async def _dry_run(self, cs: Any) -> Any:
        from keelson_engine.runtime.commit import dry_run
        from keelson_engine.runtime.mcp_client import get_gateway

        async with get_gateway().session(tenant=NW, connector=SLUG, principal="preparer") as s:
            return await dry_run(s, cs, title="M8 e2e — plan v1",
                                 origin={"system": "Keelson", "case_ref": self.case_id, "plan_version": 1})

    def dry_run(self) -> None:
        heading("7a. Dry run as the preparer: the sidecar's preview_hash equals the engine's H")
        _row, cs = self.load()
        result = db(self._dry_run(cs))
        expect(result.ok and result.preview_hash == cs.hash,
               f"dry run {result.status}: preview_hash {result.preview_hash} != H {cs.hash}")
        expect(all(i.get("ok") for i in result.items) and len(result.items) == 2, f"validated items {result.items}")
        units = sum(x["qty"] for x in TO_LINES) + abs(ALLOCATION_DELTA)
        # totals.lines counts change-set items like Merxa's validate (one transfer order + one allocation)
        expect(result.totals.get("lines") == len(result.items) and result.totals.get("units_moved") == units,
               f"totals {result.totals} (expected {len(result.items)} lines, {units} units)")
        db(self.deps.repo.update_change_set(
            tenant=NW, change_set_id=self.cs_id, status="validated", ext_change_set_id=result.ext_change_set_id,
            ext_code=result.ext_code,
            preview={"preview_hash": result.preview_hash, "items": result.items, "totals": result.totals}))
        self.ext_id, self.ext_code, self.hash = result.ext_change_set_id, result.ext_code, cs.hash
        say(f"{result.ext_code} validated: preview_hash {result.preview_hash[:12]}… == H; totals {result.totals}")

    async def _apply(self, receipt: str, approval_ref: str, key: str) -> Any:
        from keelson_engine.runtime.commit import apply_with_retry
        from keelson_engine.runtime.mcp_client import get_gateway

        async with get_gateway().session(tenant=NW, connector=SLUG, principal="autocommit") as s:
            return await apply_with_retry(s, ext_change_set_id=self.ext_id, preview_hash=self.hash,
                                          approval_ref=approval_ref, receipt=receipt, idempotency_key=key,
                                          attempts=2, backoff_s=1.0)

    def wrong_audience(self) -> None:
        heading("7b. A receipt minted for Merxa's audience is refused")
        from keelson_contracts.receipts import RECEIPT_AUDIENCE, approved_by_policy

        from keelson_engine.runtime.commit import approval_id_for, mint_receipt
        from keelson_engine.runtime.errors import McpToolError

        _row, cs = self.load()
        receipt, claims = mint_receipt(
            self.deps.crypto, change_set=cs, ext_change_set_id=self.ext_id, keelson_sub=self.actor.sub,
            ext_sub=f"connector:{SLUG}", approved_by=approved_by_policy("auto-small-transfer", "e2e"),
            approver_name=None, initiated_by="Nora Test", policy_version="e2e")
        expect(claims.aud == RECEIPT_AUDIENCE, f"default audience {claims.aud}")
        try:
            db(self._apply(receipt, approval_id_for(cs), f"e2e-wrong-aud-{uuid.uuid4().hex}"))
        except McpToolError as exc:
            expect(exc.code == "invalid_receipt" and "wrong_audience" in exc.detail,
                   f"refusal {exc.code}: {exc.detail}")
            say(f"aud {claims.aud} → {exc.code} ({ts.ascii_text(exc.detail, 90)})")
        else:
            raise E2EFailure("a receipt for Merxa's audience was accepted by the sidecar")
        state = connector_call("reader", "get_change_set", {"change_set_id": self.ext_id})
        expect(state["status"] == "validated", f"the refused apply changed the set: {state['status']}")

    def apply(self) -> None:
        heading("7c. Policy receipt bound to the sidecar (engine _credentials / _receipt_target) → applied")
        from keelson_contracts.receipts import approved_by_policy

        from keelson_engine.runtime.commit import approval_id_for, mint_receipt
        from keelson_engine.runtime.nodes.commit_nodes import _approver_claims, _credentials, _receipt_target

        _row, cs = self.load()
        approval = {"outcome": "auto", "approved_by": approved_by_policy("auto-small-transfer", "e2e"),
                    "policy_version": "e2e", "requirement": {"kind": "e2e_harness"}}
        principal, token, ext_sub = db(_credentials(self.run, SLUG, approval))
        expect(principal == "autocommit" and token is None and ext_sub == f"connector:{SLUG}",
               f"credentials {principal!r} {ext_sub!r}")
        keelson_sub, approved_by, approver_name, co = _approver_claims(approval, self.actor)
        target = db(_receipt_target(self.run, SLUG))
        expect(target.audience == FERNHILL_MCP and target.ext_iss == FERNHILL, f"receipt target {target}")
        receipt, claims = mint_receipt(
            self.deps.crypto, change_set=cs, ext_change_set_id=self.ext_id, keelson_sub=keelson_sub, ext_sub=ext_sub,
            approved_by=approved_by, approver_name=approver_name, initiated_by="Nora Test", policy_version="e2e",
            co_approved_by=co, audience=target.audience, ext_iss=target.ext_iss)
        decoded = jwt_claims(receipt)
        expect(decoded["aud"] == FERNHILL_MCP and decoded["ext_iss"] == FERNHILL and decoded["tenant"] == NW
               and decoded["ext_sub"] == f"connector:{SLUG}" and decoded["change_set_hash"] == cs.hash,
               f"receipt claims {decoded}")
        db(self.deps.repo.update_change_set(tenant=NW, change_set_id=self.cs_id, status="applying",
                                            approval_id=claims.approval_id, receipt_jti=claims.jti))
        outcome = db(self._apply(receipt, approval_id_for(cs), cs.idempotency_key))
        expect(outcome.status == "applied" and outcome.applied == 2 and outcome.ext_code == self.ext_code,
               f"apply outcome {outcome}")
        db(self.deps.repo.update_change_set(
            tenant=NW, change_set_id=self.cs_id, status="applied", ext_code=outcome.ext_code,
            result={"requirement": approval["requirement"], "approved_by": approved_by, "status": "applied",
                    "applied": outcome.applied, "audit_ref": outcome.audit_ref}))
        self.first = outcome
        self.approval_ref = approval_id_for(cs)
        self.out["apply"] = {"code": outcome.ext_code, "aud": decoded["aud"], "approved_by": approved_by}
        say(f"aud {decoded['aud']}, ext_iss {decoded['ext_iss']}, ext_sub {decoded['ext_sub']} → "
            f"{outcome.status} {outcome.ext_code} ({outcome.applied} items, {outcome.audit_ref})")

    async def _reconcile(self, cs: Any) -> Any:
        from keelson_engine.runtime.commit import reconcile
        from keelson_engine.runtime.mcp_client import get_gateway

        async with get_gateway().session(tenant=NW, connector=SLUG, principal="reader") as s:
            return await reconcile(s, ext_change_set_id=self.ext_id, change_set=cs)

    def reconcile(self) -> None:
        heading("7d. Reconcile → verified; Fernhill's CSV rows read back")
        _row, cs = self.load()
        result = db(self._reconcile(cs))
        expect(result.status == "applied" and result.verified == len(cs.items) == 2, f"reconcile {result}")
        expect(len(result.transfer_orders) == 1,
               f"transfer orders created via {self.ext_code}: {result.transfer_orders}")
        expect(result.audit_rows >= 2 and "Keelson" in (result.attribution_text or ""),
               f"audit rows {result.audit_rows}, attribution {result.attribution_text!r}")
        db(self.deps.repo.update_change_set(
            tenant=NW, change_set_id=self.cs_id, status="verified",
            result={"status": result.status, "applied": result.applied, "verified": result.verified,
                    "attribution_text": result.attribution_text, "transfer_orders": result.transfer_orders,
                    "audit_rows": result.audit_rows}))
        self.to_code = result.transfer_orders[0]
        header = self.fh.row("transfers", code=self.to_code)
        expect(header["created_via"] == f"change_set:{self.ext_code}" and header["from_wh"] == FROM_DC
               and header["to_wh"] == TO_DC and header["ship_date"] == SHIP_DATE, f"transfer header {header}")
        lines = sorted((x["sku"], x["qty"]) for x in self.fh.rows("transfer_lines", transfer_code=self.to_code))
        expect(lines == sorted((x["sku"], x["qty"]) for x in TO_LINES), f"transfer lines {lines}")
        for x in TO_LINES:
            src, dst = self.fh.row("stock", site=FROM_DC, sku=x["sku"]), self.fh.row("stock", site=TO_DC, sku=x["sku"])
            b_src, b_dst = self.before["from"][x["sku"]], self.before["to"][x["sku"]]
            expect(src["reserved"] == b_src["reserved"] + x["qty"] and src["rev"] == b_src["rev"] + 1,
                   f"{FROM_DC} {x['sku']} reserved {b_src['reserved']}→{src['reserved']}")
            expect(dst["in_transit"] == b_dst["in_transit"] + x["qty"], f"{TO_DC} {x['sku']} in transit {dst}")
        alloc = self.fh.row("store_allocations", code=str(self.alloc["code"]))
        expect(alloc["qty"] == self.alloc_qty and alloc["rev"] == self.before["alloc"]["rev"] + 1,
               f"allocation {alloc}")
        self.out["reconcile"] = {"verified": result.verified, "transfer_order": self.to_code,
                                 "attribution": result.attribution_text}
        say(f"verified {result.verified}/2; {self.to_code} created_via change_set:{self.ext_code}; "
            f"{FROM_DC} reserved +{', +'.join(str(x['qty']) for x in TO_LINES)}; allocation {alloc['code']} "
            f"qty {alloc['qty']} v{alloc['rev']}; '{ts.ascii_text(result.attribution_text, 80)}'")

    def replay(self) -> None:
        heading("7e. Apply again with the same idempotency key → identical body, still one transfer order")
        from keelson_contracts.receipts import approved_by_policy

        from keelson_engine.runtime.commit import mint_receipt

        _row, cs = self.load()
        target_claims = {"audience": FERNHILL_MCP, "ext_iss": FERNHILL}
        receipt, _claims = mint_receipt(
            self.deps.crypto, change_set=cs, ext_change_set_id=self.ext_id, keelson_sub=self.actor.sub,
            ext_sub=f"connector:{SLUG}", approved_by=approved_by_policy("auto-small-transfer", "e2e"),
            approver_name=None, initiated_by="Nora Test", policy_version="e2e", **target_claims)
        again = db(self._apply(receipt, self.approval_ref, cs.idempotency_key))
        expect(again.raw == self.first.raw, f"replayed body {again.raw} != {self.first.raw}")
        created = [t for t in self.fh.rows("transfers") if t["created_via"] == f"change_set:{self.ext_code}"]
        expect(len(created) == 1 and len(self.fh.rows("transfers")) == self.before["transfers"] + 1,
               f"transfer orders after the replay: {created}")
        listed = connector_call("reader", "list_transfer_orders", {"from_dc": FROM_DC, "to_dc": TO_DC})
        mine = [t for t in listed["items"] if t.get("created_via") == f"change_set:{self.ext_code}"]
        expect(len(mine) == 1 and mine[0]["code"] == self.to_code, f"list_transfer_orders {mine}")
        say("same key, fresh receipt → the stored response; 1 transfer order")

    def change_feed(self, ava: ts.KeelsonUser) -> None:
        heading("8. Tier C: changes_since lists the applied rows; poll → stale → the worker resyncs")
        from keelson_contracts import scn

        changes = scn.ChangesSince.model_validate(
            connector_call("reader", "scn.changes_since", {"watermark": self.snapshot["watermark"]}))
        entities = {c.entity for c in changes.changes}
        expect(changes.requires_sync and not changes.full_resync_required, f"changes_since {changes}")
        expect({"allocation", "inventory_position", "transfer_order_line"} <= entities, f"changed entities {entities}")
        refs = {c.key for c in changes.changes if c.entity == "allocation"}
        expect(str(self.alloc["code"]) in refs, f"allocation changes {refs}")
        say(f"{len(changes.changes)} change(s) after {self.snapshot['watermark']} → next {changes.next_watermark}; "
            f"entities {sorted(entities)}")
        current = snapshots(ava).get("current") or {}
        if current.get("id") == self.snapshot["id"]:
            poll(expected=True)
            say("poll_changes → True (marked stale; the worker syncs after the debounce)")
        else:  # the worker's own tier-C poll got there first
            say(f"the worker already resynced ({current.get('id')}); polling is idempotent from here")
        cur = wait_for_snapshot(ava, self.snapshot["id"], timeout_s=RESYNC_TIMEOUT_S, why="the resync after the apply")
        alloc = scn_rows("scn_allocation", cur["id"], "code = :c", c=str(self.alloc["code"]))
        expect(alloc and int(alloc[0]["qty"]) == self.alloc_qty
               and int(alloc[0]["src_version"]) == int(self.alloc["src_version"]) + 1, f"resynced allocation {alloc}")
        lines = scn_rows("scn_transfer_order_line", cur["id"], "to_code = :c", c=self.to_code)
        expect(sorted((r["sku_code"], int(r["qty"])) for r in lines) == sorted((x["sku"], x["qty"]) for x in TO_LINES),
               f"resynced transfer lines {lines}")
        sku = TO_LINES[0]["sku"]
        inv = scn_rows("scn_inventory_position", cur["id"], "location_code = :l AND sku_code = :s", l=FROM_DC, s=sku)
        want = int(self.before["from"][sku]["reserved"]) + TO_LINES[0]["qty"]
        expect(inv and int(inv[0]["reserved"]) == want, f"resynced {FROM_DC} {sku} reserved {inv}")
        poll(expected=False)
        self.out["resync"] = {"snapshot": cur["id"], "watermark": cur["watermark"]}
        say(f"new snapshot {cur['id']}: allocation qty {self.alloc_qty}, {self.to_code} lines, {FROM_DC} reserved "
            f"{want}; a second poll → False")

    def rollback(self) -> None:
        heading("9. Rollback with a user receipt (nora) → compensating change set; business columns restored")
        from keelson_contracts.receipts import ApprovalReceiptClaims

        from keelson_engine.runtime.nodes.commit_nodes import (
            _approver_claims,
            _credentials,
            _receipt_target,
            rollback_approval_id,
            rollback_key,
        )

        _row, cs = self.load()
        approval = {"outcome": "approved", "policy_version": "e2e",
                    "executor": {"sub": self.actor.sub, "username": "nora", "name": "Nora Test", "role": "approver"}}
        principal, _token, ext_sub = db(_credentials(self.run, SLUG, approval))
        keelson_sub, approved_by, approver_name, co = _approver_claims(approval, self.actor)
        target = db(_receipt_target(self.run, SLUG))
        approval_ref = rollback_approval_id(cs, 1)
        claims = ApprovalReceiptClaims.issue(
            approval_id=approval_ref, change_set_ref=self.ext_id, change_set_hash=cs.hash, tenant=NW,
            keelson_sub=keelson_sub, ext_sub=ext_sub, approved_by=approved_by, approver_name=approver_name,
            initiated_by="Nora Test", policy_version="e2e", co_approved_by=co, **target.claims())
        receipt = self.deps.crypto.sign_approval_receipt(claims)
        expect(approved_by == "user:nora" and principal == "autocommit", f"rollback approver {approved_by} {principal}")
        data = connector_call(principal, "rollback_change_set", {
            "change_set_id": self.ext_id, "approval_ref": approval_ref, "approval_receipt": receipt,
            "idempotency_key": rollback_key(cs, 1)})
        expect(data.get("status") == "rolled_back" and data.get("compensating_code"), f"rollback {data}")
        db(self.deps.repo.update_change_set(tenant=NW, change_set_id=self.cs_id, status="rolled_back",
                                            result={"rolled_back": True, "rollback_ref": approval_ref,
                                                    "rollback_change_set": data.get("compensating_code")}))
        for x in TO_LINES:
            src, dst = self.fh.row("stock", site=FROM_DC, sku=x["sku"]), self.fh.row("stock", site=TO_DC, sku=x["sku"])
            b_src, b_dst = self.before["from"][x["sku"]], self.before["to"][x["sku"]]
            expect(src["reserved"] == b_src["reserved"] and dst["in_transit"] == b_dst["in_transit"],
                   f"stock not restored: {src} / {dst}")
        alloc = self.fh.row("store_allocations", code=str(self.alloc["code"]))
        expect(alloc["qty"] == self.before["alloc"]["qty"] and alloc["rev"] == self.before["alloc"]["rev"] + 2,
               f"allocation after the rollback {alloc}")
        remaining = self.fh.rows("transfers")
        expect(all(t["code"] != self.to_code for t in remaining) and len(remaining) == self.before["transfers"],
               f"{self.to_code} still exists after the rollback")
        original = connector_call("reader", "get_change_set", {"change_set_id": self.ext_id})
        expect(original["status"] == "rolled_back", f"{self.ext_code} status {original['status']}")
        self.out["rollback"] = {"compensating": data["compensating_code"], "approval_ref": approval_ref}
        say(f"{self.ext_code} rolled back by {data['compensating_code']} ({data.get('applied')} changes): stock, "
            f"allocation qty {alloc['qty']} (v{alloc['rev']}) restored, {self.to_code} removed")


# ─────────────────────────────── main ───────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--env-file", default=DEFAULT_ENV_FILE,
                        help="dotenv file with FERNHILL_API_KEY / FERNHILL_ADMIN_KEY (when not in the environment)")
    parser.add_argument("--keep", action="store_true", help="leave the connector active and Fernhill's data as is")
    args = parser.parse_args()
    api_key, admin_key = fernhill_keys(args.env_file)
    fh = Fernhill(admin_key)
    if not fh.ready():
        print(f"ENV: {FERNHILL}/readyz is not ready (docker compose --profile adapters up -d fernhill-sidecar)",
              file=sys.stderr)
        return 2
    core_init()
    results: dict[str, Any] = {}
    ava: ts.KeelsonUser | None = None
    try:
        results["reset"] = reset(fh)
        ava = ts.keelson_login("ava")
        results["register"] = register(ava, api_key)
        snapshot = sync(ava, fh)
        results["sync"] = {"snapshot": snapshot["id"], "rows": snapshot["rows"], "watermark": snapshot["watermark"]}
        heading("6. Tier C before any change: poll_changes → False")
        poll(expected=False)
        say("no change since the snapshot's watermark")
        h = Harness(fh, snapshot)
        heading("7. Commit harness (test-only, in-process): change set → dry run → receipts → apply → reconcile")
        h.setup()
        h.dry_run()
        h.wrong_audience()
        h.apply()
        h.reconcile()
        h.replay()
        h.change_feed(ava)
        h.rollback()
        results["commit"] = h.out
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    finally:
        if not args.keep:
            try:
                fh.reset()
                say("clean-up: Fernhill reset to its golden data")
            except (E2EFailure, httpx.HTTPError) as exc:
                print(f"WARN: could not reset Fernhill: {exc}", flush=True)
            if ava is not None:
                try:
                    admin(ava, "POST", f"/connectors/{SLUG}/disable", tenant=NW)
                    say("clean-up: fernhill disabled (no syncs or polls against the reset data)")
                except httpx.HTTPError as exc:
                    print(f"WARN: could not disable {SLUG}: {exc}", flush=True)
    heading("Results")
    print(json.dumps(results, indent=2, default=str))
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
