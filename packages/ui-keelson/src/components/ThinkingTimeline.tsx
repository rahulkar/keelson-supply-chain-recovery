"use client";

import { Collapsible } from "@base-ui/react/collapsible";
import type { StageId } from "@keelson/tokens";
import { Check, ChevronDown, CircleAlert, CirclePause, CircleStop, CircleX } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { cn } from "../lib/cn";
import { formatElapsed } from "../lib/format";
import { Avatar, AvatarStack } from "./Avatar";
import { IconButton } from "./Button";
import { Shimmer } from "./Shimmer";
import { Spinner } from "./Spinner";
import { StageChips } from "./StageChips";

export type ThinkingStatus = "running" | "paused" | "done" | "error" | "cancelled";

export type ThinkingActivity = { id: string; label: string; state: "running" | "done" | "error" };

export type ThinkingAgent = {
  id: string;
  name: string;
  description: string;
  state: "running" | "done" | "error";
  activities: ThinkingActivity[];
};

export type ThinkingTimelineProps = {
  status: ThinkingStatus;
  /** e.g. "Checking your systems…" (collapsed one-liner with shimmer when running). */
  headline: string;
  stages: { id: StageId; label: string; state: "pending" | "active" | "done" }[];
  agents: ThinkingAgent[];
  reasoning?: { agentId: string; text: string }[] | undefined;
  elapsedMs?: number | undefined;
  defaultOpen?: boolean | undefined;
  onStop?: (() => void) | undefined;
  /**
   * Accessible name of the timeline region (default "Thinking"). A thread shows one timeline per
   * turn: give each a unique name (e.g. "Thinking, turn 2") so the landmarks can be told apart.
   */
  label?: string | undefined;
};

const statusText: Record<ThinkingStatus, string> = {
  running: "Working",
  paused: "Waiting for you",
  done: "Done",
  error: "Something went wrong",
  cancelled: "Stopped",
};

function StatusGlyph({ status }: { status: ThinkingStatus }) {
  switch (status) {
    case "running":
      return <Spinner size={16} className="text-accent" />;
    case "paused":
      return <CirclePause aria-hidden="true" className="size-4 text-warning" />;
    case "done":
      return <Check aria-hidden="true" className="size-4 text-success" strokeWidth={2.5} />;
    case "error":
      return <CircleAlert aria-hidden="true" className="size-4 text-danger" />;
    case "cancelled":
      return <CircleStop aria-hidden="true" className="size-4 text-fg-subtle" />;
  }
}

function ActivityGlyph({ state }: { state: ThinkingActivity["state"] }) {
  if (state === "running") return <Spinner size={12} className="mt-1 text-accent" />;
  if (state === "error") return <CircleX aria-hidden="true" className="mt-0.5 size-3.5 text-danger" />;
  return <Check aria-hidden="true" className="mt-0.5 size-3.5 text-success" strokeWidth={2.5} />;
}

const agentStateText: Record<ThinkingAgent["state"], string> = {
  running: "working",
  done: "finished",
  error: "ran into a problem",
};

/** Consecutive activities with the same sentence, shown once with a count ("… · 3 times"). */
export type ThinkingActivityGroup = { id: string; label: string; state: ThinkingActivity["state"]; count: number };

/**
 * Groups runs of identical activity sentences (parallel specialists often repeat a step, e.g.
 * one "Read supplier messages" per supplier). A group is running while any member runs, and
 * failed when any member failed.
 */
export function groupActivities(activities: readonly ThinkingActivity[]): ThinkingActivityGroup[] {
  const out: ThinkingActivityGroup[] = [];
  for (const a of activities) {
    const last = out[out.length - 1];
    if (last && last.label === a.label) {
      last.count += 1;
      if (a.state === "running" || (a.state === "error" && last.state !== "running")) last.state = a.state;
    } else {
      out.push({ id: a.id, label: a.label, state: a.state, count: 1 });
    }
  }
  return out;
}

/**
 * Activity rows shown per agent; older ones fold behind "Show N earlier steps". The **latest**
 * rows are the ones kept, so the step an agent is working on right now is always visible.
 */
export const ACTIVITY_PREVIEW = 6;

/** "5 agents · 2 working · 1 ran into a problem" (shown when 4 or more agents take part). */
export function agentsSummary(agents: readonly Pick<ThinkingAgent, "state">[]): string {
  const working = agents.filter((a) => a.state === "running").length;
  const failed = agents.filter((a) => a.state === "error").length;
  return [
    `${agents.length} agents`,
    working ? `${working} working` : null,
    failed ? `${failed} ran into a problem` : null,
    !working && !failed ? "all finished" : null,
  ]
    .filter(Boolean)
    .join(" · ");
}

