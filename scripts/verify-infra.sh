#!/usr/bin/env bash
# Smoke-verify the running infra services from inside the compose networks.
# Usage: scripts/verify-infra.sh [--skip-llm]
#   --skip-llm  skip the LiteLLM temporary-model call (no Gemini request is made)
# Requires: infra services up (scripts/up.sh, scripts/up.ps1 or docker compose up -d <infra services>).
# Each JSON result line of verify_infra.py is shown as "PASS name (ms)" / "FAIL name -> error"
# (needs python3 / python on the host, else the raw lines are shown) with a
# "[HH:MM:SS +elapsed] [step/3]" prefix; the raw JSON is kept in
# logs/verify-infra-<yyyymmdd-HHMMSS>.log. Exit code = number of failed checks.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_FILE="$ROOT/logs/verify-infra-$(date +%Y%m%d-%H%M%S).log"
SHOW_LOG="$LOG_FILE"
if command -v cygpath >/dev/null 2>&1; then
  ROOT="$(cygpath -m "$ROOT")"
  SHOW_LOG="$(cygpath -w "$LOG_FILE")"
  export MSYS_NO_PATHCONV=1
fi
IMAGE="python:3.14.8-slim-trixie@sha256:a2b82f3c48559aa0a8446d9af49826b6e2b2016f4cd2afabfe6013ec53729170"
NAME="keelson-verify-$$"
rc=0
SKIP_LLM=0
for arg in "$@"; do if [ "$arg" = "--skip-llm" ]; then SKIP_LLM=1; fi; done

# -- logging: "[HH:MM:SS +elapsed] [n/3] message" to the console and the log file --
T0=$SECONDS
STEP=0
STEP_TOTAL=3
PASSED=0
FAILED=0
mkdir -p "$(dirname "$LOG_FILE")"
exec 3>>"$LOG_FILE"
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_STEP=$'\033[36m' C_OK=$'\033[32m' C_ERR=$'\033[31m' C_DIM=$'\033[90m' C_RESET=$'\033[0m'
else
  C_STEP="" C_OK="" C_ERR="" C_DIM="" C_RESET=""
fi
fmt_secs() { # seconds -> REPLY "42s" / "3m07s"
  if [ "$1" -lt 60 ]; then REPLY="${1}s"; else printf -v REPLY '%dm%02ds' $(($1 / 60)) $(($1 % 60)); fi
}
log_c() { # COLOUR MESSAGE...
  local color=$1 tag="" now
  shift
  now="$(date +%H:%M:%S)"
  fmt_secs $((SECONDS - T0))
  if [ "$STEP" -gt 0 ]; then tag=" [$STEP/$STEP_TOTAL]"; fi
  printf '%s[%s +%s]%s %s%s\n' "$color" "$now" "$REPLY" "$tag" "$*" "${color:+$C_RESET}"
  printf '[%s +%s]%s %s\n' "$now" "$REPLY" "$tag" "$*" >&3
}
log() { log_c "" "$*"; }
step() { STEP=$((STEP + 1)); log_c "$C_STEP" "==> $*"; }

cleanup() { docker rm -f "$NAME" >/dev/null 2>&1 || true; }
on_exit() {
  cleanup
  log_c "$C_STEP" "Log file (incl. raw JSON): $SHOW_LOG"
  exec 3>&-
}
trap on_exit EXIT

PY=""
for cand in python3 python; do # the Windows "python3" store alias exists without Python: test it
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import json, sys' >/dev/null 2>&1; then PY=$cand; break; fi
done
# verify_infra.py output -> tagged lines: RAW <json> / PASS|FAIL <text> / SUM <text> / RC <n> / TXT <line>
PY_FMT="$(
  cat <<'PY'
import json, sys, time
sys.stdout.reconfigure(encoding="utf-8", newline="\n", line_buffering=True)
last = time.monotonic()
for raw in iter(sys.stdin.buffer.readline, b""):
    line = raw.decode("utf-8", "replace").rstrip("\r\n")
    now = time.monotonic()
    delta, last = now - last, now
    text = line.strip()
    obj = None
    if text.startswith("{"):
        try:
            obj = json.loads(text)
        except ValueError:
            obj = None
    if isinstance(obj, dict) and "check" in obj:
        name = str(obj["check"]) + ("@" + str(obj["network"]) if "network" in obj else "")
        print("RAW\t" + text)
        if obj.get("ok") is True:
            ms = obj.get("ms", obj.get("chat_ms"))
            took = f"({ms} ms)" if isinstance(ms, (int, float)) else f"(~{delta:.1f} s)"
            print(f"PASS\t{name}  {took}")
        else:
            rest = {k: v for k, v in obj.items() if k not in ("check", "ok")}
            detail = str(rest["error"]) if rest.get("error") else json.dumps(rest)[1:-1]
            detail = " ".join(detail.split())
            if len(detail) > 200:
                detail = detail[:197] + "..."
            print(f"FAIL\t{name}  -> {detail}")
    elif isinstance(obj, dict) and isinstance(obj.get("summary"), dict):
        s = obj["summary"]
        print("RAW\t" + text)
        print(f"SUM\tverify_infra.py: {s.get('checks')} checks, {s.get('failed')} failed")
    elif text.startswith("__verify_rc__="):
        print("RC\t" + text.split("=", 1)[1])
    else:
        print("TXT\t" + line)
