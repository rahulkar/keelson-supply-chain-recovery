#!/usr/bin/env bash
# Offline end-to-end test of the signature gate with REAL cosign (docs/supply-chain.md, 129 §8): a
# throwaway OCI registry, three tiny FROM-scratch images and test key pairs (key mode, no
# transparency log, nothing leaves the machine once the pinned registry/cosign images are present).
#   A  signed with key 1 (annotations git_sha + domain), SPDX SBOM + SLSA provenance attested
#   B  unsigned                                   C  signed and attested with key 2 (wrong key)
# Checks: verify-images.sh passes A only (B unsigned, C wrong key, A by tag refused, A with a
# different git_sha refused); scripts/cloudrun/deploy.sh --verify-only accepts a signed release that
# lists A and refuses one listing B or a tampered release file - before any gcloud call (a fake
# gcloud on PATH proves none happens); compose-release.sh writes its override for the good release only,
# and that override renders with compose.yaml (`docker compose config`, when compose is installed).
# Needs Docker (cosign runs in the pinned image). Everything is named keelson_sc_e2e_* and removed.
# Usage: scripts/supply-chain/tests/e2e_sign_verify.sh      Exit status = number of failed checks.
# shellcheck disable=SC2319  # "[ cond ]; result <name> $?" reports the condition status on purpose
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
host_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
REGISTRY_IMAGE="registry:3.1.2@sha256:ddf754342cfc8acc51a56d5d0ab6af06826461864460636d8bd5c546dab2a7b8"
COSIGN_IMAGE="$(sed -n 's/^COSIGN_IMAGE=//p' "$ROOT/infra/supply-chain/signing.env")"
P="keelson_sc_e2e_$$"
REG="${P}_reg"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/keelson_sc_e2e.XXXXXX")"
DOMAIN="demo.example.com"
fail=0
BUILT=()

# shellcheck disable=SC2329  # invoked by the EXIT trap
cleanup() {
  docker rm -fv "$REG" >/dev/null 2>&1 || true
  for i in "${BUILT[@]}"; do docker image rm -f "$i" >/dev/null 2>&1 || true; done
  rm -rf "$WORK"
}
trap cleanup EXIT

result() {  # name ok(0/1) detail
  local ok=true; [ "$2" -eq 0 ] || { ok=false; fail=$((fail + 1)); }
  printf '{"check":"%s","ok":%s,"detail":"%s"}\n' "$1" "$ok" "$(printf '%s' "$3" | tr '\n"' '  ' | tr -d '\134' | cut -c1-300)"
}
cos() {  # cosign in the registry's network namespace (localhost:5000 = the registry), WORK mounted at /w
  MSYS_NO_PATHCONV=1 docker run --rm --memory 256m --network "container:$REG" -e COSIGN_PASSWORD= \
    -v "$(host_path "$WORK"):/w" -w /w "$COSIGN_IMAGE" "$@"
}
OFFLINE=(--use-signing-config=false --tlog-upload=false)

# 1. registry + images
# A fixed host port (Docker Desktop's daemon cannot push through a randomly assigned one); retried on clashes.
HOST_PORT=""
for _ in 1 2 3 4 5; do
  port=$((15000 + RANDOM % 5000))
  if MSYS_NO_PATHCONV=1 docker run -d --name "$REG" --memory 128m -p "127.0.0.1:${port}:5000" "$REGISTRY_IMAGE" >/dev/null 2>&1; then
    HOST_PORT="$port"; break
  fi
  docker rm -fv "$REG" >/dev/null 2>&1 || true
done
[ -n "$HOST_PORT" ] || { echo "could not start the registry" >&2; exit 99; }
for _ in $(seq 1 30); do curl -fsS "http://127.0.0.1:${HOST_PORT}/v2/" >/dev/null 2>&1 && break; sleep 1; done
declare -A REF=()
for n in a b c; do
  mkdir -p "$WORK/ctx-$n" && printf 'keelson supply-chain e2e image %s %s\n' "$n" "$P" >"$WORK/ctx-$n/id.txt"
  printf 'FROM scratch\nCOPY id.txt /id.txt\n' >"$WORK/ctx-$n/Dockerfile"
  tag="127.0.0.1:${HOST_PORT}/keelson_sc_e2e/$n:1"  # 127.0.0.0/8 is a plain-http registry by default
  BUILT+=("$tag")
  if ! docker build -q -t "$tag" "$(host_path "$WORK/ctx-$n")" >/dev/null || ! docker push -q "$tag" >/dev/null; then
    echo "build/push $n failed" >&2; exit 99
  fi
  digest="$(docker image inspect --format '{{index .RepoDigests 0}}' "$tag")"; digest="${digest##*@}"
  REF[$n]="localhost:5000/keelson_sc_e2e/$n@$digest"
done

