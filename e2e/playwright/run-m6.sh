#!/usr/bin/env bash
# M6 browser checks without a model (tests/m6-alerts.spec.ts): the alerts inbox opens a case whose
# `signal` run must never reach the planner, so the engine worker is HELD around the spec.
#
#   e2e/playwright/run-m6.sh            # demo reset → hold the worker → spec → release → demo reset
#   e2e/playwright/run-m6.sh --no-reset # keep the current demo data (no reset before / after)
#
# The hold (e2e/m6_hold.py) runs in a throwaway engine-api container (`keelson_m6_hold`): it locks
# the ADK sessions table and parks a deterministic gate run on the worker's only slot, so the case's
# queued run stays queued; the spec cancels it, and stopping the hold (SIGTERM) cancels anything
# still queued since the hold began before the lock is dropped. A full demo reset deletes cases, so
# it never runs while the hold is active.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
root="$(cd "$here/../.." && pwd)"
mount_root="$(cd "$root" && (pwd -W 2>/dev/null || pwd))"
reset=1
[[ "${1:-}" == "--no-reset" ]] && { reset=0; shift; }
compose() { (cd "$root" && env -u LITELLM_MASTER_KEY docker compose "$@"); }

if [[ $reset -eq 1 ]]; then
  since="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  bash "$root/scripts/demo-reset.sh"
  # the post-reset scn sync re-pushes the tenant network to the hub (exposure needs it)
  echo "waiting for the post-reset scn sync to push the tenant network"
  for _ in $(seq 1 150); do
    docker logs --since "$since" keelson-engine-worker-1 2>&1 | grep -q "tenant network pushed" && break
    sleep 2
  done
  docker logs --since "$since" keelson-engine-worker-1 2>&1 | grep -q "tenant network pushed"     || { echo "no network push after the reset" >&2; exit 1; }
fi

docker rm -f keelson_m6_hold >/dev/null 2>&1 || true
MSYS_NO_PATHCONV=1 compose run -d --name keelson_m6_hold --no-deps -v "$mount_root/e2e:/e2e:ro" engine-api \
  python -u /e2e/m6_hold.py --max-s 900 >/dev/null
release() {
  docker stop -t 30 keelson_m6_hold >/dev/null 2>&1 || true
  docker logs keelson_m6_hold 2>&1 | grep -E "^(HOLDING|RELEASED|deadline)" || true
  docker rm -f keelson_m6_hold >/dev/null 2>&1 || true
}
trap release EXIT
for _ in $(seq 1 120); do
  if docker logs keelson_m6_hold 2>&1 | grep -q '^HOLDING'; then break; fi
  if ! docker ps --format '{{.Names}}' | grep -q '^keelson_m6_hold$'; then
    docker logs keelson_m6_hold 2>&1 | tail -20; echo "the worker hold failed to start" >&2; exit 1
  fi
  sleep 1
done
docker logs keelson_m6_hold 2>&1 | grep '^HOLDING' || { echo "the worker hold did not engage" >&2; exit 1; }

status=0
E2E_M6_WORKER_HELD=1 bash "$here/run.sh" m6-alerts "$@" || status=$?
release
trap - EXIT
if [[ $reset -eq 1 ]]; then bash "$root/scripts/demo-reset.sh" >/dev/null && echo "final demo reset ok"; fi
exit $status
