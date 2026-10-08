#!/usr/bin/env bash
# Start the Keelson + Merxa stack (docker compose) with verbose, timestamped progress.
# Bash twin of scripts/up.ps1 for Git Bash on Windows, Linux and macOS.
#
#   1. prerequisites: Docker engine / compose versions and memory, GEMINI_API_KEY (only
#      "yes" / "no" is printed), the owner of host port 80, host variables that shadow .env
#   2. runs scripts/init.sh when .env or secrets/ are missing/incomplete (idempotent)
#   3. host variables that shadow a .env key are dropped for compose (env -u NAME ...);
#      compose gives the shell environment precedence over .env. GEMINI_API_KEY is
#      intentionally taken from the host.
#   4. docker compose up -d --build [services] with plain progress (build steps stream)
#   5. waits until every service is healthy / running / completed: a progress line per poll
#      (5 s), a line per state change, the last log lines of still-starting services every
#      ~30 s, the last 40 log lines of a failed service
#   6. status table, total time, public URLs
# Every line is prefixed with "[HH:MM:SS +elapsed] [step/6]" and also written to
# logs/up-<yyyymmdd-HHMMSS>.log under the repository root.
#
# Usage: scripts/up.sh [--infra] [--services a,b] [--obs] [--no-build] [--timeout N]
#   --infra          infrastructure services only: pg-core pg-litellm pg-merxa valkey
#                    keycloak kc-config litellm egress caddy
#   --services a,b   only these services (and their dependencies); comma or space separated
#   --obs            also enable the "obs" profile (Phoenix at http://traces.localhost)
#   --no-build       skip --build
#   --timeout N      seconds to wait for services to become healthy (default 420)
#   -h, --help       show this help
# Exit: 0 stack up, 1 a service failed (or a prerequisite / compose error), 2 still
# starting after --timeout, 64 invalid arguments.
set -euo pipefail
shopt -s extglob

usage() { awk 'NR > 1 && /^#/ { sub(/^# ?/, ""); print; next } NR > 1 { exit }' "$0"; }
bad_usage() { printf 'up.sh: %s (see scripts/up.sh --help)\n' "$*" >&2; exit 64; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
ENV_FILE="$ROOT/.env"
ONE_SHOT=" kc-config bootstrap signal-hub-bootstrap "   # must reach 'exited 0'
INFRA_SERVICES=(pg-core pg-litellm pg-merxa valkey keycloak kc-config litellm egress caddy)
POLL_SEC=5          # wait-loop poll interval
TAIL_EVERY_SEC=30   # show the last log lines of still-starting services this often

SERVICES=()
INFRA=0
OBS=0
NO_BUILD=0
TIMEOUT=420
add_services() {
  local parts=() part
  IFS=', ' read -r -a parts <<<"$1"
  for part in ${parts[@]+"${parts[@]}"}; do
    if [ -n "$part" ]; then SERVICES+=("$part"); fi
  done
}
while [ $# -gt 0 ]; do
  case "$1" in
    --infra) INFRA=1 ;;
    --obs) OBS=1 ;;
    --no-build) NO_BUILD=1 ;;
    --services) [ $# -ge 2 ] || bad_usage "--services needs a value"; add_services "$2"; shift ;;
    --services=*) add_services "${1#*=}" ;;
    --timeout) [ $# -ge 2 ] || bad_usage "--timeout needs a value"; TIMEOUT="$2"; shift ;;
    --timeout=*) TIMEOUT="${1#*=}" ;;
    -h | --help) usage; exit 0 ;;
    *) bad_usage "unknown argument: $1" ;;
  esac
  shift
done
case "$TIMEOUT" in '' | *[!0-9]*) bad_usage "--timeout needs a whole number of seconds" ;; esac

# Git Bash on Windows: keep MSYS from rewriting arguments such as tasklist's /FI.
IS_WINDOWS=0
if command -v cygpath >/dev/null 2>&1; then
  IS_WINDOWS=1
  export MSYS_NO_PATHCONV=1
