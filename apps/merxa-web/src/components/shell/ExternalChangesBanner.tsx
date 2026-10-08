"use client";

import { IconRefresh, IconTransferIn, IconX } from "@tabler/icons-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { attributionText, EXTERNAL_CHANGE_WINDOW_MS, recentlyApplied } from "@/lib/attribution";
import { apiFetch } from "@/lib/client-api";
import { formatRelative } from "@/lib/format";
import type { ChangeSetSummary, Page } from "@/lib/types";
import { refreshNow, useNow } from "@/lib/use-now";

export const POLL_INTERVAL_MS = 20_000;
const DISMISS_KEY = "merxa.dismissedExternalChanges";

function readDismissed(): Set<string> {
  if (typeof window === "undefined") return new Set();
  try {
    const raw = window.sessionStorage.getItem(DISMISS_KEY);
    return new Set(raw ? (JSON.parse(raw) as string[]) : []);
  } catch {
    return new Set();
  }
}

function writeDismissed(codes: Set<string>): void {
  try {
    window.sessionStorage.setItem(DISMISS_KEY, JSON.stringify([...codes]));
  } catch {
    // storage unavailable (private mode): the dismissal lasts for this page only
  }
}

/**
 * Global "External changes received" notice: shown while any change batch was applied in
 * the last 10 minutes. Polls `GET /v1/change-sets?status=applied` (same origin) while the
 * tab is visible.
 */
export function ExternalChangesBanner({
  initial,
  pollMs = POLL_INTERVAL_MS,
}: {
  initial: ChangeSetSummary[];
  pollMs?: number;
}) {
  const router = useRouter();
  const now = useNow(); // null until hydrated: no SSR/clock mismatch
  const [items, setItems] = useState<ChangeSetSummary[]>(initial);
  const [dismissed, setDismissed] = useState<Set<string>>(readDismissed);
  const seen = useRef<Set<string>>(new Set(initial.map((c) => c.code)));
  const [hasNew, setHasNew] = useState(false);

  const poll = useCallback(async () => {
    try {
      const page = await apiFetch<Page<ChangeSetSummary>>("/v1/change-sets", { query: { status: "applied", limit: 25 } });
      setItems(page.items);
      if (page.items.some((c) => !seen.current.has(c.code))) {
        page.items.forEach((c) => seen.current.add(c.code));
        setHasNew(true);
      }
      refreshNow();
    } catch {
      // Transient errors keep the last known state; the next poll retries.
    }
  }, []);

  useEffect(() => {
    let timer: ReturnType<typeof setInterval> | null = null;
    const start = () => {
      if (timer === null) timer = setInterval(() => void poll(), pollMs);
    };
    const stop = () => {
      if (timer !== null) clearInterval(timer);
      timer = null;
    };
    const onVisibility = () => {
      if (document.visibilityState === "visible") {
        void poll();
        start();
      } else {
        stop();
      }
    };
    if (document.visibilityState !== "hidden") start();
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      stop();
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, [poll, pollMs]);

  const recent = useMemo(
    () => (now === null ? [] : recentlyApplied(items, now, EXTERNAL_CHANGE_WINDOW_MS).filter((c) => !dismissed.has(c.code))),
    [items, now, dismissed],
  );

  if (now === null || recent.length === 0) return null;

  const dismiss = () => {
    const next = new Set(dismissed);
    recent.forEach((c) => next.add(c.code));
    setDismissed(next);
    writeDismissed(next);
  };
  const reload = () => {
    setHasNew(false);
    router.refresh();
  };

  const [first] = recent;
  return (
    <div className="mx-external-strip">
      <div className="mx-banner mx-banner--external" role="status" aria-live="polite" data-testid="external-changes-banner">
        <span className="mx-banner__icon" aria-hidden="true">
          <IconTransferIn size={18} />
        </span>
        <div className="mx-banner__body">
          <div className="mx-banner__title">
            {recent.length === 1
              ? "External changes received"
              : `External changes received: ${recent.length} change batches in the last 10 minutes`}
          </div>
          {recent.length === 1 && first ? (
            <div>
              <Link href={`/change-batches/${encodeURIComponent(first.code)}`} className="mx-mono">
                {first.code}
              </Link>{" "}
              {first.title} · {attributionText(first)} · applied {formatRelative(first.applied_at, now)}
            </div>
          ) : (
            <ul>
              {recent.slice(0, 4).map((c) => (
                <li key={c.code}>
                  <Link href={`/change-batches/${encodeURIComponent(c.code)}`} className="mx-mono">
                    {c.code}
                  </Link>{" "}
                  {c.title} · {attributionText(c)} · {formatRelative(c.applied_at, now)}
                </li>
              ))}
              {recent.length > 4 && <li>and {recent.length - 4} more…</li>}
            </ul>
          )}
        </div>
        <div className="mx-banner__actions">
          {hasNew && (
            <button type="button" className="mx-btn mx-btn--sm" onClick={reload}>
              <IconRefresh size={14} aria-hidden="true" /> Reload data
            </button>
          )}
          <Link href="/change-batches?status=applied" className="mx-btn mx-btn--sm">
            View batches
          </Link>
          <button type="button" className="mx-btn mx-btn--sm mx-btn--ghost" onClick={dismiss} aria-label="Dismiss notice">
            <IconX size={14} aria-hidden="true" />
          </button>
        </div>
      </div>
    </div>
  );
}
