"use client";

import { Badge } from "@keelson/ui";
import { useMemo } from "react";
import { groupingRows } from "@/lib/alerts/grouping";
import type { AlertDetailView } from "@/lib/alerts/types";

const TITLE = "Why these reports are grouped";

/**
 * Alert drawer section (M8, `docs/contracts/123` §5): for each member report, how the Signal Hub
 * grouped it (method chip, distance / shared place codes / similarity / differing bits) and the
 * hub's explanation sentence; syndicated near-duplicates sit under their original ("counted once").
 * Hidden when the hub sends no memberships (an older hub). Headlines are reported (untrusted) text:
 * plain text only, labelled as such.
 */
export function GroupingExplainer({ detail }: { detail: Pick<AlertDetailView, "signals" | "memberships"> }) {
  const rows = useMemo(() => groupingRows(detail), [detail]);
  if (rows.length === 0) return null;
  return (
    <section aria-label={TITLE} className="flex flex-col gap-2">
      <h3 className="text-caption font-medium tracking-wide text-fg-subtle uppercase">{TITLE}</h3>
      <ol aria-label="Grouped reports" className="flex flex-col gap-2">
        {rows.map((r) => (
          <li key={r.key} data-method={r.method} className="flex flex-col gap-1 rounded-md border border-border px-3 py-2">
            <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <Badge size="sm" tone={r.tone}>
                {r.label}
              </Badge>
              {r.chips.map((c) => (
                <span key={c} className="font-mono text-caption text-fg-muted">
                  {c}
                </span>
              ))}
            </div>
            <p className="text-ui text-fg">{r.explanation}</p>
            {r.headline !== null ? (
              <p className="text-caption break-words text-fg-muted">
                <span className="font-medium">Reported text:</span> {r.headline}
                {r.source ? ` · ${r.source}` : ""}
              </p>
            ) : null}
            {r.copies.length > 0 ? (
              <p className="text-caption text-fg-muted">
                Also reported by{" "}
                {r.copies.map((c) => (c.bits !== null ? `${c.source} (${c.bits} of 64 bits differ)` : c.source)).join(", ")}: counted once.
              </p>
            ) : null}
          </li>
        ))}
      </ol>
    </section>
  );
}
