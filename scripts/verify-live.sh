#!/usr/bin/env bash
# Live verification of M4 + M5 + M6 + M7 in one go (real Gemini models behind LiteLLM). Run it once model
# credits are available again; everything here was prepared offline.
#
# Order: the pending M4 live checks (thin slice x2, what-if x2, follow-up x2), the live evals (all
# cases), the LLM Playwright specs, then the M5 live e2e (Act 3 + auto-commit), and a final demo
# reset. M6: Act 1 (typhoon pack → alert → open case → signal run → approach card, twice; the second
# pass replays). M7: the foreign-ERP proof (northwind-test on Merxa /mcp-lite: crawl → advisory options →
# per-op apply of the mapped lines with approval → guided rest → read-back) and the Signal Hub's
# provisioned extraction key (/v1/sources reports llm_key_source db; a pending news item is extracted).
# M8: one live in-app eval run (record_or_replay, trim_delay_actual) within its budget; needs
# EVALS_LIVE_ENABLED=1 on engine-api and the runner up (docker compose --profile evals up -d evals-runner).
# Every step runs even if an earlier one failed; the summary at the end lists them and the
# script exits 1 when any failed.
#
# Cost: the engine records every model call (LLM_REPLAY_MODE=record_or_replay, the compose default),
# so each script's second pass replays its first one for free; rerunning this whole script after a
# failure only pays for requests that changed. Never run it against a stack you are presenting from
# (it resets the demo data many times).
#
# Usage: scripts/verify-live.sh [--only STEP[,STEP...]] [--list]
#   steps: m4-thin-slice m4-whatif m4-followup evals playwright m5-act3 m5-autocommit m6-act1
#          m7-foreign-erp m7-hub-key m8-evals reset
#   env DEMO_USER_PASSWORD (default: read from .env), LLM_REPLAY_MODE must not be "off"/"replay"
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"
export MSYS_NO_PATHCONV=1
ALL_STEPS=(m4-thin-slice m4-whatif m4-followup evals playwright m5-act3 m5-autocommit m6-act1 m7-foreign-erp m7-hub-key m8-evals reset)
ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --only) ONLY="$2"; shift ;;
    --list) printf '%s\n' "${ALL_STEPS[@]}"; exit 0 ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

if [ -z "${DEMO_USER_PASSWORD:-}" ]; then
  DEMO_USER_PASSWORD="$(grep -E '^DEMO_USER_PASSWORD=' .env | cut -d= -f2- || true)"
fi
[ -n "${DEMO_USER_PASSWORD:-}" ] || { echo "DEMO_USER_PASSWORD is not set and not in .env" >&2; exit 2; }
export DEMO_USER_PASSWORD

compose() { env -u LITELLM_MASTER_KEY docker compose "$@"; }

mode="$(docker exec keelson-engine-worker-1 printenv LLM_REPLAY_MODE 2>/dev/null || true)"
case "$mode" in
  record_or_replay|record) ;;
  *) echo "engine-worker LLM_REPLAY_MODE='${mode:-unset}': use record_or_replay so reruns are free" >&2; exit 2 ;;
esac
running="$(docker ps --format '{{.Names}}' | grep -c '^keelson-' || true)"
[ "$running" -ge 13 ] || { echo "the stack is not up (${running} keelson-* containers)" >&2; exit 2; }

# The thin-slice style scripts run in a small python container on the edge network.
py_e2e() {
  docker run --rm --memory 256m --network keelson_edge -e DEMO_USER_PASSWORD -e PYTHONPATH=/contracts/src \
    -v "$root/e2e:/e2e:ro" -v "$root/packages/contracts-py:/contracts:ro" \
    -v keelson_pipcache:/root/.cache/pip python:3.14-slim-trixie sh -c \
    "pip install -q --root-user-action=ignore httpx==0.28.1 pydantic==2.13.5 && python -u /e2e/$1"
}
# The M5 scripts run inside the engine image (engine env, core access for seeding / run checks).
engine_e2e() {
  compose run --rm --no-deps -T -v "$root/e2e:/e2e:ro" engine-api python -u "/e2e/$1"
}

declare -a RESULTS=()
step() {
  local name="$1"; shift
  if [ -n "$ONLY" ] && [[ ",$ONLY," != *",$name,"* ]]; then return 0; fi
  echo; echo "================ $name ================"
  local start=$SECONDS
  if "$@"; then RESULTS+=("PASS  $name  $((SECONDS - start)) s"); else RESULTS+=("FAIL  $name  $((SECONDS - start)) s"); fi
}

twice() { "$@" && { echo "--- second pass (replayed) ---"; "$@"; }; }

step m4-thin-slice twice py_e2e api_thin_slice.py
step m4-whatif     twice py_e2e api_m4_whatif.py
step m4-followup   twice py_e2e api_m4_followup.py
step evals docker run --rm --memory 256m --network keelson_edge -e PYTHONDONTWRITEBYTECODE=1 \
  -v "$root:/work:ro" -v "$root/evals/results:/work/evals/results" \
  -v keelson_pipcache:/root/.cache/pip -w /work/evals python:3.14-slim-trixie sh -c \
  "pip install -q --root-user-action=ignore -r requirements.txt && python -u run_live.py --preflight && python -u run_live.py"
# LLM Playwright specs (the non-LLM ones run offline: m5-offline, dev-blocks, layout, auth, ...)
step playwright bash e2e/playwright/run.sh act3-thin-slice act2-whatif keyboard-approve a11y layout
step m5-act3       engine_e2e api_m5_act3.py
step m5-autocommit engine_e2e api_m5_autocommit.py
# M6 Act 1: the signal run's planner is the only live call (the hub needs no model: pre-baked extractions)
step m6-act1      twice engine_e2e api_m6_act1.py
# M7: the generic tier-A proof on a foreign ERP (planner, specialists, strategist are live; the crawl,
# per-op commit and read-back need no model). Twice: the second pass replays the recorded calls.
step m7-foreign-erp twice engine_e2e api_m7_foreign_erp.py
# M7 Signal Hub: the bootstrap-provisioned LiteLLM key (alias keelson-signal-hub) extracts pending news
step m7-hub-key engine_e2e m7_hub_key.py
# M8 Evals UI: one live in-app eval run within its budget (docs/contracts/128 §7)
step m8-evals engine_e2e api_m8_evals_live.py
step reset bash scripts/demo-reset.sh

echo; echo "================ summary ================"
printf '%s\n' "${RESULTS[@]}"
echo "LLM replay: docker compose logs engine-worker | grep llm.replay  (hit = free, recorded = live)"
for r in "${RESULTS[@]}"; do [[ "$r" == FAIL* ]] && exit 1; done
exit 0
