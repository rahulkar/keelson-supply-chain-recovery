import { parseAgUiEvent } from "@keelson/contracts";
import { describe, expect, it } from "vitest";
import {
  M1_FLOW,
  WHAT_IF,
  liveDelivery,
  replayDelivery,
  runId,
  toAction,
  withoutRunIds,
  type RecordedEvent,
  type Recording,
} from "../../../test/fixtures/recorded";
import {
  caseStreamReducer,
  initialCaseStreamState,
  isDecidedPause,
  selectCurrentRun,
  selectHeadline,
  selectOpenInterruptIds,
  selectRunningRun,
  selectStages,
  timelineStatus,
  type CaseStreamAction,
  type CaseStreamState,
} from "./reducer";

function fold(events: RecordedEvent[], start: CaseStreamState = initialCaseStreamState, live = true): CaseStreamState {
  return events.reduce((s, e) => caseStreamReducer(s, toAction(e, live)), start);
}

function upTo(rec: Recording, seq: number): RecordedEvent[] {
  const idx = rec.events.findIndex((e) => e.seq === seq);
  return rec.events.slice(0, idx + 1);
}

function event(raw: Record<string, unknown>, seq: number | null): CaseStreamAction {
  const parsed = parseAgUiEvent(raw);
  if (!parsed.ok) throw new Error(parsed.message);
  return { type: "event", event: parsed.event, sc: parsed.sc, seq, live: true };
}

const sc = (over: Record<string, unknown> = {}) => ({
  tenantId: "demo-retail",
  caseId: M1_FLOW.case_id,
  stage: "understanding",
  prov: "fact",
  label: "x",
  ...over,
});

const ERROR_ID = "6f1e7c8a-9d55-4b1c-8d3e-4f500b1c2d3e";
const GENERIC = `Something went wrong while working on this case. Please try again; if it keeps happening, contact support with error ID ${ERROR_ID}.`;

const CHAT = runId(M1_FLOW, "chat");
const RESUME = runId(M1_FLOW, "resume_approach");
const ACTION = runId(M1_FLOW, "action_apply");
const APPROVE = runId(M1_FLOW, "resume_approval");
const APPROACH_ID = `approach-${M1_FLOW.case_id}-1`;
const APPROVAL_ID = "approval-cs-1-1";

/** What `GET /api/cases/{id}` lists as `open_interrupts` right after event `seq` of the M1 flow. */
function m1PendingAt(seq: number): string[] {
  if (seq >= 17 && seq < 18) return [APPROACH_ID]; // paused for the approach until the resume starts
  if (seq >= 66 && seq < 67) return [APPROVAL_ID]; // paused for approval until the resume starts
  return [];
}

/**
 * Fold like CaseView does: after every RUN_STARTED / RUN_FINISHED / RUN_ERROR the snapshot is
 * re-read and its pending interrupts are synced into the reducer.
 */
function foldSynced(events: RecordedEvent[], pendingAt: (seq: number) => string[] = m1PendingAt): CaseStreamState {
  return events.reduce((s, e) => {
    const next = caseStreamReducer(s, toAction(e, true));
    const type = String(e.event["type"]);
    if (e.persist && (type === "RUN_STARTED" || type === "RUN_FINISHED" || type === "RUN_ERROR")) {
      return caseStreamReducer(next, { type: "server_interrupts", openIds: pendingAt(e.seq), asOfSeq: e.seq });
    }
    return next;
  }, initialCaseStreamState);
}

describe("recorded fixtures", () => {
  it("are valid AG-UI 1.0 events with Keelson metadata", () => {
    for (const rec of [...M1_FLOW.events, ...WHAT_IF.events]) {
      const parsed = parseAgUiEvent(JSON.stringify(rec.event));
      expect(parsed.ok, `seq ${rec.seq} ${String(rec.event["type"])}`).toBe(true);
    }
  });
});