PY
)"
fmt_checks() {
  if [ -n "$PY" ]; then
    "$PY" -u -c "$PY_FMT"
  else
    local l
    while IFS= read -r l || [ -n "$l" ]; do printf 'TXT\t%s\n' "${l%$'\r'}"; done
  fi
}
# run_checks CMD...: run a verify_infra.py container, log readable results, set CHECK_RC.
run_checks() {
  local kind text
  CHECK_RC=1 # unless the exit-code marker arrives
  while IFS=$'\t' read -r kind text; do
    case "$kind" in
      RAW) printf '        json: %s\n' "$text" >&3 ;;
      PASS) PASSED=$((PASSED + 1)); log_c "$C_OK" "  PASS  $text" ;;
      FAIL) FAILED=$((FAILED + 1)); log_c "$C_ERR" "  FAIL  $text" ;;
      SUM) log_c "$C_DIM" "  ($text)" ;;
      RC) CHECK_RC=$text ;;
      *)
        case "$text" in
          __verify_rc__=*) CHECK_RC=${text#__verify_rc__=} ;;
          *'"ok": true'*) PASSED=$((PASSED + 1)); log "  $text" ;;   # raw (no python)
          *'"ok": false'*) FAILED=$((FAILED + 1)); log_c "$C_ERR" "  $text" ;;
          *[![:space:]]*) log "  $text" ;;
        esac
        ;;
    esac
  done < <({
    if "$@" 2>&1; then code=0; else code=$?; fi
    printf '__verify_rc__=%s\n' "$code"
  } | fmt_checks)
}

log_c "$C_STEP" "Infra smoke checks - log file: $SHOW_LOG"
if [ "$SKIP_LLM" = 1 ]; then log "LLM smoke check: skipped (--skip-llm)"; else log "LLM smoke check: included (one tiny Gemini call through LiteLLM)"; fi
if [ -z "$PY" ]; then log "No python3/python on the host: showing the raw JSON lines."; fi
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  log "Image $IMAGE is present locally."
else
  log "Image $IMAGE not present locally - pulling it ..."
  docker pull "$IMAGE" 2>&1 | while IFS= read -r line || [ -n "$line" ]; do log "    ${line%$'\r'}"; done || true
fi

# Main checks need app (auth.localhost, keycloak, egress) + ai (litellm).
step "Running main checks on keelson_app + keelson_ai ..."
log "Creating container $NAME (--memory 128m, network keelson_app, --env-file .env)"
out="$(docker create --name "$NAME" --memory 128m --network keelson_app --env-file "$ROOT/.env" \
  -v "$ROOT/scripts:/scripts:ro" "$IMAGE" python /scripts/verify_infra.py all "$@" 2>&1)" || {
  code=$?
  printf '%s\n' "$out" | while IFS= read -r line; do log "    ${line%$'\r'}"; done
  log_c "$C_ERR" "ERROR: docker create failed (are the infra services up?)"
  exit "$code"
}
log "Attaching it to keelson_ai (LiteLLM) and starting verify_infra.py all ..."
out="$(docker network connect keelson_ai "$NAME" 2>&1)" || {
  code=$?
  log_c "$C_ERR" "ERROR: docker network connect keelson_ai failed: ${out%$'\r'}"
  exit "$code"
}
run_checks docker start -a "$NAME"
rc=$CHECK_RC
cleanup

# Name resolution per network (S7): public hostnames must resolve to Caddy.
for net in app edge; do
  step "DNS resolution on keelson_$net (public hostnames must resolve to Caddy) ..."
  run_checks docker run --rm --memory 64m --network "keelson_$net" -e VERIFY_NETWORK="$net" \
    -v "$ROOT/scripts:/scripts:ro" "$IMAGE" python /scripts/verify_infra.py dns
  if [ "$CHECK_RC" != 0 ]; then rc=$((rc + 1)); fi
done

fmt_secs $((SECONDS - T0))
if [ "$rc" -eq 0 ]; then
  log_c "$C_OK" "All infra checks passed."
  log_c "$C_OK" "$PASSED passed, $FAILED failed, total $REPLY"
else
  log_c "$C_ERR" "$rc infra check(s) failed."
  log_c "$C_ERR" "$PASSED passed, $FAILED failed, total $REPLY"
fi
exit "$rc"