fi
display_path() { if [ "$IS_WINDOWS" = 1 ]; then cygpath -w "$1"; else printf '%s\n' "$1"; fi; }

# -- logging: "[HH:MM:SS +elapsed] [n/6] message" to the console and logs/up-<ts>.log --
T0=$SECONDS
STEP=0
STEP_TOTAL=6
mkdir -p "$ROOT/logs"
LOG_FILE="$ROOT/logs/up-$(date +%Y%m%d-%H%M%S).log"
exec 3>>"$LOG_FILE"
ESC=$'\033'
if [ -t 1 ] && [ -z "${NO_COLOR:-}" ]; then
  C_STEP="${ESC}[36m" C_OK="${ESC}[32m" C_WARN="${ESC}[33m" C_ERR="${ESC}[31m" C_DIM="${ESC}[90m" C_RESET="${ESC}[0m"
else
  C_STEP="" C_OK="" C_WARN="" C_ERR="" C_DIM="" C_RESET=""
fi
if printf -v NOW '%(%H:%M:%S)T' -1 2>/dev/null; then # bash >= 4.2: no fork per line
  set_now() { printf -v NOW '%(%H:%M:%S)T' -1; }
else
  set_now() { NOW="$(date +%H:%M:%S)"; }
fi
fmt_secs() { # seconds -> REPLY "42s" / "3m07s"
  if [ "$1" -lt 60 ]; then REPLY="${1}s"; else printf -v REPLY '%dm%02ds' $(($1 / 60)) $(($1 % 60)); fi
}
log_c() { # COLOUR MESSAGE...
  local color=$1 tag=""
  shift
  set_now
  fmt_secs $((SECONDS - T0))
  if [ "$STEP" -gt 0 ]; then tag=" [$STEP/$STEP_TOTAL]"; fi
  printf '%s[%s +%s]%s %s%s\n' "$color" "$NOW" "$REPLY" "$tag" "$*" "${color:+$C_RESET}"
  printf '[%s +%s]%s %s\n' "$NOW" "$REPLY" "$tag" "$*" >&3
}
log() { log_c "" "$*"; }
ok() { log_c "$C_OK" "$*"; }
step() { STEP=$((STEP + 1)); log_c "$C_STEP" "==> $*"; }
warn() { log_c "$C_WARN" "WARN: $*"; }
die() { log_c "$C_ERR" "ERROR: $*"; exit 1; }
clean_line() { # $1 -> REPLY without CR and ANSI colour codes
  REPLY=${1%$'\r'}
  case "$REPLY" in *"$ESC"*) REPLY=${REPLY//${ESC}\[*([0-9;?])[A-Za-z]/} ;; esac
}
stream() { # INDENT [COLOUR]: log every stdin line
  local indent=$1 color=${2:-} line
  while IFS= read -r line || [ -n "$line" ]; do
    clean_line "$line"
    log_c "$color" "$indent$REPLY"
  done
}
run_logged() { # INDENT COLOUR CMD...: log CMD's stdout + stderr line by line, return its exit code
  local indent=$1 color=$2 rc
  shift 2
  set +e
  "$@" 2>&1 | stream "$indent" "$color"
  rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}
on_exit() {
  local code=$?
  fmt_secs $((SECONDS - T0))
  log_c "$C_STEP" "Finished after $REPLY (exit $code) - log file: $(display_path "$LOG_FILE")"
  exec 3>&-
}
trap on_exit EXIT

# -- docker compose helpers --
ENV_UNSET=()      # "-u NAME" pairs: host variables that shadow a .env key
COMPOSE_OPTS=()   # --profile obs
compose() { env ${ENV_UNSET[@]+"${ENV_UNSET[@]}"} docker compose ${COMPOSE_OPTS[@]+"${COMPOSE_OPTS[@]}"} "$@"; }

