#!/usr/bin/env bash
# Resolve container image references to their immutable pinned form `name:tag@sha256:<index digest>`
# (the form scripts/supply-chain/check_pins.py requires everywhere; docs/supply-chain.md).
#
#   scripts/supply-chain/pin-image.sh caddy:2.11.7-alpine python:3.14.8-slim-trixie
#       -> prints one pinned reference per argument
#   scripts/supply-chain/pin-image.sh --verify caddy:2.11.7-alpine@sha256:d854...
#       -> checks that the tag still resolves to the pinned digest ("ok" / "moved <new digest>")
#
# Uses `docker buildx imagetools inspect` (a registry lookup: nothing is pulled, no disk is used).
# The digest is the one the registry serves for the tag: the multi-platform index when there is
# one, so every platform stays pinnable. Always pin an EXACT tag (2.11.7-alpine, not 2-alpine):
# Renovate then proposes version and digest updates as reviewable PRs.
# Exit status = number of references that could not be resolved (or, with --verify, that moved).
set -uo pipefail

usage() { sed -n '2,13p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; }

VERIFY=0
refs=()
for a in "$@"; do
  case "$a" in
    --verify) VERIFY=1 ;;
    -h | --help) usage; exit 0 ;;
    -*) printf 'pin-image.sh: unknown option %s\n' "$a" >&2; exit 2 ;;
    *) refs+=("$a") ;;
  esac
done
[ ${#refs[@]} -gt 0 ] || { usage >&2; exit 2; }
command -v docker >/dev/null || { echo "pin-image.sh: docker is required" >&2; exit 2; }

REF_RE='^[a-z0-9]([a-z0-9._-]*[a-z0-9])?(:[0-9]+)?(/[a-z0-9]([a-z0-9._-]*[a-z0-9])?)*:[A-Za-z0-9_][A-Za-z0-9._-]{0,127}(@sha256:[a-f0-9]{64})?$'
fail=0
for ref in "${refs[@]}"; do
  if ! [[ "$ref" =~ $REF_RE ]]; then
    printf 'pin-image.sh: %s: not name:tag[@sha256:...] (an exact tag is required)\n' "$ref" >&2
    fail=$((fail + 1)); continue
  fi
  name_tag="${ref%@*}"
  pinned=""; [ "$name_tag" != "$ref" ] && pinned="${ref#*@}"
  if ! digest="$(docker buildx imagetools inspect --format '{{.Manifest.Digest}}' "$name_tag" 2>&1)" ||
     ! [[ "$digest" =~ ^sha256:[a-f0-9]{64}$ ]]; then
    printf 'pin-image.sh: %s: %s\n' "$name_tag" "$(printf '%s' "$digest" | tail -n 1)" >&2
    fail=$((fail + 1)); continue
  fi
  if [ "$VERIFY" = 1 ]; then
    if [ -z "$pinned" ]; then
      printf 'unpinned %s -> %s@%s\n' "$name_tag" "$name_tag" "$digest"; fail=$((fail + 1))
    elif [ "$pinned" = "$digest" ]; then
      printf 'ok %s\n' "$ref"
    else
      printf 'moved %s -> %s@%s\n' "$ref" "$name_tag" "$digest"; fail=$((fail + 1))
    fi
  else
    printf '%s@%s\n' "$name_tag" "$digest"
  fi
done
exit "$fail"