describe("caseStreamReducer — full M1 flow delivered live", () => {
  const state = foldSynced(liveDelivery(M1_FLOW));

  it("keeps one run per RUN_STARTED, in start order, with final statuses", () => {
    expect(state.runOrder).toEqual([CHAT, RESUME, ACTION, APPROVE]);
    expect(state.runOrder.map((id) => state.runs[id]?.status)).toEqual(["paused", "done", "paused", "done"]);
    expect(state.runStatus).toBe("done");
    expect(state.currentRunId).toBe(APPROVE);
    expect(selectRunningRun(state)).toBeNull();
    expect(state.lastSeq).toBe(80);
    expect(state.invalidEvents).toBe(0);
  });

  it("builds agents and humanised activities from SUBAGENT_* and TOOL_CALL_* (sc.label)", () => {
    const chat = state.runs[CHAT]!;
    expect(chat.agents.map((a) => [a.slug, a.name, a.state])).toEqual([["intake", "Intake", "done"]]);
    const resume = state.runs[RESUME]!;
    expect(resume.agents.map((a) => [a.slug, a.name, a.state])).toEqual([
      ["inventory_demand", "Inventory & demand analyst", "done"],
      ["recommender", "Strategist", "done"],
    ]);
    const analyst = resume.agents[0]!;
    expect(analyst.activities.map((x) => [x.label, x.state, x.toolName])).toEqual([
      ["Read 8 inventory positions from Merxa", "done", "get_inventory"],
      ["Read 4 demand forecast rows from Merxa", "done", "get_demand_forecast"],
    ]);
    expect(analyst.activities.every((x) => x.evidenceRefs.length === 1 && x.evidenceRefs[0]!.startsWith("ev_"))).toBe(true);
  });

  // Browser e2e finding: while a tool call runs, the engine's label is the tool name with spaces
  // ("Checking get inventory in Merxa"); the timeline shows a sentence instead.
  it("shows a humanised sentence while a tool call is running", () => {
    const start = M1_FLOW.events.findIndex((e) => (e.event as { type?: string }).type === "TOOL_CALL_START");
    expect(start).toBeGreaterThan(0);
    const running = fold(M1_FLOW.events.slice(0, start + 1));
    const analyst = running.runs[RESUME]!.agents[0]!;
    expect(analyst.activities.map((x) => [x.label, x.state])).toEqual([["Reading inventory positions from Merxa", "running"]]);
    for (const a of analyst.activities) expect(a.label).not.toMatch(/get inventory|get_inventory/);
  });

  it("appends live reasoning deltas and replaces them with the persisted aggregate", () => {
    const chat = state.runs[CHAT]!;
    expect(chat.reasoning).toHaveLength(1);
    expect(chat.reasoning[0]).toMatchObject({ agentId: chat.agents[0]!.id, final: true });
    expect(chat.reasoning[0]!.text).toBe("The user reports a supplier trim delay.");
    const resume = state.runs[RESUME]!;
    expect(resume.reasoning.map((r) => r.text)).toEqual(["Checking stock against forecast.", "Rebalancing beats doing nothing."]);
    expect(resume.reasoning.map((r) => r.agentId)).toEqual(resume.agents.map((a) => a.id));
  });

  it("shows streamed deltas before the aggregate arrives", () => {
    const mid = fold(upTo(M1_FLOW, 5).concat(M1_FLOW.events.filter((e) => e.seq === 0 && e.run_id === CHAT).slice(0, 1)));
    const entry = mid.runs[CHAT]!.reasoning[0]!;
    expect(entry.final).toBe(false);
    expect(entry.text.length).toBeGreaterThan(0);
    expect("The user reports a supplier trim delay.".startsWith(entry.text)).toBe(true);
  });

  it("keys blocks by id: a re-emitted block is replaced and moves to the run that emitted it last", () => {
    const kinds = state.blockOrder.map((id) => state.blocks[id]!.envelope.kind);
    expect(kinds).toEqual([
      "approach_card",
      "kpi_strip",
      "recommended_option",
      "evidence_pack",
      "narrative",
      "plan_diff",
      "approval_card",
      "commit_tracker",
    ]);
    const kpi = Object.values(state.blocks).find((b) => b.envelope.kind === "kpi_strip")!;
    expect(kpi.revision).toBe(2);
    expect(kpi.runId).toBe(RESUME);
    const items = (kpi.envelope.props as { items: { preliminary: boolean }[] }).items;
    expect(items.every((i) => !i.preliminary)).toBe(true);
    const planDiff = Object.values(state.blocks).find((b) => b.envelope.kind === "plan_diff")!;
    expect(planDiff.runId).toBe(ACTION);
    expect(planDiff.envelope.title).toBe("Changes in Merxa (CS-0001)");
    const tracker = Object.values(state.blocks).find((b) => b.envelope.kind === "commit_tracker")!;
    expect(tracker.revision).toBe(3);
    expect((tracker.envelope.props as { status: string }).status).toBe("verified");
    expect(state.runs[CHAT]!.blockIds.map((id) => state.blocks[id]!.envelope.kind)).toEqual(["approach_card"]);
    expect(state.runs[RESUME]!.blockIds.map((id) => state.blocks[id]!.envelope.kind)).toEqual([
      "kpi_strip",
      "recommended_option",
      "evidence_pack",
      "narrative",
    ]);
  });

  it("collects TEXT_MESSAGE_* per run", () => {
    const texts = state.messageOrder.map((id) => state.messages[id]!);
    expect(texts.map((m) => [m.runId, m.role, m.done])).toEqual([
      [RESUME, "assistant", true],
      [APPROVE, "assistant", true],
    ]);
    expect(texts[0]!.text.startsWith("**Rebalance stock from DE1 to NL1")).toBe(true);
    expect(texts[1]!.text).toContain("verified them by reading them back");
  });

  it("opens interrupts on RUN_FINISHED{interrupt}; the case snapshot closes them once resolved", () => {
    expect(Object.keys(state.interrupts).sort()).toEqual([APPROACH_ID, APPROVAL_ID].sort());
    expect(selectOpenInterruptIds(state).size).toBe(0);
    const paused = fold(upTo(M1_FLOW, 17));
    expect([...selectOpenInterruptIds(paused)]).toEqual([APPROACH_ID]);
    expect(paused.runStatus).toBe("paused");
    expect(paused.interrupts[APPROACH_ID]).toMatchObject({ reason: "approach", runId: CHAT, open: true, openedSeq: 17 });
    const awaiting = foldSynced(upTo(M1_FLOW, 66));
    expect([...selectOpenInterruptIds(awaiting)]).toEqual([APPROVAL_ID]);
    expect(awaiting.caseMeta).toMatchObject({ status: "awaiting_approval", mode: "actual" });
  });

  it("a new run alone does not close a pending interrupt (it may be a follow-up question)", () => {
    const resumed = fold(upTo(M1_FLOW, 18));
    expect(resumed.runs[RESUME]!.status).toBe("running");
    expect([...selectOpenInterruptIds(resumed)]).toEqual([APPROACH_ID]);
    // the snapshot read after RUN_STARTED says the approach was decided
    const synced = caseStreamReducer(resumed, { type: "server_interrupts", openIds: [], asOfSeq: 18 });
    expect(selectOpenInterruptIds(synced).size).toBe(0);
    expect(isDecidedPause(synced.runs[CHAT]!, synced.interrupts)).toBe(true);
  });

  it("derives the stage chips per run (skipped stages hidden, remaining ones pending while paused)", () => {
    expect(selectStages(state.runs[CHAT]!)).toEqual([
      { id: "understanding", label: "Understanding", state: "done" },
      { id: "measuring_impact", label: "Measuring impact", state: "done" },
      { id: "building_options", label: "Building options", state: "pending" },
      { id: "ready_for_review", label: "Ready for review", state: "pending" },
    ]);
    expect(selectStages(state.runs[RESUME]!).map((s) => [s.id, s.state])).toEqual([
      ["understanding", "done"],
      ["checking_systems", "done"],
      ["measuring_impact", "done"],
      ["building_options", "done"],
      ["ready_for_review", "done"],
    ]);
    expect(selectStages(state.runs[APPROVE]!).map((s) => [s.id, s.state])).toEqual([
      ["awaiting_approval", "done"],
      ["applying", "done"],
      ["verified", "done"],
    ]);
    const applying = fold(upTo(M1_FLOW, 71));
    expect(selectStages(applying.runs[APPROVE]!).map((s) => [s.id, s.state])).toEqual([
      ["awaiting_approval", "done"],
      ["applying", "active"],
      ["verified", "pending"],
    ]);
    expect(selectHeadline(applying.runs[APPROVE]!, applying.interrupts)).toBe("Applying…");
  });

  it("writes timeline headlines from the run state", () => {
    const paused = fold(upTo(M1_FLOW, 17));
    expect(selectHeadline(paused.runs[CHAT]!, paused.interrupts)).toBe(
      "Please confirm how I should frame this disruption before I investigate.",
    );
    expect(timelineStatus(paused.runs[CHAT]!, paused.interrupts)).toBe("paused");
    // once decided (the snapshot no longer lists it) the pause is no longer "waiting for you"
    expect(selectHeadline(state.runs[CHAT]!, state.interrupts)).toBe("Decision received");
    expect(timelineStatus(state.runs[CHAT]!, state.interrupts)).toBe("done");
    expect(isDecidedPause(state.runs[CHAT]!, state.interrupts)).toBe(true);
    expect(selectStages(state.runs[CHAT]!, state.interrupts).map((s) => s.id)).toEqual(["understanding", "measuring_impact"]);
    expect(selectHeadline(state.runs[RESUME]!, state.interrupts)).toBe("Ready for review");
    expect(selectHeadline(state.runs[APPROVE]!, state.interrupts)).toBe("Verified");
    expect(selectCurrentRun(state)?.id).toBe(APPROVE);
  });

  it("an approval stays open while a follow-up question runs (regression)", () => {
    let s = foldSynced(upTo(M1_FLOW, 66));
    const q = "019a0c4f-0000-7000-8000-0000000000aa";
    s = caseStreamReducer(s, { type: "expect_run", runId: q });
    s = caseStreamReducer(s, event({ type: "RUN_STARTED", threadId: M1_FLOW.case_id, runId: q, metadata: { sc: sc({ seq: 67 }) } }, 67));
    s = caseStreamReducer(s, { type: "server_interrupts", openIds: [APPROVAL_ID], asOfSeq: 67 });
    expect([...selectOpenInterruptIds(s)]).toEqual([APPROVAL_ID]);
    s = caseStreamReducer(
      s,
      event(
        { type: "RUN_FINISHED", threadId: M1_FLOW.case_id, runId: q, outcome: { type: "success" }, metadata: { sc: sc({ seq: 68 }) } },
        68,
      ),
    );
    s = caseStreamReducer(s, { type: "server_interrupts", openIds: [APPROVAL_ID], asOfSeq: 68 });
    expect([...selectOpenInterruptIds(s)]).toEqual([APPROVAL_ID]);
    expect(isDecidedPause(s.runs[ACTION]!, s.interrupts)).toBe(false);
    expect(selectHeadline(s.runs[ACTION]!, s.interrupts)).not.toBe("Decision received");
  });

  it("records run start/finish timestamps", () => {
    const run = state.runs[RESUME]!;
    expect(run.startedAt).toBeGreaterThan(0);
    expect(run.finishedAt).toBeGreaterThanOrEqual(run.startedAt!);
  });
});

