"use client";

import { ThinkingTimeline, type ThinkingAgent } from "@keelson/ui";
import { useEffect, useState } from "react";
import { agentDescription } from "@/lib/agents";
import {
  selectHeadline,
  selectStages,
  timelineStatus,
  type InterruptEntry,
  type RunEntry,
} from "@/lib/stream/reducer";

/** Map a run from the stream reducer onto `@keelson/ui` ThinkingTimeline props. */
export function timelineProps(run: RunEntry, interrupts: Record<string, InterruptEntry>) {
  const agents: ThinkingAgent[] = run.agents.map((a) => ({
    id: a.id,
    name: a.name,
    description: agentDescription(a.slug),
    state: a.state,
    activities: a.activities.map((x) => ({ id: x.id, label: x.label, state: x.state })),
  }));
  const reasoning = run.reasoning
    .filter((r): r is typeof r & { agentId: string } => !!r.agentId && r.text.trim().length > 0)
    .map((r) => ({ agentId: r.agentId, text: r.text.trim() }));
  return {
    status: timelineStatus(run, interrupts),
    headline: selectHeadline(run, interrupts),
    stages: selectStages(run, interrupts),
    agents,
    reasoning,
  };
}

/** Elapsed time of a run; ticks once a second while it runs. */
function useElapsed(run: RunEntry): number | undefined {
  const running = run.status === "running";
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [running]);
  if (run.startedAt === null) return undefined;
  const end = running ? now : (run.finishedAt ?? null);
  return end === null ? undefined : Math.max(0, end - run.startedAt);
}

export function RunTimeline({
  run,
  interrupts,
  onStop,
  label,
}: {
  run: RunEntry;
  interrupts: Record<string, InterruptEntry>;
  onStop?: (() => void) | undefined;
  /** Unique accessible name of the timeline region (one per turn). */
  label?: string | undefined;
}) {
  const elapsedMs = useElapsed(run);
  const props = timelineProps(run, interrupts);
  return <ThinkingTimeline {...props} elapsedMs={elapsedMs} onStop={onStop} label={label} />;
}
