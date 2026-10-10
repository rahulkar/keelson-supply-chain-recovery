"""M8 Evals UI real-stack checks that never call a model (docs/contracts/128 §7), offline.

The stack runs with ``LLM_REPLAY_MODE=replay`` (fail-closed: no provider call is possible) and the
in-app runner up (``EVALS_RUNNER_TOKEN`` set; ``docker compose --profile evals up -d evals-runner``).
Like the M5–M7 offline scripts it runs inside the engine image (only httpx and the HTTP helpers of
``api_thin_slice.py`` are used):

    docker compose run --rm --no-deps -T -v D:/keelson-supply-chain-recovery/e2e:/e2e:ro \
      -v D:/keelson-supply-chain-recovery/evals/tests/fixtures:/fixtures:ro \
      engine-api python -u /e2e/api_m8_evals_offline.py

Steps (as the platform admin ``ava`` unless noted):

1. ``GET /api/admin/evals/status``: the runner is online with its suite (≥ 7 cases), the process
   replay mode is ``replay`` and both live modes are unavailable (``replay_only``).
2. A replay run of ``store_shortage_question`` (``acknowledge_reset``) is queued, claimed by the
   runner and followed through ``GET …/runs/{id}/events?after=`` until it ends ``succeeded``.
3. No live model call happened: the run's engine counters show ``llm_calls_live == 0`` and
   ``tokens_live == 0``, the tenant's gateway usage did not grow, and the case is ``pass`` or ``error``
   with ``error_code replay_miss`` (recordings may be gone: the run fails closed, never live).
4. A live request is refused: ``409 evals_unavailable`` with ``reason replay_only``.
5. The two committed CLI result files (``/fixtures/results-v1-*.json``) import as runs (201); the same
   file again answers ``duplicate: true`` (200).
6. Compare(import A, import B): ``adversarial_kb_doc`` fixed, ``adversarial_supplier_message`` changed
   (fail → error), the four cases only in A removed.
7. Trends list all three runs; the in-app run's export re-imports as a duplicate of it.
8. The tenant admin ``sam`` gets 403 on the Evals routes.

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

try:
    import api_thin_slice as ts  # noqa: E402
    from api_thin_slice import KEELSON, E2EFailure, expect, heading, say  # noqa: E402
except ImportError as exc:  # httpx missing: not the engine image / an e2e container
    print(f"ENV: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc

EVALS = f"{KEELSON}/api/admin/evals"
CASE = "store_shortage_question"
V1_A = "results-v1-20261007T080848Z.json"
V1_B = "results-v1-20261007T085022Z.json"
FINAL = ("succeeded", "failed", "cancelled")


def call(user: ts.KeelsonUser, method: str, path: str, *, body: Any = None, params: dict[str, Any] | None = None,
         timeout: float = 60.0) -> Any:
    headers = user.headers() if method != "GET" else {}
    return user.client.request(method, f"{EVALS}{path}", json=body, params=params, headers=headers, timeout=timeout)


def ok(r: Any, what: str, *codes: int) -> Any:
    codes = codes or (200,)
    expect(r.status_code in codes, f"{what}: HTTP {r.status_code} {r.text[:400]}")
    return r.json()


def gateway_tokens(sam: ts.KeelsonUser) -> int | None:
    r = sam.client.get(f"{KEELSON}/api/admin/gateway/usage", params={"days": 2})
    if r.status_code != 200:
        return None
    return sum(int(d.get("tokens_in") or 0) + int(d.get("tokens_out") or 0) for d in r.json().get("daily") or [])


def wait_for(what: str, fn: Any, *, timeout_s: float, every_s: float = 2.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise E2EFailure(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(every_s)


# ─────────────────────────────── 1. status ───────────────────────────────


def status(ava: ts.KeelsonUser) -> dict[str, Any]:
    heading("1. Status: runner online, replay-only installation")

    def online() -> dict[str, Any] | None:
        body = ok(call(ava, "GET", "/status"), "status")
        return body if body["runner"]["online"] and body["suite"] else None

    body = wait_for("the eval runner's heartbeat", online, timeout_s=90)
    cases = [c["id"] for c in body["suite"]["cases"]]
    expect(len(cases) >= 7 and CASE in cases, f"suite cases {cases}")
    expect(body["process_replay_mode"] == "replay", f"process replay mode {body['process_replay_mode']} (run the stack "
                                                    "with LLM_REPLAY_MODE=replay)")
    modes = {m["mode"]: m for m in body["modes"]}
    expect(modes["replay"]["available"] is True, f"replay unavailable: {modes['replay']}")
    for live in ("record_or_replay", "record"):
        expect(modes[live]["reason"] == "replay_only", f"{live}: {modes[live]}")
    expect(body["active_run"] is None, f"another run is active: {body['active_run'] and body['active_run']['id']}")
    say(f"runner {body['runner']['runner_id']} online, {len(cases)} cases, recordings {body['recordings']['rows']}")
    return {"cases": len(cases), "recordings": body["recordings"]["rows"]}


# ─────────────────────────────── 2–3. replay run ───────────────────────────────


def replay_run(ava: ts.KeelsonUser, sam: ts.KeelsonUser, timeout_s: float) -> dict[str, Any]:
    heading(f"2. Replay run of {CASE}")
    tokens_before = gateway_tokens(sam)
    run = ok(call(ava, "POST", "/runs", body={"mode": "replay", "case_ids": [CASE], "acknowledge_reset": True,
                                               "note": "e2e api_m8_evals_offline"}), "queue a replay run", 202)
    rid = run["id"]
    say(f"run {rid} queued")
    after = 0
    seen: list[str] = []
    deadline = time.monotonic() + timeout_s
    current = run["status"]
    while current not in FINAL:
        expect(time.monotonic() < deadline, f"run still {current} after {timeout_s:.0f} s")
        page = ok(call(ava, "GET", f"/runs/{rid}/events", params={"after": after}), "events")
        for e in page["items"]:
            seen.append(e["type"])
            say(f"  #{e['seq']:<3} {e['type']:<14} {e.get('case_id') or ''} {e.get('turn_id') or ''} "
                f"{ts.ascii_text(json.dumps(e.get('data') or {}), 100)}")
        after = page["next_after"]
        current = page["status"]
        if current not in FINAL:
            time.sleep(2.0)
    expect(current == "succeeded", f"run ended {current}")
    expect("run_started" in seen and "case_finished" in seen, f"events {seen}")

    heading("3. No live model call")
    detail = ok(call(ava, "GET", f"/runs/{rid}"), "run detail")
    counters = detail["run"]["counters"]
    expect(counters["llm_calls_live"] == 0 and counters["tokens_live"] == 0, f"live usage in a replay run: {counters}")
    [row] = detail["cases"]
    expect(row["status"] == "pass" or (row["status"] == "error" and row["error_code"] == "replay_miss"),
           f"case {row['status']} {row['error_code']}: {row['error']}")
    case = ok(call(ava, "GET", f"/runs/{rid}/cases/{CASE}"), "case result")
    if case.get("engine") is not None:
        expect(case["engine"]["llm_calls_live"] == 0, f"engine accounting {case['engine']}")
    time.sleep(15)  # LiteLLM writes spend logs in batches
    tokens_after = gateway_tokens(sam)
    if tokens_before is not None and tokens_after is not None:
        expect(tokens_after == tokens_before, f"gateway tokens grew {tokens_before} -> {tokens_after}")
    say(f"case {row['status']} ({row['error_code'] or 'no error'}); engine counters {counters}; gateway "
        f"{tokens_before} -> {tokens_after}")
    return {"run_id": rid, "case": row["status"], "error_code": row["error_code"], "counters": counters}


def live_refused(ava: ts.KeelsonUser) -> None:
    heading("4. Live runs are refused on a replay-only installation")
    r = call(ava, "POST", "/runs", body={"mode": "record_or_replay", "case_ids": [CASE], "acknowledge_reset": True,
                                          "confirm_live": "RUN LIVE",
                                          "budget": {"max_tokens": 1000, "max_cost_microusd": 1000}})
    body = ok(r, "live request", 409)
    expect(body.get("code") == "evals_unavailable" and body.get("reason") == "replay_only", f"answer {body}")
    say("409 evals_unavailable (replay_only)")


# ─────────────────────────────── 5–7. imports, compare, trends ───────────────────────────────


def imports(ava: ts.KeelsonUser, fixtures: Path) -> tuple[str, str]:
    heading("5. Import the two committed CLI result files")
    ids: list[str] = []
    for name in (V1_A, V1_B):
        path = fixtures / name
        expect(path.is_file(), f"missing fixture {path}")
        doc = json.loads(path.read_text(encoding="utf-8"))
        first = call(ava, "POST", "/import", body=doc, params={"file_name": name})
        expect(first.status_code in (200, 201), f"import {name}: HTTP {first.status_code} {first.text[:300]}")
        again = ok(call(ava, "POST", "/import", body=doc, params={"file_name": name}), f"re-import {name}")
        expect(again == {"run_id": first.json()["run_id"], "duplicate": True}, f"re-import {name}: {again}")
        ids.append(first.json()["run_id"])
        say(f"{name} -> run {ids[-1]} ({'new' if first.status_code == 201 else 'already imported'}); again: duplicate")
    return ids[0], ids[1]


def compare(ava: ts.KeelsonUser, base: str, head: str) -> None:
    heading("6. Compare the two imported runs")
    cmp = ok(call(ava, "GET", "/compare", params={"base": base, "head": head}), "compare")
    changes = {c["case_id"]: c["change"] for c in cmp["cases"]}
    expect(changes.get("adversarial_kb_doc") == "fixed", f"adversarial_kb_doc {changes.get('adversarial_kb_doc')}")
    expect(changes.get("adversarial_supplier_message") == "changed", f"supplier message {changes}")
    removed = sorted(k for k, v in changes.items() if v == "removed")
    expect(removed == ["followup_delay_doubles", "store_shortage_question", "suez_whatif", "trim_delay_actual"],
           f"removed {removed}")
    say(f"changes {changes}")


def trends_and_export(ava: ts.KeelsonUser, run_ids: list[str], in_app: str) -> None:
    heading("7. Trends and the export round trip")
    trends = ok(call(ava, "GET", "/trends", params={"limit": 100}), "trends")
    listed = {r["id"] for r in trends["runs"]}
    expect(set(run_ids) <= listed, f"trends miss {set(run_ids) - listed}")
    exported = call(ava, "GET", f"/runs/{in_app}/export")
    expect(exported.status_code == 200 and "attachment" in exported.headers.get("content-disposition", ""),
           f"export: HTTP {exported.status_code}")
    again = ok(call(ava, "POST", "/import", body=exported.json()), "re-import the export")
    expect(again == {"run_id": in_app, "duplicate": True}, f"export re-import: {again}")
    say(f"{len(trends['runs'])} runs in the trends; the export re-imports as run {in_app}")


def tenant_admin_refused() -> None:
    heading("8. A tenant admin cannot use the Evals API")
    sam = ts.keelson_login("sam")
    for path in ("/status", "/runs", "/trends"):
        r = sam.client.get(f"{EVALS}{path}")
        expect(r.status_code == 403 and r.json().get("code") == "platform_admin_required", f"sam GET {path}: {r.status_code}")
    r = sam.client.post(f"{EVALS}/runs", json={"acknowledge_reset": True}, headers=sam.headers())
    expect(r.status_code == 403, f"sam POST /runs: {r.status_code}")
    say("403 platform_admin_required")


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--fixtures-dir", type=Path, default=Path("/fixtures"))
    parser.add_argument("--run-timeout", type=float, default=1200.0, help="seconds to wait for the replay run")
    args = parser.parse_args()
    results: dict[str, Any] = {}
    try:
        ava = ts.keelson_login("ava")
        sam = ts.keelson_login("sam")
        results["status"] = status(ava)
        results["replay_run"] = replay_run(ava, sam, args.run_timeout)
        live_refused(ava)
        a, b = imports(ava, args.fixtures_dir)
        compare(ava, a, b)
        trends_and_export(ava, [a, b, results["replay_run"]["run_id"]], results["replay_run"]["run_id"])
        tenant_admin_refused()
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    heading("Results")
    print(json.dumps(results, indent=2, default=str))
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