# 2. keys, signatures, attestations (key mode, no transparency log)
# (cd + git, not git -C: Git Bash with MSYS_NO_PATHCONV=1 would hand git.exe an unconverted /c/... path)
GIT_SHA="$(cd "$ROOT" && git rev-parse HEAD 2>/dev/null || printf '%040d' 0)"
mkdir -p "$WORK/k1" "$WORK/k2"
for k in k1 k2; do
  cos generate-key-pair --output-key-prefix "$k/cosign" >/dev/null 2>&1 || { echo "key generation failed" >&2; exit 99; }
done
printf '%s\n' '{"spdxVersion":"SPDX-2.3","dataLicense":"CC0-1.0","SPDXID":"SPDXRef-DOCUMENT","name":"e2e","documentNamespace":"https://keelson.invalid/e2e","creationInfo":{"created":"2026-10-10T00:00:00Z","creators":["Tool: e2e"]},"packages":[]}' >"$WORK/sbom.spdx.json"
printf '%s\n' '{"buildDefinition":{"buildType":"https://keelson.invalid/e2e","externalParameters":{}},"runDetails":{"builder":{"id":"https://keelson.invalid/e2e"}}}' >"$WORK/provenance.json"
sign_attest() {  # keydir ref
  cos sign --yes "${OFFLINE[@]}" --key "$1/cosign.key" -a "git_sha=$GIT_SHA" -a "domain=$DOMAIN" "$2" &&
    cos attest --yes "${OFFLINE[@]}" --key "$1/cosign.key" --type spdxjson --predicate sbom.spdx.json "$2" &&
    cos attest --yes "${OFFLINE[@]}" --key "$1/cosign.key" --type slsaprovenance1 --predicate provenance.json "$2"
}
sign_attest k1 "${REF[a]}" >/dev/null 2>&1; result sign_and_attest_a $? "${REF[a]}"
sign_attest k2 "${REF[c]}" >/dev/null 2>&1; result sign_and_attest_c_wrong_key $? "${REF[c]}"

sed -e 's/^SIGNER_MODE=.*/SIGNER_MODE=key/' -e "s#^SIGNER_KEY=.*#SIGNER_KEY=$WORK/k1/cosign.pub#" \
  -e 's#^RELEASE_REGISTRY=.*#RELEASE_REGISTRY=localhost:5000/keelson_sc_e2e#' -e 's/^REQUIRE_PROVENANCE=.*/REQUIRE_PROVENANCE=1/' \
  "$ROOT/infra/supply-chain/signing.env" >"$WORK/signing.env"
export COSIGN_DOCKER=1 COSIGN_DOCKER_NETWORK="container:$REG"
unset COSIGN_BIN

# 3. verify-images.sh
out="$(bash "$ROOT/scripts/supply-chain/verify-images.sh" --signing-env "$WORK/signing.env" -a "git_sha=$GIT_SHA" \
  "${REF[a]}" "${REF[b]}" "${REF[c]}" "localhost:5000/keelson_sc_e2e/a:1" 2>/dev/null)"; rc=$?
statuses="$(printf '%s\n' "$out" | sed -n 's/.*"status":"\([A-Z]*\)".*/\1/p' | tr '\n' ' ')"
[ "$rc" = 3 ] && [ "$statuses" = "PASS FAIL FAIL FAIL " ]; result verify_images_a_only $? "rc=$rc statuses=$statuses"
printf '%s\n' "$out" | grep -q '"image":"localhost:5000/keelson_sc_e2e/a:1".*"signature":"refused"'; result tag_ref_refused $? "a by tag"
printf '%s\n' "$out" | grep -q "\"image\":\"${REF[c]}\".*\"signature\":\"fail\""; result wrong_key_fails $? "c"
bash "$ROOT/scripts/supply-chain/verify-images.sh" --signing-env "$WORK/signing.env" -a "git_sha=$(printf '%040d' 1)" "${REF[a]}" >/dev/null 2>&1; rc=$?
[ "$rc" = 1 ]; result other_git_sha_fails $? "rc=$rc"

# 4. deploy.sh --verify-only on signed / unsigned / tampered release files (fake gcloud records any call)
mkdir -p "$WORK/bin" && printf '#!/bin/sh\necho "gcloud $*" >>"%s/gcloud.log"\nexit 97\n' "$WORK" >"$WORK/bin/gcloud" && chmod +x "$WORK/bin/gcloud"
# every RELEASE_IMAGE_* key deploy.sh and compose-release.sh read (their release maps)
mapfile -t keys < <({ awk '/^RELEASE_IMAGE_MAP=\(/,/\)/' "$ROOT/scripts/cloudrun/deploy.sh"
  awk '/^COMPOSE_RELEASE_MAP=\(/,/^\)/' "$ROOT/scripts/supply-chain/compose-release.sh"; } | grep -o ':[A-Z0-9_]*' | tr -d ':' | sort -u)
