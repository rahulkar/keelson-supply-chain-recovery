"""M8 signal feeds on the real stack, offline (docs/contracts/124-m8-signal-feeds.md): no model and
no internet are needed.

Runs inside the engine image with the engine's own environment, like the M6/M7 offline scripts
(``docker compose run --rm --no-deps -T -v <repo>/e2e:/e2e:ro engine-api python -u
/e2e/api_m8_feeds_offline.py``): HTTP through Caddy as a browser would, plus the engine's own Signal
Hub client for what no browser API shows (the hub's schedule and change feed).

Steps:
1. Demo reset (``ava``): every step ok.
2. Sources catalogue (``sam``, ``GET /api/admin/signals/sources``): NHC, Copernicus EMS and OFAC are
   polled ``fact`` sources with their licences and an https terms link; GDELT is an opt-in
   ``inference`` source, not subscribed and paused; MeteoAlarm (when configured) is opt-in too.
3. Opt-in round trip: ``PATCH …/sources/gdelt {enabled: true}`` → pushed to the hub, which un-pauses
   GDELT at its 900 s cadence; ``{enabled: false}`` → paused again.
4. Hurricane pack (presenter ``ava``, ``POST /api/demo/packs/hurricane-cape-route/…``): advisory 7 →
   exactly one simulated alert, medium, exposing only the four CAPE-routed lanes (``lane_crosses``),
   attributed to "NOAA National Hurricane Center (demo replay)" (public domain); advisory 8 → 8500 bp,
   re-notified as critical with the action still ``alert`` (no site exposed, so no case is drafted);
   advisory 9 → edited, the pack finishes.
5. Demo reset: the simulated alert and the pack state are gone.

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
from api_m2_admin import admin, ok  # noqa: E402
from api_thin_slice import KEELSON, E2EFailure, expect, heading, say  # noqa: E402

TENANT = "demo-retail"
PACK = "hurricane-cape-route"
CAPE_LANES = {"CGP-FXT-SEA": "lane_crosses", "CGP-RTM-SEA": "lane_crosses", "SGN-HAM-CAPE": "lane_crosses",
              "SGN-GDN-CAPE": "lane_crosses"}
# data/scenarios/hurricane-cape-route/expected.yaml: (event severity, alert severity) after steps 0..2
CHECKPOINTS = [(6500, "medium"), (8500, "critical"), (8500, "critical")]
LICENCES = {"nhc": "Public domain (NOAA/NWS)", "ofac": "Public domain (U.S. Treasury OFAC)"}

_LOOP = asyncio.new_event_loop()


def db(coro: Any) -> Any:
    return _LOOP.run_until_complete(coro)


def core_init() -> None:
    try:
        from keelson_engine.core import signal_hub
    except ImportError as exc:  # not in the engine image
        print(f"ENV: run this inside the engine image ({exc})", file=sys.stderr)
        raise SystemExit(2) from exc
    if not signal_hub.is_configured():
        print("ENV: SIGNAL_HUB_URL / SIGNAL_HUB_SHARED_SECRET are not set", file=sys.stderr)
        raise SystemExit(2)


def hub(method: str, path: str, **kw: Any) -> Any:
    """The Signal Hub over the internal network with the engine's service JWT (tenant = demo-retail)."""
    from keelson_engine.core import signal_hub

    return db(signal_hub._request(method, path, tenant=TENANT, **kw))  # noqa: SLF001 - e2e introspection


def wait_until(what: str, fn: Any, *, timeout_s: float, every_s: float = 1.0) -> Any:
    deadline = time.monotonic() + timeout_s
    while True:
        value = fn()
        if value:
            return value
        if time.monotonic() > deadline:
            raise E2EFailure(f"timed out after {timeout_s:.0f} s waiting for {what}")
        time.sleep(every_s)


def demo_reset() -> dict[str, Any]:
    ava = ts.keelson_login("ava")
    r = ava.client.post(f"{KEELSON}/api/demo/reset", json={}, headers=ava.headers(), timeout=180.0)
    expect(r.status_code == 200, f"demo reset: HTTP {r.status_code} {r.text[:300]}")
    log = r.json()
    expect(log["ok"] is True, f"demo reset failed steps: {[s['step'] for s in log['steps'] if s['status'] == 'failed']}")
    return dict(log)


