# shellcheck shell=bash
# shellcheck disable=SC2034  # SH_ADAPTER / SH_SCHEDULE / IMAGE_* are read by render() via ${!var}
# Signal Hub additions to scripts/cloudrun/deploy.sh (docs/contracts/100-m6-signals.md §9).
#
# Sourced by deploy.sh after its helpers are defined; defines functions only (no side effects when
# sourced). It uses deploy.sh's helpers and variables: gc gcq exists put_secret env_get cfg urlenc
# render build_push run_url note, PROJECT REGION REGISTRY TAG ROOT CR WORK CORE_SQL CORE_SQL_IP
# RENDER_VARS.
#
# Hooks in deploy.sh (wired; M6 integration):
#   signal_hub_init      after the derived names (image refs / run.app URLs), before any render
#   signal_hub_render    from render_all (also --render-only)
#   signal_hub_build     step 2, after the other build_push calls
#   signal_hub_secrets   step 4, after the other put_secret calls (also creates the service account)
#   signal_hub_deploy    step 5, after `run_job keelson-bootstrap` and before engine-api
#                        (the engine reads SIGNAL_HUB_URL = $URL_SIGNAL_HUB)
# The Cloud Scheduler API (cloudscheduler.googleapis.com) must be enabled with the others.
#
# Cloud Run mapping (compose → Cloud Run):
#   signal-hub-bootstrap → Job signal-hub-bootstrap (executed on every deploy, idempotent)
#   signal-hub-api       → Service signal-hub-api (internal ingress, minScale 1)
#   signal-hub-poller    → one Job per adapter, signal-hub-poll-<adapter> (`--once --adapter X`),
#                          each run by a Cloud Scheduler job on the adapter's cadence
#
# M8 feeds (docs/contracts/124-m8-signal-feeds.md): a poll job per feed adapter in the operator's
# allow-list SIGNAL_ADAPTERS (default nhc, copernicus_ems, ofac and gdelt besides the M6 ones; a paused
# source, e.g. GDELT until a workspace opts in, exits 0 at once). meteoalarm gets a job only when .env
# lists it in SIGNAL_ADAPTERS *and* sets METEOALARM_COUNTRIES (its redistribution terms are the
# operator's call). SIGNAL_ADAPTERS and METEOALARM_COUNTRIES reach the API service and every poll job
# (rendered from .env / defaults); the hub's `--once` skips an adapter outside SIGNAL_ADAPTERS anyway.

SIGNAL_HUB_DIR="${CR:?CR must be set by deploy.sh}/signal-hub"
SIGNAL_HUB_ADAPTERS=(gdacs usgs rss nhc copernicus_ems ofac gdelt meteoalarm extraction sweep)
declare -A SIGNAL_HUB_SCHEDULES=(
  [gdacs]="*/10 * * * *"
  [usgs]="*/5 * * * *"
  [rss]="*/15 * * * *"
  [nhc]="*/15 * * * *"
  [copernicus_ems]="*/30 * * * *"
  [ofac]="0 * * * *"
  [gdelt]="*/15 * * * *"
  [meteoalarm]="*/15 * * * *"
  [extraction]="*/5 * * * *"
  [sweep]="*/10 * * * *"
)
SH_DEFAULT_ADAPTERS="gdacs,usgs,rss,nhc,copernicus_ems,ofac,gdelt"
SIGNAL_HUB_SA="keelson-signal-hub"

signal_hub_init() {
  IMAGE_SIGNAL_HUB="${REGISTRY}/signal-hub:${TAG}"
  URL_SIGNAL_HUB="$(run_url signal-hub-api)"
  SH_ADAPTER=""
  SH_SCHEDULE=""
  # M8 feeds: the allow-list and the MeteoAlarm countries; jobs only for the allowed feed adapters
  # (meteoalarm needs both), extraction and sweep always
  local adapter jobs=()
  SH_SIGNAL_ADAPTERS="$(cfg SIGNAL_ADAPTERS "$SH_DEFAULT_ADAPTERS")"
  SH_SIGNAL_ADAPTERS="${SH_SIGNAL_ADAPTERS// /}"
  SH_METEOALARM_COUNTRIES="$(cfg METEOALARM_COUNTRIES "")"
  for adapter in "${SIGNAL_HUB_ADAPTERS[@]}"; do
    case "$adapter" in
      extraction|sweep) jobs+=("$adapter") ;;
      *)
        case ",$SH_SIGNAL_ADAPTERS," in *",$adapter,"*) ;; *) continue ;; esac
        if [ "$adapter" != meteoalarm ] || [ -n "$SH_METEOALARM_COUNTRIES" ]; then jobs+=("$adapter"); fi ;;
    esac
  done
  SIGNAL_HUB_ADAPTERS=("${jobs[@]}")
  RENDER_VARS+=(IMAGE_SIGNAL_HUB URL_SIGNAL_HUB SH_ADAPTER SH_SCHEDULE SH_SIGNAL_ADAPTERS SH_METEOALARM_COUNTRIES)
  export IMAGE_SIGNAL_HUB URL_SIGNAL_HUB
}

