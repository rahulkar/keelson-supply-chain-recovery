# shellcheck shell=bash
# Sigstore verification helpers (docs/supply-chain.md, docs/contracts/129-m8-supply-chain.md §6).
#
# Sourced by scripts/supply-chain/verify-images.sh, scripts/supply-chain/compose-release.sh and
# scripts/cloudrun/deploy.sh. Defines functions and SC_* variables only; never exits the caller.
# Every check fails closed: a missing signature, attestation, bundle, tool or network is a failure.
#
#   sc_load_signing_env FILE           policy (infra/supply-chain/signing.env); returns 1 + SC_ERROR
#   sc_ref_ok REF                      REF is name[:tag]@sha256:<64 hex> (never a bare tag)
#   sc_verify_ref REF [KEY=VALUE...]   signature (+ required annotations), SPDX SBOM attestation and,
#                                      with REQUIRE_PROVENANCE=1, SLSA v1 provenance; sets SC_SIGNATURE,
#                                      SC_SBOM, SC_PROVENANCE (pass|fail|skipped) and SC_ERROR
#   sc_verify_upstream REF PUBKEY      a third-party image signed with the publisher's cosign key
#   sc_verify_release FILE             FILE.sigstore.json bundle + strict KEY=value validation; fills the
#                                      associative array SC_RELEASE (RELEASE_* keys)
#   sc_json_line ...                   one JSON result line (verify-images.sh / deploy.sh output)
#
# cosign: $COSIGN_BIN, else `cosign` on PATH, else `docker run $COSIGN_IMAGE` with every file argument
# mounted read-only (COSIGN_DOCKER_NETWORK optionally names the container network, e.g. for a local
# registry in the e2e test). SC_TRACE=1 echoes each cosign command as `+ cosign ...` (deploy.sh).

SC_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SC_REPO_ROOT="$(cd "$SC_LIB_DIR/../../.." && pwd)"
SC_SIGNING_KEYS=(SIGNER_MODE SIGNER_OIDC_ISSUER SIGNER_IDENTITY_REGEXP SIGNER_REPOSITORY SIGNER_TRIGGER SIGNER_KEY
  RELEASE_REGISTRY REQUIRE_PROVENANCE COSIGN_IMAGE)
SC_REF_RE='^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+(:[A-Za-z0-9_][A-Za-z0-9._-]{0,127})?@sha256:[a-f0-9]{64}$'
# Release keys of the domain overlays (images.json domain_overlay: true; checked by the tests): their
# signatures carry the public domain they were built for (annotation domain=<RELEASE_DOMAIN>).
SC_DOMAIN_OVERLAYS=(ENGINE_CLOUDRUN KC_CONFIG_CLOUDRUN)
SC_DOMAIN_RE='^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'
SC_ERROR=""
SC_SIGNATURE=""; SC_SBOM=""; SC_PROVENANCE=""
declare -gA SC_RELEASE=()

