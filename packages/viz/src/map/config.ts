import type { BasemapMode } from "./basemap";

/**
 * Network map settings (base map, MapLibre worker). Defaults: the `auto` base map — the
 * self-hosted Protomaps tiles when `/basemap/manifest.json` describes an installed archive, else
 * OpenFreeMap Positron (light) / dark, then the committed Natural Earth outline
 * (docs/contracts/127-m8-basemap.md) — and the worker copied by the app to `/maplibre/` (same
 * origin, strict CSP).
 */
export type NetworkMapConfig = {
  /** Hosted MapLibre style URL per theme (OpenFreeMap by default), used by the `openfreemap` kind. */
  styleUrls: { light: string; dark: string };
  /** `setWorkerUrl` target: maplibre-gl's `dist/maplibre-gl-worker.mjs` (+ its `maplibre-gl-shared.mjs` next to it). */
  workerUrl: string;
  /** Base map mode and the self-hosted manifest (paths inside it are relative to its directory). */
  basemap: { mode: BasemapMode; manifestUrl: string };
};

/** `configureNetworkMap` input: any subset; nested `styleUrls` / `basemap` are merged. */
export type NetworkMapConfigPatch = {
  styleUrls?: Partial<NetworkMapConfig["styleUrls"]>;
  workerUrl?: string;
  basemap?: Partial<NetworkMapConfig["basemap"]>;
};

const DEFAULTS: NetworkMapConfig = {
  styleUrls: {
    light: "https://tiles.openfreemap.org/styles/positron",
    dark: "https://tiles.openfreemap.org/styles/dark",
  },
  workerUrl: "/maplibre/maplibre-gl-worker.mjs",
  basemap: { mode: "auto", manifestUrl: "/basemap/manifest.json" },
};

let current: NetworkMapConfig = DEFAULTS;

/**
 * Override map settings (e.g. another hosted style, a fixed base map mode). Call before the first
 * map renders. A React subtree can override the base map with `NetworkMapSettingsProvider`
 * instead (the web app does, from its runtime env).
 */
export function configureNetworkMap(partial: NetworkMapConfigPatch): void {
  current = {
    styleUrls: { ...current.styleUrls, ...partial.styleUrls },
    workerUrl: partial.workerUrl ?? current.workerUrl,
    basemap: { ...current.basemap, ...partial.basemap },
  };
}

export function networkMapConfig(): NetworkMapConfig {
  return current;
}

export const DEFAULT_NETWORK_MAP_CONFIG: Readonly<NetworkMapConfig> = DEFAULTS;