describe("caseStreamReducer — replay, duplicates and reconnects", () => {
  const live = fold(liveDelivery(M1_FLOW));

  it("a page load replay (persisted events only) yields the same thread as watching live", () => {
    const replay = fold(replayDelivery(M1_FLOW), initialCaseStreamState, false);
    expect(replay.runOrder).toEqual(live.runOrder);
    expect(replay.blocks).toEqual(Object.fromEntries(Object.entries(live.blocks).map(([k, b]) => [k, { ...b, live: false }])));
    expect(replay.messages).toEqual(live.messages);
    expect(replay.interrupts).toEqual(live.interrupts);
    for (const id of live.runOrder) {
      expect(replay.runs[id]!.reasoning).toEqual(live.runs[id]!.reasoning);
      expect(replay.runs[id]!.agents).toEqual(live.runs[id]!.agents);
      expect(selectStages(replay.runs[id]!)).toEqual(selectStages(live.runs[id]!));
    }
  });

  it("ignores events already applied (dedupe by seq)", () => {
    const twice = fold(replayDelivery(M1_FLOW), live);
    expect(twice).toBe(live);
  });

  it("a reconnect that replays from an older Last-Event-ID converges to the same state", () => {
    const head = fold(upTo(M1_FLOW, 40));
    const resumed = fold(replayDelivery(M1_FLOW, 30), head);
    expect(resumed.runOrder).toEqual(live.runOrder);
    expect(resumed.messages).toEqual(live.messages);
    expect(resumed.blocks).toEqual(live.blocks);
    expect(resumed.lastSeq).toBe(80);
  });

  it("never dedupes live-only events (no seq) and ignores late deltas after the aggregate", () => {
    const chat = state0();
    const delta = event(
      { type: "REASONING_MESSAGE_CONTENT", messageId: "rsn_3_5660350", delta: " late", metadata: { sc: sc({ agent: "intake" }) } },
      null,
    );
    const after = caseStreamReducer(chat, delta);
    expect(after.runs[CHAT]!.reasoning[0]!.text).toBe("The user reports a supplier trim delay.");
  });

  function state0() {
    return fold(upTo(M1_FLOW, 8));
  }
});