def simulated_alerts(user: ts.KeelsonUser) -> list[dict[str, Any]]:
    r = user.client.get(f"{KEELSON}/api/alerts", params={"limit": 200})
    expect(r.status_code == 200, f"GET /api/alerts: HTTP {r.status_code} {r.text[:200]}")
    return [a for a in r.json()["items"] if a.get("simulated")]


def get_alert(user: ts.KeelsonUser, alert_id: str) -> dict[str, Any]:
    r = user.client.get(f"{KEELSON}/api/alerts/{alert_id}")
    expect(r.status_code == 200, f"GET /api/alerts/{alert_id}: HTTP {r.status_code} {r.text[:200]}")
    return dict(r.json())


def pack_action(ava: ts.KeelsonUser, action: str) -> dict[str, Any]:
    r = ava.client.post(f"{KEELSON}/api/demo/packs/{PACK}/{action}", json={}, headers=ava.headers(), timeout=60.0)
    expect(r.status_code == 200, f"pack {action}: HTTP {r.status_code} {r.text[:300]}")
    return dict(r.json())


def exposure_map(detail: dict[str, Any]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for e in detail["exposures"]:
        out.setdefault(e["node_type"], {})[e["node_code"]] = e["method"]
    return out


def hub_source(connector_id: str) -> dict[str, Any]:
    items = hub("GET", f"/v1/tenants/{TENANT}/sources").get("items", [])
    row = next((i for i in items if i.get("connector_id") == connector_id), None)
    expect(row is not None, f"the hub does not list {connector_id}")
    return dict(row)


# ───────────────────────────────── 2. catalogue ─────────────────────────────────


def check_catalogue() -> dict[str, Any]:
    heading("2. Sources catalogue: M8 feeds with licences, provenance, opt-in")
    sam = ts.keelson_login("sam")
    src = ok(admin(sam, "GET", "/signals/sources"), "sources")
    expect(src.get("hub_error") is None, f"hub error {src.get('hub_error')}")
    items = {s["id"]: s for s in src["items"]}
    for sid in ("nhc", "copernicus_ems", "ofac", "gdelt"):
        expect(sid in items, f"{sid} missing from the catalogue: {sorted(items)}")
    for sid in ("nhc", "copernicus_ems", "ofac"):
        s = items[sid]
        expect(s["polled"] is True and s["opt_in"] is False and s["enabled"] is True, f"{sid}: {s}")
        expect(s["provenance"] == "fact" and (s["terms_url"] or "").startswith("https://"), f"{sid}: {s}")
    for sid, licence in LICENCES.items():
        expect(items[sid]["licence"] == licence, f"{sid} licence {items[sid]['licence']!r}")
    expect(str(items["copernicus_ems"]["licence"]).startswith("© European Union, Copernicus"),
           f"copernicus licence {items['copernicus_ems']['licence']!r}")
    expect("not a sanctions-screening service" in str(items["ofac"]["description"]), "the OFAC disclaimer is missing")
    g = items["gdelt"]
    expect(g["opt_in"] is True and g["enabled"] is False and g["paused"] is True and g["provenance"] == "inference",
           f"gdelt {g}")
    for sid, s in items.items():
        if sid.startswith("meteoalarm."):
            expect(s["opt_in"] is True and s["licence_class"] == "terms_apply", f"{sid}: {s}")
    say(f"{len(items)} sources; NHC/EMS/OFAC polled facts; GDELT opt-in, paused")
    return {"sources": sorted(items)}


# ───────────────────────────────── 3. opt-in round trip ─────────────────────────────────


def check_opt_in() -> dict[str, Any]:
    heading("3. Opt-in: subscribing un-pauses GDELT at the hub, unsubscribing pauses it again")
    sam = ts.keelson_login("sam")
    on = ok(admin(sam, "PATCH", "/signals/sources/gdelt", body={"enabled": True}), "subscribe gdelt")
    expect(on["enabled"] is True and on["hub_synced"] is True, f"subscribe {on}")
    row = hub_source("gdelt")
    expect(row["enabled"] is True and row["paused"] is False and row["effective_cadence_s"] == 900, f"hub {row}")
    off = ok(admin(sam, "PATCH", "/signals/sources/gdelt", body={"enabled": False}), "unsubscribe gdelt")
    expect(off["enabled"] is False and off["hub_synced"] is True, f"unsubscribe {off}")
    row = hub_source("gdelt")
    expect(row["enabled"] is False and row["paused"] is True, f"hub after unsubscribe {row}")
    say("gdelt: paused → subscribed (900 s, polling) → paused")
    return {"gdelt": "round trip ok"}


# ───────────────────────────────── 4. hurricane pack ─────────────────────────────────


def check_detail(detail: dict[str, Any], step: int) -> None:
    sev_bp, sev = CHECKPOINTS[step]
    a, e = detail["alert"], detail["event"]
    expect(a["simulated"] is True and a["severity"] == sev, f"step {step}: alert {a['severity']} (want {sev})")
    expect(e["severity_bp"] == sev_bp and e["taxonomy_key"] == "NAT.storm" and e["anchor_signal_id"],
           f"step {step}: event {e['severity_bp']} {e['taxonomy_key']}")
    expect(exposure_map(detail) == {"lane": CAPE_LANES}, f"step {step}: exposure {exposure_map(detail)}")
    expect(a["case_id"] is None, f"step {step}: a case was opened for a lanes-only alert ({a['case_id']})")
    for s in detail["signals"]:
        src = s["source"]
        expect(src["connector_id"] == "demo.nhc" and src["name"] == "NOAA National Hurricane Center (demo replay)"
               and src["license"] == LICENCES["nhc"] and s["provenance"] == "simulated",
               f"attribution {src} / {s['provenance']}")


def check_hurricane() -> dict[str, Any]:
    heading("4. Hurricane pack: lanes-only alert, re-notify at category 3, no case")
    ava, priya = ts.keelson_login("ava"), ts.keelson_login("priya")
    changes0 = hub("GET", "/v1/alerts/changes", params={"after": 0, "limit": 500}).get("items", [])
    seq0 = max((int(i["seq"]) for i in changes0), default=0)
    st = pack_action(ava, "start")
    expect(st.get("steps_applied") == 1, f"pack start {st}")
    alerts = wait_until("the hurricane alert", lambda: simulated_alerts(priya), timeout_s=30)
    expect(len(alerts) == 1, f"{len(alerts)} simulated alerts (want 1)")
    aid = alerts[0]["id"]
    check_detail(get_alert(priya, aid), 0)
    st = pack_action(ava, "advance")
    expect(st.get("steps_applied") == 2, f"advance 1 {st}")
    check_detail(get_alert(priya, aid), 1)
    st = pack_action(ava, "advance")
    expect(st.get("status") == "finished", f"advance 2 {st}")
    check_detail(get_alert(priya, aid), 2)
    changes = [c for c in hub("GET", "/v1/alerts/changes", params={"after": seq0, "limit": 500}).get("items", [])
               if c["alert"]["id"] == aid]
    kinds = [(c["change"], c["action"]) for c in changes]
    expect(kinds[:2] == [("raised", "alert"), ("renotified", "alert")], f"change feed {kinds}")
    expect(all(action != "alert_and_draft_case" for _, action in kinds), f"draft-case action in {kinds}")
    say(f"alert {aid}: medium → critical (re-notified), 4 CAPE lanes, no case; changes {kinds}")
    return {"alert_id": aid, "changes": kinds}


# ───────────────────────────────── 5. reset ─────────────────────────────────


def check_final_reset(aid: str) -> dict[str, Any]:
    heading("5. Demo reset clears the pack")
    demo_reset()
    priya, ava = ts.keelson_login("priya"), ts.keelson_login("ava")
    expect(not simulated_alerts(priya), "simulated alerts after the reset")
    r = priya.client.get(f"{KEELSON}/api/alerts/{aid}")
    expect(r.status_code == 404, f"the hurricane alert after the reset: HTTP {r.status_code}")
    r = ava.client.get(f"{KEELSON}/api/demo/packs")
    if r.status_code == 200:
        state = {p["id"]: p.get("status") for p in r.json()["packs"]}
        expect(state.get(PACK) == "idle", f"pack state after reset {state}")
    say("no simulated alert left; pack idle")
    return {"reset": "ok"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--skip-final-reset", action="store_true")
    args = parser.parse_args()
    core_init()
    results: dict[str, Any] = {}
    try:
        heading("1. Demo reset")
        demo_reset()
        results["catalogue"] = check_catalogue()
        results["opt_in"] = check_opt_in()
        results["hurricane"] = check_hurricane()
        if not args.skip_final_reset:
            results["final_reset"] = check_final_reset(results["hurricane"]["alert_id"])
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    heading("Results")
    print(json.dumps(results, indent=2, default=str))
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
