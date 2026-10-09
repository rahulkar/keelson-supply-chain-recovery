#!/usr/bin/env bash
# Verify Keelson images against the signing policy (infra/supply-chain/signing.env; docs/supply-chain.md).
#
#   scripts/supply-chain/verify-images.sh --release keelson-release.env
#       the signed release file (Sigstore bundle keelson-release.env.sigstore.json next to it), then
#       every RELEASE_IMAGE_* in it: keyless signature by release.yml carrying the release's git_sha
#       (and domain, for the domain overlays), SPDX SBOM attestation, SLSA v1 provenance
#   scripts/supply-chain/verify-images.sh [-a key=value]... REF...
#       the same image checks for explicit digest references (tags are refused)
#   scripts/supply-chain/verify-images.sh --compose
#       third-party images of compose.yaml: digest-pinned, and LiteLLM signed with BerriAI's vendored
#       cosign key (infra/supply-chain/keys); the others carry no publisher signature we rely on
#   scripts/supply-chain/verify-images.sh --upstream REF PUBKEY
#       one third-party image signed with its publisher's cosign key (release.yml, before building on it)
#
# Options: --signing-env FILE (default infra/supply-chain/signing.env), -h | --help
# cosign: $COSIGN_BIN, `cosign` on PATH, or the pinned COSIGN_IMAGE through docker (needs the network:
# GHCR and the public Rekor/Fulcio roots). Nothing is pulled or run besides cosign.
# Output: one JSON line per image {image, digest, signature, sbom, provenance, status, detail}, then a
# table. Exit status = number of images that FAILED (2 also for usage/policy errors).
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=scripts/supply-chain/lib/verify.sh
source "$ROOT/scripts/supply-chain/lib/verify.sh"

usage() { sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }
die() { printf 'verify-images.sh: %s\n' "$*" >&2; exit 2; }

SIGNING_ENV="$ROOT/infra/supply-chain/signing.env"
RELEASE=""; COMPOSE=0; REFS=(); ANNOTATIONS=(); UPSTREAM_REF=""; UPSTREAM_KEY=""
while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE="${2:?--release needs a file}"; shift 2 ;;
    --compose) COMPOSE=1; shift ;;
    --upstream) UPSTREAM_REF="${2:?--upstream needs REF PUBKEY}"; UPSTREAM_KEY="${3:?--upstream needs REF PUBKEY}"; shift 3 ;;
    --signing-env) SIGNING_ENV="${2:?--signing-env needs a file}"; shift 2 ;;
    -a | --annotation) ANNOTATIONS+=("${2:?-a needs key=value}"); shift 2 ;;
    -h | --help) usage; exit 0 ;;
    -*) die "unknown option $1 (see --help)" ;;
    *) REFS+=("$1"); shift ;;
  esac
