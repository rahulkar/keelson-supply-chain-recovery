"use client";

import { Check, Circle, MinusCircle, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { cn } from "../lib/cn";
import { Spinner } from "./Spinner";

export type StepState = "pending" | "running" | "ok" | "failed" | "skipped";

export type StepLogItem = {
  id: string;
  label: string;
  state: StepState;
  /** Duration in milliseconds (shown once finished). */
  ms?: number | null | undefined;
  /** Short detail (error text, counts); rendered as text, wraps. */
  detail?: ReactNode | undefined;
};

export type StepLogProps = {
  steps: readonly StepLogItem[];
  /** Names the list ("Connection test"). */
  label: string;
  className?: string | undefined;
};

const STATE_TEXT: Record<StepState, string> = {
  pending: "Waiting",
  running: "Running",
  ok: "Passed",
  failed: "Failed",
  skipped: "Skipped",
};

function StateIcon({ state }: { state: StepState }) {
  switch (state) {
    case "running":
      return <Spinner size={14} />;
    case "ok":
      return <Check className="size-3.5 text-success" />;
    case "failed":
      return <X className="size-3.5 text-danger" />;
    case "skipped":
      return <MinusCircle className="size-3.5 text-fg-subtle" />;
    default:
      return <Circle className="size-3 text-fg-subtle" />;
  }
}

function formatMs(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
}

/** What changed between two step lists, as one sentence for the live region. */
export function stepLogAnnouncement(prev: readonly StepLogItem[], next: readonly StepLogItem[]): string {
  const before = new Map(prev.map((s) => [s.id, s.state]));
  const changed = next.filter((s) => before.get(s.id) !== s.state && s.state !== "pending");
  if (changed.length === 0) return "";
  if (changed.length > 3) {
    const failed = next.find((s) => s.state === "failed");
    const passed = next.filter((s) => s.state === "ok").length;
    return failed ? `${failed.label}: failed. ${passed} of ${next.length} steps passed.` : `${passed} of ${next.length} steps passed.`;
  }
  return changed.map((s) => `${s.label}: ${STATE_TEXT[s.state].toLowerCase()}`).join(". ");
}

/**
 * Live step log (connection tests, long jobs): ordered list, each step's state is spelled out
 * (icon + text, never colour alone); state changes are announced politely.
 */
export function StepLog({ steps, label, className }: StepLogProps) {
  // "Adjusting state when a prop changes" (React docs): no effect, no extra commit.
  const [prevSteps, setPrevSteps] = useState<readonly StepLogItem[]>(steps);
  const [announcement, setAnnouncement] = useState("");
  if (prevSteps !== steps) {
    const text = stepLogAnnouncement(prevSteps, steps);
    setPrevSteps(steps);
    if (text) setAnnouncement(text);
  }
  return (
    <div className={cn("flex flex-col", className)}>
      <ol aria-label={label} className="flex flex-col divide-y divide-border rounded-md border border-border bg-surface">
        {steps.map((s) => (
          <li
            key={s.id}
            data-state={s.state}
            aria-current={s.state === "running" ? "step" : undefined}
            className={cn("flex items-start gap-2.5 px-3 py-2 text-ui", s.state === "pending" && "text-fg-muted")}
          >
            <span aria-hidden="true" className="mt-0.5 inline-flex size-4 shrink-0 items-center justify-center">
              <StateIcon state={s.state} />
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline gap-x-2">
                <span className={cn("font-medium", s.state === "failed" ? "text-danger" : s.state === "pending" ? "text-fg-muted" : "text-fg")}>
                  {s.label}
                </span>
                <span className="text-caption text-fg-subtle">{STATE_TEXT[s.state]}</span>
                {typeof s.ms === "number" && (s.state === "ok" || s.state === "failed") ? (
                  <span className="ml-auto text-caption text-fg-subtle tabular-nums">{formatMs(s.ms)}</span>
                ) : null}
              </div>
              {s.detail ? <div className="mt-0.5 text-caption break-words text-fg-muted">{s.detail}</div> : null}
            </div>
          </li>
        ))}
      </ol>
      <p className="sr-only" aria-live="polite" role="status">
        {announcement}
      </p>
    </div>
  );
}