PY=""
for cand in python3 python; do # the Windows "python3" store alias exists without Python: test it
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import json, sys' >/dev/null 2>&1; then PY=$cand; break; fi
done
# docker compose ps -a --format json (one object per line, or one array on old releases)
# -> "service|state|health|exitcode|name|ports" sorted by service.
PY_PS='
import json, sys
sys.stdout.reconfigure(encoding="utf-8", newline="\n")
raw = sys.stdin.buffer.read().decode("utf-8", "replace").strip()
items = []
for chunk in ([raw] if raw.startswith("[") else raw.splitlines()):
    if chunk.strip():
        obj = json.loads(chunk)
        items.extend(obj if isinstance(obj, list) else [obj])
for c in sorted(items, key=lambda c: (c.get("Service") or "", c.get("Name") or "")):
    vals = [c.get(k) for k in ("Service", "State", "Health", "ExitCode", "Name", "Ports")]
    print("|".join("" if v is None else str(v) for v in vals))
'
ps_lines() {
  if [ -n "$PY" ]; then
    compose ps -a --format json 2>/dev/null | "$PY" -c "$PY_PS"
  else # '|' rather than a tab: read would merge consecutive tabs (empty Health)
    compose ps -a --format '{{.Service}}|{{.State}}|{{.Health}}|{{.ExitCode}}|{{.Name}}|{{.Ports}}' 2>/dev/null | sort
  fi
}
read_state() { # -> S_SVC S_STATE S_HEALTH S_EXIT S_NAME S_PORTS (index-aligned), S_COUNT
  S_SVC=() S_STATE=() S_HEALTH=() S_EXIT=() S_NAME=() S_PORTS=()
  S_COUNT=0
  local svc state health code name ports
  while IFS='|' read -r svc state health code name ports; do
    if [ -z "$svc" ]; then continue; fi
    S_SVC+=("$svc") S_STATE+=("$state") S_HEALTH+=("$health") S_EXIT+=("$code")
    S_NAME+=("${name:-$svc}") S_PORTS+=("${ports%$'\r'}")
    S_COUNT=$((S_COUNT + 1))
  done < <(ps_lines)
}
is_one_shot() { case "$ONE_SHOT" in *" $1 "*) return 0 ;; esac; return 1; }
is_failed() { # INDEX
  if [ "${S_STATE[$1]}" = exited ] && [ "${S_EXIT[$1]}" != 0 ]; then return 0; fi
  [ "${S_HEALTH[$1]}" = unhealthy ]
}
is_pending() { # INDEX
  case "${S_STATE[$1]}" in
    created | restarting) return 0 ;;
    running)
      if [ -n "${S_HEALTH[$1]}" ] && [ "${S_HEALTH[$1]}" != healthy ]; then return 0; fi
      if is_one_shot "${S_SVC[$1]}"; then return 0; fi
      ;;
  esac
  return 1
}
svc_status() { # INDEX -> REPLY: fine-grained status for the state-change lines
  case "${S_STATE[$1]}" in
    exited) if [ "${S_EXIT[$1]}" = 0 ]; then REPLY=completed; else REPLY="exited (${S_EXIT[$1]})"; fi ;;
    running)
      if [ -n "${S_HEALTH[$1]}" ]; then REPLY=${S_HEALTH[$1]}
      elif is_one_shot "${S_SVC[$1]}"; then REPLY="running (job)"
      else REPLY=running; fi
      ;;
    *) REPLY=${S_STATE[$1]} ;;
  esac
}
svc_category() { # INDEX -> REPLY: bucket for the progress-line counters
  if is_failed "$1"; then REPLY=failed
  elif is_pending "$1"; then REPLY=starting
  elif [ "${S_STATE[$1]}" = exited ]; then REPLY=completed
  elif [ "${S_STATE[$1]}" = running ] && [ "${S_HEALTH[$1]}" = healthy ]; then REPLY=healthy
  elif [ "${S_STATE[$1]}" = running ]; then REPLY=running
  else REPLY=other; fi
}
show_logs() { # SERVICE TAIL MAXLEN INDENT: last log lines of a service
  local svc=$1 tail=$2 max=$3 indent=$4 line n=0
  while IFS= read -r line || [ -n "$line" ]; do
    clean_line "$line"
    line=$REPLY
    case "$line" in *[![:space:]]*) ;; *) continue ;; esac
    if [ "$max" -gt 0 ] && [ "${#line}" -gt "$max" ]; then line="${line:0:$((max - 3))}..."; fi
    log_c "$C_DIM" "$indent$line"
    n=$((n + 1))
  done < <(compose logs --tail "$tail" --no-log-prefix --no-color "$svc" 2>&1)
  if [ "$n" -eq 0 ]; then log_c "$C_DIM" "${indent}(no log output yet)"; fi
}