describe("caseStreamReducer — queued runs, cancellation and errors", () => {
  it("expect_run shows a queued turn that RUN_STARTED later takes over (and moves to start order)", () => {
    let s = caseStreamReducer(initialCaseStreamState, { type: "expect_run", runId: "run-b" });
    s = caseStreamReducer(s, { type: "expect_run", runId: "run-a" });
    expect(s.runOrder).toEqual(["run-b", "run-a"]);
    expect(s.runs["run-a"]!.status).toBe("queued");
    s = caseStreamReducer(
      s,
      event({ type: "RUN_STARTED", threadId: "c", runId: "run-a", metadata: { sc: sc({ seq: 1 }) } }, 1),
    );
    expect(s.runOrder).toEqual(["run-b", "run-a"]);
    s = caseStreamReducer(
      s,
      event({ type: "RUN_STARTED", threadId: "c", runId: "run-b", metadata: { sc: sc({ seq: 2 }) } }, 2),
    );
    expect(s.runOrder).toEqual(["run-a", "run-b"]);
    expect(s.runs["run-b"]!.status).toBe("running");
    expect(caseStreamReducer(s, { type: "expect_run", runId: "run-b" })).toBe(s);
  });

  it("RUN_ERROR CANCELLED lands on the queued run its sc.runId names (it never started)", () => {
    let s = fold(liveDelivery(M1_FLOW));
    s = caseStreamReducer(s, { type: "expect_run", runId: "run-q" });
    s = caseStreamReducer(
      s,
      event(
        { type: "RUN_ERROR", message: "Run cancelled.", code: "CANCELLED", metadata: { sc: sc({ seq: 81, prov: "decision", runId: "run-q" }) } },
        81,
      ),
    );
    expect(s.runs["run-q"]).toMatchObject({ status: "cancelled", error: { message: "Run cancelled.", code: "CANCELLED", errorId: null } });
    expect(s.runs[APPROVE]!.status).toBe("done");
    expect(s.runStatus).toBe("cancelled");
    expect(s.runOrder.at(-1)).toBe("run-q");
    expect(selectHeadline(s.runs["run-q"]!, s.interrupts)).toBe("Stopped");
  });

  it("RUN_ERROR fails the run its sc.runId names, keeping the generic message and the error ID", () => {
    let s = fold(upTo(M1_FLOW, 25));
    expect(selectRunningRun(s)?.id).toBe(RESUME);
    s = caseStreamReducer(
      s,
      event(
        {
          type: "RUN_ERROR",
          message: GENERIC,
          code: "INTERNAL_ERROR",
          metadata: { sc: sc({ seq: 26, runId: RESUME }), errorId: ERROR_ID },
        },
        26,
      ),
    );
    const run = s.runs[RESUME]!;
    expect(run.status).toBe("error");
    expect(run.error).toEqual({ message: GENERIC, code: "INTERNAL_ERROR", errorId: ERROR_ID });
    expect(run.agents[0]!.state).toBe("error");
    expect(run.agents[0]!.activities.every((a) => a.state === "error")).toBe(true);
    expect(selectStages(run).every((st) => st.state !== "pending")).toBe(true);
    expect(selectHeadline(run, s.interrupts)).toBe("Stopped before finishing");
    expect(s.runStatus).toBe("error");
  });

  it("never keeps exception text or a stack trace from a RUN_ERROR / SUBAGENT_ERROR (regression)", () => {
    let s = fold(upTo(M1_FLOW, 23));
    const agent = s.runs[RESUME]!.agents[0]!;
    s = caseStreamReducer(
      s,
      event(
        {
          type: "SUBAGENT_ERROR",
          subagentRunId: agent.id,
          message: 'ProgrammingError: relation "run" does not exist',
          metadata: { sc: sc({ seq: 24, agent: "inventory_demand", runId: RESUME }) },
        },
        24,
      ),
    );
    expect(s.runs[RESUME]!.agents[0]!.error).toBe("This step could not be completed.");
    const trace = 'Traceback (most recent call last):\n  File "/app/x.py", line 3, in run\nKeyError: x';
    s = caseStreamReducer(
      s,
      event({ type: "RUN_ERROR", message: trace, code: "KeyError: x", metadata: { sc: sc({ seq: 25, runId: RESUME }) } }, 25),
    );
    expect(s.runs[RESUME]!.error).toEqual({
      message: "Something went wrong while working on this case.",
      code: null,
      errorId: null,
    });
  });

  it("reads the error ID from the message when metadata.errorId is missing", () => {
    let s = fold(upTo(M1_FLOW, 25));
    s = caseStreamReducer(
      s,
      event({ type: "RUN_ERROR", message: GENERIC, code: "INTERNAL_ERROR", metadata: { sc: sc({ seq: 26, runId: RESUME }) } }, 26),
    );
    expect(s.runs[RESUME]!.error?.errorId).toBe(ERROR_ID);
  });

  it("SUBAGENT_ERROR marks the agent and its running steps as failed", () => {
    let s = fold(upTo(M1_FLOW, 23));
    const agent = s.runs[RESUME]!.agents[0]!;
    s = caseStreamReducer(
      s,
      event(
        {
          type: "SUBAGENT_ERROR",
          subagentRunId: agent.id,
          message: "boom",
          metadata: { sc: sc({ seq: 24, agent: "inventory_demand", stage: "checking_systems" }) },
        },
        24,
      ),
    );
    expect(s.runs[RESUME]!.agents[0]).toMatchObject({ state: "error", error: "boom" });
    expect(s.runs[RESUME]!.agents[0]!.activities[0]!.state).toBe("error");
  });

  it("interrupt_decided closes an interrupt for this user only once", () => {
    const paused = fold(upTo(M1_FLOW, 17));
    const id = APPROACH_ID;
    const decided = caseStreamReducer(paused, { type: "interrupt_decided", interruptId: id });
    expect(decided.interrupts[id]!.open).toBe(false);
    expect(caseStreamReducer(decided, { type: "interrupt_decided", interruptId: id })).toBe(decided);
    // still PENDING on the server (quorum incomplete): it stays closed for this user
    const synced = caseStreamReducer(decided, { type: "server_interrupts", openIds: [id], asOfSeq: 17 });
    expect(synced.interrupts[id]!.open).toBe(false);
  });

  it("interrupt_decided remembers what this user decided and when (decided-card note)", () => {
    const paused = fold(upTo(M1_FLOW, 17));
    const s = caseStreamReducer(paused, {
      type: "interrupt_decided",
      interruptId: APPROACH_ID,
      decision: "confirm",
      at: "2026-10-06T09:00:00Z",
      recorded: false,
    });
    expect(s.interrupts[APPROACH_ID]!.localDecision).toEqual({ decision: "confirm", at: "2026-10-06T09:00:00Z", recorded: false });
    // the RUN_FINISHED that opened it, applied again (e.g. a replay after a reset watermark), keeps it
    const again = fold(upTo(M1_FLOW, 17), { ...s, lastSeq: 0 });
    expect(again.interrupts[APPROACH_ID]!.localDecision?.decision).toBe("confirm");
  });

  it("server_interrupts: authoritative only for interrupts opened at or before the snapshot", () => {
    const paused = fold(upTo(M1_FLOW, 17));
    // an older snapshot (taken before the interrupt existed) does not close it
    const older = caseStreamReducer(paused, { type: "server_interrupts", openIds: [], asOfSeq: 16 });
    expect(older.interrupts[APPROACH_ID]!.open).toBe(true);
    // nor does one exactly at its own seq: engine-api may have read the interrupt list just
    // before the pausing run created it (separate reads), so it cannot be trusted to close it
    const racing = caseStreamReducer(older, { type: "server_interrupts", openIds: [], asOfSeq: 17 });
    expect(racing.interrupts[APPROACH_ID]!.open).toBe(true);
    // a newer snapshot that no longer lists it (decided elsewhere / superseded) closes it
    const newer = caseStreamReducer(racing, { type: "server_interrupts", openIds: [], asOfSeq: 18 });
    expect(newer.interrupts[APPROACH_ID]!.open).toBe(false);
    // a late answer of an older request never overrides it
    expect(caseStreamReducer(newer, { type: "server_interrupts", openIds: [APPROACH_ID], asOfSeq: 17 })).toBe(newer);
    // and a snapshot that lists it again reopens it (it really is pending)
    const reopened = caseStreamReducer(newer, { type: "server_interrupts", openIds: [APPROACH_ID], asOfSeq: 18 });
    expect(reopened.interrupts[APPROACH_ID]!.open).toBe(true);
  });

  it("a snapshot synced before the replay settles the interrupts as their events arrive", () => {
    const all = caseStreamReducer(initialCaseStreamState, { type: "server_interrupts", openIds: [], asOfSeq: 80 });
    const replayed = fold(replayDelivery(M1_FLOW), all, false);
    expect(selectOpenInterruptIds(replayed).size).toBe(0);
    const waiting = caseStreamReducer(initialCaseStreamState, { type: "server_interrupts", openIds: [APPROVAL_ID], asOfSeq: 66 });
    const upToApproval = fold(replayDelivery(M1_FLOW).filter((e) => e.seq <= 66), waiting, false);
    expect([...selectOpenInterruptIds(upToApproval)]).toEqual([APPROVAL_ID]);
  });

  it("RUN_ERROR CANCELLED of a queued run never stops the run in progress (sc.runId, regression)", () => {
    let s = fold(upTo(M1_FLOW, 25));
    expect(selectRunningRun(s)?.id).toBe(RESUME);
    s = caseStreamReducer(s, { type: "expect_run", runId: "run-q" });
    s = caseStreamReducer(
      s,
      event(
        { type: "RUN_ERROR", message: "Run cancelled.", code: "CANCELLED", metadata: { sc: sc({ seq: 26, prov: "decision", runId: "run-q" }) } },
        26,
      ),
    );
    expect(s.runs["run-q"]).toMatchObject({ status: "cancelled" });
    expect(s.runs[RESUME]!.status).toBe("running");
    expect(s.runStatus).toBe("running");
    expect(s.currentRunId).toBe(RESUME);
    expect(selectRunningRun(s)?.id).toBe(RESUME);
    // the running run's own events keep landing on it
    s = fold(
      M1_FLOW.events.filter((e) => e.seq > 26 && e.seq <= 40),
      s,
    );
    expect(s.runs["run-q"]!.agents).toEqual([]);
    expect(s.runs["run-q"]!.status).toBe("cancelled");
  });

  it("cancelling the running run while another is queued stops only the running one", () => {
    let s = fold(upTo(M1_FLOW, 25));
    s = caseStreamReducer(s, { type: "expect_run", runId: "run-q" });
    s = caseStreamReducer(
      s,
      event({ type: "RUN_ERROR", message: "Run cancelled.", code: "CANCELLED", metadata: { sc: sc({ seq: 26, runId: RESUME }) } }, 26),
    );
    expect(s.runs[RESUME]!.status).toBe("cancelled");
    expect(s.runs["run-q"]!.status).toBe("queued");
    expect(s.runStatus).toBe("cancelled");
  });

  it("RUN_ERROR of a run this tab never heard of creates its turn", () => {
    let s = fold(liveDelivery(M1_FLOW));
    s = caseStreamReducer(
      s,
      event({ type: "RUN_ERROR", message: "Run cancelled.", code: "CANCELLED", metadata: { sc: sc({ seq: 81, runId: "run-elsewhere" }) } }, 81),
    );
    expect(s.runOrder.at(-1)).toBe("run-elsewhere");
    expect(s.runs["run-elsewhere"]!.status).toBe("cancelled");
    expect(s.runs[APPROVE]!.status).toBe("done");
  });

  it("legacy events without sc.runId fall back to the current run", () => {
    let s = fold(upTo(withoutRunIds(M1_FLOW), 25));
    s = caseStreamReducer(
      s,
      event({ type: "RUN_ERROR", message: "Run cancelled.", code: "CANCELLED", metadata: { sc: sc({ seq: 26 }) } }, 26),
    );
    expect(s.runs[RESUME]!.status).toBe("cancelled");
  });

  it("counts invalid events and resets", () => {
    const s = caseStreamReducer(initialCaseStreamState, { type: "invalid_event" });
    expect(s.invalidEvents).toBe(1);
    expect(caseStreamReducer(s, { type: "reset" })).toBe(initialCaseStreamState);
  });

  it("attaches events that precede any RUN_STARTED to an anonymous run", () => {
    const s = caseStreamReducer(
      initialCaseStreamState,
      event({ type: "STEP_STARTED", stepName: "checking_systems", metadata: { sc: sc({ seq: 7 }) } }, 7),
    );
    expect(s.runOrder).toEqual(["run-7"]);
    expect(s.runs["run-7"]!.active).toBe("checking_systems");
  });
});

