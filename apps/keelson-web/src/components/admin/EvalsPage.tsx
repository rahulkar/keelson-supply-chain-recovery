"use client";

import { Badge, Button, EmptyState, Field, Select, Table } from "@keelson/ui";
import { FlaskConical, RotateCw } from "lucide-react";
import { useState } from "react";
import { EVAL_STATUS, caseScore, metricLabel, metricText, scoreKeys, summaryTiles, type EvalCase, type EvalRun } from "@/lib/admin/evals";
import { when } from "@/lib/admin/format";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AdminPageHeader, AdminSection, ResourceView } from "./common";

function CaseDetails({ c }: { c: EvalCase }) {
  const score = caseScore(c);
  if (!score.failed.length && !score.soft.length && !c.error) {
    // The engine sends counts only: the names of failed checks are in the results file.
    return score.total - score.passed > 0 ? <span className="text-caption text-danger">{score.total - score.passed} failed</span> : <span className="text-fg-subtle">—</span>;
  }
  return (
    <details>
      <summary className="cursor-pointer text-caption font-medium text-accent">
        {score.failed.length ? `${score.failed.length} failed` : c.error ? "Error" : `${score.soft.length} soft`}
      </summary>
      <ul className="mt-1 flex flex-col gap-1 text-caption">
        {c.error ? <li className="text-danger">{c.error}</li> : null}
        {[...score.failed, ...score.soft].map((k, i) => (
          <li key={`${k.name}-${i}`}>
            <span className="font-mono">{k.name}</span>
            {k.soft ? " (soft)" : ""}
            {k.detail ? <span className="text-fg-muted">: {k.detail}</span> : null}
          </li>
        ))}
      </ul>
    </details>
  );
}

function RunView({ run }: { run: EvalRun }) {
  const tiles = summaryTiles(run);
  const metrics = scoreKeys(run);
  const passed = (c: EvalCase) => c.status === "pass" || c.status === "passed";
  if (run.error) {
    return (
      <p role="alert" className="text-ui text-danger">
        {run.file ?? run.key}: the results file couldn&apos;t be read ({run.error}).
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-4">
      <p className="text-caption text-fg-muted">
        {run.file ? <span className="font-mono">{run.file}</span> : null}
        {run.started_at ? ` · started ${when(run.started_at)}` : ""}
        {run.finished_at ? ` · finished ${when(run.finished_at)}` : ""}
      </p>
      <dl className="grid grid-cols-2 gap-2 sm:grid-cols-4" aria-label="Run summary">
        {tiles.map((t) => (
          <div key={t.key} className="rounded-sm border border-border px-3 py-2">
            <dt className="text-caption text-fg-muted">{t.label}</dt>
            <dd className="text-body font-semibold text-fg tabular-nums">{t.value}</dd>
          </div>
        ))}
      </dl>
      <Table<EvalCase>
        caption="Cases"
        rows={run.cases}
        rowKey={(c) => c.id}
        rowTone={(c) => (passed(c) ? undefined : c.status === "fail" || c.status === "failed" ? "danger" : "warning")}
        empty="This run has no cases."
        columns={[
          {
            id: "case",
            header: "Case",
            rowHeader: true,
            cell: (c) => (
              <span className="flex flex-col text-left">
                <span className="font-medium">{c.title ?? c.id}</span>
                <span className="font-mono text-caption font-normal text-fg-muted">{c.id}</span>
                {c.tags.length ? <span className="text-caption font-normal text-fg-subtle">{c.tags.join(" · ")}</span> : null}
              </span>
            ),
          },
          {
            id: "status",
            header: "Result",
            nowrap: true,
            cell: (c) => (
              <Badge size="sm" tone={EVAL_STATUS[c.status]?.tone ?? "neutral"} dot>
                {EVAL_STATUS[c.status]?.label ?? c.status}
              </Badge>
            ),
          },
          {
            id: "checks",
            header: "Checks",
            nowrap: true,
            align: "end",
            cell: (c) => {
              const s = caseScore(c);
              return `${s.passed} / ${s.total}`;
            },
          },
          ...metrics.map((m) => ({ id: `m-${m}`, header: metricLabel(m), align: "end" as const, nowrap: true, cell: (c: EvalCase) => metricText(m, c.scores[m]) })),
          { id: "duration", header: "Time", align: "end", nowrap: true, cell: (c) => (typeof c.duration_s === "number" ? `${c.duration_s.toFixed(0)} s` : "—") },
          { id: "details", header: "Failures", cell: (c) => <CaseDetails c={c} /> },
        ]}
      />
    </div>
  );
}

/**
 * **Evals** (`docs/contracts/110` §6): the latest `evals/results/*.json` with scores per case.
 * Runs are started from the command line (`evals/run_live.py`); there is no in-app runner.
 */
export function EvalsPage() {
  const admin = useAdminApi();
  const runs = useResource(() => admin.evals(), "evals");
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <>
      <AdminPageHeader
        title="Evals"
        description="Results of the evaluation suite, newest first. Run it from the command line (evals/run_live.py); results appear here."
        actions={
          <Button size="sm" variant="secondary" icon={<RotateCw className="size-4" />} loading={runs.loading && runs.data !== undefined} onClick={runs.reload}>
            Refresh
          </Button>
        }
      />
      <ResourceView resource={runs} label="eval results">
        {({ runs: list, directoryAvailable }) => {
          if (list.length === 0) {
            return (
              <EmptyState
                icon={<FlaskConical />}
                title="No eval results yet"
                description={
                  directoryAvailable
                    ? "Run evals/run_live.py; its JSON results are listed here."
                    : "The results folder (evals/results) isn't mounted into the engine. Mount it read-only, then run evals/run_live.py."
                }
              />
            );
          }
          const run = list.find((r) => r.key === selected) ?? list[0]!;
          return (
            <AdminSection
              title="Results"
              actions={
                list.length > 1 ? (
                  <Field label="Run" hideLabel>
                    <Select value={run.key} onChange={setSelected} options={list.map((r) => ({ value: r.key, label: r.started_at ? when(r.started_at) : r.key }))} />
                  </Field>
                ) : undefined
              }
            >
              <RunView run={run} />
            </AdminSection>
          );
        }}
      </ResourceView>
    </>
  );
}
