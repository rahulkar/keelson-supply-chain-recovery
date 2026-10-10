"use client";

import { Button, Table } from "@keelson/ui";
import { Download, GitCompare, RotateCw } from "lucide-react";
import Link from "next/link";
import { useState, type ReactNode } from "react";
import {
  bpText,
  errorCodeText,
  isActive,
  isAdversarial,
  msText,
  tokensText,
  usdText,
  type EvalCaseResult,
  type EvalCaseRow,
} from "@/lib/admin/evalRuns";
import { when } from "@/lib/admin/format";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AdminPageHeader, AdminSection, Facts, ResourceView } from "../common";
import { AdversarialMatrix } from "./AdversarialMatrix";
import { EvalProgress } from "./EvalProgress";
import { BackLink, CaseStatusBadge, ModeBadge, PlainText, RunStatusBadge, ThresholdTable, Tiles, evalsHref, linkClass, summaryTiles } from "./common";

const download = "inline-flex h-8 items-center gap-1.5 rounded-pill border border-border-strong px-3 text-ui font-medium text-fg hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";

function Adversarial({ runId, rows, refreshKey }: { runId: string; rows: readonly EvalCaseRow[]; refreshKey: string }) {
  const admin = useAdminApi();
  const ids = rows.filter((r) => isAdversarial(r.tags) && r.status !== "skipped" && r.status !== "cancelled").map((r) => r.case_id);
  const results = useResource<EvalCaseResult[]>(() => Promise.all(ids.map((id) => admin.evalCase(runId, id))), `adv:${runId}:${ids.join(",")}:${refreshKey}`);
  if (!ids.length) return null;
  return (
    <AdminSection id="eval-adversarial" title="Adversarial outcomes" description="Whether each injection was contained: the guard flagged it, no write tool reached a model, nothing was committed without a human, and the injected text stayed out of the answers.">
      <ResourceView resource={results} label="adversarial results">
        {(r) => <AdversarialMatrix results={r} />}
      </ResourceView>
    </AdminSection>
  );
}

/** One run: summary, targets, cases (→ drill-down), adversarial outcomes, export, compare. */
export function EvalRunDetail({ runId }: { runId: string }) {
  const admin = useAdminApi();
  const [epoch, setEpoch] = useState(0);
  const detail = useResource(() => admin.evalRun(runId), `eval-run:${runId}:${epoch}`);
  const history = useResource(() => admin.evalRuns({ limit: 100 }), `eval-runs-for:${runId}:${epoch}`);
  const refresh = () => setEpoch((n) => n + 1);
  const items = history.data?.items ?? [];
  const idx = items.findIndex((r) => r.id === runId);
  const previous = idx >= 0 ? items.slice(idx + 1).find((r) => r.summary) : undefined;

  return (
    <ResourceView resource={detail} label="the eval run">
      {({ run, cases }) => (
        <>
          <BackLink href={evalsHref.overview}>← All eval runs</BackLink>
          <AdminPageHeader
            title={`Eval run · ${when(run.started_at ?? run.created_at)}`}
            description={run.note ?? (run.source === "import" ? `Imported${run.file_name ? ` from ${run.file_name}` : ""}` : "Started in the app")}
            actions={
              <>
                {previous ? (
                  <Link href={evalsHref.compare(previous.id, run.id)} className={download}>
                    <GitCompare aria-hidden="true" className="size-4" /> Compare with previous
                  </Link>
                ) : null}
                {!isActive(run.status) ? (
                  <a href={admin.evalExportHref(run.id)} download className={download}>
                    <Download aria-hidden="true" className="size-4" /> Export JSON
                  </a>
                ) : null}
                <Button size="sm" variant="secondary" icon={<RotateCw className="size-4" />} loading={detail.loading} onClick={refresh}>
                  Refresh
                </Button>
              </>
            }
          />
          <div className="flex flex-wrap items-center gap-2">
            <RunStatusBadge status={run.status} />
            <ModeBadge mode={run.mode} />
          </div>
          {isActive(run.status) ? <EvalProgress run={run} onChanged={refresh} /> : null}
          <AdminSection id="eval-summary" title="Summary">
            <Tiles label="Run summary" items={summaryTiles(run)} />
            <Facts
              items={[
                ["Requested by", run.requested_by?.name ?? run.requested_by?.username ?? "—"],
                ["Finished", when(run.finished_at)],
                ["Model calls (engine)", `${tokensText(run.counters.llm_calls_live)} live · ${tokensText(run.counters.llm_calls_replayed)} replayed${run.counters.replay_misses ? ` · ${run.counters.replay_misses} without a recording` : ""}`],
                ["Budget", run.budget ? `${tokensText(run.budget.max_tokens)} tokens · ${usdText(run.budget.max_cost_microusd)}` : "None (no live calls)"],
                ["Measured stack", run.target ?? "—"],
                ...(run.error ? [["Error", <PlainText key="e">{run.error}</PlainText>] as [string, ReactNode]] : []),
              ]}
            />
          </AdminSection>
          {run.thresholds.length ? (
            <AdminSection id="eval-targets" title="Targets" description="Goals to track, never gates. Latency, token and cost targets apply to live runs only.">
              <ThresholdTable run={run} />
            </AdminSection>
          ) : null}
          <AdminSection id="eval-cases" title="Cases">
            <Table<EvalCaseRow>
              caption="Cases"
              hideCaption
              rows={cases}
              rowKey={(c) => c.case_id}
              empty="No case has reported yet."
              rowTone={(c) => (c.status === "fail" ? "danger" : c.status === "error" ? "warning" : undefined)}
              columns={[
                {
                  id: "case",
                  header: "Case",
                  rowHeader: true,
                  cell: (c) => (
                    <span className="flex flex-col">
                      <Link href={evalsHref.case(run.id, c.case_id)} className={linkClass}>
                        {c.title}
                      </Link>
                      <span className="font-mono text-caption text-fg-muted">{c.case_id}</span>
                      {c.error_code ? <span className="text-caption text-warning">{errorCodeText(c.error_code)}</span> : null}
                    </span>
                  ),
                },
                { id: "status", header: "Result", nowrap: true, cell: (c) => <CaseStatusBadge status={c.status} /> },
                { id: "checks", header: "Checks", nowrap: true, align: "end", cell: (c) => `${c.checks_total - c.checks_failed} / ${c.checks_total}` },
                { id: "recall", header: "Recall", nowrap: true, align: "end", cell: (c) => bpText(c.entity_recall_bp) },
                { id: "grounding", header: "Grounding", nowrap: true, align: "end", cell: (c) => bpText(c.numeric_grounding_bp) },
                { id: "trajectory", header: "Trajectory", nowrap: true, align: "end", cell: (c) => bpText(c.trajectory_recall_bp) },
                { id: "first", header: "First stage", nowrap: true, align: "end", cell: (c) => msText(c.first_stage_ms) },
                { id: "options", header: "Options", nowrap: true, align: "end", cell: (c) => msText(c.options_ms) },
                { id: "tokens", header: "Tokens", nowrap: true, align: "end", cell: (c) => tokensText(c.tokens) },
                { id: "cost", header: "Cost", nowrap: true, align: "end", cell: (c) => usdText(c.cost_microusd) },
              ]}
            />
          </AdminSection>
          <Adversarial runId={run.id} rows={cases} refreshKey={String(epoch)} />
        </>
      )}
    </ResourceView>
  );
}
