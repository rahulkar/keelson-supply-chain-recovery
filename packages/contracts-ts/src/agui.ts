import { EventType, type Event as AgUiCoreEvent } from "@ag-ui/core";
import { EventSchemas } from "@ag-ui/core/schemas";
import { z } from "zod";
import { formatIssues, toBlockEnvelope, type BlockEnvelope } from "./blocks";
import { aguiInterruptSchema, aguiScMetadataSchema } from "./generated";

// ───────────────────────────── vocabularies ──────────────────────────────

/** `metadata.sc` carried by every Keelson AG-UI event (camelCase on the wire). */
export const scMetadataSchema = aguiScMetadataSchema;
export type ScMetadata = z.infer<typeof scMetadataSchema>;

/** One entry of `RUN_FINISHED.outcome.interrupts` as emitted by the engine. */
export const interruptSpecSchema = aguiInterruptSchema;
export type InterruptSpec = z.infer<typeof interruptSpecSchema>;

/** Fixed stage vocabulary (`STEP_*.stepName` and `metadata.sc.stage`), in workflow order. */
export const STAGE_IDS = [
  "understanding",
  "checking_systems",
  "measuring_impact",
  "building_options",
  "ready_for_review",
  "preparing_changes",
  "awaiting_approval",
  "applying",
  "verified",
] as const satisfies readonly ScMetadata["stage"][];
export type StageId = (typeof STAGE_IDS)[number];

/** Investigation vs remediation phase of each stage (plan §4: max 5 stages per phase). */
export const STAGE_PHASES = {
  investigation: ["understanding", "checking_systems", "measuring_impact", "building_options", "ready_for_review"],
  remediation: ["preparing_changes", "awaiting_approval", "applying", "verified"],
} as const satisfies Record<string, readonly StageId[]>;

export function isStageId(value: unknown): value is StageId {
  return typeof value === "string" && (STAGE_IDS as readonly string[]).includes(value);
}

/** The only LLM roles that emit SUBAGENT_* (`50-engine-workflow.md`). */
export const AGENT_DISPLAY_NAMES = {
  intake: "Intake",
  planner: "Planner",
  inventory_demand: "Inventory & demand analyst",
  sourcing: "Sourcing analyst",
  logistics: "Logistics analyst",
  playbook: "Playbook analyst",
  recommender: "Strategist",
  critic: "Critic",
  external_intel: "External intelligence",
} as const;
export type AgentSlug = keyof typeof AGENT_DISPLAY_NAMES;

export const ACTIVITY_TYPE_RENDER = "sc.render";
export const CASE_UPDATED_EVENT = "sc.case.updated";
export const AGUI_PROTOCOL_VERSION = "1.0";

/** AG-UI event types the Keelson engine emits in M1 (`30-engine-api-events.md`). */
export const KEELSON_EVENT_TYPES = [
  EventType.RUN_STARTED,
  EventType.RUN_FINISHED,
  EventType.RUN_ERROR,
  EventType.STEP_STARTED,
  EventType.STEP_FINISHED,
  EventType.SUBAGENT_STARTED,
  EventType.SUBAGENT_FINISHED,
  EventType.SUBAGENT_ERROR,
  EventType.TOOL_CALL_START,
  EventType.TOOL_CALL_END,
  EventType.TOOL_CALL_RESULT,
  EventType.REASONING_START,
  EventType.REASONING_MESSAGE_START,
  EventType.REASONING_MESSAGE_CONTENT,
  EventType.REASONING_MESSAGE_END,
  EventType.REASONING_END,
  EventType.ACTIVITY_SNAPSHOT,
  EventType.TEXT_MESSAGE_START,
  EventType.TEXT_MESSAGE_CONTENT,
  EventType.TEXT_MESSAGE_END,
  EventType.CUSTOM,
] as const;

export { EventType };
/** Any AG-UI 1.0 event, as typed by `@ag-ui/core`. */
export type AgUiEvent = AgUiCoreEvent;
/** Narrow an AG-UI event by its `type`. */
export type AgUiEventOf<T extends EventType> = Extract<AgUiEvent, { type: T }>;

/** `@ag-ui/core` 1.0.2 exports the event union schema as `EventSchemas` (alias of `EventSchema`) from `@ag-ui/core/schemas`. */
export const agUiEventSchema = EventSchemas;

// ───────────────────────────── parsing ───────────────────────────────────

export type ParseAgUiEventError = {
  ok: false;
  /** `invalid_json`: not JSON; `invalid_event`: fails the AG-UI 1.0 schema; `invalid_sc`: bad `metadata.sc`. */
  code: "invalid_json" | "invalid_event" | "invalid_sc";
  message: string;
  issues: string[];
};