write_release() {  # dir ref-for-every-image [ref for SIGNAL_HUB]
  mkdir -p "$1"
  {
    printf 'RELEASE_SCHEMA=keelson.release/1\nRELEASE_REPOSITORY=rahulkar/keelson-supply-chain-recovery\n'
    printf 'RELEASE_GIT_SHA=%s\nRELEASE_REF=refs/heads/main\nRELEASE_RUN_ID=1\n' "$GIT_SHA"
    printf 'RELEASE_CREATED_AT=2026-10-10T00:00:00Z\nRELEASE_DOMAIN=%s\n' "$DOMAIN"
    for k in "${keys[@]}"; do
      if [ "$k" = SIGNAL_HUB ]; then printf 'RELEASE_IMAGE_%s=%s\n' "$k" "${3:-$2}"; else printf 'RELEASE_IMAGE_%s=%s\n' "$k" "$2"; fi
    done
  } >"$1/keelson-release.env"
  cos sign-blob --yes "${OFFLINE[@]}" --key k1/cosign.key --bundle "${1#"$WORK"/}/keelson-release.env.sigstore.json" \
    "${1#"$WORK"/}/keelson-release.env" >/dev/null 2>&1
}
deploy_verify() {  # release file
  PATH="$WORK/bin:$PATH" bash "$ROOT/scripts/cloudrun/deploy.sh" keelson-e2e-123 europe-west1 --domain "$DOMAIN" \
    --env-file "$WORK/none.env" --release "$1" --signing-env "$WORK/signing.env" --verify-only 2>&1
}
write_release "$WORK/good" "${REF[a]}"; result release_signed $? "good"
out="$(deploy_verify "$WORK/good/keelson-release.env")"; rc=$?
[ "$rc" = 0 ] && [[ "$out" == *"verify-only: the release and all"* ]]; result deploy_accepts_signed_release $? "rc=$rc $(printf '%s' "$out" | tail -n 1)"
write_release "$WORK/bad" "${REF[a]}" "${REF[b]}"
out="$(deploy_verify "$WORK/bad/keelson-release.env")"; rc=$?
[ "$rc" = 2 ] && [[ "$out" == *"refusing to deploy unverified image ${REF[b]}"* ]]; result deploy_refuses_unsigned_image $? "rc=$rc $(printf '%s' "$out" | tail -n 1)"
cp -r "$WORK/good" "$WORK/tampered" && printf 'RELEASE_IMAGE_EXTRA=%s\n' "${REF[b]}" >>"$WORK/tampered/keelson-release.env"
out="$(deploy_verify "$WORK/tampered/keelson-release.env")"; rc=$?
[ "$rc" = 2 ] && [[ "$out" == *"refusing release"* ]]; result deploy_refuses_tampered_release $? "rc=$rc $(printf '%s' "$out" | tail -n 1)"
[ ! -e "$WORK/gcloud.log" ]; result no_gcloud_call $? "$(cat "$WORK/gcloud.log" 2>/dev/null | head -n 2)"

# 5. compose-release.sh writes only for the good release
bash "$ROOT/scripts/supply-chain/compose-release.sh" --release "$WORK/good/keelson-release.env" \
  --signing-env "$WORK/signing.env" --out "$WORK/compose.release.yaml" >/dev/null 2>&1; rc=$?
[ "$rc" = 0 ] && grep -q "image: ${REF[a]}" "$WORK/compose.release.yaml"; result compose_release_good $? "rc=$rc"
if docker compose version >/dev/null 2>&1; then  # the override renders with compose.yaml (.env from .env.example)
  mkdir -p "$WORK/proj" && cp "$ROOT/.env.example" "$WORK/proj/.env"
  imgs="$(docker compose --project-directory "$(host_path "$WORK/proj")" -f "$(host_path "$ROOT/compose.yaml")" \
    -f "$(host_path "$WORK/compose.release.yaml")" config --images 2>&1)"; rc=$?
  [ "$rc" = 0 ] && printf '%s\n' "$imgs" | grep -qxF "${REF[a]}" && ! printf '%s\n' "$imgs" | grep -qx 'keelson-engine-api'
  result compose_release_override_renders $? "rc=$rc $(printf '%s' "$imgs" | tail -n 2)"
fi
bash "$ROOT/scripts/supply-chain/compose-release.sh" --release "$WORK/bad/keelson-release.env" \
  --signing-env "$WORK/signing.env" --out "$WORK/compose.bad.yaml" >/dev/null 2>&1; rc=$?
[ "$rc" = 1 ] && [ ! -e "$WORK/compose.bad.yaml" ]; result compose_release_refuses_bad $? "rc=$rc"

echo "e2e_sign_verify: $fail failed check(s)" >&2
exit "$fail"
