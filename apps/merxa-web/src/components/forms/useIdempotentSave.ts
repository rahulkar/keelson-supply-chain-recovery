"use client";

import { useCallback, useRef, useState } from "react";

import { apiFetch } from "@/lib/client-api";
import { idempotencyKeyFor, isRetryable, type PendingAttempt } from "@/lib/manual-change";

type SaveRequest = { method: "POST" | "PATCH"; body?: unknown; ifMatch?: number };

/**
 * Saves through merxa-api with an `Idempotency-Key`. A retry of the identical request after
 * an ambiguous failure (network error, 5xx, request still in flight) reuses the key, so
 * merxa-api replays the first outcome instead of applying the change twice.
 */
export function useIdempotentSave() {
  const pending = useRef<PendingAttempt | null>(null);
  const [busy, setBusy] = useState(false);

  const save = useCallback(async <T,>(path: string, req: SaveRequest): Promise<T> => {
    const identity = { method: req.method, body: req.body ?? null, ifMatch: req.ifMatch ?? null };
    const key = idempotencyKeyFor(pending.current, path, identity);
    setBusy(true);
    try {
      const result = await apiFetch<T>(path, {
        method: req.method,
        body: req.body,
        ifMatch: req.ifMatch,
        idempotencyKey: key,
      });
      pending.current = null;
      return result;
    } catch (err) {
      pending.current = isRetryable(err) ? { path, body: JSON.stringify(identity), key } : null;
      throw err;
    } finally {
      setBusy(false);
    }
  }, []);

  return { save, busy };
}
