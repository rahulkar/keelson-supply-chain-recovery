/**
 * Case stream reducer (`65-web-internals.md` → useCaseStream).
 *
 * Folds the case's AG-UI 1.0 events (replayed from Postgres, then tailed live) into:
 *   stages · agents/activities (SUBAGENT_*, TOOL_CALL_* using `metadata.sc.label`) ·
 *   reasoning (append live deltas, replace on the persisted aggregate) · blocks (ACTIVITY_SNAPSHOT
 *   `sc.render`, by block id) · messages (TEXT_MESSAGE_*) · interrupts (RUN_FINISHED outcome
 *   interrupt) · runStatus — deduplicated by `metadata.sc.seq`.
 *
 * Stages, agents and reasoning are kept **per run**, because the thread shows one thinking
 * timeline per turn; `selectCurrentRun` gives the latest run's view.
 *
 * Engine facts this relies on (verified against `keelson_engine.runtime.translator` / `core.events`):
 * - persisted events carry `metadata.sc.seq` (= SSE `id`), strictly increasing per case;
 * - live-only events (reasoning deltas) have no seq and must never be deduplicated;
 * - the reasoning aggregate is persisted once (seq set) and replaces the streamed deltas;
 * - a stage can be re-entered within a run (e.g. understanding → measuring_impact → understanding);
 * - every event carries `metadata.sc.runId` (AG-UI 1.0 RUN_ERROR has no `runId` of its own; a
 *   queued run cancelled before it started has no RUN_STARTED, but its RUN_ERROR names it), so
 *   events are attributed by run id. Events recorded before the engine set `sc.runId` (legacy
 *   cases) fall back to the current run;
 * - RUN_ERROR's message is generic text ending in "… error ID <uuid>" (+ `metadata.errorId`);
 *   it is still screened here (`lib/errors.ts`) so a stack trace can never be shown;
 * - a new run does NOT by itself close a pending interrupt: only a resume (decision), a new plan
 *   version, a promotion or a commit resolves/supersedes it, and a follow-up question asked while
 *   an approval is pending leaves the approval open. The events do not say which run resumed
 *   what, so whether an interrupt is still open comes from the case snapshot
 *   (`GET /api/cases/{id}` → `open_interrupts`, as of `last_event_seq`): see `server_interrupts`.
 */
import {
  AGENT_DISPLAY_NAMES,
  EventType,
  STAGE_PHASES,
  caseUpdateFromEvent,
  errorIdFromEvent,
  interruptsFromEvent,
  isStageId,
  renderBlockFromEvent,
  type AgUiEvent,
  type BlockEnvelope,
  type CaseUpdatedValue,
  type InterruptSpec,
  type ScMetadata,
  type StageId,
} from "@keelson/contracts";
import { stageLabels } from "@keelson/tokens";
import { runningActivityLabel } from "../activityLabels";
import { GENERIC_RUN_ERROR, GENERIC_STEP_ERROR, publicErrorId, publicErrorMessage } from "../errors";

// ───────────────────────────── state ─────────────────────────────

/** `queued`: posted (202) but RUN_STARTED not seen yet. */
export type RunStatus = "queued" | "running" | "paused" | "done" | "error" | "cancelled";
export type ActivityState = "running" | "done" | "error";

export type ActivityEntry = {
  id: string;
  /** Humanised sentence (`metadata.sc.label`), never a tool name. */
  label: string;
  state: ActivityState;
  /** Real tool name: Trace tab only. */
  toolName: string | null;
  evidenceRefs: string[];
};

export type AgentEntry = {
  /** `subagentRunId` (`<invocation_id>:<node path>`). */
  id: string;
  /** Agent slug (`intake`, `inventory_demand`, …). */
  slug: string;
  name: string;
  state: ActivityState;
  activities: ActivityEntry[];
  error: string | null;
};

export type ReasoningEntry = {
  id: string;
  agentId: string | null;
  text: string;
  /** The persisted aggregate arrived: later live deltas are ignored. */
  final: boolean;
};

