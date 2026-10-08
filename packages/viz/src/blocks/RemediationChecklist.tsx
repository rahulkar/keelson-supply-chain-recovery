"use client";

import { checklistCompleted, type ChecklistStep, type ChecklistStepStatus, type RemediationChecklistProps } from "@keelson/contracts";
import { Badge, Button, Meter, type BadgeTone, cn } from "@keelson/ui";
import { CheckCircle2, Circle, CircleCheckBig, ExternalLink, ListChecks, MinusCircle, PartyPopper } from "lucide-react";
import { useId, useState } from "react";
import { formatDateTime } from "../format";
import { isAllowedHref } from "../links";
import { useActionRunner } from "../lib/useActionRunner";
import type { BlockComponentProps, ManualStepStatus } from "../types";
import { BlockFrame } from "./BlockFrame";
import { NarrativeMarkdown } from "./Narrative";

const STATUS_META: Record<ChecklistStepStatus, { label: string; tone: BadgeTone }> = {
  pending: { label: "To do", tone: "neutral" },
  done: { label: "Done · waiting for the check", tone: "info" },
  verified: { label: "Verified", tone: "success" },
  skipped: { label: "Skipped", tone: "neutral" },
};

function connectorName(slug: string): string {
  return slug.length ? slug.charAt(0).toUpperCase() + slug.slice(1) : slug;
}

/**
 * A step's deep link, if it may be put in an `href`: the contracts pattern already refuses
 * `javascript:`, `data:`, protocol-relative and userinfo links; on top of that an absolute link
 * must point at an allowlisted page (`isAllowedHref`: Merxa / Keelson hosts, never their API or
 * MCP endpoints) and a same-origin path must not be an API path.
 */
export function safeDeepLink(link: string | null | undefined): string | null {
  if (!link) return null;
  if (link.startsWith("/")) {
    if (link.startsWith("//") || link.includes("\\")) return null;
    // Judge the path the browser will actually request: dot segments ("/./api/…",
    // "/x/../api/…", also percent-encoded "%2e") are resolved by the URL parser, so a raw prefix
    // check alone would let a link reach the API (e.g. the GET that starts an OAuth hop).
    let resolved: URL;
    try {
      resolved = new URL(link, "http://keelson.localhost");
    } catch {
      return null;
    }
    if (resolved.origin !== "http://keelson.localhost") return null;
    const path = resolved.pathname.toLowerCase();
    return path === "/api" || path.startsWith("/api/") ? null : link;
  }
  return isAllowedHref(link) ? link : null;
}

/** "Steps done" for the progress bar: verified + skipped + manually done (`progress.done` when it is larger). */
export function checklistProgress(props: Pick<RemediationChecklistProps, "steps" | "progress">): { done: number; total: number } {
  const total = Math.max(props.steps.length, props.progress.total);
  const counted = props.steps.filter((s) => s.status !== "pending").length;
  return { done: Math.min(total, Math.max(counted, props.progress.done)), total };
}

/** The sentence announced when steps change status between two renders (live ticks from the stream). */
export function checklistAnnouncement(prev: readonly ChecklistStep[], next: readonly ChecklistStep[], connector: string): string {
  const before = new Map(prev.map((s) => [s.seq, s.status]));
  const changed = next.filter((s) => before.has(s.seq) && before.get(s.seq) !== s.status);
  const name = connectorName(connector);
  return changed
    .map((s) =>
      s.status === "verified"
        ? `Step ${s.seq} verified in ${name}`
        : s.status === "skipped"
          ? `Step ${s.seq} skipped`
          : s.status === "done"
            ? `Step ${s.seq} marked done`
            : `Step ${s.seq} reopened`,
    )
    .join(". ");
}

function StepIcon({ status }: { status: ChecklistStepStatus }) {
  switch (status) {
    case "verified":
      return <CircleCheckBig className="size-4.5 text-success" />;
    case "done":
      return <CheckCircle2 className="size-4.5 text-info" />;
    case "skipped":
      return <MinusCircle className="size-4.5 text-fg-subtle" />;
    default:
      return <Circle className="size-4.5 text-fg-subtle" />;
  }
}

/**
 * `remediation_checklist@1` (M5 Guide mode, `docs/contracts/95` §2) — the steps a person makes
 * by hand in the connected system, each with instructions and a deep link that opens the
 * pre-filled form there in a new tab. Steps tick to **Verified** live when the engine reads the
 * expected record back (the block is re-emitted on the stream); people can mark a step **done**
 * or **skip** it (`checklist_step` action, audited). When every step is verified or skipped the
 * card says "Guide completed".
 */
