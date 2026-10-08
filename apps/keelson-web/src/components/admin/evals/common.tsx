"use client";

import type { EvalCaseStatus, EvalMetric, EvalRunMode, EvalRunStatus, EvalsThreshold } from "@keelson/contracts";
import { EVAL_METRIC_META } from "@keelson/contracts";
import { Badge, Table, cn } from "@keelson/ui";
import Link from "next/link";
import type { ReactNode } from "react";
import { CASE_STATUS, MODE_META, OUTCOME_META, RUN_STATUS, metricText, thresholdText, type EvalRun } from "@/lib/admin/evalRuns";
import { linkClass } from "../ConnectorsPage";

export { linkClass };

export const evalsHref = {
  overview: "/admin/evals",
  run: (runId: string) => `/admin/evals/runs/${encodeURIComponent(runId)}`,
  case: (runId: string, caseId: string) => `/admin/evals/runs/${encodeURIComponent(runId)}/cases/${encodeURIComponent(caseId)}`,
  compare: (base: string, head: string) => `/admin/evals/compare?base=${encodeURIComponent(base)}&head=${encodeURIComponent(head)}`,
};

export function RunStatusBadge({ status }: { status: EvalRunStatus }) {
  const m = RUN_STATUS[status];
  return (
    <Badge size="sm" tone={m.tone} dot>
      {m.label}
    </Badge>
  );
}

export function CaseStatusBadge({ status }: { status: EvalCaseStatus | null | undefined }) {
  if (!status) return <span className="text-fg-subtle">—</span>;
  const m = CASE_STATUS[status];
  return (
    <Badge size="sm" tone={m.tone} dot>
      {m.label}
    </Badge>
  );
}

export function ModeBadge({ mode }: { mode: EvalRunMode }) {
  const m = MODE_META[mode];
  return (
    <Badge size="sm" tone={m.tone} variant="outline" title={m.description}>
      {m.label}
    </Badge>
  );
}

/** A grid of label / value tiles (`dl`). */
export function Tiles({ label, items }: { label: string; items: { key: string; label: string; value: ReactNode }[] }) {
  return (
    <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4" aria-label={label}>
      {items.map((t) => (
        <div key={t.key} className="rounded-sm border border-border px-3 py-2">
          <dt className="text-caption text-fg-muted">{t.label}</dt>
          <dd className="text-body font-semibold text-fg tabular-nums">{t.value}</dd>
        </div>
      ))}
    </dl>
  );
}

/** A run's headline numbers. */
export function summaryTiles(run: EvalRun): { key: string; label: string; value: ReactNode }[] {
  const s = run.summary;
  if (!s) return [{ key: "cases", label: "Cases", value: `${run.progress.cases_done} / ${run.progress.cases_total}` }];
  const tiles: { key: string; label: string; value: ReactNode }[] = [
    { key: "passed", label: "Passed", value: `${s.passed} / ${s.cases}` },
    { key: "pass_rate", label: "Pass rate", value: metricText("pass_rate_bp", s.pass_rate_bp) },
    { key: "checks", label: "Checks passed", value: `${s.checks - s.checks_failed} / ${s.checks}` },
    { key: "errors", label: "Errors · skipped", value: `${s.errors} · ${s.skipped + s.cancelled}` },
    { key: "recall", label: "Entity recall", value: metricText("entity_recall_mean_bp", s.entity_recall_mean_bp) },
    { key: "grounding", label: "Grounding (min)", value: metricText("numeric_grounding_min_bp", s.numeric_grounding_min_bp) },
    { key: "trajectory", label: "Trajectory recall", value: metricText("trajectory_recall_mean_bp", s.trajectory_recall_mean_bp) },
    {
      key: "tokens",
      label: s.tokens_source === "gateway" ? "Tokens / case (approx.)" : "Tokens / case",
      value: metricText("tokens_per_case_mean", s.tokens_per_case_mean),
    },
  ];
  if (s.cost_microusd_total != null) tiles.push({ key: "cost", label: "Cost", value: metricText("cost_per_case_mean_microusd", s.cost_microusd_total) });
  if (s.replay_misses) tiles.push({ key: "misses", label: "Replay misses", value: String(s.replay_misses) });
  return tiles;
}

/** Targets (never gates) with the run's outcome per metric. */
export function ThresholdTable({ run }: { run: EvalRun }) {
  const outcomes = new Map(run.threshold_results.map((r) => [r.metric, r]));
  const rows = run.thresholds;
  return (
    <Table<EvalsThreshold>
      caption="Targets"
      rows={rows}
      rowKey={(t) => t.metric}
      empty="This run has no targets."
      rowTone={(t) => (outcomes.get(t.metric)?.outcome === "breach" ? "danger" : undefined)}
      columns={[
        { id: "metric", header: "Metric", rowHeader: true, cell: (t) => EVAL_METRIC_META[t.metric as EvalMetric].label },
        { id: "target", header: "Target", nowrap: true, cell: (t) => thresholdText(t) },
        { id: "actual", header: "Actual", nowrap: true, align: "end", cell: (t) => metricText(t.metric, outcomes.get(t.metric)?.actual) },
        {
          id: "outcome",
          header: "Outcome",
          nowrap: true,
          cell: (t) => {
            const o = OUTCOME_META[outcomes.get(t.metric)?.outcome ?? "not_measured"]!;
            return (
              <Badge size="sm" tone={o.tone}>
                {o.label}
              </Badge>
            );
          },
        },
      ]}
    />
  );
}

/** Model-written text from a result (check details, errors): plain text, wraps, never a link. */
export function PlainText({ children, className }: { children: string | null | undefined; className?: string }) {
  if (!children) return null;
  return <span className={cn("whitespace-pre-wrap break-words", className)}>{children}</span>;
}

export function BackLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link href={href} className={cn(linkClass, "text-ui")}>
      {children}
    </Link>
  );
}