export type RunEntry = {
  id: string;
  status: RunStatus;
  startedAt: number | null;
  finishedAt: number | null;
  /** Stages entered, in first-visit order. */
  visited: StageId[];
  active: StageId | null;
  agents: AgentEntry[];
  reasoning: ReasoningEntry[];
  messageIds: string[];
  blockIds: string[];
  interruptIds: string[];
  error: RunError | null;
};

/** A run's failure as shown to people: screened message, enum-like code, support reference. */
export type RunError = { message: string; code: string | null; errorId: string | null };

export type BlockEntry = {
  envelope: BlockEnvelope;
  /** Run that last emitted (created or replaced) the block. */
  runId: string | null;
  seq: number | null;
  /** How many snapshots of this block were seen (1 = first). */
  revision: number;
  /** Whether the latest snapshot arrived live (after the replay caught up). */
  live: boolean;
};

export type MessageEntry = { id: string; runId: string | null; role: string; text: string; done: boolean };

/** A decision this user made in this page (`interrupt_decided`). */
export type LocalDecision = { decision: string | null; at: string | null; recorded: boolean };

export type InterruptEntry = InterruptSpec & {
  runId: string | null;
  open: boolean;
  /** `seq` of the RUN_FINISHED that opened it (`null`: not persisted, newer than any snapshot). */
  openedSeq: number | null;
  /** This user decided it (stays closed for them even while the quorum is incomplete). */
  decided: boolean;
  /** What this user decided here and when (the card shows it instead of its buttons). */
  localDecision?: LocalDecision | undefined;
};

/** Pending interrupts according to the case snapshot, authoritative up to `asOfSeq`. */
export type ServerInterrupts = { asOfSeq: number; open: ReadonlySet<string> };

export type CaseStreamState = {
  /** Highest persisted `seq` applied (the dedupe watermark). */
  lastSeq: number;
  runs: Record<string, RunEntry>;
  runOrder: string[];
  currentRunId: string | null;
  blocks: Record<string, BlockEntry>;
  blockOrder: string[];
  messages: Record<string, MessageEntry>;
  messageOrder: string[];
  interrupts: Record<string, InterruptEntry>;
  /** Latest snapshot view of the pending interrupts (`null` before the first snapshot). */
  serverInterrupts: ServerInterrupts | null;
  /** Status of the latest run (`idle` before any). */
  runStatus: RunStatus | "idle";
  caseMeta: CaseUpdatedValue | null;
  /** Events that failed validation (dropped). */
  invalidEvents: number;
};

export const initialCaseStreamState: CaseStreamState = {
  lastSeq: 0,
  runs: {},
  runOrder: [],
  currentRunId: null,
  blocks: {},
  blockOrder: [],
  messages: {},
  messageOrder: [],
  interrupts: {},
  serverInterrupts: null,
  runStatus: "idle",
  caseMeta: null,
  invalidEvents: 0,
};

export type CaseStreamAction =
  | {
      type: "event";
      event: AgUiEvent;
      sc: ScMetadata | null;
      /** Persisted sequence number, `null` for live-only events. */
      seq: number | null;
      /** Delivered live (after the initial replay caught up). */
      live?: boolean | undefined;
    }
  /** A run was accepted (202) but has not started: show the turn as queued. */
  | { type: "expect_run"; runId: string }
  /** The user decided an interrupt (the card stops being actionable for them). */
  | {
      type: "interrupt_decided";
      interruptId: string;
      decision?: string | undefined;
      at?: string | undefined;
      /** Recorded, but the quorum is incomplete (other approvers are still needed). */
      recorded?: boolean | undefined;
    }
  /**
   * The case snapshot's `open_interrupts` (PENDING on the server) as of its `last_event_seq`:
   * interrupts opened before that seq are open exactly when listed (see `openFor`).
   */
  | { type: "server_interrupts"; openIds: readonly string[]; asOfSeq: number }
  | { type: "invalid_event" }
  | { type: "reset" };

// ───────────────────────────── helpers ─────────────────────────────

function newRun(id: string, status: RunStatus): RunEntry {
  return {
    id,
    status,
    startedAt: null,
    finishedAt: null,
    visited: [],
    active: null,
    agents: [],
    reasoning: [],
    messageIds: [],
    blockIds: [],
    interruptIds: [],
    error: null,
  };
}

