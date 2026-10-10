"use client";

import { useCallback, useEffect, useState } from "react";
import {
  basemapChain,
  loadBasemapManifest,
  needsManifest,
  type BasemapKind,
  type BasemapMode,
  type InstalledBasemap,
} from "./basemap";

export type BasemapState = {
  /** What to draw now; `null` while the manifest is being resolved (show a skeleton). */
  kind: BasemapKind | null;
  /** The installed self-hosted base map (manifest + base URL) when the manifest is valid. */
  installed: InstalledBasemap | null;
  /** The drawn kind could not be loaded: move to the next one in the chain (ignored for any other kind). */
  fail: (kind: BasemapKind) => void;
};

type Resolved = { key: string; installed: InstalledBasemap | null };

/**
 * The base map fallback chain of one map (docs/contracts/127-m8-basemap.md §2.1). Resolves the
 * manifest only for `auto` / `protomaps` and only when `enabled` (a map will actually be drawn);
 * each `fail(kind)` steps to the next kind; `plain` is final.
 */
export function useBasemap({ mode, manifestUrl }: { mode: BasemapMode; manifestUrl: string }, enabled: boolean): BasemapState {
  const key = `${mode} ${manifestUrl}`;
  const wantsManifest = needsManifest(mode);
  const [resolved, setResolved] = useState<Resolved | null>(null);
  const [failed, setFailed] = useState<{ key: string; kinds: readonly BasemapKind[] }>({ key, kinds: [] });

  useEffect(() => {
    if (!enabled || !wantsManifest) return;
    let live = true;
    void loadBasemapManifest(manifestUrl).then((installed) => {
      if (live) setResolved({ key: `${mode} ${manifestUrl}`, installed });
    });
    return () => {
      live = false;
    };
  }, [enabled, wantsManifest, mode, manifestUrl]);

  const ready = !wantsManifest || resolved?.key === key;
  const installed = wantsManifest && resolved?.key === key ? resolved.installed : null;
  const failedKinds = failed.key === key ? failed.kinds : [];
  const chain = basemapChain(mode, installed !== null);
  const kind = ready ? (chain.find((k) => !failedKinds.includes(k)) ?? "plain") : null;

  const fail = useCallback(
    (k: BasemapKind) => {
      if (k === "plain") return;
      setFailed((prev) => {
        const kinds = prev.key === key ? prev.kinds : [];
        return kinds.includes(k) ? prev : { key, kinds: [...kinds, k] };
      });
    },
    [key],
  );

  return { kind, installed, fail };
}
