/**
 * Network map settings (base map styles, MapLibre worker). Defaults: OpenFreeMap Positron (light)
 * and its dark variant, worker copied by the app to `/maplibre/` (same origin, strict CSP).
 */
export type NetworkMapConfig = {
  /** MapLibre style URL per theme. */
  styleUrls: { light: string; dark: string };
  /** `setWorkerUrl` target: maplibre-gl's `dist/maplibre-gl-worker.mjs` (+ its `maplibre-gl-shared.mjs` next to it). */
  workerUrl: string;
};

const DEFAULTS: NetworkMapConfig = {
  styleUrls: {
    light: "https://tiles.openfreemap.org/styles/positron",
    dark: "https://tiles.openfreemap.org/styles/dark",
  },
  workerUrl: "/maplibre/maplibre-gl-worker.mjs",
};

let current: NetworkMapConfig = DEFAULTS;

/** Override map settings (e.g. self-hosted tiles). Call before the first map renders. */
export function configureNetworkMap(partial: Partial<NetworkMapConfig>): void {
  current = { ...current, ...partial, styleUrls: { ...current.styleUrls, ...partial.styleUrls } };
}

export function networkMapConfig(): NetworkMapConfig {
  return current;
}

export const DEFAULT_NETWORK_MAP_CONFIG: Readonly<NetworkMapConfig> = DEFAULTS;
