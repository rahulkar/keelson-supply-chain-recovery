import { describe, expect, it } from "vitest";
import {
  AGENT_DISPLAY_NAMES,
  EVENT_TYPE_KEYS,
  approachCardPropsSchema,
  checkSentence,
  checkTaskId,
  generated,
  narrativePropsSchema,
  parseBlockProps,
  recommendedOptionPropsSchema,
  runRequestSchema,
} from "../src";

// Fixtures mirror packages/contracts-py/tests/test_m4.py (docs/contracts/90).

const approach = {
  interrupt_id: "approach-case1-1",
  event: { family: "LOG", type: "LOG.chokepoint_closure", description: "Suez closes for 3 weeks", source: "chat" },
  mode: "what_if",
  matched: { count: 1, sample: [{ type: "chokepoint", code: "SUEZ", label: "Suez Canal" }] },
  assumptions: [
    { key: "duration_days", label: "Duration", value_text: "21 days", prov: "decision" },
    { key: "delay_days", label: "Delay", value_text: "10 days", prov: "inference", unconfirmed: true, source_evidence_ids: ["ev_1"] },
  ],
  checks: [{ task_id: "t1", sentence: "Open POs on lanes via Suez" }, "A plain M1 check"],
  buttons: ["confirm", "edit", "what_if", "cancel"],
  agents: [{ slug: "logistics", display_name: "Logistics analyst" }],
  editable: [
    { key: "duration_days", label: "Duration", kind: "days", value_int: 21 },
    { key: "scope", label: "Scope", kind: "ids", value_ids: ["SUEZ"] },
  ],
};

const kpi = { key: "units_at_risk", label: "Units at risk", value: 3200, unit: "units", prov: "simulated", preliminary: true };

const recommended = {
  plan_version: 2,
  option: { id: "opt-A", title: "Rebalance", summary_bullets: [], kpis: [kpi] },
  alternatives: [],
  actions: [
    { type: "apply", label: "Apply changes for me", enabled: false, disabled_reason: "What-if case: promote it to an actual case first" },
    { type: "promote", label: "Promote to actual", enabled: true },
    { type: "confirm_assumptions", label: "Confirm values", enabled: true },
  ],
  unconfirmed: [{ key: "delay_days", label: "Delay", value_text: "10 days", source: "Supplier message (unverified)" }],
  what_if: true,
};

describe("M4 approach card", () => {
  it("parses agents, task checks (and M1 string checks) and editable values", () => {
    const props = approachCardPropsSchema.parse(approach);
    expect(props.agents[0]?.display_name).toBe("Logistics analyst");
    expect(props.checks.map(checkSentence)).toEqual(["Open POs on lanes via Suez", "A plain M1 check"]);
    expect(props.checks.map(checkTaskId)).toEqual(["t1", null]);
    expect(props.editable[0]).toEqual({ ...approach.editable[0], value_ids: null });
    expect(props.assumptions[0]?.unconfirmed).toBe(false);
    expect(props.assumptions[1]?.unconfirmed).toBe(true);
  });

  it("defaults the M4 lists for pre-M4 cards", () => {
    const { agents: _a, editable: _e, ...m1 } = approach;
    const props = approachCardPropsSchema.parse({ ...m1, checks: ["a"], assumptions: [approach.assumptions[0]] });
    expect(props.agents).toEqual([]);
    expect(props.editable).toEqual([]);
    expect(props.assumptions[0]?.source_evidence_ids).toEqual([]);
  });

  it("rejects unknown editable kinds and malformed checks", () => {
    expect(approachCardPropsSchema.safeParse({ ...approach, editable: [{ key: "x", label: "X", kind: "hours" }] }).success).toBe(false);
    expect(approachCardPropsSchema.safeParse({ ...approach, checks: [{ task_id: "t1" }] }).success).toBe(false);
  });
});