describe("caseStreamReducer — attribution by metadata.sc.runId", () => {
  it("legacy recordings (no sc.runId) fold to the same thread", () => {
    for (const rec of [M1_FLOW, WHAT_IF]) {
      const current = fold(liveDelivery(rec));
      const legacy = fold(liveDelivery(withoutRunIds(rec)));
      expect(legacy.runOrder).toEqual(current.runOrder);
      expect(legacy.blocks).toEqual(current.blocks);
      expect(legacy.messages).toEqual(current.messages);
      for (const id of current.runOrder) expect(legacy.runs[id]).toEqual(current.runs[id]);
    }
  });

  it("events land on the run they name, not on whichever run is current", () => {
    let s = fold(upTo(M1_FLOW, 18));
    expect(s.currentRunId).toBe(RESUME);
    s = caseStreamReducer(
      s,
      event(
        { type: "STEP_STARTED", stepName: "ready_for_review", metadata: { sc: sc({ seq: 19, runId: CHAT, stage: "ready_for_review" }) } },
        19,
      ),
    );
    expect(s.runs[CHAT]!.visited).toContain("ready_for_review");
    expect(s.runs[RESUME]!.visited).not.toContain("ready_for_review");
  });

  it("a run first seen through a non-RUN_STARTED event is created under its id", () => {
    const s = caseStreamReducer(
      initialCaseStreamState,
      event({ type: "STEP_STARTED", stepName: "checking_systems", metadata: { sc: sc({ seq: 7, runId: "run-x" }) } }, 7),
    );
    expect(s.runOrder).toEqual(["run-x"]);
    expect(s.runs["run-x"]!.active).toBe("checking_systems");
  });
});

describe("caseStreamReducer — what-if flow", () => {
  const state = fold(liveDelivery(WHAT_IF));
  const chat = runId(WHAT_IF, "chat_what_if");
  const refused = runId(WHAT_IF, "action_refused");

  it("auto-confirmed approach: one run, no interrupt, case marked what-if", () => {
    expect(state.runOrder).toEqual([chat, refused]);
    expect(Object.keys(state.interrupts)).toEqual([]);
    expect(state.caseMeta?.mode).toBe("what_if");
    expect(state.runs[chat]!.blockIds.map((id) => state.blocks[id]!.envelope.kind)).toEqual([
      "approach_card",
      "kpi_strip",
      "recommended_option",
      "plan_diff",
      "evidence_pack",
      "narrative",
    ]);
  });

  it("the refused apply answers with a message and no blocks", () => {
    const run = state.runs[refused]!;
    expect(run.status).toBe("done");
    expect(run.blockIds).toEqual([]);
    expect(state.messages[run.messageIds[0]!]!.text).toContain("what-if case");
    expect(selectStages(run).map((s) => [s.id, s.state])).toEqual([["preparing_changes", "done"]]);
  });
});
