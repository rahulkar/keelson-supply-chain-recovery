import { describe, expect, it } from "vitest";
import {
  ACTIVITY_TYPE_RENDER,
  EventType,
  STAGE_IDS,
  caseUpdateFromEvent,
  errorIdFromEvent,
  interruptsFromEvent,
  parseAgUiEvent,
  renderBlockFromEvent,
  runIdOfEvent,
  type ScMetadata,
} from "../src";

const sc = (over: Partial<ScMetadata> = {}) => ({
  tenantId: "demo-retail",
  caseId: "019a0c4e-6f1e-7c8a-9d55-0b1c2d3e4f50",
  seq: 12,
  stage: "checking_systems",
  prov: "fact",
  label: "Read 12 open purchase orders from Merxa",
  agent: "inventory_demand",
  evidenceRefs: ["ev_3f9a01"],
  ...over,
});

const caseId = sc().caseId;
const runId = "019a0c4f-0000-7000-8000-000000000001";

const kpiEnvelope = {
  id: "blk_kpi01",
  kind: "kpi_strip",
  v: 1,
  title: "Exposure",
  placement: "inline",
  provenance: { kind: "simulated", engine: "keelson-opt@0.1", inputs_hash: "abc", as_of: "2026-10-05" },
  data_ref: null,
  props: {
    items: [
      { key: "units_at_risk", label: "Units at risk", value: 3200, unit: "units", delta: null, prov: "simulated", preliminary: true },
    ],
  },
};

const VALID: Record<string, Record<string, unknown>> = {
  RUN_STARTED: { type: "RUN_STARTED", threadId: caseId, runId, protocolVersion: "1.0", metadata: { sc: sc({ stage: "understanding", agent: null, label: "Started" }) } },
  STEP_STARTED: { type: "STEP_STARTED", stepName: "checking_systems", metadata: { sc: sc() } },
  STEP_FINISHED: { type: "STEP_FINISHED", stepName: "checking_systems", metadata: { sc: sc() } },
  SUBAGENT_STARTED: {
    type: "SUBAGENT_STARTED",
    subagentRunId: "inv-1:investigate/inventory_demand",
    name: "inventory_demand",
    description: "Inventory & demand analyst",
    metadata: { sc: sc() },
  },
  SUBAGENT_FINISHED: { type: "SUBAGENT_FINISHED", subagentRunId: "inv-1:investigate/inventory_demand", metadata: { sc: sc() } },
  SUBAGENT_ERROR: { type: "SUBAGENT_ERROR", subagentRunId: "inv-1:investigate/inventory_demand", message: "Timed out", metadata: { sc: sc() } },
  TOOL_CALL_START: { type: "TOOL_CALL_START", toolCallId: "tc_1", toolCallName: "list_purchase_orders", metadata: { sc: sc() } },
  TOOL_CALL_END: { type: "TOOL_CALL_END", toolCallId: "tc_1", metadata: { sc: sc() } },
  TOOL_CALL_RESULT: {
    type: "TOOL_CALL_RESULT",
    toolCallId: "tc_1",
    messageId: "tc_1:result",
    role: "tool",
    content: "12 open purchase orders",
    metadata: { sc: sc() },
  },
  REASONING_MESSAGE_CONTENT: { type: "REASONING_MESSAGE_CONTENT", messageId: "r1", delta: "Comparing ETAs…", metadata: { sc: sc() } },
  ACTIVITY_SNAPSHOT: {
    type: "ACTIVITY_SNAPSHOT",
    messageId: "blk_kpi01",
    activityType: ACTIVITY_TYPE_RENDER,
    content: kpiEnvelope,
    replace: true,
    metadata: { sc: sc({ stage: "measuring_impact", prov: "simulated", label: "Measured exposure" }) },
  },
  TEXT_MESSAGE_START: { type: "TEXT_MESSAGE_START", messageId: "m1", role: "assistant", metadata: { sc: sc({ stage: "ready_for_review" }) } },
  TEXT_MESSAGE_CONTENT: { type: "TEXT_MESSAGE_CONTENT", messageId: "m1", delta: "Three POs slip.", metadata: { sc: sc({ stage: "ready_for_review" }) } },
  RUN_FINISHED_INTERRUPT: {
    type: "RUN_FINISHED",
    threadId: caseId,
    runId,
    outcome: {
      type: "interrupt",
      interrupts: [
        {
          id: "int_approach_1",
          reason: "approach",
          message: "Here's what I understood and what I'll check",
          responseSchema: { type: "object", properties: { decision: { type: "string" } }, required: ["decision"] },
        },
      ],
    },
    metadata: { sc: sc({ stage: "understanding" }) },
  },
  RUN_FINISHED_SUCCESS: { type: "RUN_FINISHED", threadId: caseId, runId, outcome: { type: "success" }, metadata: { sc: sc({ stage: "ready_for_review" }) } },
  RUN_ERROR: { type: "RUN_ERROR", message: "Run cancelled", code: "CANCELLED", metadata: { sc: sc() } },
  CUSTOM: { type: "CUSTOM", name: "sc.case.updated", value: { mode: "actual", status: "open", title: "Trim delay" }, metadata: { sc: sc() } },
};

