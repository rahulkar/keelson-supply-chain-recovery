#!/usr/bin/env bash
# shellcheck disable=SC2034  # many variables are only read through ${!var} by render()
# Deploy Keelson + Merxa to Google Cloud Run (Terraform-free, gcloud + docker only).
#
#   scripts/cloudrun/deploy.sh --project <PROJECT> --region <REGION> --domain <BASE_DOMAIN> [options]
#   scripts/cloudrun/deploy.sh <PROJECT> <REGION> --domain <BASE_DOMAIN> --dry-run
#
# What it does (idempotent; every step checks before it creates) - details in docs/cloud-run.md:
#   1. enables the APIs; VPC + subnet (Private Google Access), Cloud Router + Cloud NAT,
#      Private Services Access (Cloud SQL private IP), PSC service connection policy (Memorystore)
#   2. Artifact Registry: with a signed release (--release / --release-tag), a remote repository
#      `ghcr` proxying ghcr.io serves the release's verified images by the same digests (nothing is
#      built); with --allow-unsigned (dev/smoke), every app image + the Cloud Run overlays
#      (infra/cloudrun/images) are built for linux/amd64, pushed and deployed by digest
#   3. Cloud SQL POSTGRES_18 (private IP, TLS only) and Memorystore for Valkey (cluster mode
#      disabled, PSC, reached through Direct VPC egress)
#   4. Secret Manager: one secret per secret in .env.example / secrets/ (+ GEMINI_API_KEY from the
#      environment and the derived DSNs); values go through stdin only, never argv or the log;
#      every workload's service account gets secretAccessor on exactly the secrets its YAML uses
#   5. Cloud Run: db-init job(s) (roles, databases, extensions pinned to Cloud SQL's versions),
#      keycloak (kc.sh build + start --optimized --cache=local, min = max = 1), litellm, optimizer,
#      merxa-api, kc-config job, bootstrap job, engine-api, engine-worker (worker pool), web apps
#   6. Global external Application Load Balancer: serverless NEGs, URL map (replaces Caddy's host
#      routing, incl. the keelson /api bypass), managed certificate, HTTP->HTTPS redirect,
#      Cloud Armor on auth.<domain>, IAP on gateway.<domain> (LiteLLM) and traces.<domain> (Phoenix)
#
# Image source (one is required; docs/supply-chain.md):
#   --release FILE                deploy a signed release: FILE = keelson-release.env from release.yml,
#                                 its Sigstore bundle FILE.sigstore.json next to it
#   --release-tag vX.Y.Z          the same, downloaded from the GitHub Release of SIGNER_REPOSITORY
#   --allow-unsigned              build and deploy unsigned local images (dev/smoke only; refused
#                                 when DEMO_MODE=0)
# Before ANY gcloud call, a release is verified with cosign against infra/supply-chain/signing.env:
# the release file's signature, then for every image the deploy uses its keyless signature by
# release.yml (annotations git_sha, and domain for the overlays), SBOM and SLSA provenance; its git
# sha must equal this checkout's HEAD (the manifests are rendered from it) and its domain --domain.
# Any failure exits 2. Verification runs in --dry-run too (it changes nothing).
#   --verify-only                 verify the release, print the result, exit (0 or 2); no gcloud
#   --allow-sha-mismatch          accept a release built from another commit (warning)
#   --signing-env F               signature policy (default infra/supply-chain/signing.env)
# Options:
#   --project P / --region R      required (or positional, or env PROJECT / REGION)
#   --domain D                    public base domain: keelson.D, merxa.D, auth.D, gateway.D, traces.D
#                                 (env DOMAIN; default PUBLIC_BASE_DOMAIN from .env unless localhost)
#   --dry-run                     print every command, call neither gcloud nor docker (env DRY_RUN=1)
#   --render-only DIR             only render the YAML files into DIR (no commands), then exit
#   --tag T                       image tag of local builds (default: UTC timestamp)
#   --skip-build                  (--allow-unsigned) do not build/push; deploy <registry>/<image>:<tag>
#   --iap-members LIST            comma separated IAP principals for the consoles,
#                                 e.g. group:platform-admins@example.com,user:ava@example.com
#   --enable-phoenix              also deploy Phoenix (traces.<domain>); default: use Cloud Trace
#   --sql-isolation               separate Cloud SQL instances for LiteLLM and Merxa (production);
#                                 default: one instance, databases isolated by role + CONNECT
#   --env-file F / --secrets-dir D   default: <repo>/.env and <repo>/secrets
#   -h | --help
# Environment knobs: SQL_TIER (db-custom-1-3840), VALKEY_NODE_TYPE (shared-core-nano),
#   WORKER_INSTANCES (1), SUBNET_RANGE (10.20.0.0/24), PSA_RANGE_PREFIX (20), GEMINI_API_KEY,
#   COSIGN_BIN (cosign binary; default `cosign` on PATH, else the pinned cosign image via docker).
set -Eeuo pipefail
shopt -u patsub_replacement 2>/dev/null || true

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CR="$ROOT/infra/cloudrun"

usage() { sed -n '3,/^set -Eeuo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }
die() { printf 'deploy.sh: error: %s\n' "$*" >&2; exit 2; }
note() { printf '# %s\n' "$*"; }
warn() { printf 'deploy.sh: warning: %s\n' "$*" >&2; }

# ───────────────────────────── arguments ─────────────────────────────
PROJECT="${PROJECT:-}"; REGION="${REGION:-}"; DOMAIN="${DOMAIN:-}"
DRY_RUN="${DRY_RUN:-0}"; RENDER_ONLY=""; TAG="${TAG:-}"; SKIP_BUILD="${SKIP_BUILD:-0}"
IAP_MEMBERS="${IAP_MEMBERS:-}"; ENABLE_PHOENIX="${ENABLE_PHOENIX:-0}"; SQL_ISOLATION="${SQL_ISOLATION:-0}"
ENV_FILE="${ENV_FILE:-$ROOT/.env}"; SECRETS_DIR="${SECRETS_DIR:-$ROOT/secrets}"
RELEASE_FILE=""; RELEASE_TAG=""; VERIFY_ONLY=0; ALLOW_UNSIGNED=0; ALLOW_SHA_MISMATCH=0
SIGNING_ENV="$ROOT/infra/supply-chain/signing.env"
positional=()
while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE_FILE="${2:?--release needs a file}"; shift 2 ;;
    --release-tag) RELEASE_TAG="${2:?--release-tag needs vX.Y.Z}"; shift 2 ;;
    --verify-only) VERIFY_ONLY=1; shift ;;
    --allow-unsigned) ALLOW_UNSIGNED=1; shift ;;
    --allow-sha-mismatch) ALLOW_SHA_MISMATCH=1; shift ;;
    --signing-env) SIGNING_ENV="${2:?--signing-env needs a file}"; shift 2 ;;
    --project) PROJECT="${2:?--project needs a value}"; shift 2 ;;
    --project=*) PROJECT="${1#*=}"; shift ;;
    --region) REGION="${2:?--region needs a value}"; shift 2 ;;
    --region=*) REGION="${1#*=}"; shift ;;
    --domain) DOMAIN="${2:?--domain needs a value}"; shift 2 ;;
    --domain=*) DOMAIN="${1#*=}"; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    --render-only) RENDER_ONLY="${2:?--render-only needs a directory}"; DRY_RUN=1; shift 2 ;;
    --tag) TAG="${2:?--tag needs a value}"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --iap-members) IAP_MEMBERS="${2:?--iap-members needs a value}"; shift 2 ;;
    --enable-phoenix) ENABLE_PHOENIX=1; shift ;;
    --sql-isolation) SQL_ISOLATION=1; shift ;;
    --env-file) ENV_FILE="${2:?}"; shift 2 ;;
    --secrets-dir) SECRETS_DIR="${2:?}"; shift 2 ;;
    -h | --help) usage; exit 0 ;;
    -*) die "unknown option $1 (see --help)" ;;
    *) positional+=("$1"); shift ;;
  esac
