import { describe, expect, it } from "vitest";
import { M1_FLOW, WHAT_IF, liveDelivery, runId, toAction } from "../../test/fixtures/recorded";
import { caseStreamReducer, initialCaseStreamState, type CaseStreamState } from "./stream/reducer";
import { buildTurns, canvasBlocks, followUpContext, followUps, hasActiveWork, RESULT_TAB_ORDER, staleQueuedRuns, whatChangedOf, type ThreadMessage, type Turn } from "./thread";
import type { BlockEntry } from "./stream/reducer";

function fold(events = liveDelivery(M1_FLOW), start: CaseStreamState = initialCaseStreamState) {
  return events.reduce((s, e) => caseStreamReducer(s, toAction(e)), start);
}

const CHAT = runId(M1_FLOW, "chat");
const RESUME = runId(M1_FLOW, "resume_approach");
const APPROVE = runId(M1_FLOW, "resume_approval");

const userMsg = (id: string, run: string | null, text: string, at = "2026-10-05T09:00:00Z"): ThreadMessage => ({
  id,
  role: "user",
  text,
  run_id: run,
  created_at: at,
});

describe("buildTurns", () => {
  const state = fold();
  const messages = [
    userMsg("m1", CHAT, "Our trim supplier says wadding and zips are delayed 7 days"),
    { id: "m2", role: "assistant", text: "stored copy", run_id: RESUME, created_at: null },
  ];
  const turns = buildTurns(messages, state);

  it("makes one turn per run; resumes are continuation turns without a user bubble", () => {
    expect(turns.map((t) => [t.runId, t.user?.id ?? null])).toEqual([
      [CHAT, "m1"],
      [RESUME, null],
      [runId(M1_FLOW, "action_apply"), null],
      [APPROVE, null],
    ]);
  });

  it("uses the streamed TEXT_MESSAGE as the narrative, never twice with the narrative block", () => {
    const review = turns[1]!;
    expect(review.narrative.startsWith("**Rebalance stock from DE1 to NL1")).toBe(true);
    expect(review.narrative).not.toContain("stored copy");
    expect(review.inline.map((b) => b.envelope.kind)).toEqual(["kpi_strip", "recommended_option"]);
    expect(review.inline.length).toBeLessThanOrEqual(2);
  });

  it("puts approach / approval / commit cards inline in their own turns", () => {
    expect(turns[0]!.inline.map((b) => b.envelope.kind)).toEqual(["approach_card"]);
    expect(turns[0]!.narrative).toBe("");
    expect(turns[2]!.inline.map((b) => b.envelope.kind)).toEqual(["approval_card"]);
    expect(turns[3]!.inline.map((b) => b.envelope.kind)).toEqual(["commit_tracker"]);
    expect(turns[3]!.narrative).toContain("Changed by Marco Rossi via Keelson");
  });

  it("falls back to the stored assistant message, then to the narrative block", () => {
    const noText = { ...state, messages: {}, messageOrder: [] };
    const fromStore = buildTurns(messages, noText)[1]!;
    expect(fromStore.narrative).toBe("stored copy");
    const fromBlock = buildTurns([messages[0]!], noText)[1]!;
    expect(fromBlock.narrative.startsWith("**Rebalance stock")).toBe(true);
  });

  it("shows messages whose run has not been reported yet at the end", () => {
    const pending = buildTurns([...messages, userMsg("m3", "run-new", "And for Black Friday?")], state);
    expect(pending.at(-1)).toMatchObject({ runId: "run-new", run: null, user: { id: "m3" } });
  });

  it("lists queued runs after started ones and flags runs that will never start", () => {
    let s = caseStreamReducer(initialCaseStreamState, { type: "expect_run", runId: "run-old" });
    s = caseStreamReducer(s, { type: "expect_run", runId: CHAT });
    const events = liveDelivery(M1_FLOW);
    s = fold(events.slice(0, events.findIndex((e) => e.seq === 17) + 1), s); // chat run paused
    const msgs = [userMsg("old", "run-old", "lost", "2026-01-01T00:00:00Z"), userMsg("m1", CHAT, "trims")];
    const t = buildTurns(msgs, s);
    expect(t.map((x) => x.runId)).toEqual([CHAT, "run-old"]);
    expect(t[1]!.stale).toBe(true);
    expect(staleQueuedRuns(msgs, s)).toEqual(new Set(["run-old"]));
    expect(hasActiveWork(s, staleQueuedRuns(msgs, s))).toBe(false);

    const fresh = caseStreamReducer(s, { type: "expect_run", runId: "run-next" });
    const msgs2 = [...msgs, userMsg("m9", "run-next", "next", new Date(Date.now() + 60_000).toISOString())];
    expect(staleQueuedRuns(msgs2, fresh).has("run-next")).toBe(false);
    expect(hasActiveWork(fresh, staleQueuedRuns(msgs2, fresh))).toBe(true);
  });
});

