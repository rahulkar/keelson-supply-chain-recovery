"use client";

import type { ThemeName } from "@keelson/tokens";
import { useSyncExternalStore } from "react";

function currentTheme(): ThemeName {
  if (typeof document === "undefined") return "light";
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

/** Re-render when the app switches theme (`<html data-theme>`), so canvas colours follow it. */
function subscribeTheme(onChange: () => void): () => void {
  if (typeof document === "undefined" || typeof MutationObserver === "undefined") return () => {};
  const mo = new MutationObserver(onChange);
  mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  return () => mo.disconnect();
}

/** The app theme (`<html data-theme>`) for libraries that need explicit colours (ECharts, deck.gl, MapLibre). */
export function useDocumentTheme(): ThemeName {
  return useSyncExternalStore(subscribeTheme, currentTheme, () => "light" as const);
}

const noopSubscribe = () => () => {};

/** `false` during SSR and hydration, `true` once mounted in the browser (client-only canvases). */
export function useIsClient(): boolean {
  return useSyncExternalStore(
    noopSubscribe,
    () => true,
    () => false,
  );
}

/** `prefers-reduced-motion: reduce`, read once (for imperative chart / map options). */
export function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && !!window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
}
