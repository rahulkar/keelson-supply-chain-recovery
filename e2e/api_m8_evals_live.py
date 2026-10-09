"""M8 Evals UI live check (docs/contracts/128 §7): one live in-app eval run within its budget.

Needs model credits, the stack in ``LLM_REPLAY_MODE=record_or_replay``, ``EVALS_LIVE_ENABLED=1`` on
engine-api and the in-app runner up (``docker compose --profile evals up -d evals-runner``). Runs
inside the engine image (``scripts/verify-live.sh --only m8-evals``):

1. ``/status``: the runner is online and ``record_or_replay`` is available.
2. ava starts a ``record_or_replay`` run of ``trim_delay_actual`` with a small budget (typed
   ``RUN LIVE``) and follows its events until it ends.
3. The run made live calls (``llm_calls_live > 0``, ``tokens_live > 0``), stayed within its token
   budget (plus at most the one response that crossed it: the cap is checked after each response)
   and its cost budget, and the case was evaluated (not a replay miss).

Exit 0 pass, 1 expectation failed, 2 environment missing.
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
    from api_m8_evals_offline import FINAL, call, ok, wait_for  # noqa: E402
    from api_thin_slice import E2EFailure, expect, heading, say  # noqa: E402
except ImportError as exc:
    print(f"ENV: {exc}", file=sys.stderr)
    raise SystemExit(2) from exc

CASE = "trim_delay_actual"
MAX_TOKENS = 1_500_000
MAX_COST_MICROUSD = 2_000_000
OVERSHOOT_TOKENS = 200_000
"""One model response may cross the token cap (the cap is checked after each response)."""


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    parser.add_argument("--run-timeout", type=float, default=1800.0)
    args = parser.parse_args()
    try:
        ava = ts.keelson_login("ava")
        heading("1. Live runs are available")

        def ready() -> dict[str, Any] | None:
            body = ok(call(ava, "GET", "/status"), "status")
            return body if body["runner"]["online"] and body["suite"] else None

        body = wait_for("the eval runner's heartbeat", ready, timeout_s=90)
        live = next(m for m in body["modes"] if m["mode"] == "record_or_replay")
        expect(live["available"], f"record_or_replay unavailable: {live['reason']} ({live['message']})")

        heading(f"2. Live run of {CASE}")
        run = ok(call(ava, "POST", "/runs", body={
            "mode": "record_or_replay", "case_ids": [CASE], "acknowledge_reset": True, "confirm_live": "RUN LIVE",
            "budget": {"max_tokens": MAX_TOKENS, "max_cost_microusd": MAX_COST_MICROUSD},
            "note": "verify-live m8-evals"}), "queue a live run", 202)
        rid = run["id"]
        deadline = time.monotonic() + args.run_timeout
        after = 0
        status = run["status"]
        while status not in FINAL:
            expect(time.monotonic() < deadline, f"run still {status} after {args.run_timeout:.0f} s")
            page = ok(call(ava, "GET", f"/runs/{rid}/events", params={"after": after}), "events")
            for e in page["items"]:
                say(f"  #{e['seq']:<3} {e['type']:<14} {e.get('case_id') or ''} {e.get('turn_id') or ''}")
            after, status = page["next_after"], page["status"]
            if status not in FINAL:
                time.sleep(5.0)
        expect(status == "succeeded", f"run ended {status}")

        heading("3. Live usage within the budget")
        detail = ok(call(ava, "GET", f"/runs/{rid}"), "run detail")
        counters = detail["run"]["counters"]
        [row] = detail["cases"]
        expect(counters["llm_calls_live"] > 0 and counters["tokens_live"] > 0, f"no live usage: {counters}")
        expect(counters["tokens_live"] <= MAX_TOKENS + OVERSHOOT_TOKENS, f"token budget exceeded: {counters}")
        expect(counters["cost_microusd"] <= MAX_COST_MICROUSD + 500_000, f"cost budget exceeded: {counters}")
        if counters["cost_microusd"] == 0:
            say("note: the proxy reported no cost for these calls")
        expect(row["error_code"] != "replay_miss", f"case {row['status']} {row['error_code']}")
        say(f"case {row['status']}; counters {json.dumps(counters)}")
    except E2EFailure as exc:
        print(f"FAIL: {exc}", flush=True)
        return 1
    print("PASS", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
