#!/usr/bin/env bash
# Static checks of a built container image (CI images job, release.yml before signing; plan §14).
#
#   scripts/supply-chain/check-image.sh [--allow-root] [--require-labels] IMAGE
#
# Nothing in the image is executed: the filesystem is read with `docker create` + `docker export`.
#   user         the image USER is set and is not root (0 / root); --allow-root skips it
#   litellm_pth  no litellm_init.pth anywhere (the payload of the compromised LiteLLM 1.82.7/1.82.8)
#   pth_files    every *.pth under site-packages only lists paths; a line starting with `import` runs
#                code at interpreter start-up and is refused, except the allow-listed
#                distutils-precedence.pth (setuptools) and _virtualenv.pth
#   no_secrets   no .env, *.pem, *.key, id_rsa or id_ed25519 under /app, /src, /repo or /work
#   labels       (--require-labels) org.opencontainers.image.source and .revision are set
# Output: one JSON line per check. Exit status = number of failed checks (2 for usage errors).
set -uo pipefail

usage() { sed -n '2,/^set -uo/p' "${BASH_SOURCE[0]}" | sed '$d' | sed 's/^# \{0,1\}//'; }
ALLOW_ROOT=0; LABELS=0; IMAGE=""
while [ $# -gt 0 ]; do
  case "$1" in
    --allow-root) ALLOW_ROOT=1; shift ;;
    --require-labels) LABELS=1; shift ;;
    -h | --help) usage; exit 0 ;;
    -*) echo "check-image.sh: unknown option $1" >&2; exit 2 ;;
    *) [ -z "$IMAGE" ] || { echo "check-image.sh: one image only" >&2; exit 2; }; IMAGE="$1"; shift ;;
  esac
done
[ -n "$IMAGE" ] || { usage >&2; exit 2; }
command -v docker >/dev/null || { echo "check-image.sh: docker is required" >&2; exit 2; }

PTH_ALLOW=(distutils-precedence.pth _virtualenv.pth)
fail=0
result() {  # check ok(0/1) detail
  local ok=true detail
  [ "$2" -eq 0 ] || { ok=false; fail=$((fail + 1)); }
  detail="$(printf '%s' "$3" | tr '\n"\\' '  /' | cut -c1-300)"
  printf '{"image":"%s","check":"%s","ok":%s,"detail":"%s"}\n' "$IMAGE" "$1" "$ok" "$detail"
}

docker image inspect "$IMAGE" >/dev/null 2>&1 || { echo "check-image.sh: image $IMAGE not found locally" >&2; exit 2; }
WORK="$(mktemp -d)"
CID=""
cleanup() { [ -n "$CID" ] && docker rm -f "$CID" >/dev/null 2>&1; rm -rf "$WORK"; }
trap cleanup EXIT
CID="$(docker create "$IMAGE" /nonexistent 2>/dev/null)" || { echo "check-image.sh: docker create failed" >&2; exit 2; }
docker export "$CID" | tar -tf - 2>/dev/null | sed 's#^\./##; s#^/##' >"$WORK/files" || { echo "check-image.sh: export failed" >&2; exit 2; }

# user
user="$(docker image inspect -f '{{.Config.User}}' "$IMAGE")"
if [ "$ALLOW_ROOT" = 1 ]; then
  result user 0 "skipped (--allow-root); USER=${user:-<unset>}"
else
  case "${user%%:*}" in '' | 0 | root) result user 1 "USER is ${user:-unset (root)}: set a non-root USER" ;; *) result user 0 "USER=$user" ;; esac
fi

# litellm_init.pth
hits="$(grep -E '(^|/)litellm_init\.pth$' "$WORK/files" | head -n 5 | tr '\n' ' ')"
[ -z "$hits" ]; result litellm_pth $? "${hits:-none}"

# executable .pth files
bad=""; n=0
while IFS= read -r f; do
  n=$((n + 1))
  base="$(basename "$f")"
  case " ${PTH_ALLOW[*]} " in *" $base "*) continue ;; esac
  if docker cp "$CID:/$f" - 2>/dev/null | tar -xOf - 2>/dev/null | grep -qE '^[[:space:]]*import[[:space:]]'; then
    bad+="$f "
  fi
done < <(grep -E '(^|/)site-packages/[^/]+\.pth$' "$WORK/files")
[ -z "$bad" ]; result pth_files $? "${bad:-$n .pth file(s), none executable}"

# secrets in application directories
hits="$(grep -E '^(app|src|repo|work)/' "$WORK/files" | grep -v '/site-packages/' |
  grep -E '(^|/)(\.env|id_rsa|id_ed25519)$|\.(pem|key)$' | head -n 5 | tr '\n' ' ')"
[ -z "$hits" ]; result no_secrets $? "${hits:-none}"

if [ "$LABELS" = 1 ]; then
  src="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.source"}}' "$IMAGE" 2>/dev/null)"
  rev="$(docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}}' "$IMAGE" 2>/dev/null)"
  [[ "$rev" =~ ^[0-9a-f]{40}$ ]] && [ -n "$src" ] && [ "$src" != "<no value>" ]
  result labels $? "source=${src:-} revision=${rev:-}"
fi
exit "$fail"
