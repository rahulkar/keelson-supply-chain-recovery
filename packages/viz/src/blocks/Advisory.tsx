"use client";

import { Badge } from "@keelson/ui";
import { Info } from "lucide-react";
import { formatDateTime } from "../format";

/**
 * M7 (`docs/contracts/110` §4): plans built on a **crawled** snapshot (tier-A connector, data
 * gathered by paging the vendor's read tools, not one consistent export) are advisory. The
 * engine sets `advisory` on `narrative@1`, `recommended_option@1` and `scenario_compare@1`.
 */
export type AdvisoryLabel = {
  reason: "crawled_snapshot";
  text: string;
  as_of_range: { from: string; to: string };
};

/** The contract's fixed sentence (contracts-py `scn.CRAWLED_ADVISORY_TEXT`), used when the engine sent none. */
export const ADVISORY_FALLBACK_TEXT = "Data gathered by paging, not a consistent snapshot";

/** "Data read between 5 Oct 2026, 09:12 UTC and 5 Oct 2026, 09:20 UTC" (one time when both ends are equal). */
export function advisoryRangeText(range: AdvisoryLabel["as_of_range"]): string {
  const from = formatDateTime(range.from);
  const to = formatDateTime(range.to);
  return from === to ? `Data read at ${from}` : `Data read between ${from} and ${to}`;
}

/** Short badge for card headers ("Advisory"); the text says why, colour never stands alone. */
export function AdvisoryBadge({ advisory }: { advisory: AdvisoryLabel | null | undefined }) {
  if (!advisory) return null;
  return (
    <Badge tone="warning" variant="outline" size="sm" icon={<Info />} title={`${advisory.text || ADVISORY_FALLBACK_TEXT}. ${advisoryRangeText(advisory.as_of_range)}.`}>
      Advisory
    </Badge>
  );
}

/** The advisory sentence with the as-of range, shown in the block body (readable without hovering). */
export function AdvisoryNote({ advisory, className }: { advisory: AdvisoryLabel | null | undefined; className?: string | undefined }) {
  if (!advisory) return null;
  const text = advisory.text.trim() || ADVISORY_FALLBACK_TEXT;
  return (
    <p
      role="note"
      data-advisory=""
      className={`flex items-start gap-2 rounded-sm border border-warning/40 bg-warning-soft px-3 py-2 text-ui text-fg ${className ?? ""}`.trim()}
    >
      <Info aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-warning" />
      <span>
        <span className="font-medium">Advisory: </span>
        {/[.!?]$/.test(text) ? text : `${text}.`} {advisoryRangeText(advisory.as_of_range)}.
      </span>
    </p>
  );
}
