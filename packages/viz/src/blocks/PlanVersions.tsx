"use client";

import type { PlanDiffProps, PlanDiffRow, PlanVersionLink, ReplanBanner } from "@keelson/contracts";
import { Badge, Button, type BadgeTone, cn } from "@keelson/ui";
import { BellRing, GitCompareArrows } from "lucide-react";
import { useState } from "react";
import { formatValue } from "../format";
import { NoBreakCodes } from "../lib/noBreak";
import { WhatChanged } from "./Narrative";
import { PlanDiffTable } from "./PlanDiff";

const STATUS_META: Record<PlanVersionLink["status"], { label: string; tone: BadgeTone }> = {
  proposed: { label: "Proposed", tone: "info" },
  committed: { label: "Committed", tone: "success" },
  superseded: { label: "Superseded", tone: "neutral" },
  rolled_back: { label: "Rolled back", tone: "warning" },
};

export function planStatusLabel(status: PlanVersionLink["status"]): string {
  return STATUS_META[status].label;
}

/**
 * The re-plan banner of a signal-driven update (M5, `docs/contracts/95` §4):
 * "New information: sea-air capacity on CGP-UK1-SA cut to 30% — plan updated (v2)", with an
 * optional **Compare** button that opens the plan version switcher.
 */
export function ReplanNotice({
  banner,
  onCompare,
  className,
}: {
  banner: ReplanBanner;
  onCompare?: (() => void) | undefined;
  className?: string | undefined;
}) {
  return (
    <div
      role="note"
      aria-label="Plan updated"
      className={cn("flex flex-col gap-2 rounded-md border border-info/40 bg-info-soft px-3 py-2.5 sm:flex-row sm:items-center", className)}
      data-replan-banner=""
    >
      <p className="flex min-w-0 flex-1 items-start gap-2 text-ui text-fg">
        <BellRing aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-info" />
        <span>
          <NoBreakCodes text={banner.text} />
        </span>
      </p>
      <span className="flex shrink-0 items-center gap-2">
        <Badge tone="info">Plan v{banner.plan_version}</Badge>
        {onCompare ? (
          <Button size="sm" variant="secondary" icon={<GitCompareArrows className="size-4" />} onClick={onCompare}>
            Compare v{banner.previous_plan_version} and v{banner.plan_version}
          </Button>
        ) : null}
      </span>
    </div>
  );
}

/** Row identity across plan versions: the same operation on the same record in the same system. */
function rowKey(r: PlanDiffRow): string {
  return `${r.system}\u0000${r.op}\u0000${r.target_label}`;
}

export type PlanRowChange = { kind: "added" | "removed" | "changed"; row: PlanDiffRow; previous?: PlanDiffRow | undefined };

export type PlanComparison = {
  added: PlanDiffRow[];
  removed: PlanDiffRow[];
  changed: { row: PlanDiffRow; previous: PlanDiffRow }[];
  unchanged: number;
};

/**
 * What changed between two versions' plan diffs (pure; rows matched by system + op + target).
 * A row is `changed` when its before / after text or quantity differ. Rows sharing a key are
 * paired in order.
 *
 * `delta`: the newer version's change list is a **delta on top of the previous version's applied
 * changes** (a re-plan after a commit, `95` §4: "a delta change set containing only the changed
 * lines"). The previous rows are already in effect, so none of them is "no longer in the plan":
 * `removed` stays empty, and every row of the delta is a change still to make (`added`, or
 * `changed` when it adjusts an applied change).
 */
export function comparePlanDiffs(
  previous: readonly PlanDiffRow[],
  next: readonly PlanDiffRow[],
  options: { delta?: boolean | undefined } = {},
): PlanComparison {
  const pool = new Map<string, PlanDiffRow[]>();
  for (const r of previous) pool.set(rowKey(r), [...(pool.get(rowKey(r)) ?? []), r]);
  const out: PlanComparison = { added: [], removed: [], changed: [], unchanged: 0 };
  for (const r of next) {
    const candidates = pool.get(rowKey(r));
    const match = candidates?.shift();
    if (!match) {
      out.added.push(r);
      continue;
    }
    if (match.after_text !== r.after_text || match.before_text !== r.before_text || (match.qty ?? null) !== (r.qty ?? null)) {
      out.changed.push({ row: r, previous: match });
    } else if (options.delta) {
      // every row of a delta is a change still to make, even one that repeats an applied change
      out.added.push(r);
    } else {
      out.unchanged += 1;
    }
  }
  if (!options.delta) for (const rest of pool.values()) out.removed.push(...rest);
  return out;
}

/**
 * "Against v1: 2 added, 1 changed, 3 no longer in the plan" (or "Same changes as v1"); for a
 * delta on top of applied changes "On top of v1 (already applied): 2 added, 1 changed" (or
 * "Nothing to change on top of v1").
 */
export function comparisonSentence(c: PlanComparison, previousVersion: number, options: { delta?: boolean | undefined } = {}): string {
  const parts = [
    c.added.length ? `${c.added.length} added` : null,
    c.changed.length ? `${c.changed.length} changed` : null,
    !options.delta && c.removed.length ? `${c.removed.length} no longer in the plan` : null,
  ].filter(Boolean);
  if (options.delta) {
    return parts.length ? `On top of v${previousVersion} (already applied): ${parts.join(", ")}` : `Nothing to change on top of v${previousVersion}`;
  }
  return parts.length ? `Against v${previousVersion}: ${parts.join(", ")}` : `Same changes as v${previousVersion}`;
}

