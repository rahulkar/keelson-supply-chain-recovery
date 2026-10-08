/**
 * Agent playground (`docs/contracts/110` §3): `POST /api/admin/agents/playground {specialist,
 * question}` runs one investigator + structurer turn of a saved or draft custom specialist on a
 * scratch case (tenant-scoped, budget-capped, never commits). Its activity streams as AG-UI
 * events (`text/event-stream`); the outcome is a `PlaygroundResult` — sent as
 * `CUSTOM {name: "sc.playground.result", value}`, in `RUN_FINISHED.result`, or as the whole JSON
 * answer when the server does not stream. This module folds those frames into what the panel shows.
 */
import { EventType, agentsPlaygroundResultSchema, parseAgUiEvent, type AgentsPlaygroundResult } from "@keelson/contracts";
import { z } from "zod";
import { runningActivityLabel } from "../activityLabels";
import { GENERIC_STEP_ERROR, publicErrorMessage } from "../errors";

export type PlaygroundResult = AgentsPlaygroundResult;

export const PLAYGROUND_RESULT_EVENT = "sc.playground.result";
export const PLAYGROUND_MAX_QUESTION_CHARS = 2000;
/** Live reasoning shown at most (older text is dropped from the start). */
const REASONING_MAX = 4000;

export const playgroundResultSchema = agentsPlaygroundResultSchema.strip();

export type PlaygroundActivity = { id: string; label: string; state: "running" | "done" | "error" };

export type PlaygroundState = {
  status: "idle" | "running" | "done" | "error";
  agent: string | null;
  activities: PlaygroundActivity[];
  reasoning: string;
  result: PlaygroundResult | null;
  error: string | null;
};

export const PLAYGROUND_IDLE: PlaygroundState = { status: "idle", agent: null, activities: [], reasoning: "", result: null, error: null };

export function startPlayground(): PlaygroundState {
  return { ...PLAYGROUND_IDLE, status: "running" };
}

function resultOf(value: unknown): PlaygroundResult | null {
  const direct = playgroundResultSchema.safeParse(value);
  if (direct.success) return direct.data;
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const inner = (value as Record<string, unknown>)["result"];
    if (inner !== undefined) {
      const p = playgroundResultSchema.safeParse(inner);
      if (p.success) return p.data;
    }
  }
  return null;
}

function finish(s: PlaygroundState, result: PlaygroundResult): PlaygroundState {
  const failed = result.status !== "completed";
  return {
    ...s,
    status: failed ? "error" : "done",
    result,
    error: failed ? publicErrorMessage(result.error?.message, result.status === "budget_exceeded" ? "The playground's budget ran out." : GENERIC_STEP_ERROR) : null,
    activities: s.activities.map((a) => (a.state === "running" ? { ...a, state: failed ? "error" : "done" } : a)),
  };
}

/** Fold one stream frame (an AG-UI event, or a bare result) into the panel state. Unknown frames are ignored. */
export function playgroundReduce(s: PlaygroundState, frame: unknown): PlaygroundState {
  const parsed = parseAgUiEvent(frame);
  if (!parsed.ok) {
    const result = resultOf(frame);
    return result ? finish(s, result) : s;
  }
  const { event, sc } = parsed;
  switch (event.type) {
    case EventType.SUBAGENT_STARTED:
      return { ...s, agent: event.description || event.name };
    case EventType.TOOL_CALL_START:
      if (s.activities.some((a) => a.id === event.toolCallId)) return s;
      return { ...s, activities: [...s.activities, { id: event.toolCallId, label: runningActivityLabel(event.toolCallName, sc?.label), state: "running" }] };
    case EventType.TOOL_CALL_RESULT: {
      const label = sc?.label || "Done";
      const idx = s.activities.findIndex((a) => a.id === event.toolCallId);
      const activities = [...s.activities];
      if (idx >= 0) activities[idx] = { ...activities[idx]!, label, state: "done" };
      else activities.push({ id: event.toolCallId, label, state: "done" });
      return { ...s, activities };
    }
    case EventType.REASONING_MESSAGE_CONTENT: {
      const text = (s.reasoning + event.delta).slice(-REASONING_MAX);
      return { ...s, reasoning: text };
    }
    case EventType.SUBAGENT_ERROR:
    case EventType.RUN_ERROR:
      return {
        ...s,
        status: "error",
        error: publicErrorMessage(event.message, GENERIC_STEP_ERROR),
        activities: s.activities.map((a) => (a.state === "running" ? { ...a, state: "error" } : a)),
      };
    case EventType.CUSTOM: {
      if (event.name !== PLAYGROUND_RESULT_EVENT) return s;
      const result = resultOf(event.value);
      return result ? finish(s, result) : s;
    }
    case EventType.RUN_FINISHED: {
      const result = resultOf((event as { result?: unknown }).result);
      return result ? finish(s, result) : s;
    }
    default:
      return s;
  }
}

/** After the stream closed: a turn that never reported its outcome reads as failed. */
export function settlePlayground(s: PlaygroundState): PlaygroundState {
  if (s.status !== "running") return s;
  return {
    ...s,
    status: "error",
    error: "The playground stopped without a result.",
    activities: s.activities.map((a) => (a.state === "running" ? { ...a, state: "error" } : a)),
  };
}

/** "3 tool calls · 4 model calls · 12,400 tokens · 8.2 s". */
export function usageLine(r: PlaygroundResult): string {
  const nf = new Intl.NumberFormat("en-GB");
  const parts = [
    `${nf.format(r.usage.tool_calls)} tool ${r.usage.tool_calls === 1 ? "call" : "calls"}`,
    `${nf.format(r.usage.llm_calls)} model ${r.usage.llm_calls === 1 ? "call" : "calls"}`,
    `${nf.format(r.usage.tokens_in + r.usage.tokens_out)} tokens`,
    `${(r.duration_ms / 1000).toFixed(1)} s`,
  ];
  return parts.join(" · ");
}

/** `GET /api/admin/agents/playground`: can a turn run here at all? */
export const playgroundStatusSchema = z
  .object({
    available: z.boolean(),
    mode: z.string().nullable().optional(),
    reason: z.string().nullable().optional(),
    message: z.string().nullable().optional(),
  })
  .loose();
export type PlaygroundStatus = z.infer<typeof playgroundStatusSchema>;

/** The panel's disabled reason for an unavailable playground (`null` = it can run). */
export function playgroundUnavailable(status: PlaygroundStatus | undefined): string | null {
  if (!status || status.available) return null;
  if (status.reason === "replay_only") return "Unavailable: this installation only replays recorded model answers, so it cannot answer a new question.";
  if (status.reason === "disabled") return "Unavailable: the playground is switched off in this installation.";
  return status.message ? `Unavailable: ${status.message}` : "Unavailable in this installation.";
}
