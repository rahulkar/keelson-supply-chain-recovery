"use client";

import type { EvalsRunEvent } from "@keelson/contracts";
import { Button, ConfirmDialog, Meter, StepLog, toast, type StepLogItem, type StepState } from "@keelson/ui";
import { CircleStop } from "lucide-react";
import { useEffect, useEffectEvent, useRef, useState } from "react";
import { EVALS_POLL_MS, isActive, progressText, tokensText, usdText, type EvalEvents, type EvalRun } from "@/lib/admin/evalRuns";
import { adminError } from "@/lib/admin/format";
import { useAdminApi } from "@/lib/admin/useAdmin";
import { AdminSection, InlineError } from "../common";
import { ModeBadge, RunStatusBadge } from "./common";

type Live = Pick<EvalEvents, "status" | "progress" | "counters">;

/**
 * Follows a queued / running run: polls `GET …/runs/{id}/events?after=` every
 * {@link EVALS_POLL_MS} while it is active and stops at its final status (then `onFinished`).
 */
export function useRunProgress(run: EvalRun, onFinished?: (() => void) | undefined) {
  const admin = useAdminApi();
  const [events, setEvents] = useState<EvalsRunEvent[]>([]);
  const [live, setLive] = useState<Live>({ status: run.status, progress: run.progress, counters: run.counters });
  const [error, setError] = useState<string | null>(null);
  const after = useRef(0);
  const inFlight = useRef(false);
  const finished = useEffectEvent(() => onFinished?.());
  const poll = useEffectEvent(async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    try {
      const page = await admin.evalRunEvents(run.id, after.current);
      after.current = page.next_after;
      if (page.items.length) setEvents((prev) => [...prev, ...page.items].slice(-500));
      setLive({ status: page.status, progress: page.progress, counters: page.counters });
      setError(null);
      if (!isActive(page.status)) finished();
    } catch (err) {
      setError(adminError(err));
    } finally {
      inFlight.current = false;
    }
  });
  const active = isActive(live.status);
  useEffect(() => {
    if (!active) return;
    void poll();
    const timer = setInterval(() => void poll(), EVALS_POLL_MS);
    return () => clearInterval(timer);
  }, [active, run.id]);
  return { events, live, error };
}

const CASE_STATE: Record<string, StepState> = { pass: "ok", fail: "failed", error: "failed", skipped: "skipped", cancelled: "skipped" };

/** One step per case of the run, from its progress events. */
export function caseSteps(run: Pick<EvalRun, "case_ids">, events: readonly EvalsRunEvent[]): StepLogItem[] {
  const state = new Map<string, { state: StepState; detail?: string }>();
  for (const e of events) {
    if (!e.case_id) continue;
    if (e.type === "case_started") state.set(e.case_id, { state: "running" });
    else if (e.type === "turn_started") state.set(e.case_id, { state: "running", detail: `turn ${e.turn_id ?? ""}`.trim() });
    else if (e.type === "case_finished") {
      const status = String(e.data["status"] ?? "");
      const code = e.data["error_code"];
      state.set(e.case_id, { state: CASE_STATE[status] ?? "ok", detail: [status, typeof code === "string" ? code.replace(/_/g, " ") : null].filter(Boolean).join(" · ") });
    }
  }
  return run.case_ids.map((id) => ({ id, label: id, state: state.get(id)?.state ?? "pending", detail: state.get(id)?.detail }));
}

/** The active run: progress, the cases as a step log, live budget meters and Cancel. */
export function EvalProgress({ run, onChanged }: { run: EvalRun; onChanged: () => void }) {
  const admin = useAdminApi();
  const { events, live, error } = useRunProgress(run, onChanged);
  const [cancelOpen, setCancelOpen] = useState(false);
  const view = { ...run, ...live };
  const budget = run.budget;
  const cancel = async () => {
    try {
      await admin.cancelEvalRun(run.id);
      toast.success(run.status === "queued" ? "Run cancelled." : "Cancelling: the runner stops before its next turn.");
      onChanged();
      return true;
    } catch (err) {
      toast.error(adminError(err));
      return false;
    }
  };
  return (
    <AdminSection
      id="eval-progress"
      title="Run in progress"
      description={progressText(view)}
      actions={
        isActive(live.status) ? (
          <Button size="sm" variant="danger" icon={<CircleStop className="size-4" />} disabled={run.cancel_requested} onClick={() => setCancelOpen(true)}>
            {run.cancel_requested ? "Cancelling…" : "Cancel run"}
          </Button>
        ) : undefined
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <RunStatusBadge status={live.status} />
        <ModeBadge mode={run.mode} />
      </div>
      {error ? <InlineError message={error} /> : null}
      <Meter
        label="Cases done"
        value={live.progress.cases_done}
        max={Math.max(1, live.progress.cases_total)}
        valueText={`${live.progress.cases_done} of ${live.progress.cases_total}`}
        warnAt={2}
        dangerAt={2}
      />
      {budget ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Meter label="Tokens used of the budget" value={live.counters.tokens_live} max={budget.max_tokens}
                 valueText={`${tokensText(live.counters.tokens_live)} of ${tokensText(budget.max_tokens)}`} />
          <Meter label="Cost of the budget" value={live.counters.cost_microusd} max={budget.max_cost_microusd}
                 valueText={`${usdText(live.counters.cost_microusd)} of ${usdText(budget.max_cost_microusd)}`} />
        </div>
      ) : (
        <p className="text-caption text-fg-muted">
          Model calls: {tokensText(live.counters.llm_calls_live)} live, {tokensText(live.counters.llm_calls_replayed)} replayed
          {live.counters.replay_misses ? `, ${live.counters.replay_misses} without a recording` : ""}.
        </p>
      )}
      <StepLog label="Cases" steps={caseSteps(run, events)} />
      <ConfirmDialog
        open={cancelOpen}
        onOpenChange={setCancelOpen}
        title="Cancel this eval run?"
        description="A queued run ends now. A running one stops before its next turn: the Keelson run in flight is cancelled and the case's Merxa reset still runs."
        confirmLabel="Cancel run"
        cancelLabel="Keep running"
        tone="danger"
        onConfirm={cancel}
      />
    </AdminSection>
  );
}
