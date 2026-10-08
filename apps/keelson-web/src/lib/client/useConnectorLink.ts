"use client";

/**
 * Whether the **viewer** could approve changes right now: `GET /api/connectors/merxa/link` for
 * the current user (`services/engine/README.md` → "Whose link counts — for the web").
 *
 * An `approval_card`'s own `connect` prop is evaluated for the case's initiator when the card is
 * created; it must never decide whether this viewer sees Approve or "Connect Merxa". The decision
 * API's `412 connect_required` stays the final word (the link can expire after this check).
 *
 * - not `enabled` (no actionable approval card): `unknown`, no request;
 * - first check in flight: `checking`;
 * - checked: `{status, applyReady}`; a failed check reads `unknown` (the server's 412 decides);
 * - re-checked when the tab becomes visible again or the page is restored from the bfcache
 *   (e.g. Back from the Merxa login), keeping the previous answer meanwhile (no flicker).
 *   Returning from the connect flow (`?connected=merxa`) is a fresh page load, so it is checked anew.
 */
import type { ViewerConnection } from "@keelson/viz";
import { useEffect, useState } from "react";
import type { ApiClient } from "../api/client";

const UNKNOWN: ViewerConnection = { status: "unknown" };
const CHECKING: ViewerConnection = { status: "checking" };

export function useConnectorLink(api: ApiClient, slug: "merxa", enabled: boolean): ViewerConnection {
  const [epoch, setEpoch] = useState(0);
  const [result, setResult] = useState<ViewerConnection | null>(null);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    api
      .connectorLink(slug)
      .then((link) => {
        if (!cancelled) setResult({ status: link.status, applyReady: link.applyReady });
      })
      .catch(() => {
        if (!cancelled) setResult(UNKNOWN);
      });
    return () => {
      cancelled = true;
    };
  }, [api, slug, enabled, epoch]);

  useEffect(() => {
    if (!enabled) return;
    const onVisible = () => {
      if (document.visibilityState === "visible") setEpoch((n) => n + 1);
    };
    const onShow = (e: PageTransitionEvent) => {
      if (e.persisted) setEpoch((n) => n + 1);
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("pageshow", onShow);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("pageshow", onShow);
    };
  }, [enabled]);

  if (!enabled) return UNKNOWN;
  return result ?? CHECKING;
}

const CONNECT_ERRORS: Record<string, string> = {
  denied: "The Merxa sign-in was cancelled.",
  invalid_state: "The sign-in link expired. Try connecting again.",
};

/**
 * The connect flow comes back to `return_to?connected=<slug>` or `?connect_error=<code>`. Returns
 * the outcome (for a toast) and the URL without those parameters, or `null` when neither is present.
 */
export function connectReturnOutcome(
  href: string,
): { outcome: { ok: true; connector: string } | { ok: false; message: string }; cleanUrl: string } | null {
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return null;
  }
  const connected = url.searchParams.get("connected");
  const error = url.searchParams.get("connect_error");
  if (connected === null && error === null) return null;
  url.searchParams.delete("connected");
  url.searchParams.delete("connect_error");
  const cleanUrl = `${url.pathname}${url.search}${url.hash}`;
  if (error !== null) {
    return { outcome: { ok: false, message: CONNECT_ERRORS[error] ?? "Merxa could not be connected. Try again." }, cleanUrl };
  }
  return { outcome: { ok: true, connector: connected === "merxa" ? "Merxa" : "The connector" }, cleanUrl };
}
