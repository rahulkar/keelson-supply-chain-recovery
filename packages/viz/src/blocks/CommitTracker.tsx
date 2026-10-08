"use client";

import type { CommitTrackerProps, TrackerAction } from "@keelson/contracts";
import { Badge, Button, ConfirmDialog, ProvenanceChip, cn } from "@keelson/ui";
import { Check, CircleAlert, ExternalLink, Send, Undo2 } from "lucide-react";
import { useId, useState } from "react";
import { formatValue } from "../format";
import { isAllowedHref } from "../links";
import { useActionRunner } from "../lib/useActionRunner";
import type { BlockComponentProps } from "../types";
import { BlockFrame } from "./BlockFrame";

type StepId = "applying" | "applied" | "verified" | "rolled_back";
type Step = { id: StepId; label: string };
const STEPS: Step[] = [
  { id: "applying", label: "Applying" },
  { id: "applied", label: "Applied" },
  { id: "verified", label: "Verified" },
];
/** Shown only once the change set was undone (M5, `docs/contracts/95` §3). */
const ROLLED_BACK_STEP: Step = { id: "rolled_back", label: "Rolled back" };

export const STATUS_TEXT: Record<CommitTrackerProps["status"], string> = {
  applying: "Sending the changes…",
  applied: "Changes applied — checking they landed",
  verified: "Changes applied and verified",
  conflict: "Some changes conflicted with newer data",
  rolled_back: "Changes rolled back",
};

/**
 * Steps completed per status; the step at that index (if any) is the current one.
 * applying → Applying in progress · applied → Applying + Applied done, Verified (read-back) in
 * progress · verified → all done · conflict → Applying done, Applied failed · rolled_back →
 * every step done, including the extra "Rolled back" step.
 */
export const DONE_STEPS: Record<CommitTrackerProps["status"], number> = {
  applying: 0,
  applied: 2,
  verified: 3,
  conflict: 1,
  rolled_back: 4,
};

/** The tracker's Undo button (the engine's `actions` entry), when it offers one. */
export function undoAction(props: Pick<CommitTrackerProps, "actions" | "status">): TrackerAction | null {
  if (props.status !== "applied" && props.status !== "verified") return null;
  return props.actions.find((a) => a.type === "undo") ?? null;
}

/**
 * `commit_tracker@1` — progress of a change set in the connected system, with a deep link. M5:
 * **Undo changes** (confirm dialog → `{type: "rollback", changeSetId}`; the rollback itself is
 * approved like any change), the rolled-back state, and the conflicts that stopped an undo
 * (later changes on the same records).
 */