export function RemediationChecklist({ props, envelope, onAction, busy }: BlockComponentProps<RemediationChecklistProps>) {
  const { pending, run } = useActionRunner(onAction);
  const system = connectorName(props.connector);
  const progress = checklistProgress(props);
  const completed = checklistCompleted(props);
  const verified = props.steps.filter((s) => s.status === "verified").length;

  // Announce live ticks politely ("adjust state when a prop changes", no effect needed).
  const [prevSteps, setPrevSteps] = useState(props.steps);
  const [announcement, setAnnouncement] = useState("");
  // Unique per rendering (the same block may be shown inline and in the canvas).
  const idBase = useId();
  if (prevSteps !== props.steps) {
    const text = checklistAnnouncement(prevSteps, props.steps, props.connector);
    setPrevSteps(props.steps);
    if (text) setAnnouncement(completed ? `${text}. Guide completed.` : text);
  }

  const mark = (step: ChecklistStep, status: ManualStepStatus) =>
    void run(`${step.seq}:${status}`, { type: "checklist_step", checklistId: props.checklist_id, seq: step.seq, status });

  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle={`Guide: changes to make in ${system}`}
      eyebrow="Guide me"
      icon={<ListChecks />}
      tone={completed ? "default" : "accent"}
      actions={completed ? <Badge tone="success">Guide completed</Badge> : <Badge tone="neutral">{`${progress.done} of ${progress.total}`}</Badge>}
    >
      <div className="flex flex-col gap-1.5">
        <Meter
          label="Guide progress"
          value={progress.done}
          max={progress.total}
          valueText={`${progress.done} of ${progress.total} steps done${verified ? `, ${verified} verified in ${system}` : ""}`}
          warnAt={2}
          dangerAt={3}
        />
        <p className="text-caption text-fg-muted">
          Each step opens {system} in a new tab with the form filled in. Steps tick to Verified when {system} shows the change.
        </p>
      </div>

      {completed ? (
        <p role="status" className="flex items-center gap-2 rounded-md border border-success/40 bg-success-soft px-3 py-2 text-ui font-medium text-fg" data-guide-completed="">
          <PartyPopper aria-hidden="true" className="size-4 text-success" />
          Guide completed: every step is verified or skipped.
        </p>
      ) : null}

      <ol aria-label="Steps" className="flex flex-col divide-y divide-border rounded-md border border-border">
        {props.steps.map((step) => {
          const meta = STATUS_META[step.status];
          const href = safeDeepLink(step.deep_link);
          const open = step.status === "pending" || step.status === "done";
          const titleId = `${idBase}-step-${step.seq}`;
          return (
            <li key={step.seq} data-step-status={step.status} className="flex gap-3 px-3 py-3">
              <span aria-hidden="true" className="mt-0.5 inline-flex shrink-0">
                <StepIcon status={step.status} />
              </span>
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <span id={titleId} className={cn("text-ui font-medium", step.status === "skipped" ? "text-fg-muted line-through" : "text-fg")}>
                    {step.seq}. {step.title}
                  </span>
                  <Badge size="sm" tone={meta.tone}>
                    {meta.label}
                  </Badge>
                  {step.status === "verified" && step.verified_at ? (
                    <span className="text-caption text-fg-muted">{formatDateTime(step.verified_at)}</span>
                  ) : null}
                </div>
                {open ? <NarrativeMarkdown markdown={step.instructions_md} className="text-ui" /> : null}
                {open ? (
                  <div className="flex flex-wrap items-center gap-2">
                    {href ? (
                      <a
                        href={href}
                        target="_blank"
                        rel="noopener noreferrer"
                        className="inline-flex h-8 items-center gap-1.5 rounded-pill border border-border-strong px-3 text-ui font-medium text-accent hover:bg-accent-soft focus-visible:outline-2 focus-visible:outline-focus"
                      >
                        Open in {system}
                        <ExternalLink aria-hidden="true" className="size-3.5" />
                        <span className="sr-only"> (opens in a new tab, step {step.seq})</span>
                      </a>
                    ) : null}
                    {step.status === "pending" ? (
                      <Button
                        size="sm"
                        variant="secondary"
                        disabled={busy || pending !== null}
                        loading={pending === `${step.seq}:done`}
                        aria-describedby={titleId}
                        onClick={() => mark(step, "done")}
                      >
                        Mark done
                      </Button>
                    ) : null}
                    <Button
                      size="sm"
                      variant="ghost"
                      disabled={busy || pending !== null}
                      loading={pending === `${step.seq}:skipped`}
                      aria-describedby={titleId}
                      onClick={() => mark(step, "skipped")}
                    >
                      Skip
                    </Button>
                  </div>
                ) : null}
              </div>
            </li>
          );
        })}
      </ol>
      <p className="sr-only" aria-live="polite" role="status">
        {announcement}
      </p>
    </BlockFrame>
  );
}
