import { Badge, EmptyState } from "@keelson/ui";
import { stageLabels } from "@keelson/tokens";
import { Workflow } from "lucide-react";
import { isDecidedPause, type CaseStreamState, type RunEntry, type RunStatus } from "@/lib/stream/reducer";

const STATUS: Record<RunStatus, { label: string; tone: "neutral" | "accent" | "success" | "warning" | "danger" }> = {
  queued: { label: "Queued", tone: "neutral" },
  running: { label: "Running", tone: "accent" },
  paused: { label: "Waiting for a decision", tone: "warning" },
  done: { label: "Finished", tone: "success" },
  error: { label: "Failed", tone: "danger" },
  cancelled: { label: "Stopped", tone: "neutral" },
};

function RunTrace({ run, index, state }: { run: RunEntry; index: number; state: CaseStreamState }) {
  const status = isDecidedPause(run, state.interrupts) ? { label: "Decision received", tone: "neutral" as const } : STATUS[run.status];
  const blocks = run.blockIds.map((id) => state.blocks[id]?.envelope).filter((b) => !!b);
  return (
    <li className="rounded-md border border-border p-3">
      <div className="flex flex-wrap items-center gap-2">
        <h3 className="text-ui font-semibold text-fg">Run {index + 1}</h3>
        <Badge tone={status.tone}>{status.label}</Badge>
        <code className="ml-auto truncate font-mono text-caption text-fg-muted">{run.id}</code>
      </div>
      {run.visited.length > 0 ? (
        <p className="mt-2 text-caption text-fg-muted">
          <span className="font-medium text-fg">Stages: </span>
          {run.visited.map((s) => stageLabels[s]).join(" → ")}
        </p>
      ) : null}
      {run.agents.length > 0 ? (
        <ul className="mt-2 flex flex-col gap-2" aria-label={`Agents in run ${index + 1}`}>
          {run.agents.map((a) => (
            <li key={a.id}>
              <p className="text-ui text-fg">
                {a.name} <span className="text-caption text-fg-muted">({a.state === "error" ? "failed" : a.state})</span>
              </p>
              {a.error ? <p className="text-caption text-danger">{a.error}</p> : null}
              {a.activities.length > 0 ? (
                <ol className="mt-1 ml-4 list-decimal text-caption text-fg-muted">
                  {a.activities.map((x) => (
                    <li key={x.id}>
                      {x.toolName ? <code className="mr-1 font-mono text-fg">{x.toolName}</code> : null}
                      {x.label}
                      {x.evidenceRefs.length ? <span className="ml-1">[{x.evidenceRefs.join(", ")}]</span> : null}
                    </li>
                  ))}
                </ol>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
      {blocks.length > 0 ? (
        <p className="mt-2 text-caption text-fg-muted">
          <span className="font-medium text-fg">Produced: </span>
          {blocks.map((b) => `${b.title || b.kind} (${b.kind}@${b.v})`).join(", ")}
        </p>
      ) : null}
      {run.error ? (
        <p className="mt-2 text-caption text-danger">
          {run.error.code ? `${run.error.code}: ` : ""}
          {run.error.message}
          {run.error.errorId && !run.error.message.includes(run.error.errorId) ? (
            <span className="ml-1 text-fg-muted">
              (error ID <code className="font-mono">{run.error.errorId}</code>)
            </span>
          ) : null}
        </p>
      ) : null}
    </li>
  );
}

/** Canvas → Trace (M1): every run with its stages, agents, steps (incl. tool names) and outputs. */
export function TraceView({ state }: { state: CaseStreamState }) {
  const runs = state.runOrder.map((id) => state.runs[id]).filter((r): r is RunEntry => !!r);
  if (runs.length === 0) {
    return <EmptyState icon={<Workflow />} title="Nothing to trace yet" description="Each run's stages, agents and steps appear here." />;
  }
  return (
    <ol className="flex flex-col gap-3" aria-label="Runs">
      {runs.map((run, i) => (
        <RunTrace key={run.id} run={run} index={i} state={state} />
      ))}
    </ol>
  );
}