done
[ ${#positional[@]} -ge 1 ] && PROJECT="${positional[0]}"
[ ${#positional[@]} -ge 2 ] && REGION="${positional[1]}"
[ ${#positional[@]} -le 2 ] || die "too many arguments"

# ───────────────────────────── .env access (never eval'd, values never printed) ─────────────────
env_get() {  # KEY -> value of the last KEY=... line in ENV_FILE ("" if absent)
  local key="$1" line val=""
  [ -f "$ENV_FILE" ] || { printf ''; return 0; }
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in "$key="*) val="${line#*=}" ;; esac
  done <"$ENV_FILE"
  case "$val" in \"*\") val="${val:1:${#val}-2}" ;; \'*\') val="${val:1:${#val}-2}" ;; esac
  printf '%s' "$val"
}

[ -n "$PROJECT" ] || die "PROJECT is required (--project or first argument)"
[ -n "$REGION" ] || die "REGION is required (--region or second argument)"
[[ "$PROJECT" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]] || die "invalid project id: $PROJECT"
[[ "$REGION" =~ ^[a-z]+-[a-z]+[0-9]+$ ]] || die "invalid region: $REGION"
if [ -z "$DOMAIN" ]; then
  d="$(env_get PUBLIC_BASE_DOMAIN)"
  if [ -n "$d" ] && [ "$d" != localhost ]; then DOMAIN="$d"; fi
fi
[ -n "$DOMAIN" ] || die "DOMAIN is required (--domain; the base of keelson.<domain>, auth.<domain>, ...)"
DOMAIN="$(printf '%s' "$DOMAIN" | tr '[:upper:]' '[:lower:]')"
[[ "$DOMAIN" =~ ^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$ ]] || die "invalid domain: $DOMAIN"
case "$DOMAIN" in localhost | *.localhost) die "a .localhost domain cannot be deployed" ;; esac
[ "$ENABLE_PHOENIX" = 1 ] || ENABLE_PHOENIX=0

# ───────────────────────────── pins and settings ─────────────────────────────
while IFS='=' read -r k v; do
  case "$k" in '' | \#*) continue ;; esac
  [[ "$k" =~ ^[A-Z][A-Z0-9_]*$ ]] || die "bad line in versions.env: $k"
  printf -v "$k" '%s' "$v"
done <"$CR/versions.env"

TAG="${TAG:-$(date -u +%Y%m%d%H%M%S)}"
NETWORK="${NETWORK:-keelson-vpc}"; SUBNET="${SUBNET:-keelson-run}"
SUBNET_RANGE="${SUBNET_RANGE:-10.20.0.0/24}"; PSA_RANGE_PREFIX="${PSA_RANGE_PREFIX:-20}"
SQL_TIER="${SQL_TIER:-db-custom-1-3840}"; VALKEY_NODE_TYPE="${VALKEY_NODE_TYPE:-shared-core-nano}"
WORKER_INSTANCES="${WORKER_INSTANCES:-1}"
AR_REPO="${AR_REPO:-keelson}"; REGISTRY="${REGION}-docker.pkg.dev/${PROJECT}/${AR_REPO}"
VALKEY_INSTANCE="keelson-valkey"
CORE_SQL="keelson-pg"
if [ "$SQL_ISOLATION" = 1 ]; then LITELLM_SQL="keelson-pg-litellm"; MERXA_SQL="merxa-pg"; else LITELLM_SQL="$CORE_SQL"; MERXA_SQL="$CORE_SQL"; fi
cfg() { local v; v="${!1:-}"; [ -n "$v" ] || v="$(env_get "$1")"; printf '%s' "${v:-$2}"; }
DEMO_MODE="$(cfg DEMO_MODE 1)"; DEMO_ENABLED="$(cfg DEMO_ENABLED true)"; SEED_PROFILE="$(cfg SEED_PROFILE full)"
BRAND_ENGINE_NAME="$(cfg BRAND_ENGINE_NAME Keelson)"; BRAND_VENDOR_NAME="$(cfg BRAND_VENDOR_NAME Merxa)"
DEV_ALLOW_KC_ADMIN="$(cfg DEV_ALLOW_KC_ADMIN 0)"

# ───────────────────────────── signed release gate (before any gcloud call) ─────────────────────────────
# deploy.sh image variable <- RELEASE_IMAGE_<KEY> of keelson-release.env: the deploy_var / name pairs of
# infra/supply-chain/images.json (scripts/supply-chain/check_pins.py P9 keeps them equal).
RELEASE_IMAGE_MAP=(IMAGE_ENGINE:ENGINE_CLOUDRUN IMAGE_OPTIMIZER:OPTIMIZER IMAGE_MERXA_API:MERXA_API
  IMAGE_KEELSON_WEB:KEELSON_WEB IMAGE_MERXA_WEB:MERXA_WEB IMAGE_LITELLM:LITELLM_CLOUDRUN
  IMAGE_KEYCLOAK:KEYCLOAK_CLOUDRUN IMAGE_KC_CONFIG:KC_CONFIG_CLOUDRUN IMAGE_DB_INIT:DB_INIT
  IMAGE_SIGNAL_HUB:SIGNAL_HUB)
GHCR_REMOTE_REPO="${GHCR_REMOTE_REPO:-ghcr}"
declare -A RELEASE_REFS=()
RELEASE_MODE=0
{ [ -n "$RELEASE_FILE" ] || [ -n "$RELEASE_TAG" ]; } && RELEASE_MODE=1
release_preflight() {  # verify the release + every image it deploys; fills RELEASE_REFS; dies on failure
  local pair var key ref sha url ann=()
  # shellcheck source=scripts/supply-chain/lib/verify.sh
  source "$ROOT/scripts/supply-chain/lib/verify.sh"
  sc_load_signing_env "$SIGNING_ENV" || die "$SC_ERROR"
  [ "$SC_SIGNER_MODE" = keyless ] ||
    warn "SIGNER_MODE=$SC_SIGNER_MODE: signatures are checked against a TEST KEY, not release.yml - never for a real deploy"
  if [ -n "$RELEASE_TAG" ]; then
    [ -z "$RELEASE_FILE" ] || die "--release and --release-tag are mutually exclusive"
    [[ "$RELEASE_TAG" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "invalid release tag $RELEASE_TAG (vX.Y.Z)"
    command -v curl >/dev/null || die "curl is required for --release-tag"
    RELEASE_FILE="$(mktemp -d "${TMPDIR:-/tmp}/keelson-release.XXXXXX")/keelson-release.env"
    url="https://github.com/${SC_SIGNER_REPOSITORY}/releases/download/${RELEASE_TAG}"
    show curl -fsSL --proto =https -o "$RELEASE_FILE" "$url/keelson-release.env"
    curl -fsSL --proto '=https' --max-time 120 -o "$RELEASE_FILE" "$url/keelson-release.env" </dev/null &&
      curl -fsSL --proto '=https' --max-time 120 -o "$RELEASE_FILE.sigstore.json" "$url/keelson-release.env.sigstore.json" </dev/null ||
      die "cannot download release $RELEASE_TAG from $url"
  fi
  SC_TRACE=1
  sc_verify_release "$RELEASE_FILE" || die "refusing release $RELEASE_FILE: $SC_ERROR"
  sha="$(git -C "$ROOT" rev-parse HEAD 2>/dev/null || true)"
  if [ "$sha" != "${SC_RELEASE[RELEASE_GIT_SHA]}" ]; then
    [ "$ALLOW_SHA_MISMATCH" = 1 ] || die "the release was built from ${SC_RELEASE[RELEASE_GIT_SHA]} but this checkout is at ${sha:-<unknown>}; the manifests are rendered from the checkout: check out that commit (or pass --allow-sha-mismatch)"
    warn "release commit ${SC_RELEASE[RELEASE_GIT_SHA]} differs from the checkout ${sha:-<unknown>} (--allow-sha-mismatch)"
  fi
  [ "${SC_RELEASE[RELEASE_DOMAIN]:-}" = "$DOMAIN" ] ||
    die "the release's domain overlays were built for '${SC_RELEASE[RELEASE_DOMAIN]:-<none>}' (repository variable KEELSON_PUBLIC_DOMAIN), not $DOMAIN"
  for pair in "${RELEASE_IMAGE_MAP[@]}"; do
    var="${pair%%:*}"; key="${pair#*:}"; ref="${SC_RELEASE[RELEASE_IMAGE_$key]:-}"
    [ -n "$ref" ] || die "the release has no RELEASE_IMAGE_$key (needed for $var)"
    ann=("git_sha=${SC_RELEASE[RELEASE_GIT_SHA]}")
    case " ${SC_DOMAIN_OVERLAYS[*]} " in *" $key "*) ann+=("domain=$DOMAIN") ;; esac
    sc_verify_ref "$ref" "${ann[@]}" || die "refusing to deploy unverified image $ref: $SC_ERROR"
    RELEASE_REFS[$var]="$ref"
  done
  SC_TRACE=0
  if [ "$ENABLE_PHOENIX" = 1 ]; then
    sc_ref_ok "$PHOENIX_IMAGE" || die "PHOENIX_IMAGE in versions.env must be pinned by digest"
  fi
  note "release ${SC_RELEASE[RELEASE_REF]} (git ${SC_RELEASE[RELEASE_GIT_SHA]}, run ${SC_RELEASE[RELEASE_RUN_ID]}): ${#RELEASE_REFS[@]} images verified"
}
BS="projects/${PROJECT}/global/backendServices"
# Cloud Armor rule 1000 on keycloak-bs (see step 6); tested against Keycloak's path handling by
# scripts/cloudrun/tests/test_cloudrun.py (RE2-compatible: no look-arounds, no backreferences).
KC_EDGE_PATH="request.path.urlDecodeUni().lower()"
KC_EDGE_EXPRESSION="${KC_EDGE_PATH}.matches('^/+(admin|realms(;[^/]*)?/+master)([/;?]|\$)') || ${KC_EDGE_PATH}.matches('(^|/)[.][.]?(/|;|\$)')"
HOSTS="keelson.${DOMAIN},merxa.${DOMAIN},auth.${DOMAIN},gateway.${DOMAIN}"
[ "$ENABLE_PHOENIX" = 1 ] && HOSTS="${HOSTS},traces.${DOMAIN}"

# ───────────────────────────── secrets ─────────────────────────────
# Every secret of .env.example (scripts/cloudrun/tests check this list against the file) plus
# the ones compose.yaml reads from .env without listing them in .env.example.
ENV_SECRETS=(
  PG_CORE_SUPERPASSWORD KEELSON_DB_PASSWORD ADK_DB_PASSWORD KEYCLOAK_DB_PASSWORD
  LITELLM_DB_PASSWORD LITELLM_MASTER_KEY LITELLM_SALT_KEY LITELLM_UI_PASSWORD
  MERXA_DB_PASSWORD VALKEY_PASSWORD KC_ADMIN_PASSWORD
  KEELSON_BFF_CLIENT_SECRET KEELSON_DEMO_CLIENT_SECRET MERXA_WEB_CLIENT_SECRET MERXA_DEMO_CLIENT_SECRET
  DEMO_USER_PASSWORD SESSION_SECRET DEMO_ACCESS_SECRET OPTIMIZER_SHARED_SECRET PHOENIX_SECRET
  MERXA_WEBHOOK_SECRET MERXA_SNAPSHOT_SECRET
  SIGNAL_HUB_SHARED_SECRET SIGNALS_DB_PASSWORD
)
# Secrets compose reads that may legitimately be unset in .env: created by a helper with a
# placeholder value instead (LITELLM_SIGNALS_KEY: infra/cloudrun/signal-hub.sh stores "unset" until a
# dedicated LiteLLM virtual key is provisioned; the hub then leaves news pending_extraction).
OPTIONAL_SECRETS=(LITELLM_SIGNALS_KEY)
# compose `secrets:` files (secrets/<file>) -> env name read_secret() falls back to.
FILE_SECRETS=(keelson_client_assertion_key.pem keelson_receipt_key.pem keelson_data_key)

secret_id() {  # ENV_NAME -> Secret Manager id: keelson-<kebab>, without a doubled prefix
  local s; s="$(printf '%s' "$1" | tr '[:upper:]_' '[:lower:]-')"
  case "$s" in keelson-*) printf '%s' "$s" ;; *) printf 'keelson-%s' "$s" ;; esac
}
file_env_name() { local b="${1%.pem}"; printf '%s' "$b" | tr '[:lower:]' '[:upper:]'; }

# ───────────────────────────── command runners ─────────────────────────────
show() { printf '+'; printf ' %q' "$@"; printf '\n'; }
run() {  # run a command (echoed); in dry-run only echo it. stdin is /dev/null: callers loop over `read`
  show "$@"
  [ "$DRY_RUN" = 1 ] && return 0
  "$@" </dev/null
}
gc() { run gcloud "$@" --project="$PROJECT" --quiet; }
gcq() {  # like gc, but the command's own output is discarded (bindings print whole policies)
  show gcloud "$@" --project="$PROJECT" --quiet
  [ "$DRY_RUN" = 1 ] && return 0
  gcloud "$@" --project="$PROJECT" --quiet </dev/null >/dev/null
}
query() {  # query DEFAULT_FOR_DRY_RUN cmd... -> stdout of cmd (silent; dry-run prints DEFAULT)
  local dflt="$1"; shift
  if [ "$DRY_RUN" = 1 ]; then printf '%s' "$dflt"; return 0; fi
  "$@" </dev/null
}
exists() {  # exists cmd... -> 0 if the describe command succeeds (dry-run: never)
  [ "$DRY_RUN" = 1 ] && return 1
  "$@" --project="$PROJECT" </dev/null >/dev/null 2>&1
}
put_secret() {  # put_secret ID SOURCE_DESCRIPTION VALUE  (VALUE never echoed, sent on stdin)
  local id="$1" desc="$2" value="$3" current=""
  if ! exists gcloud secrets describe "$id"; then
    gc secrets create "$id" --replication-policy=automatic --labels=app=keelson
  fi
  if [ "$DRY_RUN" = 1 ]; then
    printf '+ gcloud secrets versions add %s --data-file=- --project=%s --quiet   # stdin: %s\n' "$id" "$PROJECT" "$desc"
    return 0
  fi
  [ -n "$value" ] || die "secret $id is empty ($desc)"
  current="$(gcloud secrets versions access latest --secret="$id" --project="$PROJECT" </dev/null 2>/dev/null || true)"
  if [ "$current" = "$value" ]; then
    note "secret $id unchanged"
  else
    printf '+ gcloud secrets versions add %s --data-file=- --project=%s --quiet   # stdin: %s\n' "$id" "$PROJECT" "$desc"
    printf '%s' "$value" | gcloud secrets versions add "$id" --data-file=- --project="$PROJECT" --quiet >/dev/null
  fi
}
secret_value() {  # current value of a secret in Secret Manager ("" if none / dry-run)
  [ "$DRY_RUN" = 1 ] && return 0
  gcloud secrets versions access latest --secret="$1" --project="$PROJECT" </dev/null 2>/dev/null || true
}

# ───────────────────────────── rendering ─────────────────────────────
RENDER_VARS=(PROJECT REGION PROJECT_NUMBER DOMAIN NETWORK SUBNET WORKER_INSTANCES
  IMAGE_ENGINE IMAGE_OPTIMIZER IMAGE_MERXA_API IMAGE_KEELSON_WEB IMAGE_MERXA_WEB IMAGE_LITELLM
  IMAGE_KEYCLOAK IMAGE_KC_CONFIG IMAGE_DB_INIT IMAGE_PHOENIX
  URL_ENGINE_API URL_KEYCLOAK URL_LITELLM URL_OPTIMIZER URL_MERXA_API CORE_SQL_IP
  DEMO_MODE DEMO_ENABLED SEED_PROFILE BRAND_ENGINE_NAME BRAND_VENDOR_NAME
  PIN_POSTGIS PIN_VECTOR PIN_BTREE_GIST PIN_PGCRYPTO BS SQL_INSTANCE SQL_IP DB_ROLES DB_ROLE_ENV)
render() {  # render SRC DST: substitute ${VAR} placeholders, drop comments + disabled phoenix blocks
  local src="$1" dst="$2" content var left
  content="$(cat "$src")"
  if [ "$ENABLE_PHOENIX" != 1 ]; then
    content="$(printf '%s\n' "$content" | sed '/# BEGIN phoenix/,/# END phoenix/d')"
  fi
  # Full-line comments are documentation only (and may mention placeholders): drop them.
  content="$(printf '%s\n' "$content" | sed '/^[[:space:]]*#/d')"
  for var in "${RENDER_VARS[@]}"; do
    content="${content//"\${$var}"/"${!var:-}"}"
  done
  if left="$(printf '%s\n' "$content" | grep -oE '\$\{[A-Z][A-Z0-9_]*\}' | sort -u | tr '\n' ' ')" && [ -n "$left" ]; then
    die "unrendered placeholders in $src: $left"
  fi
  mkdir -p "$(dirname "$dst")"
  printf '%s\n' "$content" >"$dst"
}
role_env_block() {  # YAML env entries (<ROLE>_DB_PASSWORD secret refs) for the db-init job
  local role name
  for role in "$@"; do
    name="$(printf '%s' "$role" | tr '[:lower:]' '[:upper:]')_DB_PASSWORD"
    printf '                - name: %s\n                  valueFrom:\n                    secretKeyRef:\n                      name: %s\n                      key: latest\n' \
      "$name" "$(secret_id "$name")"
  done
}
roles_on() {  # roles/databases that live on a Cloud SQL instance
  local inst="$1" roles=()
  [ "$inst" = "$CORE_SQL" ] && roles+=(keelson adk keycloak)
  [ "$inst" = "$LITELLM_SQL" ] && roles+=(litellm)
  [ "$inst" = "$MERXA_SQL" ] && roles+=(merxa)
  [ "$inst" = "$CORE_SQL" ] && [ "$ENABLE_PHOENIX" = 1 ] && roles+=(phoenix)
  printf '%s' "${roles[*]}"
}
sql_instances() { printf '%s\n' "$CORE_SQL" "$LITELLM_SQL" "$MERXA_SQL" | awk '!seen[$0]++'; }

# ───────────────────────────── signature gate (no gcloud call happens before it) ─────────────────────────────
if [ "$RELEASE_MODE" = 1 ]; then
  [ "$ALLOW_UNSIGNED" != 1 ] || die "--allow-unsigned and a release are mutually exclusive"
  [ "$SKIP_BUILD" != 1 ] || die "--skip-build has no meaning with a release (nothing is built)"
  release_preflight
elif [ "$VERIFY_ONLY" = 1 ]; then
  die "--verify-only needs --release FILE or --release-tag vX.Y.Z"
elif [ -z "$RENDER_ONLY" ]; then
  [ "$ALLOW_UNSIGNED" = 1 ] ||
    die "local builds are unsigned: deploy a signed release (--release FILE | --release-tag vX.Y.Z) or pass --allow-unsigned (dev/smoke only)"
  [ "$DEMO_MODE" != 0 ] || die "--allow-unsigned is refused when DEMO_MODE=0 (production): deploy a signed release"
  warn "deploying UNSIGNED local builds (--allow-unsigned): no signature, SBOM or provenance is checked"
fi
if [ "$VERIFY_ONLY" = 1 ]; then
  note "verify-only: the release and all ${#RELEASE_REFS[@]} images verified; nothing was deployed"
  exit 0
fi

# ───────────────────────────── derived names ─────────────────────────────
PROJECT_NUMBER="${PROJECT_NUMBER:-}"
if [ -z "$PROJECT_NUMBER" ]; then
  PROJECT_NUMBER="$(query 000000000000 gcloud projects describe "$PROJECT" --format='value(projectNumber)')"
fi
run_url() { printf 'https://%s-%s.%s.run.app' "$1" "$PROJECT_NUMBER" "$REGION"; }  # deterministic URL
URL_ENGINE_API="$(run_url engine-api)"; URL_KEYCLOAK="$(run_url keycloak)"; URL_LITELLM="$(run_url litellm)"
URL_OPTIMIZER="$(run_url optimizer)"; URL_MERXA_API="$(run_url merxa-api)"
for img in engine optimizer merxa-api keelson-web merxa-web litellm keycloak kc-config db-init phoenix; do
  v="IMAGE_$(printf '%s' "$img" | tr '[:lower:]-' '[:upper:]_')"
  printf -v "$v" '%s/%s:%s' "$REGISTRY" "$img" "$TAG"
done
# M6 Signal Hub (docs/contracts/100 §9): image, internal URL (the engine's SIGNAL_HUB_URL), jobs.
# shellcheck source=infra/cloudrun/signal-hub.sh
source "$CR/signal-hub.sh"
signal_hub_init
# signed release: every first-party image is the verified ghcr.io digest, pulled through the Artifact
# Registry remote repository (same digest = same content; the signatures were verified at the source)
for v in "${!RELEASE_REFS[@]}"; do
  ref="${RELEASE_REFS[$v]}"
  case "$ref" in ghcr.io/*@sha256:*) ;; *) die "release image $ref is not on ghcr.io (the remote repository proxies ghcr.io only)" ;; esac
  printf -v "$v" '%s-docker.pkg.dev/%s/%s/%s' "$REGION" "$PROJECT" "$GHCR_REMOTE_REPO" "${ref#ghcr.io/}"
done
CORE_SQL_IP="${CORE_SQL_IP:-10.0.0.3}"; SQL_IP=""; SQL_INSTANCE=""; DB_ROLES=""; DB_ROLE_ENV=""

WORK="${RENDER_ONLY:-$(mktemp -d "${TMPDIR:-/tmp}/keelson-cloudrun.XXXXXX")}"
render_all() {
  local f inst
  for f in "$CR"/services/*.yaml; do
    [ "$ENABLE_PHOENIX" = 1 ] || [ "$(basename "$f")" != phoenix.yaml ] || continue
    render "$f" "$WORK/services/$(basename "$f")"
  done
  render "$CR/workerpools/engine-worker.yaml" "$WORK/workerpools/engine-worker.yaml"
  render "$CR/jobs/keelson-bootstrap.yaml" "$WORK/jobs/keelson-bootstrap.yaml"
  render "$CR/jobs/keelson-kc-config.yaml" "$WORK/jobs/keelson-kc-config.yaml"
  while read -r inst; do
    SQL_INSTANCE="$inst"; SQL_IP="$(sql_ip_of "$inst")"; DB_ROLES="$(roles_on "$inst")"
    # shellcheck disable=SC2086  # role list is split on purpose
    DB_ROLE_ENV="$(role_env_block $DB_ROLES)"
    render "$CR/jobs/keelson-db-init.yaml" "$WORK/jobs/keelson-db-init-$inst.yaml"
  done < <(sql_instances)
  SQL_INSTANCE=""; SQL_IP=""; DB_ROLES=""; DB_ROLE_ENV=""
  render "$CR/lb/url-map.yaml" "$WORK/lb/url-map.yaml"
  render "$CR/lb/http-redirect.yaml" "$WORK/lb/http-redirect.yaml"
  signal_hub_render
}
declare -A SQL_IPS=()
sql_ip_of() { printf '%s' "${SQL_IPS[$1]:-10.0.0.3}"; }

if [ -n "$RENDER_ONLY" ]; then
  render_all
  note "rendered into $WORK"
  exit 0
fi

note "Keelson -> Cloud Run: project=$PROJECT ($PROJECT_NUMBER) region=$REGION domain=$DOMAIN tag=$TAG dry_run=$DRY_RUN images=$([ "$RELEASE_MODE" = 1 ] && echo signed-release || echo unsigned-local)"
note "rendered manifests: $WORK"

# ───────────────────────────── preflight ─────────────────────────────
if [ "$DRY_RUN" != 1 ]; then
  for tool in gcloud docker jq; do command -v "$tool" >/dev/null || die "$tool is required"; done
  [ -n "${GEMINI_API_KEY:-}" ] || die "GEMINI_API_KEY must be set in the environment"
fi
[ -f "$ENV_FILE" ] || { [ "$DRY_RUN" = 1 ] && warn "$ENV_FILE not found (run scripts/init.sh)"; } || die "$ENV_FILE not found (run scripts/init.sh)"
for f in "${FILE_SECRETS[@]}"; do
  [ -f "$SECRETS_DIR/$f" ] || { [ "$DRY_RUN" = 1 ] && warn "$SECRETS_DIR/$f not found"; } || die "$SECRETS_DIR/$f not found (run scripts/init.sh)"
done

# ───────────────────────────── 1. APIs and network ─────────────────────────────
note "1/6 APIs and network"
gc services enable run.googleapis.com artifactregistry.googleapis.com sqladmin.googleapis.com \
  servicenetworking.googleapis.com compute.googleapis.com secretmanager.googleapis.com \
  memorystore.googleapis.com networkconnectivity.googleapis.com serviceconsumermanagement.googleapis.com \
  iap.googleapis.com logging.googleapis.com cloudtrace.googleapis.com cloudscheduler.googleapis.com
exists gcloud compute networks describe "$NETWORK" ||
  gc compute networks create "$NETWORK" --subnet-mode=custom
exists gcloud compute networks subnets describe "$SUBNET" --region="$REGION" ||
  gc compute networks subnets create "$SUBNET" --network="$NETWORK" --region="$REGION" \
    --range="$SUBNET_RANGE" --enable-private-ip-google-access
exists gcloud compute routers describe keelson-router --region="$REGION" ||
  gc compute routers create keelson-router --network="$NETWORK" --region="$REGION"
exists gcloud compute routers nats describe keelson-nat --router=keelson-router --region="$REGION" ||
  gc compute routers nats create keelson-nat --router=keelson-router --region="$REGION" \
    --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges
exists gcloud compute addresses describe keelson-psa --global ||
  gc compute addresses create keelson-psa --global --purpose=VPC_PEERING \
    --prefix-length="$PSA_RANGE_PREFIX" --network="$NETWORK"
if [ "$DRY_RUN" = 1 ] || ! gcloud services vpc-peerings list --network="$NETWORK" --project="$PROJECT" </dev/null 2>/dev/null | grep -q servicenetworking; then
  gc services vpc-peerings connect --service=servicenetworking.googleapis.com --ranges=keelson-psa --network="$NETWORK"
fi
exists gcloud network-connectivity service-connection-policies describe keelson-memorystore --region="$REGION" ||
  gc network-connectivity service-connection-policies create keelson-memorystore --network="$NETWORK" \
    --region="$REGION" --service-class=gcp-memorystore \
    --subnets="https://www.googleapis.com/compute/v1/projects/${PROJECT}/regions/${REGION}/subnetworks/${SUBNET}"

# ───────────────────────────── 2. images ─────────────────────────────
note "2/6 Artifact Registry and images (linux/amd64, deployed by digest)"
exists gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" ||
  gc artifacts repositories create "$AR_REPO" --repository-format=docker --location="$REGION" \
    --description="Keelson + Merxa images"
if [ "$RELEASE_MODE" = 1 ]; then
  # Signed release: nothing is built. The remote repository serves the verified ghcr.io images by the
  # same digests (RELEASE_REFS, verified before step 1).
  exists gcloud artifacts repositories describe "$GHCR_REMOTE_REPO" --location="$REGION" ||
    gc artifacts repositories create "$GHCR_REMOTE_REPO" --repository-format=docker --location="$REGION" \
      --mode=remote-repository --remote-docker-repo=https://ghcr.io \
      --description="ghcr.io proxy for signed Keelson releases (digests verified before deploy)"
fi
if [ "$RELEASE_MODE" != 1 ] || [ "$ENABLE_PHOENIX" = 1 ]; then
  run gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet
fi
digest_of() {  # image:tag -> repo@sha256:... after push (dry-run: image:tag)
  query "$1" docker inspect --format='{{index .RepoDigests 0}}' "$1"
}
build_push() {  # VAR dockerfile context [build args...]  (a signed release builds nothing)
  local var="$1" file="$2" ctx="$3" ref; shift 3
  ref="${!var}"
  if [ "$SKIP_BUILD" != 1 ] && [ "$RELEASE_MODE" != 1 ]; then
    run docker build --platform linux/amd64 -f "$file" -t "$ref" "$@" "$ctx"
    run docker push "$ref"
    printf -v "$var" '%s' "$(digest_of "$ref")"
  fi
}
mirror() {  # VAR source-image : third-party image (pinned by digest in versions.env) re-tagged into Artifact Registry
  local var="$1" src="$2" ref="${!1}"
  if [ "$SKIP_BUILD" != 1 ]; then
    run docker pull --platform linux/amd64 "$src"
    run docker tag "$src" "$ref"
    run docker push "$ref"
    printf -v "$var" '%s' "$(digest_of "$ref")"
  fi
}
ENGINE_BASE="${REGISTRY}/engine-base:${TAG}"
if [ "$SKIP_BUILD" != 1 ] && [ "$RELEASE_MODE" != 1 ]; then
  run docker build --platform linux/amd64 -f "$ROOT/services/engine/Dockerfile" -t "$ENGINE_BASE" "$ROOT"
fi
build_push IMAGE_ENGINE "$CR/images/engine.Dockerfile" "$ROOT" \
  --build-arg "ENGINE_IMAGE=$ENGINE_BASE" --build-arg "PUBLIC_BASE_DOMAIN=$DOMAIN"
build_push IMAGE_OPTIMIZER "$ROOT/services/optimizer/Dockerfile" "$ROOT"
build_push IMAGE_MERXA_API "$ROOT/services/merxa-api/Dockerfile" "$ROOT/services/merxa-api"
build_push IMAGE_KEELSON_WEB "$ROOT/apps/keelson-web/Dockerfile" "$ROOT"
build_push IMAGE_MERXA_WEB "$ROOT/apps/merxa-web/Dockerfile" "$ROOT"
build_push IMAGE_KEYCLOAK "$CR/images/keycloak.Dockerfile" "$CR/images" --build-arg "KEYCLOAK_IMAGE=$KEYCLOAK_IMAGE"
build_push IMAGE_LITELLM "$CR/images/litellm.Dockerfile" "$ROOT" --build-arg "LITELLM_IMAGE=$LITELLM_IMAGE"
build_push IMAGE_KC_CONFIG "$CR/images/kc-config.Dockerfile" "$ROOT" \
  --build-arg "KC_CONFIG_IMAGE=$KC_CONFIG_IMAGE" --build-arg "PUBLIC_BASE_DOMAIN=$DOMAIN"
build_push IMAGE_DB_INIT "$CR/images/db-init/Dockerfile" "$CR/images/db-init" \
  --build-arg "POSTGRES_CLIENT_IMAGE=$POSTGRES_CLIENT_IMAGE"
signal_hub_build
[ "$ENABLE_PHOENIX" = 1 ] && mirror IMAGE_PHOENIX "$PHOENIX_IMAGE"

# ───────────────────────────── 3. Cloud SQL + Memorystore ─────────────────────────────
note "3/6 Cloud SQL ($CLOUDSQL_DATABASE_VERSION, private IP, TLS only) and Memorystore for Valkey"
PG_SUPER="$(env_get PG_CORE_SUPERPASSWORD)"
while read -r inst; do
  exists gcloud sql instances describe "$inst" ||
    gc sql instances create "$inst" --database-version="$CLOUDSQL_DATABASE_VERSION" --edition=ENTERPRISE \
      --tier="$SQL_TIER" --region="$REGION" --network="projects/${PROJECT}/global/networks/${NETWORK}" \
      --no-assign-ip --ssl-mode=ENCRYPTED_ONLY --availability-type=zonal --storage-auto-increase \
      --backup-start-time=03:00 --database-flags=max_connections=200 --deletion-protection
  # The postgres password never appears in the log; gcloud takes it as an argument (local only).
  printf '+ gcloud sql users set-password postgres --instance=%s --password=[PG_CORE_SUPERPASSWORD] --project=%s --quiet\n' "$inst" "$PROJECT"
  if [ "$DRY_RUN" != 1 ]; then
    [ -n "$PG_SUPER" ] || die "PG_CORE_SUPERPASSWORD missing in $ENV_FILE"
    gcloud sql users set-password postgres --instance="$inst" --password="$PG_SUPER" --project="$PROJECT" --quiet </dev/null >/dev/null
  fi
  SQL_IPS[$inst]="$(query 10.0.0.3 sh -c "gcloud sql instances describe '$inst' --project='$PROJECT' --format=json | jq -r '.ipAddresses[] | select(.type==\"PRIVATE\") | .ipAddress' | head -n1")"
  [ -n "${SQL_IPS[$inst]}" ] || die "no private IP for Cloud SQL instance $inst"
done < <(sql_instances)
CORE_SQL_IP="${SQL_IPS[$CORE_SQL]}"
exists gcloud memorystore instances describe "$VALKEY_INSTANCE" --location="$REGION" ||
  gc memorystore instances create "$VALKEY_INSTANCE" --location="$REGION" \
    --endpoints="[{\"connections\":[{\"pscAutoConnection\":{\"network\":\"projects/${PROJECT}/global/networks/${NETWORK}\",\"projectId\":\"${PROJECT}\"}}]}]" \
    --mode=cluster-disabled --shard-count=1 --replica-count=0 --node-type="$VALKEY_NODE_TYPE" \
    --engine-version="$VALKEY_ENGINE_VERSION" --authorization-mode=auth-disabled \
    --transit-encryption-mode=transit-encryption-disabled --persistence-config-mode=aof \
    --aof-config-append-fsync=every-sec --engine-configs=maxmemory-policy=noeviction
VALKEY_IP="$(query 10.20.1.2 sh -c "gcloud memorystore instances describe '$VALKEY_INSTANCE' --location='$REGION' --project='$PROJECT' --format=json | jq -r '[.endpoints[]?.connections[]?.pscAutoConnection | select(.connectionType==\"CONNECTION_TYPE_PRIMARY\") | .ipAddress][0] // empty'")"
[ -n "$VALKEY_IP" ] || die "could not read the Memorystore primary endpoint"

# ───────────────────────────── 4. secrets + service accounts ─────────────────────────────
note "4/6 Secret Manager (values on stdin only) and service accounts"
for name in "${ENV_SECRETS[@]}"; do
  put_secret "$(secret_id "$name")" ".env $name" "$(env_get "$name")"
done
for f in "${FILE_SECRETS[@]}"; do
  val=""; [ "$DRY_RUN" = 1 ] || val="$(cat "$SECRETS_DIR/$f")"
  put_secret "$(secret_id "$(file_env_name "$f")")" "secrets/$f" "$val"
done
put_secret keelson-gemini-api-key "env GEMINI_API_KEY" "${GEMINI_API_KEY:-}"
urlenc() {  # RFC 3986 percent-encoding (everything but unreserved), byte-wise (UTF-8 safe)
  local LC_ALL=C s="$1" out="" c i
  for ((i = 0; i < ${#s}; i++)); do
    c="${s:i:1}"
    case "$c" in
      [A-Za-z0-9._~-]) out+="$c" ;;
      *) printf -v c '%%%02X' "'$c"; out+="$c" ;;
    esac
  done
  printf '%s' "$out"
}
dsn() {  # driver role db host -> DSN (password from .env, percent-encoded; TLS required)
  local pw; pw="$(urlenc "$(env_get "$(printf '%s' "$2" | tr '[:lower:]' '[:upper:]')_DB_PASSWORD")")"
  case "$1" in
    asyncpg) printf 'postgresql+asyncpg://%s:%s@%s:5432/%s?ssl=require' "$2" "$pw" "$4" "$3" ;;
    libpq) printf 'postgresql://%s:%s@%s:5432/%s?sslmode=require' "$2" "$pw" "$4" "$3" ;;
  esac
}
put_secret keelson-database-url "derived: keelson@${CORE_SQL}" "$(dsn asyncpg keelson keelson "$CORE_SQL_IP")"
put_secret keelson-adk-database-url "derived: adk@${CORE_SQL}" "$(dsn asyncpg adk adk "$CORE_SQL_IP")"
put_secret keelson-merxa-database-url "derived: merxa@${MERXA_SQL}" "$(dsn asyncpg merxa merxa "$(sql_ip_of "$MERXA_SQL")")"
put_secret keelson-litellm-database-url "derived: litellm@${LITELLM_SQL}" "$(dsn libpq litellm litellm "$(sql_ip_of "$LITELLM_SQL")")"
put_secret keelson-valkey-url "derived: ${VALKEY_INSTANCE} primary endpoint" "redis://${VALKEY_IP}:6379/0"
if [ "$ENABLE_PHOENIX" = 1 ]; then
  PHOENIX_DB_PASSWORD="$(env_get PHOENIX_DB_PASSWORD)"
  [ -n "$PHOENIX_DB_PASSWORD" ] || PHOENIX_DB_PASSWORD="$(secret_value keelson-phoenix-db-password)"
  if [ -z "$PHOENIX_DB_PASSWORD" ] && [ "$DRY_RUN" != 1 ]; then
    PHOENIX_DB_PASSWORD="$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)"
  fi
  put_secret keelson-phoenix-db-password "generated once" "$PHOENIX_DB_PASSWORD"
  put_secret keelson-phoenix-database-url "derived: phoenix@${CORE_SQL}" \
    "postgresql://phoenix:$(urlenc "$PHOENIX_DB_PASSWORD")@${CORE_SQL_IP}:5432/phoenix?sslmode=require"
fi
SERVICE_ACCOUNTS=(keelson-engine keelson-web keelson-optimizer merxa-api merxa-web keelson-litellm keelson-keycloak keelson-jobs)
[ "$ENABLE_PHOENIX" = 1 ] && SERVICE_ACCOUNTS+=(keelson-phoenix)
for sa in "${SERVICE_ACCOUNTS[@]}"; do
  email="${sa}@${PROJECT}.iam.gserviceaccount.com"
  exists gcloud iam service-accounts describe "$email" ||
    gc iam service-accounts create "$sa" --display-name="Keelson Cloud Run: $sa"
  for role in roles/logging.logWriter roles/monitoring.metricWriter roles/cloudtrace.agent; do
    gcq projects add-iam-policy-binding "$PROJECT" --member="serviceAccount:${email}" --role="$role" --condition=None
  done
done
signal_hub_secrets  # derived DSNs, the optional LiteLLM signals key, service account keelson-signal-hub

# ───────────────────────────── 5. Cloud Run ─────────────────────────────
note "5/6 Cloud Run jobs, services and the worker pool"
render_all
# Least privilege: each service account may read exactly the secrets its manifests reference.
while read -r sa sid; do
  gcq secrets add-iam-policy-binding "$sid" --member="serviceAccount:${sa}" --role=roles/secretmanager.secretAccessor
done < <(for f in "$WORK"/services/*.yaml "$WORK"/workerpools/*.yaml "$WORK"/jobs/*.yaml; do
  awk '/serviceAccountName:/ {sa=$2} /secretKeyRef:/ {getline; if ($1 == "name:") print sa, $2}' "$f"
done | sort -u)
replace_service() { gc run services replace "$WORK/services/$1.yaml" --region="$REGION"; }
run_job() { gc run jobs replace "$WORK/jobs/$1.yaml" --region="$REGION"; gc run jobs execute "$1" --region="$REGION" --wait; }
while read -r inst; do run_job "keelson-db-init-$inst"; done < <(sql_instances)
replace_service keycloak
replace_service litellm
replace_service optimizer
replace_service merxa-api
[ "$ENABLE_PHOENIX" = 1 ] && replace_service phoenix
run_job keelson-kc-config
run_job keelson-bootstrap
signal_hub_deploy  # bootstrap job, internal API service, poll jobs + Cloud Scheduler (before the engine)
replace_service engine-api
gc run worker-pools replace "$WORK/workerpools/engine-worker.yaml" --region="$REGION"
replace_service keelson-web
replace_service merxa-web

# ───────────────────────────── 6. load balancer ─────────────────────────────
note "6/6 global external Application Load Balancer for: ${HOSTS}"
exists gcloud compute addresses describe keelson-ip --global ||
  gc compute addresses create keelson-ip --global --ip-version=IPV4
LB_IP="$(query 203.0.113.10 gcloud compute addresses describe keelson-ip --global --project="$PROJECT" --format='value(address)')"
CERT="keelson-cert-$(printf '%s' "$HOSTS" | cksum | cut -d' ' -f1)"  # new name when the host set changes
exists gcloud compute ssl-certificates describe "$CERT" --global ||
  gc compute ssl-certificates create "$CERT" --global --domains="$HOSTS"
LB_SERVICES=(engine-api keelson-web merxa-api merxa-web keycloak litellm)
[ "$ENABLE_PHOENIX" = 1 ] && LB_SERVICES+=(phoenix)
for svc in "${LB_SERVICES[@]}"; do
  exists gcloud compute network-endpoint-groups describe "${svc}-neg" --region="$REGION" ||
    gc compute network-endpoint-groups create "${svc}-neg" --region="$REGION" \
      --network-endpoint-type=serverless --cloud-run-service="$svc"
  if ! exists gcloud compute backend-services describe "${svc}-bs" --global; then
    gc compute backend-services create "${svc}-bs" --global --load-balancing-scheme=EXTERNAL_MANAGED
    gc compute backend-services add-backend "${svc}-bs" --global \
      --network-endpoint-group="${svc}-neg" --network-endpoint-group-region="$REGION"
  fi
done
# auth.<domain>: Caddy answered 403 for /admin* and /realms/master*; Cloud Armor does it here.
exists gcloud compute security-policies describe keelson-auth-edge ||
  gc compute security-policies create keelson-auth-edge --description="Keycloak edge: no admin console, no master realm"
if [ "$DEV_ALLOW_KC_ADMIN" != 1 ]; then
  # request.path is the RAW path, but Keycloak (Quarkus/Vert.x) percent-decodes it, ignores
  # ;matrix parameters and resolves dot segments before routing: decode first, then block
  # /admin and /realms/master (any case, repeated slashes, ;params) and every dot-segment path
  # (Keycloak never needs one). Re-applied on every run so an older expression is replaced.
  if exists gcloud compute security-policies rules describe 1000 --security-policy=keelson-auth-edge; then
    kc_rule_verb=update
  else
    kc_rule_verb=create
  fi
  gc compute security-policies rules "$kc_rule_verb" 1000 --security-policy=keelson-auth-edge --action=deny-403 \
    --expression="$KC_EDGE_EXPRESSION" \
    --description="block /admin* and /realms/master* (DEV_ALLOW_KC_ADMIN=0)"
else
  warn "DEV_ALLOW_KC_ADMIN=1: the Keycloak admin console is reachable on auth.${DOMAIN}"
  exists gcloud compute security-policies rules describe 1000 --security-policy=keelson-auth-edge &&
    gc compute security-policies rules delete 1000 --security-policy=keelson-auth-edge
fi
gc compute backend-services update keycloak-bs --global --security-policy=keelson-auth-edge
# Consoles: IAP (Google identity) replaces Caddy's forward_auth to engine-api (platform-admin).
IAP_BACKENDS=(litellm-bs)
[ "$ENABLE_PHOENIX" = 1 ] && IAP_BACKENDS+=(phoenix-bs)
[ -n "$IAP_MEMBERS" ] || warn "--iap-members not set: nobody can open gateway.${DOMAIN} until access is granted"
for bs in "${IAP_BACKENDS[@]}"; do
  gc compute backend-services update "$bs" --global --iap=enabled
  IFS=',' read -r -a members <<<"$IAP_MEMBERS"
  for m in "${members[@]}"; do
    [ -n "$m" ] || continue
    gc iap web add-iam-policy-binding --resource-type=backend-services --service="$bs" \
      --member="$m" --role=roles/iap.httpsResourceAccessor
  done
done
gc compute url-maps import keelson-urlmap --global --source="$WORK/lb/url-map.yaml"
gc compute url-maps import keelson-http-redirect --global --source="$WORK/lb/http-redirect.yaml"
if exists gcloud compute target-https-proxies describe keelson-https-proxy --global; then
  gc compute target-https-proxies update keelson-https-proxy --global --ssl-certificates="$CERT" --url-map=keelson-urlmap
else
  gc compute target-https-proxies create keelson-https-proxy --global --ssl-certificates="$CERT" --url-map=keelson-urlmap
fi
exists gcloud compute target-http-proxies describe keelson-http-proxy --global ||
  gc compute target-http-proxies create keelson-http-proxy --global --url-map=keelson-http-redirect
exists gcloud compute forwarding-rules describe keelson-https --global ||
  gc compute forwarding-rules create keelson-https --global --load-balancing-scheme=EXTERNAL_MANAGED \
    --address=keelson-ip --target-https-proxy=keelson-https-proxy --ports=443
exists gcloud compute forwarding-rules describe keelson-http --global ||
  gc compute forwarding-rules create keelson-http --global --load-balancing-scheme=EXTERNAL_MANAGED \
    --address=keelson-ip --target-http-proxy=keelson-http-proxy --ports=80
# Caddy redacted OAuth codes in its access log; Cloud Run/ALB request logs cannot, so drop them.
if [ "$DRY_RUN" = 1 ] || ! gcloud logging sinks describe _Default --project="$PROJECT" --format=json </dev/null 2>/dev/null | grep -q keelson-oauth-query; then
  gc logging sinks update _Default \
    --add-exclusion='name=keelson-oauth-query,filter=httpRequest.requestUrl=~"[?&](code|session_state|ticket)="'
fi

note "done. Create DNS A records -> ${LB_IP}: ${HOSTS//,/ }"
note "certificate: gcloud compute ssl-certificates describe ${CERT} --global --project=${PROJECT} (ACTIVE after DNS)"
note "then: https://keelson.${DOMAIN}  https://merxa.${DOMAIN}  https://gateway.${DOMAIN} (IAP)"