describe("parseAgUiEvent — accepts valid AG-UI 1.0 events", () => {
  for (const [name, event] of Object.entries(VALID)) {
    it(`accepts ${name} (object and JSON string)`, () => {
      for (const input of [event, JSON.stringify(event)]) {
        const result = parseAgUiEvent(input);
        expect(result.ok, JSON.stringify(result)).toBe(true);
        if (result.ok) {
          expect(result.event.type).toBe(event["type"]);
          expect(result.sc?.tenantId).toBe("demo-retail");
        }
      }
    });
  }

  it("accepts an event without metadata.sc (sc: null)", () => {
    const result = parseAgUiEvent({ type: "STEP_STARTED", stepName: "understanding" });
    expect(result).toMatchObject({ ok: true, sc: null });
  });

  it("applies sc defaults (agent/seq null)", () => {
    const { agent: _a, seq: _s, ...minimal } = sc();
    const result = parseAgUiEvent({ type: "STEP_STARTED", stepName: "understanding", metadata: { sc: minimal } });
    expect(result.ok && result.sc).toMatchObject({ agent: null, seq: null });
  });
});

describe("parseAgUiEvent — rejects invalid events", () => {
  const cases: [string, unknown, string][] = [
    ["malformed JSON", "{not json", "invalid_json"],
    ["unknown type", { type: "SOMETHING_ELSE", metadata: { sc: sc() } }, "invalid_event"],
    ["missing type", { stepName: "understanding" }, "invalid_event"],
    ["TOOL_CALL_RESULT without messageId", { type: "TOOL_CALL_RESULT", toolCallId: "t", content: "x" }, "invalid_event"],
    ["RUN_STARTED without runId", { type: "RUN_STARTED", threadId: caseId }, "invalid_event"],
    ["non-integer timestamp", { type: "STEP_STARTED", stepName: "x", timestamp: 1.5 }, "invalid_event"],
    ["interrupt outcome with no interrupts", { type: "RUN_FINISHED", threadId: caseId, runId, outcome: { type: "interrupt", interrupts: [] } }, "invalid_event"],
    ["array metadata", { type: "STEP_STARTED", stepName: "x", metadata: [] }, "invalid_event"],
    ["null input", null, "invalid_event"],
    ["unknown stage in sc", { type: "STEP_STARTED", stepName: "x", metadata: { sc: { ...sc(), stage: "thinking" } } }, "invalid_sc"],
    ["snake_case tenant_id in sc", { type: "STEP_STARTED", stepName: "x", metadata: { sc: { ...sc(), tenant_id: "demo-retail" } } }, "invalid_sc"],
    ["bad provenance in sc", { type: "STEP_STARTED", stepName: "x", metadata: { sc: { ...sc(), prov: "guess" } } }, "invalid_sc"],
    ["bad evidence ref in sc", { type: "STEP_STARTED", stepName: "x", metadata: { sc: { ...sc(), evidenceRefs: ["evidence-1"] } } }, "invalid_sc"],
    ["missing label in sc", { type: "STEP_STARTED", stepName: "x", metadata: { sc: { ...sc(), label: undefined } } }, "invalid_sc"],
  ];
  for (const [name, input, code] of cases) {
    it(`rejects ${name}`, () => {
      const result = parseAgUiEvent(input);
      expect(result.ok).toBe(false);
      if (!result.ok) {
        expect(result.code).toBe(code);
        expect(result.message.length).toBeGreaterThan(0);
      }
    });
  }
});

