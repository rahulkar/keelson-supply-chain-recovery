"use client";

import type { KpiItem, KpiStripProps } from "@keelson/contracts";
import { Badge, ProvenanceChip, cn } from "@keelson/ui";
import { ArrowDownRight, ArrowUpRight, Gauge } from "lucide-react";
import { formatDelta, formatValue } from "../format";
import type { BlockComponentProps } from "../types";
import { BlockFrame } from "./BlockFrame";

export type KpiTilesProps = {
  items: KpiItem[];
  /** Compact tiles (inside the recommended option card). */
  size?: "md" | "sm" | undefined;
  className?: string | undefined;
};

/**
 * KPI tiles as a description list. Provenance chips appear only on values that are not plain
 * facts (plan §12); preliminary values carry a "Preliminary" marker.
 */
export function KpiTiles({ items, size = "md", className }: KpiTilesProps) {
  // Columns follow the space the tiles actually get (container queries), not the viewport: the
  // same block sits in the narrow thread column next to the open canvas on a wide screen. Four
  // tiles stay 2 × 2 until all four fit in one row (no lone tile on a second row).
  const cols = cn(
    "grid gap-3",
    items.length === 1 ? "grid-cols-1" : "grid-cols-2",
    (items.length === 3 || items.length >= 5) && "@md:grid-cols-3",
    items.length >= 4 && "@2xl:grid-cols-4",
  );
  return (
    <div className={cn("@container", className)}>
      <dl className={cols}>
        {items.map((item) => (
          <div
            key={item.key}
            data-kpi={item.key}
            className={cn("flex min-w-0 flex-col gap-1 rounded-md bg-surface-2", size === "md" ? "px-3.5 py-3" : "px-3 py-2")}
          >
            {/* Labels wrap (never truncated: a cut-off label loses its meaning). */}
            <dt className="text-caption text-pretty break-words text-fg-muted">{item.label}</dt>
            <dd className="flex flex-wrap items-center gap-x-2 gap-y-1">
              <span
                className={cn("font-semibold whitespace-nowrap text-fg tabular-nums", size === "md" ? "text-heading" : "text-body")}
              >
                {formatValue(item.value, item.unit)}
              </span>
              {item.prov !== "fact" ? <ProvenanceChip prov={item.prov} compact={size === "sm"} /> : null}
              {item.preliminary ? (
                <span className="text-caption font-medium text-warning">
                  Preliminary<span className="sr-only">, based on priors</span>
                </span>
              ) : null}
            </dd>
            {item.delta !== null && item.delta !== undefined ? (
              <dd className="flex items-center gap-1 text-caption text-fg-muted tabular-nums">
                {item.delta > 0 ? (
                  <ArrowUpRight aria-hidden="true" className="size-3.5" />
                ) : item.delta < 0 ? (
                  <ArrowDownRight aria-hidden="true" className="size-3.5" />
                ) : null}
                <span>
                  <span className="sr-only">Change: </span>
                  {formatDelta(item.delta, item.unit)}
                </span>
              </dd>
            ) : null}
          </div>
        ))}
      </dl>
    </div>
  );
}

/** `kpi_strip@1` — headline numbers (exposure, units at risk, …). */
export function KpiStrip({ props, envelope }: BlockComponentProps<KpiStripProps>) {
  const anyPreliminary = props.items.some((i) => i.preliminary);
  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle="Key figures"
      icon={<Gauge />}
      showProvenance={false}
      showAsOf
      actions={anyPreliminary ? <Badge tone="warning">Preliminary · based on priors</Badge> : undefined}
    >
      <KpiTiles items={props.items} />
    </BlockFrame>
  );
}
