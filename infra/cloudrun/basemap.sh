# shellcheck shell=bash
# shellcheck disable=SC2034  # BB / KEELSON_BASEMAP are read by render() via ${!var}
# Self-hosted base map additions to scripts/cloudrun/deploy.sh (docs/contracts/127-m8-basemap.md).
#
# Sourced by deploy.sh after its helpers are defined; defines functions only (no side effects when
# sourced). Uses deploy.sh's helpers and variables: gc gcq exists cfg die warn note run,
# PROJECT REGION DRY_RUN ENABLE_BASEMAP BASEMAP_DIR RENDER_VARS.
#
# Hooks in deploy.sh:
#   basemap_init     after the derived names, before any render (validates the flags, sets ${BB},
#                    ${KEELSON_BASEMAP} and the keelson-web build args)
#   basemap_deploy   step 6, before the URL map import (the URL map references the backend bucket)
#
# Cloud Run mapping (compose → GCP), only with --enable-basemap --basemap-dir DIR:
#   `basemap` volume served by Caddy at /basemap/*  →  bucket gs://<project>-keelson-basemap (objects
#   under basemap/, public read: OpenStreetMap-derived map data, fonts and sprites only) behind the
#   backend bucket keelson-basemap-bb with Cloud CDN, routed by the URL map (keelson.<domain>/basemap/*).
#   GCS serves byte ranges (206), so the browser code is identical and the CSP stays 'self'.
#   DIR comes from scripts/basemap/export.sh (a filled local volume). Without the flag the URL map has
#   no /basemap/* rule: keelson-web answers /basemap/manifest.json with 204 and `auto` falls back.

BASEMAP_BUCKET_BACKEND="keelson-basemap-bb"
BASEMAP_IMMUTABLE="public, max-age=31536000, immutable"
BASEMAP_DAILY="public, max-age=86400"
KEELSON_WEB_BUILD_ARGS=()

basemap_init() {
  BB="projects/${PROJECT}/global/backendBuckets"
  BASEMAP_BUCKET="${PROJECT}-keelson-basemap"
  local default_mode=auto
  [ "$ENABLE_BASEMAP" = 1 ] && default_mode=protomaps
  KEELSON_BASEMAP="$(cfg KEELSON_BASEMAP "$default_mode")"
  case "$KEELSON_BASEMAP" in auto | protomaps | openfreemap | offline) ;; *) die "KEELSON_BASEMAP must be auto, protomaps, openfreemap or offline" ;; esac
  local csp
  csp="$(cfg KEELSON_CSP_MAP_ORIGINS "")"
  [ -n "$csp" ] && KEELSON_WEB_BUILD_ARGS=(--build-arg "KEELSON_CSP_MAP_ORIGINS=$csp")
  if [ "$ENABLE_BASEMAP" = 1 ]; then
    if [ -z "$BASEMAP_DIR" ] || [ ! -f "$BASEMAP_DIR/manifest.json" ]; then
      [ "$DRY_RUN" = 1 ] || die "--enable-basemap needs --basemap-dir DIR with manifest.json (scripts/basemap/export.sh DIR)"
      warn "--basemap-dir has no manifest.json; the dry run prints the upload commands anyway"
    fi
  elif [ -n "$BASEMAP_DIR" ]; then
    warn "--basemap-dir is ignored without --enable-basemap"
  fi
  RENDER_VARS+=(BB KEELSON_BASEMAP)
}

basemap_deploy() {
  [ "$ENABLE_BASEMAP" = 1 ] || return 0
  local gs="gs://${BASEMAP_BUCKET}" dir="${BASEMAP_DIR:-./basemap}"
  note "base map: bucket ${gs}/basemap/ + backend bucket ${BASEMAP_BUCKET_BACKEND} (Cloud CDN)"
  gc services enable storage.googleapis.com
  exists gcloud storage buckets describe "$gs" ||
    gc storage buckets create "$gs" --location="$REGION" --uniform-bucket-level-access \
      --no-public-access-prevention --labels=app=keelson,content=basemap
  # Public read: the bucket holds nothing but OpenStreetMap-derived tiles, fonts and sprites.
  gcq storage buckets add-iam-policy-binding "$gs" --member=allUsers --role=roles/storage.objectViewer
  # Archive, glyphs, sprites, licences first (dotfiles = staging never uploaded), manifest.json LAST,
  # then prune archives the new manifest no longer references.
  gc storage rsync "$dir/v4" "$gs/basemap/v4" --recursive --exclude='(^|/)\.' --cache-control="$BASEMAP_IMMUTABLE"
  gc storage rsync "$dir/fonts" "$gs/basemap/fonts" --recursive --delete-unmatched-destination-objects \
    --exclude='(^|/)\.' --cache-control="$BASEMAP_DAILY" --content-type=application/x-protobuf
  gc storage rsync "$dir/sprites" "$gs/basemap/sprites" --recursive --delete-unmatched-destination-objects \
    --exclude='(^|/)\.' --cache-control="$BASEMAP_DAILY"
  gc storage rsync "$dir/LICENSES" "$gs/basemap/LICENSES" --recursive --delete-unmatched-destination-objects \
    --exclude='(^|/)\.' --cache-control="$BASEMAP_DAILY"
  gc storage cp "$dir/manifest.json" "$gs/basemap/manifest.json" --cache-control=no-cache --content-type=application/json
  gc storage rsync "$dir/v4" "$gs/basemap/v4" --recursive --delete-unmatched-destination-objects \
    --exclude='(^|/)\.' --cache-control="$BASEMAP_IMMUTABLE"
  if ! exists gcloud compute backend-buckets describe "$BASEMAP_BUCKET_BACKEND"; then
    gc compute backend-buckets create "$BASEMAP_BUCKET_BACKEND" --gcs-bucket-name="$BASEMAP_BUCKET" \
      --enable-cdn --cache-mode=USE_ORIGIN_HEADERS \
      --custom-response-header='X-Content-Type-Options: nosniff' \
      --custom-response-header='Cross-Origin-Resource-Policy: same-origin'
  fi
}
