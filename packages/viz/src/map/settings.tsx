"use client";

import { createContext, useContext, useMemo, type ReactNode } from "react";
import type { BasemapMode } from "./basemap";
import { networkMapConfig, type NetworkMapConfig } from "./config";

type BasemapSettings = NetworkMapConfig["basemap"];

const NetworkMapSettingsContext = createContext<Partial<BasemapSettings> | null>(null);

/**
 * Per-subtree base map settings over `configureNetworkMap` — the web app wraps its pages with the
 * mode from its runtime env (`KEELSON_BASEMAP`), so switching needs a restart, not a rebuild.
 */
export function NetworkMapSettingsProvider({
  basemap,
  children,
}: {
  basemap?: { mode?: BasemapMode; manifestUrl?: string };
  children: ReactNode;
}) {
  const mode = basemap?.mode;
  const manifestUrl = basemap?.manifestUrl;
  const value = useMemo<Partial<BasemapSettings>>(
    () => ({ ...(mode ? { mode } : {}), ...(manifestUrl ? { manifestUrl } : {}) }),
    [mode, manifestUrl],
  );
  return <NetworkMapSettingsContext.Provider value={value}>{children}</NetworkMapSettingsContext.Provider>;
}

/** Effective base map settings: the nearest provider over the module config (`configureNetworkMap`). */
export function useNetworkMapSettings(): BasemapSettings {
  const override = useContext(NetworkMapSettingsContext);
  const base = networkMapConfig().basemap;
  return useMemo(
    () => ({ mode: override?.mode ?? base.mode, manifestUrl: override?.manifestUrl ?? base.manifestUrl }),
    [override, base.mode, base.manifestUrl],
  );
}
