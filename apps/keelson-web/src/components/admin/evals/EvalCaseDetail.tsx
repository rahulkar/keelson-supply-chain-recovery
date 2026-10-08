"use client";

import { Badge, Meter, Table, Tabs } from "@keelson/ui";
import { Check, X } from "lucide-react";
import Link from "next/link";
import { useState } from "react";
import {
  bpText,
  errorCodeText,
  isAdversarial,
  isUuid,
  latencyRows,
  msText,
  orderedChecks,
  tokensText,
  usageRows,
  usdText,
  type EvalCaseResult,
  type EvalTurn,
  type LatencyRow,
  type UsageRow,
} from "@/lib/admin/evalRuns";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AdminPageHeader, AdminSection, Facts, ResourceView } from "../common";
import { AdversarialMatrix } from "./AdversarialMatrix";
import { BackLink, CaseStatusBadge, PlainText, evalsHref, linkClass } from "./common";

type Check = EvalCaseResult["checks"][number];

function CheckList({ title, checks }: { title: string; checks: readonly Check[] }) {
  if (!checks.length) return null;
  return (
    <div className="flex flex-col gap-1">
      <h3 className="text-ui font-semibold text-fg">{title}</h3>
      <ul className="flex flex-col gap-1">
        {orderedChecks(checks).map((c, i) => (
          <li key={`${c.name}-${i}`} className="flex items-start gap-2 text-ui">
            {c.ok ? <Check aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-success" /> : <X aria-hidden="true" className="mt-0.5 size-4 shrink-0 text-danger" />}
            <span className="min-w-0">
              <span className="font-mono text-caption">{c.name}</span>{" "}
              <Badge size="sm" tone={c.ok ? "success" : c.soft ? "warning" : "danger"}>
                {c.ok ? "Passed" : c.soft ? "Failed (soft)" : "Failed"}
              </Badge>
              {c.detail ? (
                <span className="block text-caption text-fg-muted">
                  <PlainText>{c.detail}</PlainText>
                </span>
              ) : null}
            </span>
          </li>
        ))}
      </ul>
    </div>
  );
}

function ChecksTab({ result }: { result: EvalCaseResult }) {
  return (
    <div className="flex flex-col gap-4">
      {result.turns.map((t) => (
        <CheckList key={t.id} title={`Turn ${t.id}${t.skipped ? ` (skipped: ${t.skipped})` : ""}`} checks={t.checks ?? []} />
      ))}
      <CheckList title="Case" checks={result.checks} />
      {!result.turns.length && !result.checks.length ? <p className="text-ui text-fg-muted">This case has no checks.</p> : null}
    </div>
  );
}

/** Required patterns ✓/✗, forbidden hits, and the calls made per agent and tool. */
export function TrajectoryDiff({ turn }: { turn: EvalTurn }) {
  const t = turn.trajectory;
  if (!t) return <p className="text-ui text-fg-muted">Turn {turn.id} has no trajectory expectation.</p>;
  return (
    <div className="flex flex-col gap-3">
      <h3 className="text-ui font-semibold text-fg">
        Turn {turn.id} · recall {bpText(t.recall_bp)} (target {bpText(t.min_recall_bp)}) · {t.total_calls} tool call{t.total_calls === 1 ? "" : "s"}
        {t.max_calls != null ? ` (at most ${t.max_calls})` : ""}
      </h3>
      {t.required.length ? (
        <ul aria-label={`Required calls of turn ${turn.id}`} className="flex flex-col gap-1">
          {t.required.map((p) => {
            const ok = t.matched.includes(p);
            return (
              <li key={p} className="flex items-center gap-2 text-ui">
                {ok ? <Check aria-hidden="true" className="size-4 text-success" /> : <X aria-hidden="true" className="size-4 text-danger" />}
                <span className="font-mono text-caption">{p}</span>
                <span className="text-caption text-fg-muted">{ok ? "called" : "missing"}</span>
              </li>
            );
          })}
        </ul>
      ) : null}
      {t.forbidden.length ? (
        <p className="text-ui">
          Forbidden: <span className="font-mono text-caption">{t.forbidden.join(", ")}</span> ·{" "}
          {t.forbidden_hits.length ? <span className="text-danger">called: {t.forbidden_hits.join(", ")}</span> : <span className="text-success">none called</span>}
        </p>
      ) : null}
      <Table
        caption={`Tool calls of turn ${turn.id}`}
        rows={t.calls}
        rowKey={(c) => `${c.agent}:${c.tool}`}
        empty="No tool calls."
        columns={[
          { id: "agent", header: "Agent", rowHeader: true, cell: (c) => <span className="font-mono text-caption">{c.agent || "—"}</span> },
          { id: "tool", header: "Tool", cell: (c) => <span className="font-mono text-caption">{c.tool}</span> },
          { id: "n", header: "Calls", align: "end", cell: (c) => String(c.n) },
        ]}
      />
      {turn.agents_started?.length ? <p className="text-caption text-fg-muted">Agents started: {turn.agents_started.join(", ")}</p> : null}
    </div>
  );
}

function EntitiesTab({ result }: { result: EvalCaseResult }) {
  const turns = result.turns.filter((t) => t.entities);
  if (!turns.length) return <p className="text-ui text-fg-muted">No turn of this case has a golden entity list{result.entity_recall_bp != null ? ` (recall ${bpText(result.entity_recall_bp)})` : ""}.</p>;
  return (
    <div className="flex flex-col gap-4">
      {turns.map((t) => {
        const e = t.entities!;
        return (
          <div key={t.id} className="flex flex-col gap-2">
            <h3 className="text-ui font-semibold text-fg">
              Turn {t.id} · recall {bpText(e.recall_bp)} (target {bpText(e.min_recall_bp)})
            </h3>
            <ul aria-label={`Golden entities of turn ${t.id}`} className="flex flex-wrap gap-1.5">
              {e.golden.map((g) => {
                const missing = e.missing.includes(g);
                return (
                  <li key={g}>
                    <Badge size="sm" tone={missing ? "danger" : "success"} icon={missing ? <X aria-hidden="true" className="size-3" /> : <Check aria-hidden="true" className="size-3" />}>
                      <span className="font-mono">{g}</span>
                      <span className="sr-only">{missing ? " missing" : " found"}</span>
                    </Badge>
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

function LatencyTab({ result }: { result: EvalCaseResult }) {
  const turns = result.turns.map((t) => ({ turn: t, rows: latencyRows(t) })).filter((x) => x.rows.length);
  if (!turns.length) return <p className="text-ui text-fg-muted">No timings were measured.</p>;
  return (
    <div className="flex flex-col gap-4">
      {turns.map(({ turn, rows }) => (
        <div key={turn.id} className="flex flex-col gap-2">
          <h3 className="text-ui font-semibold text-fg">Turn {turn.id}</h3>
          {rows.map((r: LatencyRow) =>
            r.budget != null ? (
              <Meter
                key={r.stage}
                label={`${r.label} (budget ${msText(r.budget)})`}
                value={r.ms ?? 0}
                max={r.budget}
                warnAt={0.8}
                dangerAt={1.0001}
                valueText={r.ms == null ? "Not reached" : `${msText(r.ms)} of ${msText(r.budget)}${r.ms > r.budget ? " (over budget)" : ""}`}
              />
            ) : (
              <p key={r.stage} className="text-caption text-fg-muted">
                {r.label}: {msText(r.ms)}
              </p>
            ),
          )}
        </div>
      ))}
    </div>
  );
}

function UsageTab({ result }: { result: EvalCaseResult }) {
  const rows = usageRows(result.usage);
  const engine = result.engine;
  return (
    <div className="flex flex-col gap-3">
      {result.usage ? (
        <Table<UsageRow>
          caption="LLM usage per agent"
          rows={rows}
          rowKey={(r) => r.agent}
          empty="No model calls."
          columns={[
            { id: "agent", header: "Agent", rowHeader: true, cell: (r) => <span className="font-mono text-caption">{r.agent}</span> },
            { id: "calls", header: "Calls", align: "end", cell: (r) => tokensText(r.calls) },
            { id: "replayed", header: "Replayed", align: "end", cell: (r) => tokensText(r.replayed) },
            { id: "prompt", header: "Prompt", align: "end", cell: (r) => tokensText(r.prompt) },
            { id: "completion", header: "Completion", align: "end", cell: (r) => tokensText(r.completion) },
            { id: "reasoning", header: "Reasoning", align: "end", cell: (r) => tokensText(r.reasoning) },
            { id: "wall", header: "Wall time", align: "end", cell: (r) => msText(r.latencyMs) },
            { id: "cost", header: "Cost", align: "end", cell: (r) => usdText(r.cost) },
          ]}
        />
      ) : (
        <p className="text-ui text-fg-muted">
          {result.tokens_gateway != null
            ? `About ${tokensText(result.tokens_gateway)} tokens (the gateway's usage around the case, which also counts other traffic).`
            : "No usage was reported."}
        </p>
      )}
      {engine ? (
        <Facts
          items={[
            ["Engine-metered calls", `${tokensText(engine.llm_calls_live)} live · ${tokensText(engine.llm_calls_replayed)} replayed`],
            ["Live tokens", tokensText(engine.tokens_live)],
            ["Without a recording", String(engine.replay_misses)],
            ["Cost", usdText(engine.cost_microusd)],
          ]}
        />
      ) : null}
    </div>
  );
}

/** One case of a run: checks, tool trajectory, entities, latency, usage and adversarial outcomes. */
export function EvalCaseDetail({ runId, caseId }: { runId: string; caseId: string }) {
  const admin = useAdminApi();
  const result = useResource(() => admin.evalCase(runId, caseId), `eval-case:${runId}:${caseId}`);
  const [tab, setTab] = useState("checks");
  return (
    <ResourceView resource={result} label="the case result">
      {(r) => {
        const tabs = [
          { id: "checks", label: "Checks", badge: String(r.turns.reduce((n, t) => n + (t.checks?.length ?? 0), r.checks.length)) },
          { id: "trajectory", label: "Trajectory" },
          { id: "entities", label: "Entities" },
          { id: "latency", label: "Latency" },
          { id: "usage", label: "Usage" },
          ...(isAdversarial(r.tags) ? [{ id: "adversarial", label: "Adversarial" }] : []),
        ];
        return (
          <>
            <BackLink href={evalsHref.run(runId)}>← Back to the run</BackLink>
            <AdminPageHeader title={r.title} description={<span className="font-mono">{r.case_id}</span>} />
            <AdminSection id="eval-case-summary" title="Outcome">
              <div className="flex flex-wrap items-center gap-2">
                <CaseStatusBadge status={r.status} />
                {r.tags.map((t) => (
                  <Badge key={t} size="sm" variant="outline">
                    {t}
                  </Badge>
                ))}
              </div>
              <Facts
                items={[
                  ["Why it stopped", errorCodeText(r.error_code) ?? "—"],
                  ["Error", r.error ? <PlainText key="e">{r.error}</PlainText> : "—"],
                  ["Duration", msText(r.duration_ms)],
                  ["Entity recall · grounding · trajectory", `${bpText(r.entity_recall_bp)} · ${bpText(r.numeric_grounding_bp)} · ${bpText(r.trajectory_recall_bp)}`],
                  [
                    "Keelson case",
                    isUuid(r.keelson_case_id) ? (
                      <span key="k">
                        <Link href={`/cases/${r.keelson_case_id}`} className={linkClass}>
                          Open case
                        </Link>
                        <span className="text-caption text-fg-muted"> (a demo reset may have removed it)</span>
                      </span>
                    ) : (
                      "—"
                    ),
                  ],
                ]}
              />
            </AdminSection>
            <AdminSection id="eval-case-detail" title="Details">
              <Tabs tabs={tabs} value={tab} onValueChange={setTab} label="Case details">
                {(active) => (
                  <div className="pt-3">
                    {active === "checks" ? <ChecksTab result={r} /> : null}
                    {active === "trajectory" ? (
                      <div className="flex flex-col gap-4">
                        {r.turns.map((t) => (
                          <TrajectoryDiff key={t.id} turn={t} />
                        ))}
                      </div>
                    ) : null}
                    {active === "entities" ? <EntitiesTab result={r} /> : null}
                    {active === "latency" ? <LatencyTab result={r} /> : null}
                    {active === "usage" ? <UsageTab result={r} /> : null}
                    {active === "adversarial" ? <AdversarialMatrix results={[r]} /> : null}
                  </div>
                )}
              </Tabs>
            </AdminSection>
          </>
        );
      }}
    </ResourceView>
  );
}
