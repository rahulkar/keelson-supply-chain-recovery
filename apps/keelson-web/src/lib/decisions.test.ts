import { describe, expect, it } from "vitest";
import { interruptDecision } from "./decisions";
import type { InterruptEntry } from "./stream/reducer";

const entry = (over: Partial<InterruptEntry> = {}): InterruptEntry =>
  ({ id: "approach-c-1", reason: "approach", runId: "r1", open: false, openedSeq: 17, decided: false, ...over }) as InterruptEntry;

describe("interruptDecision (decided-card carry-over)", () => {
  it("open interrupts keep their buttons", () => {
    expect(interruptDecision({ open: true, entry: entry({ open: true }), server: undefined, viewerName: "Priya" })).toBeUndefined();
  });

  it("never-seen interrupts (RUN_FINISHED not arrived yet) keep the old behaviour", () => {
    expect(interruptDecision({ open: false, entry: undefined, server: undefined, viewerName: "Priya" })).toBeUndefined();
  });

  it("prefers the server's record of who decided", () => {
    expect(
      interruptDecision({
        open: false,
        entry: entry({ decided: true, localDecision: { decision: "confirm", at: "x", recorded: false } }),
        server: { id: "approach-c-1", decision: "edit", decided_by: ["Ana", { username: "marco" }, { name: null }], decided_at: "2026-10-06T10:00:00Z" },
        viewerName: "Priya",
      }),
    ).toEqual({ status: "decided", decision: "edit", by: ["Ana", "marco"], at: "2026-10-06T10:00:00Z" });
  });

  it("superseded / expired → closed", () => {
    expect(interruptDecision({ open: false, entry: entry(), server: { id: "x", status: "SUPERSEDED" }, viewerName: "P" })).toEqual({ status: "closed" });
  });

  it("this viewer's own decision, including an incomplete quorum", () => {
    expect(
      interruptDecision({ open: false, entry: entry({ decided: true, localDecision: { decision: "approve", at: "t", recorded: true } }), server: undefined, viewerName: "Marco Rossi" }),
    ).toEqual({ status: "recorded", decision: "approve", by: "Marco Rossi", at: "t" });
  });

  it("seen open, now closed, nothing else known → closed", () => {
    expect(interruptDecision({ open: false, entry: entry(), server: undefined, viewerName: "P" })).toEqual({ status: "closed" });
  });
});