log_c "$C_STEP" "Keelson stack start - log file: $(display_path "$LOG_FILE")"
log "Options: services=[${SERVICES[*]+${SERVICES[*]}}] infra=$INFRA obs=$OBS no-build=$NO_BUILD timeout=${TIMEOUT}s"

# -- 1. prerequisites --
step "Checking prerequisites (Docker, GEMINI_API_KEY, port 80, host environment)"
DOCKER_BIN="$(command -v docker || true)"
if [ -z "$DOCKER_BIN" ]; then die "docker CLI not found - install/start Docker Desktop."; fi
log "docker CLI      : $DOCKER_BIN"
INFO="$(docker info --format '{{.ServerVersion}}|{{.MemTotal}}|{{.NCPU}}|{{.OperatingSystem}}' 2>/dev/null)" ||
  die "Docker engine is not reachable - start Docker Desktop."
IFS='|' read -r D_SERVER D_MEM D_CPUS D_OS <<<"${INFO%$'\r'}"
case "$D_MEM" in '' | *[!0-9]*) D_MEM_H="?" ;; *) D_MEM_H="$(awk -v b="$D_MEM" 'BEGIN { printf "%.1f GiB", b / 1073741824 }')" ;; esac
D_CLIENT="$(docker version --format '{{.Client.Version}}' 2>/dev/null || echo '?')"
log "Docker engine   : server $D_SERVER, client $D_CLIENT ($D_OS)"
log "Docker resources: $D_CPUS CPUs, $D_MEM_H memory"
COMPOSE_VERSION="$(docker compose version --short 2>/dev/null)" ||
  die "docker compose (v2 plugin) not available - update Docker Desktop."
log "Docker compose  : $COMPOSE_VERSION"

GEMINI_SOURCE="process environment"
if [ -z "${GEMINI_API_KEY:-}" ] && [ "$IS_WINDOWS" = 1 ] && command -v powershell.exe >/dev/null 2>&1; then
  # Like up.ps1: fall back to the Windows user environment (setx) of an older terminal.
  user_key="$(powershell.exe -NoProfile -NonInteractive -Command "[Environment]::GetEnvironmentVariable('GEMINI_API_KEY','User')" 2>/dev/null | tr -d '\r\n')" || user_key=""
  if [ -n "$user_key" ]; then
    export GEMINI_API_KEY="$user_key"
    GEMINI_SOURCE="Windows user environment, loaded for this run"
  fi
  unset user_key
fi
if [ -z "${GEMINI_API_KEY:-}" ]; then
  log "GEMINI_API_KEY  : no"
  die "GEMINI_API_KEY is not set. Export it from your shell profile (never in .env), e.g. export GEMINI_API_KEY=<key> (Windows: setx GEMINI_API_KEY <key>), then open a new terminal."
fi
log "GEMINI_API_KEY  : yes ($GEMINI_SOURCE; value not shown)"