function agentName(slug: string, description: unknown): string {
  if (slug in AGENT_DISPLAY_NAMES) return AGENT_DISPLAY_NAMES[slug as keyof typeof AGENT_DISPLAY_NAMES];
  return typeof description === "string" && description ? description : slug;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function eventTimestamp(event: AgUiEvent): number | null {
  const ts = (event as { timestamp?: unknown }).timestamp;
  return typeof ts === "number" && Number.isFinite(ts) ? ts : null;
}

/** Mutable working copy of the state for one action (copy-on-write per touched entry). */
class Draft {
  readonly s: CaseStreamState;
  private copiedRuns = new Set<string>();

  constructor(prev: CaseStreamState) {
    this.s = { ...prev, runs: { ...prev.runs } };
  }

  run(id: string): RunEntry {
    const existing = this.s.runs[id];
    if (!existing) {
      const created = newRun(id, "running");
      this.s.runs[id] = created;
      this.s.runOrder = [...this.s.runOrder, id];
      this.copiedRuns.add(id);
      return created;
    }
    if (!this.copiedRuns.has(id)) {
      const copy: RunEntry = {
        ...existing,
        visited: [...existing.visited],
        agents: existing.agents.map((a) => ({ ...a, activities: [...a.activities] })),
        reasoning: [...existing.reasoning],
        messageIds: [...existing.messageIds],
        blockIds: [...existing.blockIds],
        interruptIds: [...existing.interruptIds],
      };
      this.s.runs[id] = copy;
      this.copiedRuns.add(id);
      return copy;
    }
    return existing;
  }

  /**
   * The run an event belongs to: `metadata.sc.runId` (created on demand, e.g. a replay that starts
   * after its RUN_STARTED), or — for legacy events recorded without it — the current run.
   */
  runOf(sc: ScMetadata | null, seq: number | null): RunEntry {
    const id = sc?.runId;
    return id ? this.run(id) : this.currentRun(seq);
  }

  /** The current run (created on demand). Only for legacy events without `sc.runId`. */
  currentRun(seq: number | null): RunEntry {
    const id = this.s.currentRunId;
    if (id && this.s.runs[id]) return this.run(id);
    // Events before any RUN_STARTED (a compacted or truncated history): an anonymous run.
    const anon = `run-${seq ?? this.s.runOrder.length + 1}`;
    this.s.currentRunId = anon;
    return this.run(anon);
  }
}

function findAgent(run: RunEntry, event: AgUiEvent, sc: ScMetadata | null): AgentEntry | null {
  const subRunId = str((event as { subagentRunId?: unknown }).subagentRunId);
  if (subRunId) {
    const exact = run.agents.find((a) => a.id === subRunId);
    if (exact) return exact;
  }
  const slug = sc?.agent ?? null;
  if (!slug) return null;
  for (let i = run.agents.length - 1; i >= 0; i--) {
    const a = run.agents[i] as AgentEntry;
    if (a.slug === slug && a.state === "running") return a;
  }
  for (let i = run.agents.length - 1; i >= 0; i--) {
    const a = run.agents[i] as AgentEntry;
    if (a.slug === slug) return a;
  }
  return null;
}

function settleAgents(run: RunEntry, outcome: "done" | "error"): void {
  for (const a of run.agents) {
    if (a.state === "running") a.state = outcome;
    a.activities = a.activities.map((x) => (x.state === "running" ? { ...x, state: outcome } : x));
  }
}

/**
 * Whether an interrupt is open: never once this user decided it; as the snapshot says when the
 * snapshot is strictly newer than the event that opened it; otherwise as the events say.
 *
 * Strictly: engine-api reads `open_interrupts` and `last_event_seq` in separate transactions, so
 * a snapshot taken while a run pauses can report the pausing RUN_FINISHED's seq with an interrupt
 * list from just before it existed. A pending approval must never look closed (nothing would
 * re-open it); at worst a resolved one stays clickable until the next run starts (and the server
 * answers 409).
 */
function openFor(it: InterruptEntry, server: ServerInterrupts | null): boolean {
  if (it.decided) return false;
  if (server && it.openedSeq !== null && it.openedSeq < server.asOfSeq) return server.open.has(it.id);
  return it.open;
}

// ───────────────────────────── reducer ─────────────────────────────

export function caseStreamReducer(state: CaseStreamState, action: CaseStreamAction): CaseStreamState {
  switch (action.type) {
    case "reset":
      return initialCaseStreamState;
    case "invalid_event":
      return { ...state, invalidEvents: state.invalidEvents + 1 };
    case "expect_run": {
      if (state.runs[action.runId]) return state;
      const run = newRun(action.runId, "queued");
      return {
        ...state,
        runs: { ...state.runs, [action.runId]: run },
        runOrder: [...state.runOrder, action.runId],
        runStatus: state.runStatus === "running" ? state.runStatus : "queued",
      };
    }
    case "interrupt_decided": {
      const it = state.interrupts[action.interruptId];
      if (!it || it.decided) return state;
      const localDecision: LocalDecision = { decision: action.decision ?? null, at: action.at ?? null, recorded: action.recorded ?? false };
      return { ...state, interrupts: { ...state.interrupts, [action.interruptId]: { ...it, open: false, decided: true, localDecision } } };
    }
    case "server_interrupts": {
      // An older snapshot (out-of-order refresh) never overrides a newer one.
      if (state.serverInterrupts && action.asOfSeq < state.serverInterrupts.asOfSeq) return state;
      const server: ServerInterrupts = { asOfSeq: action.asOfSeq, open: new Set(action.openIds) };
      let changed = false;
      const interrupts: Record<string, InterruptEntry> = {};
      for (const [id, it] of Object.entries(state.interrupts)) {
        const open = openFor(it, server);
        interrupts[id] = open === it.open ? it : { ...it, open };
        if (open !== it.open) changed = true;
      }
      return { ...state, serverInterrupts: server, interrupts: changed ? interrupts : state.interrupts };
    }
    case "event":
      return applyEvent(state, action);
  }
}

function applyEvent(state: CaseStreamState, action: Extract<CaseStreamAction, { type: "event" }>): CaseStreamState {
  const { event, sc } = action;
  const seq = action.seq !== null && action.seq > 0 ? action.seq : null;
  if (seq !== null && seq <= state.lastSeq) return state; // replay overlap / reconnect duplicate
  const d = new Draft(state);
  if (seq !== null) d.s.lastSeq = seq;
  const ts = eventTimestamp(event);

  switch (event.type) {
    case EventType.RUN_STARTED: {
      const run = d.run(event.runId);
      // Turns follow start order (a run announced as queued earlier moves to its real place).
      d.s.runOrder = [...d.s.runOrder.filter((id) => id !== run.id), run.id];
      run.status = "running";
      run.startedAt = ts ?? run.startedAt;
      run.finishedAt = null;
      run.error = null;
      d.s.currentRunId = event.runId;
      d.s.runStatus = "running";
      // Interrupts stay as they are: a new run may be a resume (resolves one) or just a follow-up
      // question (leaves it pending). The case snapshot settles it (`server_interrupts`).
      break;
    }

    case EventType.RUN_FINISHED: {
      // Legacy events (no sc.runId) of a run whose RUN_STARTED is missing went to the current run.
      const run = d.s.runs[event.runId] || sc?.runId ? d.run(event.runId) : d.currentRun(seq);
      const specs = interruptsFromEvent(event);
      run.status = specs.length > 0 ? "paused" : "done";
      run.finishedAt = ts;
      run.active = null;
      settleAgents(run, "done");
      if (specs.length > 0) {
        const interrupts = { ...d.s.interrupts };
        for (const spec of specs) {
          const entry: InterruptEntry = {
            ...spec,
            runId: run.id,
            open: true,
            openedSeq: seq,
            decided: d.s.interrupts[spec.id]?.decided ?? false,
            localDecision: d.s.interrupts[spec.id]?.localDecision,
          };
          interrupts[spec.id] = { ...entry, open: openFor(entry, d.s.serverInterrupts) };
          if (!run.interruptIds.includes(spec.id)) run.interruptIds.push(spec.id);
        }
        d.s.interrupts = interrupts;
      }
      if (d.s.currentRunId === run.id || d.s.currentRunId === null) d.s.runStatus = run.status;
      break;
    }

    case EventType.RUN_ERROR: {
      // AG-UI's RUN_ERROR has no runId: `metadata.sc.runId` says which run failed or was
      // cancelled — the running one, or a queued one cancelled before it ever started.
      const cancelled = event.code === "CANCELLED";
      const current = d.s.currentRunId ? d.s.runs[d.s.currentRunId] : undefined;
      const currentActive = !!current && (current.status === "running" || current.status === "queued");
      const run = d.runOf(sc, seq);
      if (run.status === "queued") {
        // Never started: its turn takes its place after the runs that did.
        d.s.runOrder = [...d.s.runOrder.filter((id) => id !== run.id), run.id];
      }
      run.status = cancelled ? "cancelled" : "error";
      run.finishedAt = ts;
      run.active = null;
      run.error = {
        message: publicErrorMessage(event.message, cancelled ? "Stopped." : GENERIC_RUN_ERROR),
        code: typeof event.code === "string" && /^[A-Z][A-Z0-9_]{1,63}$/.test(event.code) ? event.code : null,
        errorId: cancelled ? null : publicErrorId(errorIdFromEvent(event), event.message),
      };
      settleAgents(run, "error");
      // The case's status follows this run unless another run is still in progress.
      if (run.id === d.s.currentRunId || !currentActive) {
        d.s.currentRunId = run.id;
        d.s.runStatus = run.status;
      }
      break;
    }

    case EventType.STEP_STARTED: {
      if (!isStageId(event.stepName)) break;
      const run = d.runOf(sc, seq);
      if (!run.visited.includes(event.stepName)) run.visited.push(event.stepName);
      run.active = event.stepName;
      break;
    }

    case EventType.STEP_FINISHED: {
      const run = d.runOf(sc, seq);
      if (run.active === event.stepName) run.active = null;
      if (isStageId(event.stepName) && !run.visited.includes(event.stepName)) run.visited.push(event.stepName);
      break;
    }

    case EventType.SUBAGENT_STARTED: {
      const run = d.runOf(sc, seq);
      const existing = run.agents.find((a) => a.id === event.subagentRunId);
      if (existing) {
        existing.state = "running";
        existing.error = null;
      } else {
        run.agents.push({
          id: event.subagentRunId,
          slug: event.name,
          name: agentName(event.name, event.description),
          state: "running",
          activities: [],
          error: null,
        });
      }
      break;
    }

    case EventType.SUBAGENT_FINISHED:
    case EventType.SUBAGENT_ERROR: {
      const run = d.runOf(sc, seq);
      const agent = run.agents.find((a) => a.id === event.subagentRunId) ?? findAgent(run, event, sc);
      if (!agent) break;
      const failed = event.type === EventType.SUBAGENT_ERROR;
      agent.state = failed ? "error" : "done";
      agent.error = failed ? publicErrorMessage(event.message, GENERIC_STEP_ERROR) : null;
      agent.activities = agent.activities.map((x) =>
        x.state === "running" ? { ...x, state: failed ? "error" : "done" } : x,
      );
      break;
    }

    case EventType.TOOL_CALL_START: {
      const run = d.runOf(sc, seq);
      const agent = findAgent(run, event, sc);
      if (!agent || agent.activities.some((x) => x.id === event.toolCallId)) break;
      agent.activities.push({
        id: event.toolCallId,
        label: runningActivityLabel(event.toolCallName, sc?.label),
        state: "running",
        toolName: event.toolCallName,
        evidenceRefs: sc?.evidenceRefs ?? [],
      });
      break;
    }

    case EventType.TOOL_CALL_RESULT: {
      const run = d.runOf(sc, seq);
      const agent = findAgent(run, event, sc);
      if (!agent) break;
      const label = sc?.label || (typeof event.content === "string" ? event.content : "") || "Done";
      const idx = agent.activities.findIndex((x) => x.id === event.toolCallId);
      const refs = sc?.evidenceRefs ?? [];
      if (idx >= 0) {
        const prev = agent.activities[idx] as ActivityEntry;
        agent.activities[idx] = { ...prev, label, state: "done", evidenceRefs: refs.length ? refs : prev.evidenceRefs };
      } else {
        agent.activities.push({ id: event.toolCallId, label, state: "done", toolName: null, evidenceRefs: refs });
      }
      break;
    }

    case EventType.REASONING_START:
    case EventType.REASONING_MESSAGE_START: {
      const run = d.runOf(sc, seq);
      if (run.reasoning.some((r) => r.id === event.messageId)) break;
      run.reasoning.push({ id: event.messageId, agentId: findAgent(run, event, sc)?.id ?? null, text: "", final: false });
      break;
    }

    case EventType.REASONING_MESSAGE_CONTENT: {
      const run = d.runOf(sc, seq);
      let idx = run.reasoning.findIndex((r) => r.id === event.messageId);
      if (idx < 0) {
        run.reasoning.push({ id: event.messageId, agentId: findAgent(run, event, sc)?.id ?? null, text: "", final: false });
        idx = run.reasoning.length - 1;
      }
      const entry = run.reasoning[idx] as ReasoningEntry;
      if (seq !== null) {
        // Persisted aggregate: the whole thought summary, once.
        run.reasoning[idx] = { ...entry, text: event.delta, final: true };
      } else if (!entry.final) {
        run.reasoning[idx] = { ...entry, text: entry.text + event.delta };
      }
      break;
    }

    case EventType.TEXT_MESSAGE_START: {
      const run = d.runOf(sc, seq);
      if (!d.s.messages[event.messageId]) {
        d.s.messages = {
          ...d.s.messages,
          [event.messageId]: { id: event.messageId, runId: run.id, role: event.role ?? "assistant", text: "", done: false },
        };
        d.s.messageOrder = [...d.s.messageOrder, event.messageId];
        run.messageIds.push(event.messageId);
      }
      break;
    }

    case EventType.TEXT_MESSAGE_CONTENT: {
      const prev = d.s.messages[event.messageId];
      if (prev) {
        d.s.messages = { ...d.s.messages, [event.messageId]: { ...prev, text: prev.text + event.delta } };
      } else {
        const run = d.runOf(sc, seq);
        d.s.messages = {
          ...d.s.messages,
          [event.messageId]: { id: event.messageId, runId: run.id, role: "assistant", text: event.delta, done: false },
        };
        d.s.messageOrder = [...d.s.messageOrder, event.messageId];
        run.messageIds.push(event.messageId);
      }
      break;
    }

    case EventType.TEXT_MESSAGE_END: {
      const prev = d.s.messages[event.messageId];
      if (prev && !prev.done) d.s.messages = { ...d.s.messages, [event.messageId]: { ...prev, done: true } };
      break;
    }

    case EventType.ACTIVITY_SNAPSHOT: {
      const envelope = renderBlockFromEvent(event);
      if (!envelope) break;
      const run = d.runOf(sc, seq);
      const prev = d.s.blocks[envelope.id];
      if (prev?.runId && prev.runId !== run.id && d.s.runs[prev.runId]) {
        const owner = d.run(prev.runId);
        owner.blockIds = owner.blockIds.filter((b) => b !== envelope.id);
      }
      run.blockIds = [...run.blockIds.filter((b) => b !== envelope.id), envelope.id];
      d.s.blocks = {
        ...d.s.blocks,
        [envelope.id]: { envelope, runId: run.id, seq, revision: (prev?.revision ?? 0) + 1, live: !!action.live },
      };
      d.s.blockOrder = [...d.s.blockOrder.filter((b) => b !== envelope.id), envelope.id];
      break;
    }

    case EventType.CUSTOM: {
      const update = caseUpdateFromEvent(event);
      if (update) d.s.caseMeta = { ...(d.s.caseMeta ?? {}), ...update };
      break;
    }

    default:
      // TOOL_CALL_END, REASONING_*_END and anything newer: nothing to fold.
      break;
  }
  return d.s;
}

// ───────────────────────────── selectors ─────────────────────────────

export type StageView = { id: StageId; label: string; state: "pending" | "active" | "done" };

/**
 * The stage chips for one run: the phase(s) the run touched; a stage that was skipped (not
 * visited but before the furthest one reached) is left out; stages still ahead are `pending`
 * while the run can continue (running / queued / waiting for a decision) and dropped once it
 * ended (pass `interrupts` so a pause whose decision was taken counts as ended).
 */
export function selectStages(run: RunEntry, interrupts?: Record<string, InterruptEntry>): StageView[] {
  const phases = Object.values(STAGE_PHASES) as readonly (readonly StageId[])[];
  const touched = phases.filter((p) => p.some((s) => run.visited.includes(s)));
  const shown = touched.length > 0 ? touched : run.status === "running" || run.status === "queued" ? [phases[0] ?? []] : [];
  const waiting = run.status === "paused" && !(interrupts && isDecidedPause(run, interrupts));
  const ongoing = run.status === "running" || run.status === "queued" || waiting;
  const out: StageView[] = [];
  for (const phase of shown) {
    const furthest = Math.max(-1, ...phase.map((s, i) => (run.visited.includes(s) ? i : -1)));
    phase.forEach((id, i) => {
      const visited = run.visited.includes(id);
      if (!visited && i < furthest) return;
      if (!visited && !ongoing) return;
      const state = run.active === id ? "active" : visited ? "done" : "pending";
      out.push({ id, label: stageLabels[id], state });
    });
  }
  return out;
}

/**
 * A run that paused for a decision which is no longer pending (the case snapshot no longer lists
 * it — decided or superseded — or this user decided it): it is no longer "waiting for you".
 */
export function isDecidedPause(run: RunEntry, interrupts: Record<string, InterruptEntry>): boolean {
  return run.status === "paused" && run.interruptIds.length > 0 && run.interruptIds.every((id) => interrupts[id]?.open !== true);
}

/** Collapsed one-liner for the thinking timeline. */
export function selectHeadline(run: RunEntry, interrupts: Record<string, InterruptEntry>): string {
  if (isDecidedPause(run, interrupts)) return "Decision received";
  switch (run.status) {
    case "queued":
      return "Getting started…";
    case "running": {
      const stage = run.active ?? run.visited.at(-1) ?? null;
      return stage ? `${stageLabels[stage]}…` : "Getting started…";
    }
    case "paused": {
      const first = run.interruptIds.map((id) => interrupts[id]).find(Boolean);
      return first?.message ?? "Waiting for your decision";
    }
    case "done": {
      const stage = run.visited.at(-1);
      return stage ? stageLabels[stage] : "Done";
    }
    case "error":
      // The full (generic) message and the error ID are shown under the timeline (RunErrorNotice).
      return "Stopped before finishing";
    case "cancelled":
      return "Stopped";
  }
}

/** Timeline status vocabulary of `@keelson/ui` `ThinkingTimeline`. */
export function timelineStatus(
  run: RunEntry,
  interrupts: Record<string, InterruptEntry>,
): "running" | "paused" | "done" | "error" | "cancelled" {
  if (isDecidedPause(run, interrupts)) return "done";
  return run.status === "queued" ? "running" : run.status;
}

export function selectCurrentRun(state: CaseStreamState): RunEntry | null {
  const id = state.currentRunId ?? state.runOrder.at(-1) ?? null;
  return id ? (state.runs[id] ?? null) : null;
}

/** The run in progress (composer busy + Stop), if any. Queued runs do not block the composer. */
export function selectRunningRun(state: CaseStreamState): RunEntry | null {
  for (let i = state.runOrder.length - 1; i >= 0; i--) {
    const run = state.runs[state.runOrder[i] as string];
    if (run?.status === "running") return run;
  }
  return null;
}

export function selectOpenInterruptIds(state: CaseStreamState): Set<string> {
  return new Set(Object.values(state.interrupts).filter((i) => i.open).map((i) => i.id));
}

export function blocksOf(state: CaseStreamState, ids: readonly string[]): BlockEntry[] {
  return ids.map((id) => state.blocks[id]).filter((b): b is BlockEntry => !!b);
}