export function CommitTracker({ props, envelope, onAction, busy }: BlockComponentProps<CommitTrackerProps>) {
  const conflict = props.status === "conflict";
  const rolledBack = props.status === "rolled_back";
  const steps = rolledBack ? [...STEPS, ROLLED_BACK_STEP] : STEPS;
  const doneCount = DONE_STEPS[props.status];
  const link = isAllowedHref(props.ext_url) ? props.ext_url : null;
  const undo = undoAction(props);
  const { pending, run } = useActionRunner(onAction);
  const [confirming, setConfirming] = useState(false);
  const undoReasonId = useId();
  const conflictsId = useId();

  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle={`Changes in ${props.system}`}
      eyebrow="Changes"
      icon={rolledBack ? <Undo2 /> : <Send />}
      tone={conflict ? "danger" : "default"}
      showProvenance={false}
      actions={
        rolledBack ? (
          <Badge tone="neutral">Rolled back</Badge>
        ) : props.status === "verified" ? (
          <ProvenanceChip prov="committed" verified />
        ) : props.status === "applied" ? (
          <ProvenanceChip prov="committed" />
        ) : undefined
      }
    >
      <p className={cn("text-ui font-medium", conflict ? "text-danger" : "text-fg")} data-status={props.status}>
        {conflict ? <CircleAlert aria-hidden="true" className="mr-1.5 inline size-4 align-[-3px]" /> : null}
        {rolledBack ? `${STATUS_TEXT.rolled_back} in ${props.system}` : STATUS_TEXT[props.status]}
      </p>

      <ol aria-label="Commit progress" className="flex items-center gap-2">
        {steps.map((step, i) => {
          const done = i < doneCount;
          const current = i === doneCount;
          return (
            <li
              key={step.id}
              aria-current={current ? "step" : undefined}
              className="flex flex-1 items-center gap-2 text-caption"
            >
              <span
                aria-hidden="true"
                className={cn(
                  "inline-flex size-6 shrink-0 items-center justify-center rounded-pill border text-caption",
                  done && "border-success bg-success-soft text-success",
                  current && !conflict && "border-accent bg-accent-soft text-accent-soft-fg",
                  current && conflict && "border-danger bg-danger-soft text-danger",
                  !done && !current && "border-border text-fg-subtle",
                )}
              >
                {done ? <Check className="size-3.5" strokeWidth={2.5} /> : i + 1}
              </span>
              <span className={cn(done || current ? "text-fg" : "text-fg-subtle")}>
                {step.label}
                <span className="sr-only">
                  {done ? " (done)" : current ? (conflict ? " (failed)" : " (in progress)") : " (not started)"}
                </span>
              </span>
              {i < steps.length - 1 ? <span aria-hidden="true" className="h-px flex-1 bg-border" /> : null}
            </li>
          );
        })}
      </ol>

      <dl className="flex flex-wrap gap-x-6 gap-y-1 text-ui">
        <div className="flex gap-1.5">
          <dt className="text-fg-muted">Applied</dt>
          <dd className="font-semibold tabular-nums">{formatValue(props.applied, "count")}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-fg-muted">Verified</dt>
          <dd className="font-semibold tabular-nums">{formatValue(props.verified, "count")}</dd>
        </div>
        {props.ext_code ? (
          <div className="flex gap-1.5">
            <dt className="text-fg-muted">{props.system} batch</dt>
            <dd className="font-mono">{props.ext_code}</dd>
          </div>
        ) : null}
      </dl>

      {props.conflicts.length > 0 ? (
        <section
          aria-labelledby={conflictsId}
          className="flex flex-col gap-1.5 rounded-md border border-warning/40 bg-warning-soft px-3 py-2.5 text-ui"
          data-conflicts=""
        >
          <p id={conflictsId} className="font-medium text-fg">
            These changes can't be undone: {props.conflicts.length === 1 ? "1 record was" : `${props.conflicts.length} records were`} changed again
            later in {props.system}.
          </p>
          <ul className="flex flex-col gap-1">
            {props.conflicts.map((c) => (
              <li key={`${c.entity_ref}:${c.detail}`} className="flex flex-wrap gap-x-2">
                <span className="font-mono whitespace-nowrap text-fg">{c.entity_ref}</span>
                <span className="text-fg-muted">{c.detail}</span>
              </li>
            ))}
          </ul>
        </section>
      ) : null}

      {link || undo ? (
        <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
          {undo ? (
            <Button
              size="sm"
              variant="secondary"
              icon={<Undo2 className="size-4" />}
              disabled={busy || !undo.enabled || pending !== null}
              loading={pending === "undo"}
              aria-describedby={!undo.enabled && undo.disabled_reason ? undoReasonId : undefined}
              onClick={() => setConfirming(true)}
            >
              {undo.label}
            </Button>
          ) : null}
          {link ? (
            <a
              href={link}
              target="_blank"
              rel="noopener noreferrer"
              className="inline-flex w-fit items-center gap-1.5 rounded-pill text-ui font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-focus"
            >
              Open in {props.system}
              <ExternalLink aria-hidden="true" className="size-3.5" />
              <span className="sr-only"> (opens in a new tab)</span>
            </a>
          ) : null}
        </div>
      ) : null}
      {undo && !undo.enabled && undo.disabled_reason ? (
        <p id={undoReasonId} className="text-caption text-fg-muted">
          {undo.disabled_reason}
        </p>
      ) : null}

      {undo ? (
        <ConfirmDialog
          open={confirming}
          onOpenChange={setConfirming}
          title={`Undo these changes in ${props.system}?`}
          description={`This restores the ${formatValue(props.applied, "count")} changed record${props.applied === 1 ? "" : "s"} to how they were before. The undo is approved like any other change and is recorded in ${props.system}'s audit trail.`}
          confirmLabel="Undo changes"
          tone="danger"
          onConfirm={async () => {
            await run("undo", { type: "rollback", changeSetId: props.change_set_id });
          }}
        />
      ) : null}
    </BlockFrame>
  );
}