done
modes=0
[ -n "$RELEASE" ] && modes=$((modes + 1))
[ "$COMPOSE" = 1 ] && modes=$((modes + 1))
[ ${#REFS[@]} -gt 0 ] && modes=$((modes + 1))
[ -n "$UPSTREAM_REF" ] && modes=$((modes + 1))
[ "$modes" = 1 ] || die "give exactly one of --release FILE, --compose, --upstream REF PUBKEY or REF... (see --help)"
sc_load_signing_env "$SIGNING_ENV" || die "$SC_ERROR"
[ "$SC_SIGNER_MODE" = keyless ] || printf 'verify-images.sh: WARNING: SIGNER_MODE=%s (test key), not the release.yml identity\n' "$SC_SIGNER_MODE" >&2

failed=0
ROWS=()
record() {  # image digest signature sbom provenance status detail
  sc_json_line "$@"
  ROWS+=("$(printf '%-6s %-60s sig=%-7s sbom=%-7s prov=%-7s %s' "$6" "$(printf '%s' "$1" | cut -c1-60)" "$3" "$4" "$5" "$7")")
  [ "$6" = PASS ] || [ "$6" = PINNED ] || failed=$((failed + 1))
}
check_ref() {  # name ref [annotations...]
  local name="$1" ref="$2" digest=""
  shift 2
  [[ "$ref" == *@sha256:* ]] && digest="${ref##*@}"
  if sc_verify_ref "$ref" "$@"; then
    record "$name" "$digest" "$SC_SIGNATURE" "$SC_SBOM" "$SC_PROVENANCE" PASS "$ref"
  else
    record "$name" "$digest" "$SC_SIGNATURE" "$SC_SBOM" "$SC_PROVENANCE" FAIL "$SC_ERROR"
  fi
}

if [ -n "$RELEASE" ]; then
  if ! sc_verify_release "$RELEASE"; then
    record "$(basename "$RELEASE")" "" fail skipped skipped FAIL "$SC_ERROR"
  else
    record "$(basename "$RELEASE")" "" pass skipped skipped PASS \
      "git_sha=${SC_RELEASE[RELEASE_GIT_SHA]} ref=${SC_RELEASE[RELEASE_REF]} domain=${SC_RELEASE[RELEASE_DOMAIN]:-}"
    mapfile -t keys < <(printf '%s\n' "${!SC_RELEASE[@]}" | grep '^RELEASE_IMAGE_' | sort)
    [ ${#keys[@]} -gt 0 ] || record "$(basename "$RELEASE")" "" skipped skipped skipped FAIL "no RELEASE_IMAGE_* entries"
    for k in "${keys[@]}"; do
      ann=("git_sha=${SC_RELEASE[RELEASE_GIT_SHA]}")
      # domain overlays carry the public domain they were built for
      case " ${SC_DOMAIN_OVERLAYS[*]} " in *" ${k#RELEASE_IMAGE_} "*) ann+=("domain=${SC_RELEASE[RELEASE_DOMAIN]:-}") ;; esac
      check_ref "${k#RELEASE_IMAGE_}" "${SC_RELEASE[$k]}" "${ann[@]}"
    done
  fi
elif [ "$COMPOSE" = 1 ]; then
  declare -A UPSTREAM_KEYS=([ghcr.io/berriai/litellm-non_root]=infra/supply-chain/keys/litellm-cosign.pub)
  while read -r ref; do
    name="${ref%%@*}"; name="${name%:*}"
    digest=""; [[ "$ref" == *@sha256:* ]] && digest="${ref##*@}"
    if ! sc_ref_ok "$ref"; then
      record "$ref" "$digest" refused skipped skipped FAIL "not pinned by digest (scripts/supply-chain/pin-image.sh)"
    elif [ -n "${UPSTREAM_KEYS[$name]:-}" ]; then
      if sc_verify_upstream "$ref" "${UPSTREAM_KEYS[$name]}"; then
        record "$ref" "$digest" pass skipped skipped PASS "publisher key ${UPSTREAM_KEYS[$name]}"
      else
        record "$ref" "$digest" "$SC_SIGNATURE" skipped skipped FAIL "$SC_ERROR"
      fi
    else
      record "$ref" "$digest" none skipped skipped PINNED "digest-pinned; no publisher signature is relied on"
    fi
  done < <(sed -n 's/^[[:space:]]*image:[[:space:]]*\([^[:space:]#]*\).*$/\1/p' "$ROOT/compose.yaml" | sort -u)
elif [ -n "$UPSTREAM_REF" ]; then
  digest=""; [[ "$UPSTREAM_REF" == *@sha256:* ]] && digest="${UPSTREAM_REF##*@}"
  if sc_verify_upstream "$UPSTREAM_REF" "$UPSTREAM_KEY"; then
    record "$UPSTREAM_REF" "$digest" pass skipped skipped PASS "publisher key $UPSTREAM_KEY"
  else
    record "$UPSTREAM_REF" "$digest" "$SC_SIGNATURE" skipped skipped FAIL "$SC_ERROR"
  fi
else
  for ref in "${REFS[@]}"; do check_ref "$ref" "$ref" "${ANNOTATIONS[@]}"; done
fi

printf '%s\n' "${ROWS[@]}" >&2
printf 'verify-images: %d failed\n' "$failed" >&2
exit "$failed"