export type ParsedAgUiEvent = {
  ok: true;
  event: AgUiEvent;
  /** Keelson's `metadata.sc`, or `null` when the event carries none. */
  sc: ScMetadata | null;
};

export type ParseAgUiEventResult = ParsedAgUiEvent | ParseAgUiEventError;

/**
 * Validate one AG-UI 1.0 event (an SSE `data:` payload string or an already-decoded object)
 * with the official `@ag-ui/core` Zod schemas, and extract/validate Keelson's `metadata.sc`.
 *
 * Never throws. An event whose `metadata.sc` is present but malformed is rejected
 * (`invalid_sc`); an event without `metadata.sc` is accepted with `sc: null`.
 */
export function parseAgUiEvent(json: unknown): ParseAgUiEventResult {
  let value: unknown = json;
  if (typeof json === "string") {
    try {
      value = JSON.parse(json);
    } catch (err) {
      return {
        ok: false,
        code: "invalid_json",
        message: err instanceof Error ? err.message : "Malformed JSON",
        issues: [],
      };
    }
  }
  const parsed = agUiEventSchema.safeParse(value);
  if (!parsed.success) {
    const issues = formatIssues(parsed.error);
    return { ok: false, code: "invalid_event", message: issues[0] ?? "Invalid AG-UI event", issues };
  }
  const event = parsed.data as AgUiEvent;
  const rawSc = (event.metadata as Record<string, unknown> | undefined)?.["sc"];
  if (rawSc === undefined) return { ok: true, event, sc: null };
  const sc = scMetadataSchema.safeParse(rawSc);
  if (!sc.success) {
    const issues = formatIssues(sc.error).map((i) => `metadata.sc.${i}`);
    return { ok: false, code: "invalid_sc", message: issues[0] ?? "Invalid metadata.sc", issues };
  }
  return { ok: true, event, sc: sc.data };
}

/** The render-block envelope carried by an `ACTIVITY_SNAPSHOT{activityType:"sc.render"}`, else `null`. */
export function renderBlockFromEvent(event: AgUiEvent): BlockEnvelope | null {
  if (event.type !== EventType.ACTIVITY_SNAPSHOT || event.activityType !== ACTIVITY_TYPE_RENDER) return null;
  return toBlockEnvelope(event.content);
}

/**
 * Keelson interrupts from a `RUN_FINISHED{outcome:{type:"interrupt"}}` (validated against
 * `InterruptSpec`; malformed entries are dropped). Empty for any other event.
 */
export function interruptsFromEvent(event: AgUiEvent): InterruptSpec[] {
  if (event.type !== EventType.RUN_FINISHED) return [];
  const outcome = event.outcome;
  if (!outcome || outcome.type !== "interrupt") return [];
  const out: InterruptSpec[] = [];
  for (const raw of outcome.interrupts) {
    const parsed = interruptSpecSchema.safeParse(raw);
    if (parsed.success) out.push(parsed.data);
  }
  return out;
}

/**
 * The run an event belongs to: `RUN_STARTED` / `RUN_FINISHED` carry `runId` themselves; every other
 * event (notably `RUN_ERROR`, which has no `runId` in AG-UI 1.0) is attributed by
 * `metadata.sc.runId`. `null` for events recorded before the engine set `sc.runId`.
 */
export function runIdOfEvent(event: AgUiEvent, sc: ScMetadata | null): string | null {
  if (event.type === EventType.RUN_STARTED || event.type === EventType.RUN_FINISHED) return event.runId;
  return sc?.runId ?? null;
}

const ERROR_ID_RE = /^[0-9A-Za-z][0-9A-Za-z_-]{7,63}$/;

/**
 * `metadata.errorId` of a `RUN_ERROR` / `SUBAGENT_ERROR` (the reference support uses to find the
 * logged failure), or `null`. Only an opaque id shape is accepted, so free text is never shown
 * as an "error ID".
 */
export function errorIdFromEvent(event: AgUiEvent): string | null {
  const value = (event.metadata as Record<string, unknown> | undefined)?.["errorId"];
  return typeof value === "string" && ERROR_ID_RE.test(value) ? value : null;
}

/** `CUSTOM{name:"sc.case.updated"}` payload, else `null`. */
export const caseUpdatedValueSchema = z
  .object({
    mode: z.string().optional(),
    status: z.string().optional(),
    title: z.string().optional(),
  })
  .loose();
export type CaseUpdatedValue = z.infer<typeof caseUpdatedValueSchema>;

export function caseUpdateFromEvent(event: AgUiEvent): CaseUpdatedValue | null {
  if (event.type !== EventType.CUSTOM || event.name !== CASE_UPDATED_EVENT) return null;
  const parsed = caseUpdatedValueSchema.safeParse(event.value);
  return parsed.success ? parsed.data : null;
}