describe("canvasBlocks", () => {
  it("splits canvas blocks into Result and Evidence", () => {
    const { result, evidence } = canvasBlocks(fold());
    expect(result.map((b) => b.envelope.kind)).toEqual(["plan_diff"]);
    expect(evidence.map((b) => b.envelope.kind)).toEqual(["evidence_pack"]);
  });

  it("orders Result as scenario_compare, network_map, impact_graph, stockout_timeline, plan_diff, then the rest (80 §6)", () => {
    const arrival = ["plan_diff", "kpi_strip", "stockout_timeline", "impact_graph", "network_map", "plan_diff", "scenario_compare", "evidence_pack"];
    const blocks: CaseStreamState["blocks"] = {};
    arrival.forEach((kind, i) => {
      const id = `b${i}`;
      blocks[id] = {
        envelope: { id, kind, v: 1, title: kind, placement: "canvas", provenance: null, data_ref: null, props: {} },
        runId: null,
        seq: i + 1,
        revision: 1,
        live: false,
      } as CaseStreamState["blocks"][string];
    });
    const state: CaseStreamState = { ...initialCaseStreamState, blocks, blockOrder: arrival.map((_, i) => `b${i}`) };
    const { result, evidence } = canvasBlocks(state);
    expect(result.map((b) => `${b.envelope.kind}:${b.envelope.id}`)).toEqual([
      "scenario_compare:b6",
      "network_map:b4",
      "impact_graph:b3",
      "stockout_timeline:b2",
      "plan_diff:b0",
      "plan_diff:b5",
      "kpi_strip:b1",
    ]);
    expect(RESULT_TAB_ORDER).toEqual(["scenario_compare", "network_map", "impact_graph", "stockout_timeline", "plan_diff"]);
    expect(evidence.map((b) => b.envelope.id)).toEqual(["b7"]);
  });
});

describe("followUps", () => {
  it("offers questions only at the end of a finished turn", () => {
    const state = fold();
    const turns = buildTurns([], state);
    expect(followUps(turns[0]!)).toEqual([]); // paused for the approach
    expect(followUps(turns[1]!)).toContain("Why this option over doing nothing?");
    expect(followUps(turns[3]!)).toContain("Summarise what changed in Merxa");
    const whatIf = buildTurns([], fold(liveDelivery(WHAT_IF)));
    expect(followUps(whatIf.at(-1)!).length).toBeGreaterThan(0);
  });
});

describe("M4 follow-ups (docs/contracts/90 §6)", () => {
  const entry = (kind: string, props: Record<string, unknown>, id = `blk_${kind}`): BlockEntry => ({
    envelope: { id, kind, v: 1, title: "", placement: "inline", provenance: null, data_ref: null, props } as unknown as BlockEntry["envelope"],
    runId: "r1",
    seq: 1,
    revision: 1,
    live: false,
  });
  const doneTurn = (inline: BlockEntry[]): Turn =>
    ({
      key: "run:r1",
      runId: "r1",
      user: null,
      run: { status: "done" },
      narrative: "x",
      whatChanged: null,
      inline,
      stale: false,
    }) as unknown as Turn;

  it("followUpContext reads the editable values of the case's latest approach card", () => {
    const card = (id: string, editable: unknown) => ({ ...entry("approach_card", { editable }, id), seq: 1 });
    const blocks = [card("a1", [{ key: "delay_days", label: "Delay", kind: "days" }]), card("a2", [{ key: "duration_days", label: "Duration", kind: "days" }, { bad: 1 }])];
    const state = { blockOrder: blocks.map((b) => b.envelope.id), blocks: Object.fromEntries(blocks.map((b) => [b.envelope.id, b])) } as unknown as Parameters<typeof followUpContext>[0];
    expect(followUpContext(state)).toEqual({ editable: [{ key: "duration_days", label: "Duration", kind: "days" }] });
    expect(followUpContext({ blockOrder: [], blocks: {} } as unknown as Parameters<typeof followUpContext>[0])).toEqual({ editable: [] });
  });

  it("the What changed line comes from the latest narrative block that has one", () => {
    expect(whatChangedOf([entry("narrative", { markdown: "a" })])).toBeNull();
    expect(
      whatChangedOf([
        entry("narrative", { markdown: "a", what_changed: "Plan v2: old" }, "n1"),
        entry("kpi_strip", { items: [] }),
        entry("narrative", { markdown: "b", what_changed: "  Plan v3:\n delay 7 → 14 days " }, "n2"),
      ]),
    ).toBe("Plan v3: delay 7 → 14 days");
  });

  it("chips name the first real alternative (explain) and ask a re-simulation; they never act", () => {
    const option = (alternatives: { id: string; title: string }[]) =>
      entry("recommended_option", { plan_version: 2, option: { id: "opt-B" }, alternatives, actions: [] });
    const delay = { editable: [{ key: "delay_days", label: "Delay", kind: "days" }] };
    expect(followUps(doneTurn([option([{ id: "n", title: "Do nothing" }, { id: "opt-A", title: "Rebalance between DCs" }])]), delay)).toEqual([
      "Why not Rebalance between DCs?",
      "What if the delay doubles?",
      "Which stores are most exposed?",
    ]);
    // Regression (generic, not scenario-specific): the re-simulation chip follows the case's own
    // time value — a closure asks about its duration — and never assumes a "delay".
    const closure = { editable: [{ key: "affected_receipts", label: "Receipts", kind: "ids" }, { key: "duration_days", label: "Closure (days)", kind: "days" }] };
    expect(followUps(doneTurn([option([])]), closure)[1]).toBe("What if the closure doubles?");
    expect(followUps(doneTurn([option([])]), { editable: [{ label: "Demand change", kind: "pct" }] })[1]).toBe("What if this lasts twice as long?");
    expect(followUps(doneTurn([option([])]))[1]).toBe("What if this lasts twice as long?");
    expect(followUps(doneTurn([option([{ id: "n", title: "Do nothing" }])]))[0]).toBe("Why this option over doing nothing?");
    const long = "A very long option title that goes on and on beyond any chip width";
    expect(followUps(doneTurn([option([{ id: "x", title: long }])]))[0]).toBe(`Why not ${long.slice(0, 47)}…?`);
  });
});