describe("M4 recommended option", () => {
  it("parses unconfirmed values, what_if and the new actions", () => {
    const r = parseBlockProps({ kind: "recommended_option", v: 1, props: recommended });
    expect(r.ok).toBe(true);
    const props = recommendedOptionPropsSchema.parse(recommended);
    expect(props.actions.map((a) => a.type)).toEqual(["apply", "promote", "confirm_assumptions"]);
    expect(props.unconfirmed[0]?.source).toContain("Supplier");
  });

  it("defaults unconfirmed/what_if for pre-M4 cards", () => {
    const { unconfirmed: _u, what_if: _w, ...m1 } = recommended;
    const props = recommendedOptionPropsSchema.parse({ ...m1, actions: [{ type: "apply", label: "Apply", enabled: true }] });
    expect(props.unconfirmed).toEqual([]);
    expect(props.what_if).toBe(false);
  });

  it("rejects unknown action types", () => {
    const bad = { ...recommended, actions: [{ type: "rollback", label: "Undo", enabled: true }] };
    expect(recommendedOptionPropsSchema.safeParse(bad).success).toBe(false);
  });
});

describe("M4 narrative, runs, llm", () => {
  it("narrative carries an optional what_changed line", () => {
    expect(narrativePropsSchema.parse({ markdown: "x", what_changed: "Plan v2: delay doubled" }).what_changed).toBe(
      "Plan v2: delay doubled",
    );
    expect(narrativePropsSchema.parse({ markdown: "x" }).what_changed).toBeNull();
  });

  it("run requests carry confirm_assumptions actions and typed edits", () => {
    const base = { run_id: "r", tenant: "demo-retail", case_id: "c", actor: { sub: "u", username: "u", name: "U", roles: [] } };
    expect(
      runRequestSchema.safeParse({
        ...base,
        kind: "action",
        payload: { type: "confirm_assumptions", option_id: "opt-A", plan_version: 2, assumption_keys: ["delay_days"] },
      }).success,
    ).toBe(true);
    expect(
      runRequestSchema.safeParse({
        ...base,
        kind: "resume",
        payload: {
          interrupt_id: "approach-c-1",
          decision: "edit",
          edits: [{ key: "duration_days", value_int: 14 }, { key: "scope", value_ids: ["SUEZ"] }],
          removed_checks: ["t3"],
        },
      }).success,
    ).toBe(true);
  });

  it("bounds client decisions (assumption keys, typed edits, removed checks)", () => {
    const base = { run_id: "r", tenant: "demo-retail", case_id: "c", actor: { sub: "u", username: "u", name: "U", roles: [] } };
    const keys = Array.from({ length: 51 }, (_, i) => `k${i}`);
    const resume = (payload: object) =>
      runRequestSchema.safeParse({ ...base, kind: "resume", payload: { interrupt_id: "approach-c-1", decision: "edit", ...payload } }).success;
    expect(
      runRequestSchema.safeParse({
        ...base,
        kind: "action",
        payload: { type: "confirm_assumptions", option_id: "opt-A", plan_version: 2, assumption_keys: keys },
      }).success,
    ).toBe(false);
    expect(resume({ removed_checks: keys })).toBe(false);
    expect(resume({ removed_checks: keys.slice(0, 50) })).toBe(true);
    expect(resume({ edits: keys.map((key) => ({ key, value_int: 1 })) })).toBe(false);
    expect(resume({ edits: [{ key: "k".repeat(65), value_int: 1 }] })).toBe(false);
    expect(resume({ edits: [{ key: "scope", value_ids: Array.from({ length: 501 }, (_, i) => `id${i}`) }] })).toBe(false);
  });

  it("exposes the taxonomy keys and the M4 LLM schemas", () => {
    expect(EVENT_TYPE_KEYS).toEqual([
      "SUP.delay", "SUP.capacity_loss", "LOG.chokepoint_closure", "LOG.port_closure", "LOG.strike",
      "NAT.storm", "NAT.flood", "NAT.earthquake", "DEM.spike", "DEM.drop", "OTHER",
    ]);
    for (const name of [
      "llmInvestigationPlanSchema",
      "llmCritiqueSchema",
      "llmFollowupClassificationSchema",
      "llmGuardVerdictSchema",
      "llmUntrustedExtractionSchema",
      "llmScenarioUpdateSchema",
    ]) {
      expect(generated).toHaveProperty(name);
    }
    expect(
      generated.llmFindingsSchema.safeParse({ task_id: "t", status: "ok", claims: [], affected: [], gaps: [], scenario_updates: [] }).success,
    ).toBe(true);
  });

  it("knows the Playbook analyst", () => {
    expect(AGENT_DISPLAY_NAMES.playbook).toBe("Playbook analyst");
  });
});
