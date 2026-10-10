import "server-only";

import { isBasemapMode, type BasemapMode } from "@keelson/viz";

/**
 * Network map base map mode from the runtime env `KEELSON_BASEMAP` (docs/contracts/127-m8-basemap.md):
 * `auto` (default: self-hosted Protomaps when installed, else OpenFreeMap, else the built-in
 * outline), `protomaps` (own origin only), `openfreemap` or `offline`. Read per request by the app
 * layout, so switching needs a restart of keelson-web, not a rebuild. Anything else → `auto`.
 */
export function basemapModeFromEnv(raw: string | undefined = process.env.KEELSON_BASEMAP): BasemapMode {
  const value = raw?.trim().toLowerCase();
  return isBasemapMode(value) ? value : "auto";
}