describe("event helpers", () => {
  const parse = (e: unknown) => {
    const r = parseAgUiEvent(e);
    if (!r.ok) throw new Error(r.message);
    return r.event;
  };

  it("renderBlockFromEvent extracts sc.render envelopes only", () => {
    const block = renderBlockFromEvent(parse(VALID["ACTIVITY_SNAPSHOT"]));
    expect(block).toMatchObject({ id: "blk_kpi01", kind: "kpi_strip", v: 1, placement: "inline" });
    expect(renderBlockFromEvent(parse({ ...VALID["ACTIVITY_SNAPSHOT"], activityType: "other" }))).toBeNull();
    expect(renderBlockFromEvent(parse(VALID["STEP_STARTED"]))).toBeNull();
  });

  it("interruptsFromEvent returns validated Keelson interrupts", () => {
    const interrupts = interruptsFromEvent(parse(VALID["RUN_FINISHED_INTERRUPT"]));
    expect(interrupts).toHaveLength(1);
    expect(interrupts[0]).toMatchObject({ id: "int_approach_1", reason: "approach" });
    expect(interruptsFromEvent(parse(VALID["RUN_FINISHED_SUCCESS"]))).toEqual([]);
    const foreign = parse({
      type: "RUN_FINISHED",
      threadId: caseId,
      runId,
      outcome: { type: "interrupt", interrupts: [{ id: "x", reason: "tool_approval" }] },
    });
    expect(interruptsFromEvent(foreign)).toEqual([]);
  });

  it("caseUpdateFromEvent reads sc.case.updated", () => {
    expect(caseUpdateFromEvent(parse(VALID["CUSTOM"]))).toEqual({ mode: "actual", status: "open", title: "Trim delay" });
    expect(caseUpdateFromEvent(parse(VALID["STEP_STARTED"]))).toBeNull();
  });

  it("accepts metadata.sc.runId (engine sets it on every event) and defaults it to null", () => {
    // regression: the strict sc schema rejected every engine event once runId was added
    const withRun = parseAgUiEvent({ type: "RUN_ERROR", message: "x", code: "INTERNAL_ERROR", metadata: { sc: sc({ runId }) } });
    expect(withRun.ok && withRun.sc?.runId).toBe(runId);
    const legacy = parseAgUiEvent(VALID["STEP_STARTED"]);
    expect(legacy.ok && legacy.sc?.runId).toBeNull();
    expect(parseAgUiEvent({ type: "STEP_STARTED", stepName: "x", metadata: { sc: { ...sc(), runId: "" } } })).toMatchObject({
      ok: false,
      code: "invalid_sc",
    });
  });

  it("runIdOfEvent: the event's own runId, else metadata.sc.runId, else null", () => {
    const pick = (e: unknown) => {
      const r = parseAgUiEvent(e);
      if (!r.ok) throw new Error(r.message);
      return runIdOfEvent(r.event, r.sc);
    };
    expect(pick(VALID["RUN_STARTED"])).toBe(runId);
    expect(pick(VALID["RUN_FINISHED_SUCCESS"])).toBe(runId);
    expect(pick({ type: "RUN_ERROR", message: "Stopped", code: "CANCELLED", metadata: { sc: sc({ runId: "run-q" }) } })).toBe("run-q");
    expect(pick(VALID["RUN_ERROR"])).toBeNull();
    expect(pick({ type: "STEP_STARTED", stepName: "understanding" })).toBeNull();
  });

  it("errorIdFromEvent reads an opaque metadata.errorId only", () => {
    const id = "6f1e7c8a-9d55-4b1c-8d3e-4f500b1c2d3e";
    expect(errorIdFromEvent(parse({ ...VALID["RUN_ERROR"], metadata: { sc: sc(), errorId: id } }))).toBe(id);
    expect(errorIdFromEvent(parse(VALID["RUN_ERROR"]))).toBeNull();
    expect(errorIdFromEvent(parse({ ...VALID["RUN_ERROR"], metadata: { sc: sc(), errorId: "Traceback (most recent call last)" } }))).toBeNull();
    expect(errorIdFromEvent(parse({ ...VALID["RUN_ERROR"], metadata: { sc: sc(), errorId: 42 } }))).toBeNull();
  });

  it("exposes the 9 stages in order and EventType from @ag-ui/core", () => {
    expect(STAGE_IDS).toHaveLength(9);
    expect(STAGE_IDS[0]).toBe("understanding");
    expect(STAGE_IDS.at(-1)).toBe("verified");
    expect(EventType.ACTIVITY_SNAPSHOT).toBe("ACTIVITY_SNAPSHOT");
  });
});