port80_owner() { # -> REPLY "name|pid" of the host port 80 listener, "" when free, "?" when unknown
  local pid="" name="" line=""
  REPLY=""
  if [ "$IS_WINDOWS" = 1 ] && command -v netstat >/dev/null 2>&1; then
    # columns: proto, local, foreign, state (localised), pid; listeners have foreign *:0
    pid="$(netstat -ano 2>/dev/null | tr -d '\r' | awk '$1 == "TCP" && $2 ~ /:80$/ && $3 ~ /:0$/ { print $5; exit }')" || true
    if [ -n "$pid" ]; then
      name="$(tasklist /FI "PID eq $pid" /FO CSV /NH 2>/dev/null | tr -d '\r' | awk -F'","' '/^"/ { sub(/^"/, "", $1); print $1; exit }')" || true
      REPLY="${name:-pid $pid}|$pid"
    fi
  elif command -v ss >/dev/null 2>&1; then
    line="$(ss -ltnp 2>/dev/null | awk '$4 ~ /:80$/ { print; exit }')" || true
    if [ -n "$line" ]; then
      name="$(printf '%s\n' "$line" | sed -n 's/.*users:(("\([^"]*\)",pid=\([0-9]*\).*/\1|\2/p')"
      REPLY="${name:-?|?}"
    fi
  elif command -v lsof >/dev/null 2>&1; then
    REPLY="$(lsof -nP -iTCP:80 -sTCP:LISTEN 2>/dev/null | awk 'NR == 2 { print $1 "|" $2; exit }')" || true
  else
    REPLY="?"
  fi
}
port80_owner
DOCKER_OWNER_RE='^(com\.docker\.backend|wslrelay|vpnkit|docker)'
case "$REPLY" in
  "") log "Port 80         : free (Caddy will bind it)" ;;
  "?") log "Port 80         : owner unknown (no netstat / ss / lsof)" ;;
  "?|?") log "Port 80         : in use; owner not visible (run as root to see it)" ;;
  *)
    P_OWNER="${REPLY%%|*}" P_PID="${REPLY#*|}"
    if [[ "$P_OWNER" =~ $DOCKER_OWNER_RE ]]; then
      log "Port 80         : held by '$P_OWNER' (pid $P_PID) - Docker, OK"
    else
      log "Port 80         : in use by '$P_OWNER' (pid $P_PID)"
      warn "Port 80 is already in use by '$P_OWNER'. Caddy needs it (see docs/infra.md, 'Port 80 busy')."
    fi
    ;;
esac

EXPORTED=$'\n'"$(compgen -e 2>/dev/null || env | sed -n 's/^\([A-Za-z_][A-Za-z0-9_]*\)=.*/\1/p')"$'\n'
shadowed_names() { # -> SHADOWED / SHADOWED_N: host variables that would shadow a .env key
  SHADOWED=()
  SHADOWED_N=0
  if [ ! -f "$ENV_FILE" ]; then return 0; fi
  local line name re='^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)[[:space:]]*='
  while IFS= read -r line || [ -n "$line" ]; do
    if [[ "$line" =~ $re ]]; then
      name=${BASH_REMATCH[1]}
      if [ "$name" = GEMINI_API_KEY ]; then continue; fi
      case "$EXPORTED" in *$'\n'"$name"$'\n'*) SHADOWED+=("$name"); SHADOWED_N=$((SHADOWED_N + 1)) ;; esac
    fi
  done <"$ENV_FILE"
}
if [ -f "$ENV_FILE" ]; then
  shadowed_names
  if [ "$SHADOWED_N" -gt 0 ]; then
    log_c "$C_WARN" ".env shadowed by: ${SHADOWED[*]} (host env; dropped for compose in step 3)"
  else
    log ".env shadowing  : none (no host variable overrides a .env key)"
  fi
else
  log ".env shadowing  : .env not found yet (created in step 2)"
fi
if [ -n "$PY" ]; then log "JSON parsing    : $PY"; else log "JSON parsing    : no python found - using docker's Go templates"; fi

# -- 2. init (idempotent) --
run_init() { # ARGS...: scripts/init.sh, keeping its step tag but dropping its own clock
  local rc
  set +e
  "$BASH" "$SCRIPT_DIR/init.sh" "$@" 2>&1 | {
    re='^\[[0-9][0-9]:[0-9][0-9]:[0-9][0-9] \+[^]]*\] ?(.*)$'
    while IFS= read -r line || [ -n "$line" ]; do
      clean_line "$line"
      line=$REPLY
      if [[ "$line" =~ $re ]]; then line=${BASH_REMATCH[1]}; fi
      log_c "$C_DIM" "  init: $line"
    done
  }
  rc=${PIPESTATUS[0]}
  set -e
  return "$rc"
}
step "Checking .env and secrets/ (scripts/init.sh --check)"
init_rc=0
run_init --check || init_rc=$?
if [ "$init_rc" -ne 0 ]; then
  log_c "$C_WARN" "init check reported missing/invalid values (exit $init_rc)"
  log "Generating missing .env values / secrets (existing values are kept)"
  run_init || die "init failed"