/** Agents before which the timeline summarises the group (avatar stack overflow, summary line). */
export const MANY_AGENTS = 4;

function AgentRow({ agent, reasoning }: { agent: ThinkingAgent; reasoning: string[] }) {
  const [open, setOpen] = useState(false);
  const [showAll, setShowAll] = useState(false);
  const count = agent.activities.length;
  const groups = useMemo(() => groupActivities(agent.activities), [agent.activities]);
  const shown = showAll ? groups : groups.slice(-ACTIVITY_PREVIEW);
  const hidden = groups.length - shown.length;
  const folds = groups.length > ACTIVITY_PREVIEW;
  return (
    <li className="rounded-md" data-agent={agent.id} data-state={agent.state}>
      <Collapsible.Root open={open} onOpenChange={setOpen}>
        <Collapsible.Trigger
          className={cn(
            "group flex w-full items-center gap-3 rounded-md px-2 py-1.5 text-left",
            "hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus",
          )}
        >
          <Avatar name={agent.name} size="sm" decorative />
          <span className="min-w-0 flex-1">
            <span className="block truncate text-ui font-medium text-fg">{agent.name}</span>
            <span className="block truncate text-caption text-fg-muted">{agent.description}</span>
          </span>
          <span className="sr-only">, {agentStateText[agent.state]}</span>
          <span className="flex shrink-0 items-center gap-2 text-caption text-fg-subtle">
            {agent.state === "running" ? <Spinner size={12} className="text-accent" /> : null}
            {agent.state === "error" ? <CircleAlert aria-hidden="true" className="size-3.5 text-danger" /> : null}
            {count > 0 ? <span>{count === 1 ? "1 step" : `${count} steps`}</span> : null}
            <ChevronDown
              aria-hidden="true"
              className="size-4 transition-transform duration-150 group-data-panel-open:rotate-180"
            />
          </span>
        </Collapsible.Trigger>
        <Collapsible.Panel className="overflow-hidden">
          <div className="ml-[2.375rem] border-l border-border pt-1 pb-2 pl-3">
            {count > 0 ? (
              <>
                {folds ? (
                  <button
                    type="button"
                    aria-expanded={showAll}
                    onClick={() => setShowAll((v) => !v)}
                    className="mb-1.5 rounded-sm text-caption font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
                  >
                    {showAll ? "Show fewer steps" : `Show ${hidden} earlier ${hidden === 1 ? "step" : "steps"}`}
                  </button>
                ) : null}
                <ul aria-label={`${agent.name} activity`} className="flex flex-col gap-1.5">
                  {shown.map((a) => (
                    <li key={a.id} className="flex items-start gap-2 text-ui text-fg-muted" data-state={a.state} data-count={a.count}>
                      <ActivityGlyph state={a.state} />
                      <span className="min-w-0 flex-1">
                        {a.label}
                        {a.count > 1 ? <span className="text-fg-subtle"> · {a.count} times</span> : null}
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            ) : (
              <p className="text-ui text-fg-subtle">No steps yet.</p>
            )}
            {reasoning.length > 0 ? (
              <div className="mt-2 rounded-sm bg-surface-2 px-3 py-2 text-ui whitespace-pre-wrap text-fg-muted italic">
                {reasoning.join("\n\n")}
              </div>
            ) : null}
          </div>
        </Collapsible.Panel>
      </Collapsible.Root>
    </li>
  );
}

/**
 * Progressive-disclosure "thinking" display (plan §4): a single status line (shimmering while
 * running) that expands into stage chips, the agents consulted with their activity sentences,
 * and optional reasoning. Collapsed by default. Only stage changes are announced
 * (`aria-live="polite"`). Honors `prefers-reduced-motion`.
 */
export function ThinkingTimeline({
  status,
  headline,
  stages,
  agents,
  reasoning = [],
  elapsedMs,
  defaultOpen = false,
  onStop,
  label = "Thinking",
}: ThinkingTimelineProps) {
  const [open, setOpen] = useState(defaultOpen);

  // Announce stage changes only (not activities / reasoning). Keyed by stage id, so a stage
  // flipping from active to done is not announced twice. A timeline that mounts for a run that
  // is not live (thread reload, earlier turns) stays silent until its stage actually changes.
  const currentStage = useMemo(() => {
    const active = stages.find((s) => s.state === "active");
    if (active) return active;
    const done = stages.filter((s) => s.state === "done");
    return done[done.length - 1];
  }, [stages]);
  const [announcement, setAnnouncement] = useState("");
  const lastAnnounced = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const id = currentStage?.id ?? null;
    if (lastAnnounced.current === undefined) {
      lastAnnounced.current = id;
      if (currentStage && status === "running") setAnnouncement(currentStage.label);
      return;
    }
    if (!currentStage || id === lastAnnounced.current) return;
    lastAnnounced.current = id;
    setAnnouncement(currentStage.label);
  }, [currentStage, status]);

  const reasoningByAgent = useMemo(() => {
    const map = new Map<string, string[]>();
    for (const r of reasoning) {
      if (!r.text.trim()) continue;
      const list = map.get(r.agentId) ?? [];
      list.push(r.text);
      map.set(r.agentId, list);
    }
    return map;
  }, [reasoning]);
  const agentIds = new Set(agents.map((a) => a.id));
  const orphanReasoning: string[] = [];
  for (const [agentId, texts] of reasoningByAgent) if (!agentIds.has(agentId)) orphanReasoning.push(...texts);

  const running = status === "running";
  const elapsed = elapsedMs !== undefined ? formatElapsed(elapsedMs) : null;
  let line: ReactNode = headline;
  if (running) line = <Shimmer>{headline}</Shimmer>;

  return (
    <section
      aria-label={label}
      data-status={status}
      className={cn(
        "rounded-lg border border-transparent transition-colors duration-200",
        open && "border-border bg-surface",
        status === "error" && "border-danger/40",
      )}
    >
      <div role="status" aria-live="polite" aria-atomic="true" className="sr-only" data-testid="thinking-live">
        {announcement}
      </div>
      <Collapsible.Root open={open} onOpenChange={setOpen}>
        <div className="flex min-h-11 items-center gap-2 px-2">
          <Collapsible.Trigger
            className={cn(
              "group flex min-h-9 min-w-0 flex-1 items-center gap-2.5 rounded-md px-1.5 text-left",
              "focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus",
            )}
          >
            <span className="inline-flex w-4 shrink-0 justify-center">
              <StatusGlyph status={status} />
            </span>
            <span className="min-w-0 flex-1 truncate text-ui font-medium text-fg">
              <span className="sr-only">{statusText[status]}: </span>
              {line}
            </span>
            <span className="inline-flex shrink-0 items-center gap-1 text-caption font-medium text-fg-muted group-hover:text-fg">
              <span className="sr-only sm:not-sr-only">{open ? "Hide thinking" : "Show thinking"}</span>
              <ChevronDown
                aria-hidden="true"
                className="size-4 transition-transform duration-150 group-data-panel-open:rotate-180"
              />
            </span>
          </Collapsible.Trigger>
          {agents.length > 0 ? <AvatarStack agents={agents} max={3} size="xs" className="hidden shrink-0 sm:flex" /> : null}
          {elapsed ? (
            <span className="shrink-0 text-caption text-fg-subtle tabular-nums">
              <span className="sr-only">Elapsed </span>
              {elapsed}
            </span>
          ) : null}
          {running && onStop ? (
            <IconButton
              label="Stop"
              size="sm"
              variant="ghost"
              icon={<CircleStop className="size-4" />}
              onClick={onStop}
            />
          ) : null}
        </div>
        <Collapsible.Panel className="overflow-hidden">
          <div className="flex flex-col gap-3 px-3 pt-1 pb-3">
            <StageChips stages={stages} />
            {agents.length >= MANY_AGENTS ? (
              <p className="px-2 text-caption text-fg-muted" data-agents-summary="">
                {agentsSummary(agents)}
              </p>
            ) : null}
            {agents.length > 0 ? (
              <ul aria-label="Agents consulted" className="flex flex-col gap-0.5">
                {agents.map((agent) => (
                  <AgentRow key={agent.id} agent={agent} reasoning={reasoningByAgent.get(agent.id) ?? []} />
                ))}
              </ul>
            ) : null}
            {orphanReasoning.length > 0 ? (
              <div className="rounded-sm bg-surface-2 px-3 py-2 text-ui whitespace-pre-wrap text-fg-muted italic">
                {orphanReasoning.join("\n\n")}
              </div>
            ) : null}
          </div>
        </Collapsible.Panel>
      </Collapsible.Root>
    </section>
  );
}
