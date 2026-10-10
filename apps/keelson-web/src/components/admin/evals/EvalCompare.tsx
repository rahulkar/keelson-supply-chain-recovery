"use client";

import { EVAL_METRICS, EVAL_METRIC_META, type EvalsCompare } from "@keelson/contracts";
import { Badge, Field, Select, Table } from "@keelson/ui";
import { useRouter } from "next/navigation";
import { CHANGE_META, deltaText, metricText, runLabel, type EvalRun } from "@/lib/admin/evalRuns";
import { when } from "@/lib/admin/format";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AdminPageHeader, AdminSection, ResourceView } from "../common";
import { BackLink, CaseStatusBadge, ModeBadge, Tiles, evalsHref } from "./common";

type CaseCmp = EvalsCompare["cases"][number];

const CASE_DELTAS: { key: string; label: string }[] = [
  { key: "entity_recall_bp", label: "Recall" },
  { key: "numeric_grounding_bp", label: "Grounding" },
  { key: "trajectory_recall_bp", label: "Trajectory" },
  { key: "tokens", label: "Tokens" },
];

function deltaTone(better: boolean | null | undefined): "success" | "danger" | "neutral" {
  return better === true ? "success" : better === false ? "danger" : "neutral";
}

function RunPicker({ label, value, runs, onChange }: { label: string; value: string; runs: readonly EvalRun[]; onChange: (id: string) => void }) {
  const options = runs.filter((r) => r.summary).map((r) => ({ value: r.id, label: `${runLabel(r.started_at ?? r.created_at)} · ${r.mode}${r.source === "import" ? " · import" : ""}` }));
  if (!options.some((o) => o.value === value)) options.unshift({ value, label: "This run" });
  return (
    <Field label={label}>
      <Select value={value} onChange={onChange} options={options} />
    </Field>
  );
}

/** Head against base: delta tiles (toned by each metric's direction) and every case's change. */
export function EvalCompareView({ base, head }: { base: string; head: string }) {
  const admin = useAdminApi();
  const router = useRouter();
  const cmp = useResource(() => admin.compareEvalRuns(base, head), `eval-compare:${base}:${head}`);
  const runs = useResource(() => admin.evalRuns({ limit: 100 }), "eval-runs-compare");
  return (
    <>
      <BackLink href={evalsHref.overview}>← All eval runs</BackLink>
      <AdminPageHeader title="Compare eval runs" description="What changed from the base run to the head run. Targets are goals, never gates." />
      <AdminSection id="eval-compare-pick" title="Runs">
        <div className="grid gap-3 sm:grid-cols-2">
          <RunPicker label="Base (before)" value={base} runs={runs.data?.items ?? []} onChange={(id) => router.push(evalsHref.compare(id, head))} />
          <RunPicker label="Head (after)" value={head} runs={runs.data?.items ?? []} onChange={(id) => router.push(evalsHref.compare(base, id))} />
        </div>
      </AdminSection>
      <ResourceView resource={cmp} label="the comparison">
        {(c) => (
          <>
            <AdminSection id="eval-compare-summary" title="Summary">
              <p className="flex flex-wrap items-center gap-2 text-caption text-fg-muted">
                Base {when(c.base.started_at ?? c.base.created_at)} <ModeBadge mode={c.base.mode} /> → head {when(c.head.started_at ?? c.head.created_at)} <ModeBadge mode={c.head.mode} />
              </p>
              <Tiles
                label="Metric deltas"
                items={EVAL_METRICS.map((m) => {
                  const d = c.summary_deltas[m];
                  return {
                    key: m,
                    label: EVAL_METRIC_META[m].label,
                    value: (
                      <span className="flex flex-col">
                        <span>
                          {metricText(m, d?.base)} → {metricText(m, d?.head)}
                        </span>
                        <Badge size="sm" tone={deltaTone(d?.better)}>
                          {deltaText(m, d?.delta)}
                          {d?.better === true ? " (better)" : d?.better === false ? " (worse)" : ""}
                        </Badge>
                      </span>
                    ),
                  };
                })}
              />
            </AdminSection>
            <AdminSection id="eval-compare-cases" title="Cases">
              <Table<CaseCmp>
                caption="Case changes"
                hideCaption
                rows={c.cases}
                rowKey={(x) => x.case_id}
                rowTone={(x) => (x.change === "regressed" ? "danger" : x.change === "fixed" ? "accent" : undefined)}
                columns={[
                  {
                    id: "case",
                    header: "Case",
                    rowHeader: true,
                    cell: (x) => (
                      <span className="flex flex-col">
                        <span className="font-medium">{x.title}</span>
                        <span className="font-mono text-caption text-fg-muted">{x.case_id}</span>
                      </span>
                    ),
                  },
                  {
                    id: "change",
                    header: "Change",
                    nowrap: true,
                    cell: (x) => (
                      <Badge size="sm" tone={CHANGE_META[x.change]?.tone ?? "neutral"} dot>
                        {CHANGE_META[x.change]?.label ?? x.change}
                      </Badge>
                    ),
                  },
                  {
                    id: "status",
                    header: "Base → head",
                    nowrap: true,
                    cell: (x) => (
                      <span className="flex items-center gap-1">
                        <CaseStatusBadge status={x.base_status} /> → <CaseStatusBadge status={x.head_status} />
                      </span>
                    ),
                  },
                  ...CASE_DELTAS.map((d) => ({
                    id: d.key,
                    header: d.label,
                    nowrap: true,
                    align: "end" as const,
                    cell: (x: CaseCmp) => {
                      const v = x.metric_deltas[d.key];
                      if (!v || v.delta === null) return "—";
                      return <span className={v.better === false ? "text-danger" : v.better ? "text-success" : undefined}>{deltaText(d.key, v.delta)}</span>;
                    },
                  })),
                  {
                    id: "checks",
                    header: "Checks",
                    cell: (x) =>
                      x.checks_newly_failed.length || x.checks_newly_passed.length ? (
                        <details>
                          <summary className="cursor-pointer text-caption font-medium text-accent">
                            {x.checks_newly_failed.length} newly failing · {x.checks_newly_passed.length} newly passing
                          </summary>
                          <ul className="mt-1 flex flex-col gap-0.5 font-mono text-caption">
                            {x.checks_newly_failed.map((n) => (
                              <li key={`f-${n}`} className="text-danger">
                                ✗ {n}
                              </li>
                            ))}
                            {x.checks_newly_passed.map((n) => (
                              <li key={`p-${n}`} className="text-success">
                                ✓ {n}
                              </li>
                            ))}
                          </ul>
                        </details>
                      ) : (
                        <span className="text-fg-subtle">—</span>
                      ),
                  },
                ]}
              />
            </AdminSection>
          </>
        )}
      </ResourceView>
    </>
  );
}