signal_hub_build() {
  build_push IMAGE_SIGNAL_HUB "$ROOT/services/signal-hub/Dockerfile" "$ROOT"
}

signal_hub_secrets() {
  local pw admin email role
  # keelson-signal-hub-shared-secret and keelson-signals-db-password: deploy.sh's ENV_SECRETS.
  pw="$(urlenc "$(env_get SIGNALS_DB_PASSWORD)")"
  put_secret keelson-signals-database-url "derived: signals@${CORE_SQL}" \
    "postgresql://signals:${pw}@${CORE_SQL_IP}:5432/signals?sslmode=require"
  admin="$(urlenc "$(env_get PG_CORE_SUPERPASSWORD)")"
  put_secret keelson-signals-admin-dsn "derived: postgres@${CORE_SQL} (bootstrap only)" \
    "postgresql://postgres:${admin}@${CORE_SQL_IP}:5432/postgres?sslmode=require"
  # Dedicated LiteLLM virtual key for news extraction (never the master key). "unset" = no key
  # provisioned yet: news items stay pending_extraction until it is.
  put_secret keelson-litellm-signals-key ".env LITELLM_SIGNALS_KEY (or 'unset')" "$(cfg LITELLM_SIGNALS_KEY unset)"
  email="${SIGNAL_HUB_SA}@${PROJECT}.iam.gserviceaccount.com"
  exists gcloud iam service-accounts describe "$email" ||
    gc iam service-accounts create "$SIGNAL_HUB_SA" --display-name="Keelson Cloud Run: $SIGNAL_HUB_SA"
  for role in roles/logging.logWriter roles/monitoring.metricWriter roles/cloudtrace.agent; do
    gcq projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${email}" --role="$role" --condition=None
  done
}

signal_hub_render() {
  local adapter
  render "$SIGNAL_HUB_DIR/signal-hub-api.yaml" "$WORK/signal-hub/signal-hub-api.yaml"
  render "$SIGNAL_HUB_DIR/signal-hub-bootstrap.yaml" "$WORK/signal-hub/signal-hub-bootstrap.yaml"
  for adapter in "${SIGNAL_HUB_ADAPTERS[@]}"; do
    SH_ADAPTER="$adapter"
    SH_SCHEDULE="${SIGNAL_HUB_SCHEDULES[$adapter]}"
    render "$SIGNAL_HUB_DIR/signal-hub-poll.yaml" "$WORK/signal-hub/signal-hub-poll-${adapter}.yaml"
  done
  SH_ADAPTER=""
  SH_SCHEDULE=""
}

signal_hub_deploy() {
  local adapter job uri verb sa sid scheduler_sa
  signal_hub_render
  # Least privilege: the service account reads exactly the secrets its manifests reference.
  while read -r sa sid; do
    gcq secrets add-iam-policy-binding "$sid" --member="serviceAccount:${sa}" --role=roles/secretmanager.secretAccessor
  done < <(for f in "$WORK"/signal-hub/*.yaml; do
    awk '/serviceAccountName:/ {sa=$2} /secretKeyRef:/ {getline; if ($1 == "name:") print sa, $2}' "$f"
  done | sort -u)
  gc run jobs replace "$WORK/signal-hub/signal-hub-bootstrap.yaml" --region="$REGION"
  gc run jobs execute signal-hub-bootstrap --region="$REGION" --wait
  gc run services replace "$WORK/signal-hub/signal-hub-api.yaml" --region="$REGION"
  scheduler_sa="keelson-jobs@${PROJECT}.iam.gserviceaccount.com"
  for adapter in "${SIGNAL_HUB_ADAPTERS[@]}"; do
    job="signal-hub-poll-${adapter}"
    gc run jobs replace "$WORK/signal-hub/${job}.yaml" --region="$REGION"
    gcq run jobs add-iam-policy-binding "$job" --region="$REGION" \
      --member="serviceAccount:${scheduler_sa}" --role=roles/run.invoker
    uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/${job}:run"
    if exists gcloud scheduler jobs describe "$job" --location="$REGION"; then verb=update; else verb=create; fi
    gc scheduler jobs "$verb" http "$job" --location="$REGION" --schedule="${SIGNAL_HUB_SCHEDULES[$adapter]}" \
      --uri="$uri" --http-method=POST --oauth-service-account-email="$scheduler_sa"
  done
  note "signal-hub: ${URL_SIGNAL_HUB} (internal) + jobs ${SIGNAL_HUB_ADAPTERS[*]/#/signal-hub-poll-}"
}
