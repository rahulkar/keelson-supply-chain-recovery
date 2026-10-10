"use client";

import { EVAL_METRIC_META } from "@keelson/contracts";
import { Tabs, cn } from "@keelson/ui";
import { TrendChart } from "@keelson/viz";
import Link from "next/link";
import { useState } from "react";
import { CASE_STATUS, TREND_METRICS, metricText, runLabel, trendSeries, type EvalTrendsData } from "@/lib/admin/evalRuns";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AdminSection, ResourceView } from "../common";
import { evalsHref } from "./common";

type Tab = "all" | "replay" | "live";

const TABS: { id: Tab; label: string }[] = [
  { id: "all", label: "All runs" },
  { id: "replay", label: "Replay" },
  { id: "live", label: "Live" },
];

/** Strip of the last runs per case (status letters with text, colour never alone). */
const STRIP_RUNS = 10;
const CELL: Record<string, string> = {
  pass: "bg-success-soft text-success",
  fail: "bg-danger-soft text-danger",
  error: "bg-warning-soft text-warning",
  skipped: "bg-surface-2 text-fg-muted",
  cancelled: "bg-surface-2 text-fg-muted",
};

function CaseStrip({ trends }: { trends: EvalTrendsData }) {
  const runs = trends.runs.slice(-STRIP_RUNS);
  const offset = trends.runs.length - runs.length;
  if (!trends.cases.length) return null;
  return (
    <div role="group" aria-label="Case results over the last runs" tabIndex={0} className="overflow-x-auto rounded-md border border-border">
      <table className="w-full text-caption">
        <caption className="sr-only">Case results over the last runs</caption>
        <thead>
          <tr className="border-b border-border">
            <th scope="col" className="px-3 py-1.5 text-left font-medium text-fg-muted">
              Case
            </th>
            {runs.map((r) => (
              <th key={r.id} scope="col" className="px-1 py-1.5 font-normal text-fg-subtle">
                <Link href={evalsHref.run(r.id)} className="hover:underline">
                  {runLabel(r.at)}
                </Link>
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {trends.cases.map((c) => (
            <tr key={c.case_id} className="border-b border-border last:border-0">
              <th scope="row" className="px-3 py-1 text-left font-mono font-normal text-fg">
                {c.case_id}
              </th>
              {c.statuses.slice(offset).map((st, i) => (
                <td key={runs[i]?.id ?? i} className="px-1 py-1 text-center">
                  {st ? (
                    <span className={cn("inline-block min-w-12 rounded-sm px-1.5 py-0.5 font-medium", CELL[st])}>{CASE_STATUS[st].label}</span>
                  ) : (
                    <span className="text-fg-subtle">–</span>
                  )}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** Small multiples of the run metrics (with their targets) and the per-case status strip. */
export function EvalTrends({ refreshKey }: { refreshKey: string }) {
  const admin = useAdminApi();
  const [tab, setTab] = useState<Tab>("all");
  const trends = useResource(() => admin.evalTrends(tab), `eval-trends:${tab}:${refreshKey}`);
  return (
    <AdminSection id="eval-trends" title="Trends" description="Targets are dashed. They are goals to track, never gates.">
      <Tabs tabs={TABS} value={tab} onValueChange={(v) => setTab(v as Tab)} label="Runs shown">
        <ResourceView resource={trends} label="trends">
          {(t) =>
            t.runs.length < 2 ? (
              <p className="py-2 text-ui text-fg-muted">Trends appear once two runs of this kind have finished.</p>
            ) : (
              <div className="flex flex-col gap-4 pt-3">
                <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
                  {TREND_METRICS.map((metric) => {
                    const { points, threshold } = trendSeries(t, metric, tab);
                    return (
                      <TrendChart
                        key={metric}
                        caption={EVAL_METRIC_META[metric].label}
                        data={points}
                        format={(v) => metricText(metric, Math.round(v))}
                        threshold={threshold}
                      />
                    );
                  })}
                </div>
                {t.runs.some((r) => r.tokens_source === "gateway") ? (
                  <p className="text-caption text-fg-muted">
                    Token counts of imported command-line runs are approximate (the gateway&apos;s usage around each case).
                  </p>
                ) : null}
                <CaseStrip trends={t} />
              </div>
            )
          }
        </ResourceView>
      </Tabs>
    </AdminSection>
  );
}
