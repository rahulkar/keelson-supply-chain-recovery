import { describe, expect, it } from "vitest";
import {
  approvalCardPropsSchema,
  checklistCompleted,
  commitTrackerPropsSchema,
  demoResetResultSchema,
  narrativePropsSchema,
  parseBlockProps,
  policyContextSchema,
  policyDecisionSchema,
  policyQuorumSchema,
  policyRuleSchema,
  policySetSchema,
  quorumProgress,
  recommendedOptionPropsSchema,
  remediationChecklistPropsSchema,
  runRequestSchema,
} from "../src";

// Fixtures mirror packages/contracts-py/tests/test_m5.py (docs/contracts/95).

const HASH = "d1bbcbc4cb6b0706ba28795e350e70f90c2564efa920a0cdadbc1028667b83bc";
const FOUR_EYES = { roles: ["approver", "compliance"], count: 2, exclude_initiator: true };

const step = {
  seq: 1,
  title: "Create transfer order DE1 → NL1",
  instructions_md: "Open **Transfer orders → New** and save.",
  deep_link: "http://merxa.localhost/transfer-orders/new?from=DE1&to=NL1",
  expected: { entity_type: "transfer_order", entity_ref: null, match: { from_dc: "DE1", to_dc: "NL1" } },
  status: "verified",
  verified_at: "2026-10-07T10:00:00Z",
  evidence_id: "ev_0192f0c0a1b27c3d8e9f0a1b2c3d4e60",
};
const checklist = {
  checklist_id: "chk_case1_v1_opt2",
  connector: "merxa",
  steps: [step, { ...step, seq: 2, status: "pending", verified_at: null, evidence_id: null, deep_link: null }],
  progress: { done: 1, total: 2 },
};

const quorum = {
  required: FOUR_EYES,
  approvals: [{ sub: "u-marco", name: "Marco Rossi", role: "approver", at: "2026-10-07T10:00:00Z" }],
  remaining: 1,
  missing_roles: ["compliance"],
};

const approvalCard = {
  interrupt_id: "approval-cs_01-1",
  change_set_id: "cs_01",
  change_set_hash: HASH,
  plan_version: 1,
  summary: { lines: 1, units: 520, value_cents: 1_300_000 },
  reason: "Includes a new purchase order",
  required_roles: ["approver", "compliance"],
  connect: { status: "active", connect_url: null },
  buttons: ["approve", "reject", "revise"],
};

describe("M5 remediation checklist (Guide mode)", () => {
  it("parses through the block registry and reports completion", () => {
    const r = parseBlockProps({ kind: "remediation_checklist", v: 1, props: checklist });
    expect(r.ok).toBe(true);
    const parsed = remediationChecklistPropsSchema.parse(checklist);
    expect(checklistCompleted(parsed)).toBe(false);
    const done = { ...checklist, steps: [step, { ...checklist.steps[1], status: "skipped" }], progress: { done: 2, total: 2 } };
    expect(checklistCompleted(remediationChecklistPropsSchema.parse(done))).toBe(true);
    const manual = { ...done, steps: [step, { ...checklist.steps[1], status: "done" }] };
    expect(checklistCompleted(remediationChecklistPropsSchema.parse(manual))).toBe(false);
  });

  it("rejects unknown step states and missing exact-shape keys", () => {
    expect(remediationChecklistPropsSchema.safeParse({ ...checklist, steps: [{ ...step, status: "verifying" }] }).success).toBe(false);
    const { deep_link: _omit, ...noLink } = step;
    expect(remediationChecklistPropsSchema.safeParse({ ...checklist, steps: [noLink] }).success).toBe(false);
    expect(remediationChecklistPropsSchema.safeParse({ ...checklist, extra: 1 }).success).toBe(false);
  });
});

