import { useCallback, useEffect, useRef, useState } from "react";
import type { BlockAction, BlockRendererProps } from "../types";

/**
 * Runs a block action and tracks which button is pending (spinner + double-submit guard).
 *
 * The guard is a ref, not the `pending` state: two invocations in the same tick (a double
 * click dispatched before React re-renders, Enter + click, …) would both see stale state and
 * raise the decision / case action twice. Errors from `onAction` are swallowed here — the app
 * surfaces API errors (e.g. a toast); the block only clears its pending state.
 */
export function useActionRunner(onAction: BlockRendererProps["onAction"]) {
  const [pending, setPending] = useState<string | null>(null);
  const inFlight = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const run = useCallback(
    async (key: string, action: BlockAction) => {
      if (inFlight.current) return;
      inFlight.current = true;
      setPending(key);
      try {
        await onAction(action);
      } catch {
        // The app surfaces API errors (toast); the block only clears its pending state.
      } finally {
        inFlight.current = false;
        if (mounted.current) setPending(null);
      }
    },
    [onAction],
  );

  return { pending, run };
}