fi
ok ".env and secrets/ are complete."

# -- 3. host env vars shadowing .env --
step "Host environment variables vs .env"
shadowed_names
for name in ${SHADOWED[@]+"${SHADOWED[@]}"}; do
  warn "Host environment variable $name shadows .env - ignoring it for compose (env -u $name)."
  ENV_UNSET+=(-u "$name")
done
if [ "$SHADOWED_N" -eq 0 ]; then log "No host environment variable shadows a .env key."; fi
log "GEMINI_API_KEY is taken from the host environment (value not shown)."

# -- 4. compose up --
step "Starting containers (docker compose up)"
if [ "$INFRA" = 1 ]; then SERVICES=("${INFRA_SERVICES[@]}" ${SERVICES[@]+"${SERVICES[@]}"}); fi
if [ "$OBS" = 1 ]; then COMPOSE_OPTS=(--profile obs); fi
# Plain progress: build steps and container events stream line by line (no TTY redraw).
# Older compose releases lack the global --progress flag; the env vars cover those.
PROGRESS_OPTS=()
compose_help="$(docker compose --help 2>/dev/null || true)"
case "$compose_help" in
  *--progress*) PROGRESS_OPTS=(--progress plain) ;;
  *) export BUILDKIT_PROGRESS=plain COMPOSE_PROGRESS=plain ;;
esac
UP_ARGS=(up -d)
if [ "$NO_BUILD" != 1 ]; then UP_ARGS+=(--build); fi
if [ -n "${SERVICES[*]+x}" ]; then
  log "Services : ${SERVICES[*]}"
elif [ "$OBS" = 1 ]; then
  log "Services : all (default profile + obs)"
else
  log "Services : all (default profile)"
fi
if [ "$NO_BUILD" = 1 ]; then log "Build    : no (--no-build)"; else log "Build    : yes (cached layers are reused)"; fi
log "Command  : ${ENV_UNSET[*]+env ${ENV_UNSET[*]} }docker compose ${COMPOSE_OPTS[*]+${COMPOSE_OPTS[*]} }${PROGRESS_OPTS[*]+${PROGRESS_OPTS[*]} }${UP_ARGS[*]}${SERVICES[*]+ ${SERVICES[*]}}"

cd "$ROOT"
UP_START=$SECONDS
up_rc=0
run_logged '    ' '' compose ${PROGRESS_OPTS[@]+"${PROGRESS_OPTS[@]}"} "${UP_ARGS[@]}" ${SERVICES[@]+"${SERVICES[@]}"} || up_rc=$?
fmt_secs $((SECONDS - UP_START))
UP_TOOK=$REPLY
if [ "$up_rc" -ne 0 ]; then die "docker compose up failed after $UP_TOOK (exit $up_rc)"; fi
ok "docker compose up finished in $UP_TOOK"