/**
 * Whether a version's change list is a delta on top of the version before it: that one was
 * committed (its changes are applied), so the re-plan lists only what changes from there.
 */
export function isDeltaOf(previous: { status: PlanVersionLink["status"] } | undefined): boolean {
  return previous?.status === "committed";
}

/** One plan version as the switcher shows it. */
export type PlanVersionView = {
  version: number;
  label: string;
  status: PlanVersionLink["status"];
  current: boolean;
  /** The version's one-line "What changed" (its narrative), if any. */
  whatChanged?: string | null | undefined;
  /** The version's plan diff (`plan_diff@1` props), if one was emitted. */
  diff?: PlanDiffProps | null | undefined;
};

/**
 * Plan version switcher (v1 ↔ v2): pick a version to see its status, its "What changed" line,
 * how its changes differ from the version before, and its plan diff table.
 */
export function PlanVersionSwitcher({ versions, initialVersion }: { versions: readonly PlanVersionView[]; initialVersion?: number | undefined }) {
  const sorted = [...versions].sort((a, b) => a.version - b.version);
  const fallback = sorted.find((v) => v.current)?.version ?? sorted.at(-1)?.version ?? 1;
  const [selected, setSelected] = useState<number>(initialVersion ?? fallback);
  const index = Math.max(0, sorted.findIndex((v) => v.version === selected));
  const view = sorted[index];
  const previous = index > 0 ? sorted[index - 1] : undefined;
  if (!view) return <p className="text-ui text-fg-muted">No plan versions yet.</p>;
  const delta = isDeltaOf(previous);
  const comparison = previous?.diff && view.diff ? comparePlanDiffs(previous.diff.rows, view.diff.rows, { delta }) : null;

  return (
    <div className="flex flex-col gap-4" data-plan-versions="">
      <div role="group" aria-label="Plan versions" className="flex flex-wrap gap-2">
        {sorted.map((v) => {
          const meta = STATUS_META[v.status];
          const active = v.version === view.version;
          return (
            <button
              key={v.version}
              type="button"
              aria-pressed={active}
              onClick={() => setSelected(v.version)}
              className={cn(
                "inline-flex h-9 items-center gap-2 rounded-pill border px-3 text-ui font-medium focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
                active ? "border-accent bg-accent-soft text-accent-soft-fg" : "border-border-strong text-fg hover:bg-surface-2",
              )}
            >
              {v.label}
              <span className="text-caption font-normal text-fg-muted">
                · {meta.label}
                {v.current ? " (current)" : ""}
              </span>
            </button>
          );
        })}
      </div>

      <section aria-label={`${view.label} details`} className="flex flex-col gap-3">
        <p className="flex flex-wrap items-center gap-2 text-ui">
          <span className="font-semibold text-fg">{view.label}</span>
          <Badge tone={STATUS_META[view.status].tone}>{STATUS_META[view.status].label}</Badge>
          {view.current ? <Badge tone="accent">Current</Badge> : null}
        </p>
        <WhatChanged text={view.whatChanged} />
        {comparison && previous ? (
          <div className="flex flex-col gap-1.5" data-plan-comparison="">
            <p className="text-ui font-medium text-fg">{comparisonSentence(comparison, previous.version, { delta })}</p>
            {comparison.added.length + comparison.changed.length + comparison.removed.length > 0 ? (
              <ul className="flex flex-col gap-1 text-ui">
                {comparison.added.map((r) => (
                  <li key={`a-${r.seq}`} className="flex flex-wrap gap-x-2">
                    <Badge size="sm" tone="success">
                      Added
                    </Badge>
                    <span className="text-fg">
                      <NoBreakCodes text={r.target_label} />
                    </span>
                    <span className="text-fg-muted">{r.after_text}</span>
                  </li>
                ))}
                {comparison.changed.map(({ row, previous: before }) => (
                  <li key={`c-${row.seq}`} className="flex flex-wrap gap-x-2">
                    <Badge size="sm" tone="info">
                      Changed
                    </Badge>
                    <span className="text-fg">
                      <NoBreakCodes text={row.target_label} />
                    </span>
                    <span className="text-fg-muted">
                      {before.after_text} → {row.after_text}
                    </span>
                  </li>
                ))}
                {comparison.removed.map((r) => (
                  <li key={`r-${r.seq}`} className="flex flex-wrap gap-x-2">
                    <Badge size="sm" tone="neutral">
                      Removed
                    </Badge>
                    <span className="text-fg">
                      <NoBreakCodes text={r.target_label} />
                    </span>
                    <span className="text-fg-muted">{r.after_text}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        ) : null}
        {view.diff ? (
          <div className="flex flex-col gap-1.5">
            <p className="text-caption text-fg-muted">
              {formatValue(view.diff.totals.lines, "count")} line{view.diff.totals.lines === 1 ? "" : "s"} ·{" "}
              {formatValue(view.diff.totals.units, "units")} · {formatValue(view.diff.totals.value_cents, "eur_cents")}
            </p>
            <PlanDiffTable rows={view.diff.rows} caption={`Changes in ${view.label}`} />
          </div>
        ) : (
          <p className="text-ui text-fg-muted">No change list was produced for this version.</p>
        )}
      </section>
    </div>
  );
}