sc_load_signing_env() {  # FILE
  local file="$1" line k v known x
  SC_ERROR=""
  [ -f "$file" ] || { SC_ERROR="signing policy $file not found"; return 1; }
  for k in "${SC_SIGNING_KEYS[@]}"; do printf -v "SC_$k" '%s' ""; done
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in '' | \#*) continue ;; esac
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=(.*)$ ]] || { SC_ERROR="bad line in $file: $line"; return 1; }
    k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
    known=0
    for x in "${SC_SIGNING_KEYS[@]}"; do [ "$x" = "$k" ] && known=1; done
    [ "$known" = 1 ] || { SC_ERROR="unknown key $k in $file"; return 1; }
    printf -v "SC_$k" '%s' "$v"
  done <"$file"
  case "$SC_SIGNER_MODE" in
    keyless)
      [ -n "$SC_SIGNER_OIDC_ISSUER" ] && [ -n "$SC_SIGNER_IDENTITY_REGEXP" ] && [ -n "$SC_SIGNER_REPOSITORY" ] &&
        [ -n "$SC_SIGNER_TRIGGER" ] || { SC_ERROR="keyless mode needs SIGNER_OIDC_ISSUER, SIGNER_IDENTITY_REGEXP, SIGNER_REPOSITORY and SIGNER_TRIGGER"; return 1; }
      [[ "$SC_SIGNER_IDENTITY_REGEXP" == ^*\$ ]] || { SC_ERROR="SIGNER_IDENTITY_REGEXP must be anchored (^...\$)"; return 1; }
      ;;
    key)
      [ -n "$SC_SIGNER_KEY" ] || { SC_ERROR="key mode needs SIGNER_KEY (a public key file)"; return 1; }
      case "$SC_SIGNER_KEY" in /* | [A-Za-z]:*) ;; *) SC_SIGNER_KEY="$SC_REPO_ROOT/$SC_SIGNER_KEY" ;; esac
      [ -f "$SC_SIGNER_KEY" ] || { SC_ERROR="SIGNER_KEY $SC_SIGNER_KEY not found"; return 1; }
      ;;
    *) SC_ERROR="SIGNER_MODE must be keyless or key"; return 1 ;;
  esac
  [[ "$SC_SIGNER_REPOSITORY" =~ ^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)?$ ]] || { SC_ERROR="bad SIGNER_REPOSITORY"; return 1; }
  [[ "$SC_RELEASE_REGISTRY" =~ ^[a-z0-9]([a-z0-9.-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)+$ ]] ||
    { SC_ERROR="bad RELEASE_REGISTRY"; return 1; }
  [[ "$SC_REQUIRE_PROVENANCE" =~ ^[01]$ ]] || { SC_ERROR="REQUIRE_PROVENANCE must be 0 or 1"; return 1; }
  [[ "$SC_COSIGN_IMAGE" =~ @sha256:[a-f0-9]{64}$ ]] || { SC_ERROR="COSIGN_IMAGE must be pinned by digest"; return 1; }
  return 0
}

sc_ref_ok() { [[ "$1" =~ $SC_REF_RE ]]; }

sc_host_path() {  # path for a docker bind mount (Git Bash -> C:/...)
  if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi
}

sc_cosign_q() {  # ERRFILE ARGS...: cosign, stdout discarded, stderr into ERRFILE (traced on stdout first)
  local err="$1"
  shift
  if [ "${SC_TRACE:-0}" = 1 ]; then printf '+'; printf ' %q' cosign "$@"; printf '\n'; fi
  sc_cosign "$@" >/dev/null 2>"$err"
}

sc_cosign() {  # cosign ARGS... (stdout/stderr pass through)
  if [ -n "${COSIGN_BIN:-}" ]; then
    "$COSIGN_BIN" "$@" </dev/null
    return
  fi
  if command -v cosign >/dev/null 2>&1; then
    cosign "$@" </dev/null
    return
  fi
  command -v docker >/dev/null 2>&1 || { echo "neither cosign nor docker is available" >&2; return 127; }
  local args=() mounts=() a flag path n=0 dir
  for a in "$@"; do
    flag=""; path="$a"
    if [[ "$a" == --*=* ]]; then flag="${a%%=*}="; path="${a#*=}"; fi
    if [ -f "$path" ]; then
      n=$((n + 1)); dir="$(cd "$(dirname "$path")" && pwd)"
      mounts+=(-v "$(sc_host_path "$dir"):/sc_in$n:ro")
      args+=("${flag}/sc_in$n/$(basename "$path")")
    else
      args+=("$a")
    fi
  done
  local net=()
  [ -n "${COSIGN_DOCKER_NETWORK:-}" ] && net=(--network "$COSIGN_DOCKER_NETWORK")
  MSYS_NO_PATHCONV=1 docker run --rm -i --memory 256m "${net[@]}" "${mounts[@]}" "${SC_COSIGN_IMAGE:?COSIGN_IMAGE is not set}" "${args[@]}" </dev/null
}

sc_identity_args() {  # -> SC_ID_ARGS
  if [ "$SC_SIGNER_MODE" = key ]; then
    SC_ID_ARGS=(--key "$SC_SIGNER_KEY" --insecure-ignore-tlog=true)
  else
    SC_ID_ARGS=(--certificate-oidc-issuer "$SC_SIGNER_OIDC_ISSUER"
      --certificate-identity-regexp "$SC_SIGNER_IDENTITY_REGEXP"
      --certificate-github-workflow-repository "$SC_SIGNER_REPOSITORY"
      --certificate-github-workflow-trigger "$SC_SIGNER_TRIGGER")
  fi
}

sc_last_error() {  # FILE -> last non-empty line, one line, at most 200 chars
  grep -v '^[[:space:]]*$' "$1" 2>/dev/null | grep -iv 'insecure practice' | tail -n 1 | tr -d '\r' | cut -c1-200
}

sc_verify_ref() {  # REF [KEY=VALUE annotation...]
  local ref="$1" ann=() a err
  shift
  SC_SIGNATURE="fail"; SC_SBOM="skipped"; SC_PROVENANCE="skipped"; SC_ERROR=""
  if ! sc_ref_ok "$ref"; then
    SC_SIGNATURE="refused"; SC_ERROR="not a digest reference (name@sha256:<64 hex>); tags are mutable"
    return 1
  fi
  for a in "$@"; do
    [[ "$a" =~ ^[a-z_]+=[A-Za-z0-9._/-]+$ ]] || { SC_ERROR="bad annotation $a"; return 1; }
    ann+=(-a "$a")
  done
  sc_identity_args
  err="$(mktemp)"
  if sc_cosign_q "$err" verify "${SC_ID_ARGS[@]}" "${ann[@]}" "$ref"; then
    SC_SIGNATURE="pass"
  else
    SC_ERROR="signature: $(sc_last_error "$err")"; rm -f "$err"; return 1
  fi
  if sc_cosign_q "$err" verify-attestation "${SC_ID_ARGS[@]}" --type spdxjson "$ref"; then
    SC_SBOM="pass"
  else
    SC_SBOM="fail"; SC_ERROR="sbom attestation: $(sc_last_error "$err")"; rm -f "$err"; return 1
  fi
  if [ "$SC_REQUIRE_PROVENANCE" = 1 ]; then
    if sc_cosign_q "$err" verify-attestation "${SC_ID_ARGS[@]}" --type slsaprovenance1 "$ref"; then
      SC_PROVENANCE="pass"
    elif [ "$SC_SIGNER_MODE" = keyless ] && command -v gh >/dev/null 2>&1 &&
      gh attestation verify "oci://$ref" --repo "$SC_SIGNER_REPOSITORY" --bundle-from-oci \
        --signer-workflow "$SC_SIGNER_REPOSITORY/.github/workflows/release.yml" \
        --predicate-type https://slsa.dev/provenance/v1 >/dev/null 2>"$err" </dev/null; then
      SC_PROVENANCE="pass"
    else
      SC_PROVENANCE="fail"; SC_ERROR="provenance: $(sc_last_error "$err")"; rm -f "$err"; return 1
    fi
  fi
  rm -f "$err"
  return 0
}

sc_verify_upstream() {  # REF PUBKEY  (publisher key; Rekor inclusion is verified as well)
  local ref="$1" key="$2" err
  SC_ERROR=""; SC_SIGNATURE="fail"
  sc_ref_ok "$ref" || { SC_SIGNATURE="refused"; SC_ERROR="not a digest reference"; return 1; }
  case "$key" in /* | [A-Za-z]:*) ;; *) key="$SC_REPO_ROOT/$key" ;; esac
  [ -f "$key" ] || { SC_ERROR="publisher key $key not found"; return 1; }
  err="$(mktemp)"
  if sc_cosign_q "$err" verify --key "$key" "$ref"; then
    SC_SIGNATURE="pass"; rm -f "$err"; return 0
  fi
  SC_ERROR="publisher signature: $(sc_last_error "$err")"; rm -f "$err"; return 1
}

sc_verify_release() {  # FILE (FILE.sigstore.json next to it)
  local file="$1" bundle="$1.sigstore.json" err line k v
  SC_RELEASE=(); SC_ERROR=""
  [ -f "$file" ] || { SC_ERROR="release file $file not found"; return 1; }
  [ -f "$bundle" ] || { SC_ERROR="release bundle $bundle not found"; return 1; }
  sc_identity_args
  err="$(mktemp)"
  if ! sc_cosign_q "$err" verify-blob "${SC_ID_ARGS[@]}" --bundle "$bundle" "$file"; then
    SC_ERROR="release signature: $(sc_last_error "$err")"; rm -f "$err"; return 1
  fi
  rm -f "$err"
  declare -A seen=()
  while IFS= read -r line || [ -n "$line" ]; do
    line="${line%$'\r'}"
    case "$line" in '' | \#*) continue ;; esac
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]*)$ ]] || { SC_ERROR="bad line in release file: ${line:0:80}"; return 1; }
    k="${BASH_REMATCH[1]}"; v="${BASH_REMATCH[2]}"
    [ -z "${seen[$k]:-}" ] || { SC_ERROR="duplicate key $k in release file"; return 1; }
    seen[$k]=1
    case "$k" in
      RELEASE_SCHEMA) [ "$v" = keelson.release/1 ] ;;
      RELEASE_REPOSITORY) [ "$v" = "$SC_SIGNER_REPOSITORY" ] || [ "$SC_SIGNER_MODE" = key ] ;;
      RELEASE_GIT_SHA) [[ "$v" =~ ^[0-9a-f]{40}$ ]] ;;
      RELEASE_REF) [[ "$v" =~ ^refs/(heads/main|tags/v[0-9]+\.[0-9]+\.[0-9]+)$ ]] ;;
      RELEASE_RUN_ID) [[ "$v" =~ ^[0-9]{1,20}$ ]] ;;
      RELEASE_CREATED_AT) [[ "$v" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$ ]] ;;
      RELEASE_DOMAIN) [ -z "$v" ] || [[ "$v" =~ $SC_DOMAIN_RE ]] ;;
      RELEASE_IMAGE_*) [[ "$k" =~ ^RELEASE_IMAGE_[A-Z0-9_]+$ ]] && sc_ref_ok "$v" && [[ "$v" == "$SC_RELEASE_REGISTRY"/* ]] ;;
      *) false ;;
    esac || { SC_ERROR="invalid release entry $k=${v:0:120}"; return 1; }
    SC_RELEASE[$k]="$v"
  done <"$file"
  for k in RELEASE_SCHEMA RELEASE_REPOSITORY RELEASE_GIT_SHA RELEASE_REF RELEASE_RUN_ID RELEASE_CREATED_AT; do
    [ -n "${SC_RELEASE[$k]:-}" ] || { SC_ERROR="release file lacks $k"; return 1; }
  done
  return 0
}

sc_json_escape() { local s="${1//\\/\\\\}"; s="${s//\"/\\\"}"; printf '%s' "${s//$'\t'/ }"; }

sc_json_line() {  # image digest signature sbom provenance status detail
  printf '{"image":"%s","digest":"%s","signature":"%s","sbom":"%s","provenance":"%s","status":"%s","detail":"%s"}\n' \
    "$(sc_json_escape "$1")" "$(sc_json_escape "$2")" "$3" "$4" "$5" "$6" "$(sc_json_escape "$7")"
}