# -- 5. wait for health --
step "Waiting up to ${TIMEOUT} s for services (poll every ${POLL_SEC} s, log tail every ~${TAIL_EVERY_SEC} s)"
WAIT_START=$SECONDS
LAST_TAIL=$WAIT_START
P_NAME=() P_STATUS=() # status per container name from the previous poll
FIRST=1
prev_status() { # NAME -> REPLY ("(new)" when unknown)
  local j
  REPLY="(new)"
  for j in ${P_NAME[@]+"${!P_NAME[@]}"}; do
    if [ "${P_NAME[$j]}" = "$1" ]; then REPLY=${P_STATUS[$j]}; return 0; fi
  done
  return 0
}
initial_state() { # one line per status: "healthy (13) caddy, engine-api, ..."
  local i st n names seen="|" line
  log "Initial state of $S_COUNT container(s):"
  for i in ${S_SVC[@]+"${!S_SVC[@]}"}; do
    svc_status "$i"
    case "$seen" in *"|$REPLY|"*) ;; *) seen="$seen$REPLY|" ;; esac
  done
  while IFS= read -r st; do
    if [ -z "$st" ]; then continue; fi
    n=0 names=""
    for i in ${S_SVC[@]+"${!S_SVC[@]}"}; do
      svc_status "$i"
      if [ "$REPLY" = "$st" ]; then n=$((n + 1)); names="$names, ${S_SVC[$i]}"; fi
    done
    printf -v line '  %-14s (%2d) %s' "$st" "$n" "${names#, }"
    log "$line"
  done < <(printf '%s\n' "$seen" | tr '|' '\n' | sort)
}
while :; do
  POLL_START=$SECONDS
  read_state
  WAITED=$((SECONDS - WAIT_START))
  if [ "$FIRST" = 1 ]; then initial_state; fi

  N_HEALTHY=0 N_RUNNING=0 N_COMPLETED=0 N_STARTING=0 N_FAILED=0 N_OTHER=0
  PENDING_IDX=() FAILED_IDX=()
  PENDING_NAMES=""
  NEW_NAME=() NEW_STATUS=()
  for i in ${S_SVC[@]+"${!S_SVC[@]}"}; do
    svc_status "$i"
    cur=$REPLY
    NEW_NAME+=("${S_NAME[$i]}") NEW_STATUS+=("$cur")
    svc_category "$i"
    category=$REPLY
    if [ "$FIRST" != 1 ]; then
      prev_status "${S_NAME[$i]}"
      if [ "$REPLY" != "$cur" ]; then
        from=$REPLY
        case "$category" in failed) color=$C_ERR ;; starting) color=$C_WARN ;; *) color=$C_OK ;; esac
        fmt_secs "$WAITED"
        log_c "$color" "${S_SVC[$i]}: $from -> $cur ($REPLY)"
      fi
    fi
    case "$category" in
      failed) N_FAILED=$((N_FAILED + 1)) FAILED_IDX+=("$i") ;;
      starting)
        N_STARTING=$((N_STARTING + 1)) PENDING_IDX+=("$i")
        case ", $PENDING_NAMES, " in *", ${S_SVC[$i]}, "*) ;; *) PENDING_NAMES="${PENDING_NAMES:+$PENDING_NAMES, }${S_SVC[$i]}" ;; esac
        ;;
      completed) N_COMPLETED=$((N_COMPLETED + 1)) ;;
      healthy) N_HEALTHY=$((N_HEALTHY + 1)) ;;
      running) N_RUNNING=$((N_RUNNING + 1)) ;;
      *) N_OTHER=$((N_OTHER + 1)) ;;
    esac
  done
  P_NAME=(${NEW_NAME[@]+"${NEW_NAME[@]}"})
  P_STATUS=(${NEW_STATUS[@]+"${NEW_STATUS[@]}"})
  FIRST=0

  progress="${WAITED}s/${TIMEOUT}s  healthy $N_HEALTHY | running $N_RUNNING | completed $N_COMPLETED | starting $N_STARTING | failed $N_FAILED"
  if [ "$N_OTHER" -gt 0 ]; then progress="$progress | other $N_OTHER"; fi
  progress="$progress  of $S_COUNT"
  if [ -n "$PENDING_NAMES" ]; then progress="$progress  pending: $PENDING_NAMES"; fi
  if [ "$N_FAILED" -gt 0 ]; then log_c "$C_ERR" "$progress"
  elif [ -n "$PENDING_NAMES" ]; then log "$progress"
  else ok "$progress"; fi

  if [ "$N_STARTING" -eq 0 ] || [ "$N_FAILED" -gt 0 ]; then break; fi

  if [ $((SECONDS - LAST_TAIL)) -ge "$TAIL_EVERY_SEC" ]; then
    LAST_TAIL=$SECONDS
    for i in ${PENDING_IDX[@]+"${PENDING_IDX[@]}"}; do
      if [ "${S_STATE[$i]}" = created ]; then
        log_c "$C_DIM" "  ${S_SVC[$i]}: created - waiting for its dependencies"
        continue
      fi
      svc_status "$i"
      log_c "$C_DIM" "  ${S_SVC[$i]} ($REPLY) - last log lines:"
      show_logs "${S_SVC[$i]}" 3 160 '      '
    done
  fi
  rest=$((POLL_SEC - (SECONDS - POLL_START)))
  if [ "$rest" -lt 1 ]; then rest=1; fi
  sleep "$rest"
  if [ $((SECONDS - WAIT_START)) -ge "$TIMEOUT" ]; then break; fi
