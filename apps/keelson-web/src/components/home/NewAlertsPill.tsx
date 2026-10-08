"use client";

import { Bell, BellDot } from "lucide-react";
import Link from "next/link";
import { useAlertChannel } from "@/lib/alerts/channel";

/** "N new alerts" text for the Home pill (`null` count = not known yet / unavailable). */
export function newAlertsLabel(count: number | null): string {
  if (count === null) return "Alerts";
  if (count === 0) return "No new alerts";
  return count === 1 ? "1 new alert" : `${count} new alerts`;
}

/**
 * Home's alert pill (M6): live from the tenant alert channel (`AlertChannelProvider` in the app
 * shell). The count is announced politely when it changes.
 */
export function NewAlertsPill() {
  const channel = useAlertChannel();
  const count = channel?.newCount ?? null;
  const hasNew = count !== null && count > 0;
  const Icon = hasNew ? BellDot : Bell;
  return (
    <Link
      href="/alerts"
      data-new-alerts={count ?? undefined}
      className={
        "inline-flex items-center gap-1.5 rounded-pill border px-3 py-1 text-caption font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus " +
        (hasNew ? "border-accent/50 bg-accent-soft text-accent-soft-fg hover:bg-accent-soft/80" : "border-border bg-surface text-fg-muted hover:bg-surface-2")
      }
    >
      <Icon aria-hidden="true" className="size-3.5" />
      <span aria-live="polite">{newAlertsLabel(count)}</span>
    </Link>
  );
}
