#!/usr/bin/env bash
# Full demo reset: POST /api/demo/reset (docs/contracts/95-m5-remediation.md section 6).
#
# Signs in to Keelson through the persona switcher as a persona that may reset (platform-admin or
# demo-presenter; default "ava"), then POSTs /api/demo/reset with the session cookie, X-CSRF-Token
# and Origin, and prints the step log. Exit 1 when the request is refused or any step failed.
#
# Usage: scripts/demo-reset.sh [--keep-consents] [--base-url URL] [--persona NAME]
#   --keep-consents   rehearsal mode: keep Merxa consents + Token Broker links
#   env DEMO_ACCESS_SECRET  sent as X-Demo-Access (hosted demos)
set -euo pipefail

BASE_URL="http://keelson.localhost"
PERSONA="ava"
KEEP="false"
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-consents) KEEP="true" ;;
    --base-url) BASE_URL="${2%/}"; shift ;;
    --persona) PERSONA="$2"; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

JAR="$(mktemp)"
OUT="$(mktemp)"
trap 'rm -f "$JAR" "$OUT"' EXIT
ACCESS=()
if [ -n "${DEMO_ACCESS_SECRET:-}" ]; then ACCESS=(-H "X-Demo-Access: ${DEMO_ACCESS_SECRET}"); fi

status="$(curl -sS -o "$OUT" -w '%{http_code}' -c "$JAR" -b "$JAR" -X POST "${BASE_URL}/api/demo/login-as" \
  -H "Origin: ${BASE_URL}" -H "Content-Type: application/json" ${ACCESS[@]+"${ACCESS[@]}"} \
  --data "{\"username\": \"${PERSONA}\"}")"
if [ "$status" != "200" ]; then
  echo "sign-in as '${PERSONA}' failed (HTTP ${status}): $(cat "$OUT")" >&2
  exit 1
fi
CSRF="$(sed -n 's/.*"csrf_token"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' "$OUT")"
if [ -z "$CSRF" ]; then
  echo "no csrf_token in the sign-in response" >&2
  exit 1
fi

status="$(curl -sS -o "$OUT" -w '%{http_code}' -c "$JAR" -b "$JAR" -X POST "${BASE_URL}/api/demo/reset" \
  -H "Origin: ${BASE_URL}" -H "X-CSRF-Token: ${CSRF}" -H "Content-Type: application/json" \
  --max-time 120 --data "{\"keep_consents\": ${KEEP}}")"
if [ "$status" != "200" ]; then
  echo "demo reset refused (HTTP ${status}): $(cat "$OUT")" >&2
  exit 1
fi
if command -v python3 >/dev/null 2>&1; then
  python3 - "$OUT" <<'PY'
import json, sys
log = json.load(open(sys.argv[1], encoding="utf-8"))
print(f"Demo reset {'OK' if log['ok'] else 'FAILED'} in {log['duration_ms']} ms (keep_consents={log['keep_consents']})")
for s in log["steps"]:
    counts = " ".join(f"{k}={v}" for k, v in s.get("counts", {}).items())
    print(f"  {s['step']:<22} {s['status']:<8} {s['duration_ms']:>6} ms  {s.get('detail') or ''} {counts}")
PY
else
  cat "$OUT"; echo
fi
grep -q '"ok":[[:space:]]*true' "$OUT"