describe("M5 approval card, commit tracker, option, narrative", () => {
  it("approval card carries optional quorum progress, rejection and bundle", () => {
    const pre = approvalCardPropsSchema.parse(approvalCard);
    expect(pre.quorum).toBeNull();
    expect(pre.rejection).toBeNull();
    expect(pre.bundle).toEqual([]);
    const card = approvalCardPropsSchema.parse({
      ...approvalCard,
      quorum,
      rejection: { sub: "u-lena", name: "Lena Vogel", role: "compliance", at: "2026-10-07T10:05:00Z", reason: "No" },
      bundle: [{ connector: "merxa", system: "Merxa", change_set_id: "cs_01", change_set_hash: HASH, lines: 1 }],
    });
    expect(quorumProgress(card.quorum!)).toEqual({ approved: 1, needed: 2, met: false });
    expect(policyQuorumSchema.parse(quorum).missing_roles).toEqual(["compliance"]);
  });

  it("commit tracker knows rolled_back and the undo action", () => {
    const base = { change_set_id: "cs_01", system: "Merxa", status: "applied", applied: 2, verified: 2 };
    const t = commitTrackerPropsSchema.parse({ ...base, actions: [{ type: "undo", label: "Undo changes", enabled: true }] });
    expect(t.actions[0]?.type).toBe("undo");
    expect(commitTrackerPropsSchema.parse({ ...base, status: "rolled_back" }).conflicts).toEqual([]);
    expect(commitTrackerPropsSchema.safeParse({ ...base, actions: [{ type: "redo", label: "x", enabled: true }] }).success).toBe(false);
  });

  it("recommended option carries apply/guide line counts", () => {
    const kpi = { key: "units_at_risk", label: "Units at risk", value: 1, unit: "units", prov: "simulated", preliminary: false };
    const rec = recommendedOptionPropsSchema.parse({
      plan_version: 1,
      option: { id: "opt-A", title: "Rebalance", summary_bullets: [], kpis: [kpi] },
      alternatives: [],
      actions: [
        { type: "apply", label: "Apply 3 changes for me", enabled: true },
        { type: "guide", label: "Guide me through 2", enabled: true },
      ],
      remediation_mix: { apply_lines: 3, guide_lines: 2 },
    });
    expect(rec.remediation_mix).toEqual({ apply_lines: 3, guide_lines: 2 });
  });

  it("narrative carries a re-plan banner and plan version links", () => {
    const n = narrativePropsSchema.parse({
      markdown: "x",
      banner: { text: "New information: sea-air capacity cut to 30% — plan updated (v2)", plan_version: 2, previous_plan_version: 1 },
      plan_versions: [
        { version: 1, label: "v1", status: "committed", current: false },
        { version: 2, label: "v2", status: "proposed", current: true },
      ],
    });
    expect(n.banner?.signal_id).toBeNull();
    expect(narrativePropsSchema.parse({ markdown: "x" }).plan_versions).toEqual([]);
  });
});

describe("M5 policy, actions, demo reset", () => {
  it("validates policy rules, sets, contexts and decisions", () => {
    const rule = { id: "four-eyes", description: "Two people", when: "cs.incremental_cost_cents > 1000000", require: FOUR_EYES, priority: 3 };
    expect(policyRuleSchema.parse(rule).require).toEqual(FOUR_EYES);
    expect(policyRuleSchema.parse({ ...rule, require: "auto" }).require).toBe("auto");
    expect(policyRuleSchema.safeParse({ ...rule, require: "human" }).success).toBe(false);
    expect(policySetSchema.parse({ version: "p-2", rules: [rule] }).rules).toHaveLength(1);
    expect(
      policyContextSchema.parse({
        cs: { lines: 1, units: 1, value_cents: 1, ops: ["po.create"], connectors: ["merxa"], incremental_cost_cents: 0, lost_units_after: 0, levers: [] },
        case: { mode: "actual", origin: "webhook", tainted: false, unconfirmed: 0 },
        initiator: { roles: ["planner"] },
      }).case.origin,
    ).toBe("webhook");
    expect(
      policyDecisionSchema.parse({ outcome: "human", rule_id: "four-eyes", policy_version: "p-2", requirement: FOUR_EYES, reason: "r" }).outcome,
    ).toBe("human");
  });

  it("run requests accept rollback and checklist_step actions", () => {
    const run = (payload: unknown) => ({
      run_id: "r1",
      tenant: "demo-retail",
      case_id: "c1",
      kind: "action",
      actor: { sub: "u", username: "u", name: "U", roles: ["planner"] },
      payload,
    });
    expect(runRequestSchema.safeParse(run({ type: "rollback", change_set_id: "cs_01" })).success).toBe(true);
    expect(runRequestSchema.safeParse(run({ type: "checklist_step", checklist_id: "chk", seq: 1, status: "done" })).success).toBe(true);
    expect(runRequestSchema.safeParse(run({ type: "checklist_step", checklist_id: "chk", seq: 1, status: "verified" })).success).toBe(false);
  });

  it("parses the demo reset step log", () => {
    const r = demoResetResultSchema.parse({
      ok: true,
      keep_consents: false,
      started_at: "2026-10-07T10:00:00Z",
      duration_ms: 8200,
      steps: [{ step: "merxa_scenario_reset", status: "ok", duration_ms: 900, counts: { cases: 3 } }],
    });
    expect(r.steps[0]?.detail).toBeNull();
    expect(demoResetResultSchema.safeParse({ ...r, steps: [{ step: "x", status: "boom", duration_ms: 1 }] }).success).toBe(false);
  });
});
