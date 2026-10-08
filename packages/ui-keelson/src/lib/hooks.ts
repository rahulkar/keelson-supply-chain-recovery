import { useCallback, useSyncExternalStore } from "react";

function subscribeMedia(query: string, onChange: () => void): () => void {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return () => {};
  const mql = window.matchMedia(query);
  mql.addEventListener("change", onChange);
  return () => mql.removeEventListener("change", onChange);
}

function readMedia(query: string): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") return false;
  return window.matchMedia(query).matches;
}

/**
 * `true` while the media query matches. SSR / no-matchMedia environments return `serverDefault`
 * on the server snapshot so hydration is deterministic.
 */
export function useMediaQuery(query: string, serverDefault = false): boolean {
  const subscribe = useCallback((cb: () => void) => subscribeMedia(query, cb), [query]);
  return useSyncExternalStore(
    subscribe,
    () => readMedia(query),
    () => serverDefault,
  );
}

/** `prefers-reduced-motion: reduce` (WCAG 2.3.3). */
export function usePrefersReducedMotion(): boolean {
  return useMediaQuery("(prefers-reduced-motion: reduce)", false);
}

/** Breakpoint where the canvas becomes a 55 % side panel instead of a sheet. */
export const CANVAS_SIDE_QUERY = "(min-width: 1280px)";
