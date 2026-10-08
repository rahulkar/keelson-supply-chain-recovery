"use client";

import { provenanceMeta, type ProvenanceKind } from "@keelson/tokens";
import {
  Check,
  CheckCheck,
  Database,
  FlaskConical,
  Lightbulb,
  SlidersHorizontal,
  User,
  type LucideIcon,
} from "lucide-react";
import { cn } from "../lib/cn";

/** Contract provenance kinds (`metadata.sc.prov`, block `prov`). `verified` is UI-only. */
export type Prov = Exclude<ProvenanceKind, "verified">;

const icons: Record<ProvenanceKind, LucideIcon> = {
  fact: Database,
  inference: Lightbulb,
  assumption: SlidersHorizontal,
  simulated: FlaskConical,
  decision: User,
  committed: Check,
  verified: CheckCheck,
};

/** Colour + border style per kind (literal class strings so Tailwind can see them). */
const styles: Record<ProvenanceKind, string> = {
  fact: "text-prov-fact bg-prov-fact-soft border border-solid border-prov-fact/60",
  inference: "text-prov-inference bg-prov-inference-soft border border-dashed border-prov-inference",
  assumption: "text-prov-assumption bg-prov-assumption-soft border border-dotted border-prov-assumption",
  simulated: "text-prov-simulated bg-prov-simulated-soft border border-solid border-prov-simulated/70 k-prov-hatched",
  decision: "text-prov-decision bg-prov-decision-soft border border-transparent",
  committed: "text-prov-committed bg-prov-committed-soft border-[3px] border-double border-prov-committed",
  verified: "text-prov-verified bg-prov-verified-soft border-[3px] border-double border-prov-verified",
};

export type ProvenanceChipProps = {
  prov: Prov | "verified";
  /** Committed and read back from the connected system → shown as "Verified" (committed + badge). */
  verified?: boolean | undefined;
  /** Override the visible label (defaults to the kind's label, e.g. "Simulated"). */
  label?: string | undefined;
  size?: "sm" | "md" | undefined;
  /** Icon-only (label kept for screen readers and as tooltip). */
  compact?: boolean | undefined;
  className?: string | undefined;
};

/**
 * Provenance chip: colour + icon + border style (plan §12) — never colour alone.
 * fact solid/database · inference dashed/lightbulb · assumption dotted/sliders ·
 * simulated hatched/flask · decision borderless/person · committed double/check ·
 * verified = committed + badge/double-check.
 */
export function ProvenanceChip({ prov, verified = false, label, size = "sm", compact = false, className }: ProvenanceChipProps) {
  const kind: ProvenanceKind = verified || prov === "verified" ? "verified" : prov;
  const meta = provenanceMeta[kind];
  const Icon = icons[kind];
  const text = label ?? meta.label;
  return (
    <span
      className={cn(
        "relative inline-flex shrink-0 items-center gap-1 rounded-pill font-medium whitespace-nowrap",
        size === "sm" ? "h-5 px-1.5 text-caption" : "h-6 px-2 text-ui",
        compact && (size === "sm" ? "w-5 justify-center px-0" : "w-6 justify-center px-0"),
        styles[kind],
        className,
      )}
      title={`${meta.label}: ${meta.description}`}
      data-prov={kind}
    >
      <Icon aria-hidden="true" className={size === "sm" ? "size-3" : "size-3.5"} strokeWidth={2.25} />
      <span className={compact ? "sr-only" : undefined}>
        <span className="sr-only">Provenance: </span>
        {text}
      </span>
      {kind === "verified" ? (
        <span
          aria-hidden="true"
          data-prov-badge=""
          className="absolute -top-1 -right-1 size-2.5 rounded-pill border-2 border-surface bg-prov-verified"
        />
      ) : null}
    </span>
  );
}
