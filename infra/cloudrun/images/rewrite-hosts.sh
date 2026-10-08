#!/bin/sh
# Rewrite the dev origins (http://<host>.localhost) in config files to the deployment's public
# origins (https://<host>.<PUBLIC_BASE_DOMAIN>). Used by the Cloud Run overlay images for files
# that compose bind-mounts and that hard-code the dev hostnames:
#   infra/tenants.yaml, data/packs/*/pack.yaml       (engine overlay)
#   infra/keycloak/config/*.yaml                     (kc-config overlay)
# Usage: rewrite-hosts.sh <domain> <file|dir>...   (directories: every *.yaml/*.yml inside)
# Fails if the domain is not a plain DNS name or if a ".localhost" origin survives.
set -eu

domain="${1:-}"
[ -n "$domain" ] || { echo "usage: rewrite-hosts.sh <domain> <file|dir>..." >&2; exit 2; }
shift
if ! printf '%s' "$domain" | grep -Eq '^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$'; then
  echo "rewrite-hosts: invalid domain '$domain' (lower-case DNS name with at least one dot)" >&2
  exit 2
fi
case "$domain" in
  localhost | *.localhost) echo "rewrite-hosts: refusing a .localhost domain" >&2; exit 2 ;;
esac

files=""
for p in "$@"; do
  if [ -d "$p" ]; then
    files="$files $(find "$p" -type f \( -name '*.yaml' -o -name '*.yml' \) | sort | tr '\n' ' ')"
  elif [ -f "$p" ]; then
    files="$files $p"
  else
    echo "rewrite-hosts: no such file or directory: $p" >&2
    exit 2
  fi
done

for f in $files; do
  sed -E -i "s#http://(keelson|merxa|auth|gateway|traces)\.localhost#https://\1.${domain}#g" "$f"
done

# shellcheck disable=SC2086  # word splitting of the file list is intended
if left="$(grep -nE '(https?://)?[a-z0-9.-]*\.localhost' $files)"; then
  echo "rewrite-hosts: .localhost references left after rewriting:" >&2
  echo "$left" >&2
  exit 1
fi
echo "rewrite-hosts: rewrote $(echo "$files" | wc -w) file(s) for ${domain}"