done
fmt_secs $((SECONDS - WAIT_START))
WAIT_TOOK=$REPLY

if [ "$N_FAILED" -gt 0 ]; then
  for i in "${FAILED_IDX[@]}"; do
    svc_status "$i"
    log_c "$C_ERR" "---- ${S_SVC[$i]} ($REPLY): last 40 log lines ----"
    show_logs "${S_SVC[$i]}" 40 0 '  '
  done
elif [ "$N_STARTING" -gt 0 ]; then
  warn "Timeout after $WAIT_TOOK - showing the last 20 log lines of each service still starting."
  for i in "${PENDING_IDX[@]}"; do
    if [ "${S_STATE[$i]}" = created ]; then continue; fi
    svc_status "$i"
    log_c "$C_WARN" "---- ${S_SVC[$i]} ($REPLY): last 20 log lines ----"
    show_logs "${S_SVC[$i]}" 20 0 '  '
  done
else
  ok "All services are healthy / running / completed after $WAIT_TOOK of waiting."
fi

# -- 6. summary --
step "Summary"
printf -v line '  %-22s %-20s %s' Service Status Ports
log "$line"
printf -v line '  %-22s %-20s %s' ------- ------ -----
log "$line"
for i in ${S_SVC[@]+"${!S_SVC[@]}"}; do
  if [ "${S_STATE[$i]}" = exited ]; then
    if [ "${S_EXIT[$i]}" = 0 ]; then status=completed; else status="FAILED (exit ${S_EXIT[$i]})"; fi
  elif [ -n "${S_HEALTH[$i]}" ]; then status=${S_HEALTH[$i]}
  else status=${S_STATE[$i]}; fi
  printf -v line '  %-22s %-20s %s' "${S_SVC[$i]}" "$status" "${S_PORTS[$i]}"
  log "${line%"${line##*[![:space:]]}"}"
done
fmt_secs $((SECONDS - T0))
log "Total time: $REPLY (compose up $UP_TOOK, waiting $WAIT_TOOK)"

log "Public URLs (HTTP, via Caddy on port 80):"
log "  http://keelson.localhost   Keelson (engine UI + /api)"
log "  http://merxa.localhost     Merxa Retail Suite (mock ERP)"
log "  http://auth.localhost      Keycloak (realms keelson, merxa)"
log "  http://gateway.localhost   LiteLLM console (platform-admin)"
if [ "$OBS" = 1 ]; then log "  http://traces.localhost    Phoenix traces (platform-admin)"; fi
log "Browsers resolve *.localhost automatically; for Node/Python on a Windows host run scripts\\hosts.ps1."

if [ "$N_FAILED" -gt 0 ]; then
  names=""
  for i in "${FAILED_IDX[@]}"; do names="${names:+$names, }${S_SVC[$i]}"; done
  log_c "$C_ERR" "Failed: $names  -> docker compose logs <service>"
  exit 1
fi
if [ "$N_STARTING" -gt 0 ]; then
  warn "Still starting after $TIMEOUT s: $PENDING_NAMES"
  exit 2
fi
log "Next: bash scripts/verify-infra.sh --skip-llm   (drop --skip-llm to include one tiny Gemini call)"
ok "Stack is up."
