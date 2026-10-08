"use client";

import { useSyncExternalStore } from "react";

/**
 * Shared wall-clock ticker for "time ago" / "applied in the last 10 minutes" logic.
 * Returns null during SSR and hydration (no server/client clock mismatch), then the
 * current time, refreshed every `NOW_TICK_MS`.
 */
export const NOW_TICK_MS = 15_000;

const listeners = new Set<() => void>();
let current = 0;
let timer: ReturnType<typeof setInterval> | null = null;

function tick(): void {
  current = Date.now();
  listeners.forEach((l) => l());
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    current = Date.now();
    timer = setInterval(tick, NOW_TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (listeners.size === 0 && timer !== null) {
      clearInterval(timer);
      timer = null;
    }
  };
}

function getSnapshot(): number {
  if (current === 0) current = Date.now();
  return current;
}

function getServerSnapshot(): number | null {
  return null;
}

export function useNow(): number | null {
  return useSyncExternalStore<number | null>(subscribe, getSnapshot, getServerSnapshot);
}

/** Forces the next snapshot to re-read the clock (e.g. right after new data arrives). */
export function refreshNow(): void {
  tick();
}
