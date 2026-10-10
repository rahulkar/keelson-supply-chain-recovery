"""M8 signal clustering real-stack checks that never call a model (docs/contracts/123), offline.

Runs inside the engine image with the engine's own environment, like the M6/M7 offline scripts
(``docker compose run --rm --no-deps -T -v <repo>/e2e:/e2e:ro engine-api python -u
/e2e/api_m8_clustering_offline.py``): HTTP through Caddy as a browser would (Keelson), plus the
engine's hub client for what no browser API shows (the hub's event bundle after the reset). The pack
ships pre-baked extractions and no chat is posted, so no model is ever called.

Steps:
1. Demo reset (``ava``): every step ok, the ``signal_hub`` step included.
2. Clustering settings: ``ava`` (platform admin) reads them editable, ``sam`` (tenant admin)
   read-only; settings version >= 1, local profile ``local-terms-v1``, pgvector available on pg-core.
3. Presenter ``ava`` starts and advances ``port-strike-syndication`` through every step.
4. ``priya`` (planner: ``ava`` holds only ``platform-admin``, which the alert routes do not accept)
   sees exactly one simulated alert, for the Hamburg strike: its detail has memberships
   ``{founded, near_duplicate, attach_vector}``; the near-duplicate differs by at most 6 SimHash bits
   and names the original; the vector member's similarity reaches the local strict threshold; the
   event is ``confirmed`` with the confidence of **two** independent sources (the syndicated copy is
   not counted: 7000 x 8000 x 10000 = 5600 bp); no explanation repeats any headline.
5. ``sam``'s settings change → 403 ``platform_admin_required``; ``ava``'s change on a stale
   ``base_version`` → 409 ``stale_version`` (nothing is changed).
6. The counts of the last 7 days show the near-duplicate and the vector attach.
7. A final demo reset clears the pack: no simulated alert, the strike event is gone from the hub.

Exit 0 pass, 1 expectation failed (``FAIL:`` line), 2 environment missing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import api_thin_slice as ts  # noqa: E402
from api_thin_slice import KEELSON, E2EFailure, expect, heading, say  # noqa: E402

TENANT = "demo-retail"
PACK = "port-strike-syndication"
CLUSTERING = f"{KEELSON}/api/admin/signals/clustering"
TWO_SOURCE_CONFIDENCE = 5600  # trade press 7000 x two independent sources 8000 x fresh 10000

_LOOP = asyncio.new_event_loop()


def db(coro: Any) -> Any:
    return _LOOP.run_until_complete(coro)


def core_init() -> None:
    try:
        from keelson_engine.core.db import init_db
    except ImportError as exc:  # not in the engine image
        print(f"ENV: run this inside the engine image ({exc})", file=sys.stderr)
        raise SystemExit(2) from exc
    from keelson_engine.core import signal_hub

    if not signal_hub.is_configured():
        print("ENV: SIGNAL_HUB_URL / SIGNAL_HUB_SHARED_SECRET are not set", file=sys.stderr)
        raise SystemExit(2)
    db(init_db())


def hub_event_exists(event_id: str) -> bool:
    from keelson_engine.core import signal_hub

    try:
        db(signal_hub._request("GET", f"/v1/events/{event_id}", tenant=TENANT))  # noqa: SLF001 - e2e introspection
    except signal_hub.HubNotFound:
        return False
    return True


def demo_reset(ava: ts.KeelsonUser) -> dict[str, Any]:
    started = time.monotonic()
    r = ava.client.post(f"{KEELSON}/api/demo/reset", json={}, headers=ava.headers(), timeout=180.0)
    expect(r.status_code == 200, f"demo reset: HTTP {r.status_code} {r.text[:300]}")
    log = r.json()
    failed = [s["step"] for s in log["steps"] if s["status"] == "failed"]
    expect(log["ok"] is True and not failed, f"demo reset failed steps: {failed}")
    hub_step = next((s for s in log["steps"] if s["step"] == "signal_hub"), None)
    expect(hub_step is not None and hub_step["status"] == "ok", f"signal_hub reset step: {hub_step}")
    say(f"demo reset ok in {time.monotonic() - started:.1f} s ({json.dumps(hub_step.get('counts'))})")
    return {"signal_hub": hub_step.get("counts")}


def simulated_alerts(user: ts.KeelsonUser) -> list[dict[str, Any]]:
    r = user.client.get(f"{KEELSON}/api/alerts", params={"limit": 200})
    expect(r.status_code == 200, f"GET /api/alerts: HTTP {r.status_code} {r.text[:200]}")
    return [a for a in r.json()["items"] if a.get("simulated")]


def clustering(user: ts.KeelsonUser) -> dict[str, Any]:
    r = user.client.get(CLUSTERING)
    expect(r.status_code == 200, f"GET clustering as {user.username}: HTTP {r.status_code} {r.text[:300]}")
    return dict(r.json())


# ───────────────────────────────── steps ─────────────────────────────────


def check_settings(ava: ts.KeelsonUser, sam: ts.KeelsonUser) -> dict[str, Any]:
    heading("2. Clustering settings: platform admin edits, tenant admin reads")
    mine, theirs = clustering(ava), clustering(sam)
    expect(mine["editable"] is True and theirs["editable"] is False, "editable must follow the platform-admin role")
    cfg = mine["config"]
    expect(cfg["version"] >= 1, f"settings version {cfg['version']}")
    backend = mine["vector_backend"]
    expect(backend["local_profile"] == "local-terms-v1", f"local profile {backend['local_profile']}")
    expect(backend["pgvector"] is True, "pgvector should be available in the pg-core signals database")
    say(f"settings v{cfg['version']}: simhash {cfg['simhash']}, local thresholds {cfg['vector']['local']}, "
        f"remote {backend['remote'] or backend['remote_reason']}")
    return {"version": cfg["version"], "local": cfg["vector"]["local"], "remote_reason": backend["remote_reason"]}


def check_pack(ava: ts.KeelsonUser, priya: ts.KeelsonUser, local: dict[str, int]) -> dict[str, Any]:
    heading(f"3-4. Replay {PACK}: one alert, grouped by first report / near-duplicate / similar description")
    r = ava.client.post(f"{KEELSON}/api/demo/packs/{PACK}/start", json={}, headers=ava.headers(), timeout=60.0)
    expect(r.status_code == 200, f"pack start: HTTP {r.status_code} {r.text[:300]}")
    state = r.json()
    for _ in range(10):
        if state.get("status") == "finished":
            break
        r = ava.client.post(f"{KEELSON}/api/demo/packs/{PACK}/advance", json={}, headers=ava.headers(), timeout=60.0)
        expect(r.status_code == 200, f"pack advance: HTTP {r.status_code} {r.text[:300]}")
        state = r.json()
    expect(state.get("status") == "finished", f"pack not finished: {state.get('status')}")
    deadline = time.monotonic() + 30
    alerts: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        alerts = simulated_alerts(priya)
        if alerts:
            break
        time.sleep(1)
    expect(len(alerts) == 1, f"expected exactly one simulated alert, got {len(alerts)}")
    alert = alerts[0]
    r = priya.client.get(f"{KEELSON}/api/alerts/{alert['id']}")
    expect(r.status_code == 200, f"alert detail: HTTP {r.status_code} {r.text[:200]}")
    detail = r.json()
    event = detail["event"]
    expect(event["kind"] == "labor" and event["subtype"] == "strike", f"event {event['kind']}/{event['subtype']}")
    memberships = {m["signal_id"]: m for m in detail.get("memberships") or []}
    methods = sorted(m["method"] for m in memberships.values())
    expect(methods == ["attach_vector", "founded", "near_duplicate"], f"membership methods {methods}")
    by_method = {m["method"]: m for m in memberships.values()}
    near, vector, first = by_method["near_duplicate"], by_method["attach_vector"], by_method["founded"]
    expect(near["hamming_bits"] is not None and 0 <= near["hamming_bits"] <= 6, f"hamming bits {near['hamming_bits']}")
    expect(near["duplicate_of_signal_id"] == first["signal_id"], "the near-duplicate must name the first report")
    expect(vector["similarity_bp"] >= local["strict_bp"], f"vector similarity {vector['similarity_bp']} < strict {local['strict_bp']}")
    expect(vector["similarity_profile"] == "local-terms-v1", f"profile {vector['similarity_profile']}")
    expect(event["status"] == "confirmed" and event["confidence_bp"] == TWO_SOURCE_CONFIDENCE,
           f"event {event['status']} confidence {event['confidence_bp']} (expected 2 independent sources)")
    titles = [s["title"] for s in detail["signals"]]
    for m in memberships.values():
        expect(all(t not in m["explanation"] for t in titles), f"an explanation repeats a headline: {m['explanation']}")
    ports = {e["node_code"] for e in detail["exposures"] if e["node_type"] == "port"}
    expect("DEHAM" in ports, f"exposed ports {sorted(ports)}")
    say(f"alert {alert['id']}: {methods}; near-duplicate {near['hamming_bits']} bits; vector {vector['similarity_bp']} bp; "
        f"confidence {event['confidence_bp']} bp")
    return {"alert_id": alert["id"], "event_id": event["id"], "methods": methods,
            "hamming_bits": near["hamming_bits"], "similarity_bp": vector["similarity_bp"]}


def check_writes(ava: ts.KeelsonUser, sam: ts.KeelsonUser) -> dict[str, Any]:
    heading("5. Changing the settings: platform admins only, optimistic on base_version")
    current = clustering(ava)["config"]
    body = {"base_version": current["version"], "simhash": current["simhash"], "vector": current["vector"]}
    r = sam.client.put(CLUSTERING, json=body, headers=sam.headers())
    expect(r.status_code == 403 and r.json().get("code") == "platform_admin_required",
           f"tenant admin PUT: HTTP {r.status_code} {r.text[:200]}")
    r = ava.client.put(CLUSTERING, json={**body, "base_version": current["version"] + 5}, headers=ava.headers())
    expect(r.status_code == 409 and r.json().get("code") == "stale_version", f"stale PUT: HTTP {r.status_code} {r.text[:200]}")
    expect(clustering(ava)["config"]["version"] == current["version"], "a refused change must not bump the version")
    say("tenant admin 403, stale base 409, version unchanged")
    return {"version": current["version"]}


def check_counts(ava: ts.KeelsonUser) -> dict[str, Any]:
    heading("6. Counts of the last 7 days")
    counts = clustering(ava)["counts"]
    expect(counts["near_duplicate"] >= 1 and counts["attach_vector"] >= 1 and counts["simhash_near_duplicates"] >= 1,
           f"counts {counts}")
    say(json.dumps({k: v for k, v in counts.items() if v}))
    return counts


def check_final_reset(ava: ts.KeelsonUser, priya: ts.KeelsonUser, event_id: str) -> dict[str, Any]:
    heading("7. Demo reset clears the pack's signals, memberships and alerts")
    out = demo_reset(ava)
    expect(simulated_alerts(priya) == [], "a simulated alert survived the demo reset")
    expect(not hub_event_exists(event_id), "the strike event survived the demo reset")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-final-reset", action="store_true")
    args = parser.parse_args()
    core_init()
    results: dict[str, Any] = {}
    try:
        ava, sam, priya = ts.keelson_login("ava"), ts.keelson_login("sam"), ts.keelson_login("priya")
        heading("1. Demo reset")
        results["reset"] = demo_reset(ava)
        results["settings"] = check_settings(ava, sam)
        results["pack"] = check_pack(ava, priya, results["settings"]["local"])
        results["writes"] = check_writes(ava, sam)
        results["counts"] = check_counts(ava)
        if not args.skip_final_reset:
            results["final_reset"] = check_final_reset(ava, priya, results["pack"]["event_id"])
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    heading("Results")
    print(json.dumps(results, indent=2, default=str))
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
