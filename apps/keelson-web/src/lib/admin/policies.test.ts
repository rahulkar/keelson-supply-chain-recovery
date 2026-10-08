import { describe, expect, it } from "vitest";
import {
  CONTEXT_PRESETS,
  celHints,
  checkContext,
  conditionChange,
  guardedChanges,
  ordered,
  parseList,
  requirementAtLeast,
  requirementText,
  validateRules,
  type PolicyRuleView,
} from "./policies";

/** The engine's default rules (core/policies.py DEFAULT_RULES). */
const DEFAULTS: PolicyRuleView[] = [
  { id: "refuse-unconfirmed", description: "What-if never commits", when: 'case.mode == "what_if" || case.unconfirmed > 0', require: "refuse", priority: 10 },
  {
    id: "four-eyes",
    description: "Two people",
    when: 'cs.incremental_cost_cents > 1000000 || cs.ops.exists(o, o in ["po.create", "po_line.cancel", "production_order.resequence"])',
    require: { roles: ["approver", "compliance"], count: 2, exclude_initiator: true },
    priority: 20,
  },
  { id: "human-untrusted", description: "Signals need people", when: 'case.origin != "chat" || case.tainted', require: { roles: ["approver"], count: 1, exclude_initiator: false }, priority: 30 },
  {
    id: "auto-small-transfer",
    description: "Small transfers",
    when: 'cs.ops.all(o, o in ["allocation.set_qty", "transfer_order.create"]) && cs.lines <= 10 && cs.units <= 500 && initiator.roles.exists(r, r == "planner")',
    require: "auto",
    priority: 40,
  },
  { id: "default", description: "Default", when: "true", require: { roles: ["approver"], count: 1, exclude_initiator: false }, priority: 50 },
];

const clone = () => structuredClone(DEFAULTS);
const rule = (rules: PolicyRuleView[], id: string) => rules.find((r) => r.id === id)!;
const reasons = (old: PolicyRuleView[], next: PolicyRuleView[]) => guardedChanges(old, next).map((g) => `${g.ruleId}: ${g.reason}`);

describe("conditionChange (engine condition_change port)", () => {
  it.each([
    ["cs.lines <= 10", "cs.lines <= 10", "same"],
    ["cs.lines <= 10", "cs.lines <= 20", "wider"],
    ["cs.lines <= 10", "cs.lines <= 5", "narrower"],
    ["cs.incremental_cost_cents > 1000000", "cs.incremental_cost_cents > 500000", "wider"],
    ["10 >= cs.lines", "20 >= cs.lines", "wider"],
    ["cs.lines <= 10 && cs.units <= 500", "cs.lines <= 20 && cs.units <= 100", "unknown"],
    ["!(cs.lines <= 10)", "!(cs.lines <= 20)", "unknown"],
    ["cs.lines == 10", "cs.lines == 20", "unknown"],
    ["cs.lines <= 10", "cs.units <= 10", "unknown"],
    ["cs.lines <= 10", "cs.lines <= 10 && true", "unknown"],
  ])("%s → %s: %s", (a, b, want) => {
    expect(conditionChange(a, b)).toBe(want);
  });
});

describe("requirementAtLeast", () => {
  const four = { roles: ["approver", "compliance"], count: 2, exclude_initiator: true };
  const one = { roles: ["approver"], count: 1, exclude_initiator: false };
  it("orders requirements by strictness", () => {
    expect(requirementAtLeast(four, one)).toBe(true);
    expect(requirementAtLeast(one, four)).toBe(false);
    expect(requirementAtLeast({ ...four, exclude_initiator: false }, four)).toBe(false);
    expect(requirementAtLeast({ roles: ["approver", "compliance"], count: 1, exclude_initiator: false }, one)).toBe(false);
    expect(requirementAtLeast(one, { roles: ["approver", "compliance"], count: 1, exclude_initiator: false })).toBe(true);
  });
});

describe("guardedChanges (engine guarded_changes port)", () => {
  it("no change, description edits, lowering auto caps and tightening are fine for a tenant admin", () => {
    expect(guardedChanges(DEFAULTS, clone())).toEqual([]);
    const next = clone();
    rule(next, "default").description = "Anything else needs an approver";
    rule(next, "auto-small-transfer").when = rule(next, "auto-small-transfer").when.replace("cs.units <= 500", "cs.units <= 200");
    rule(next, "human-untrusted").require = { roles: ["approver"], count: 2, exclude_initiator: true };
    expect(reasons(DEFAULTS, next)).toEqual([]);
    // Removing the auto rule and adding a refuse rule are tightenings too.
    const fewer = clone().filter((r) => r.id !== "auto-small-transfer");
    fewer.push({ id: "refuse-big", description: "Too big", when: "cs.units > 100000", require: "refuse", priority: 5 });
    expect(reasons(DEFAULTS, fewer)).toEqual([]);
  });

  it("raising auto caps, a new auto rule and moving it ahead are guarded", () => {
    const raised = clone();
    rule(raised, "auto-small-transfer").when = rule(raised, "auto-small-transfer").when.replace("cs.units <= 500", "cs.units <= 5000");
    expect(reasons(DEFAULTS, raised)).toEqual(["auto-small-transfer: raises the automatic-approval caps"]);

    const added = clone();
    added.push({ id: "auto-all", description: "Everything", when: "true", require: "auto", priority: 45 });
    // Like the engine: a new auto rule also counts as moving ahead of the existing rules after it.
    expect(reasons(DEFAULTS, added)).toEqual(["auto-all: adds an automatic-approval rule", "auto-all: moves an automatic-approval rule ahead of other rules"]);

    const moved = clone();
    rule(moved, "auto-small-transfer").priority = 15;
    expect(reasons(DEFAULTS, moved)).toEqual(["auto-small-transfer: moves an automatic-approval rule ahead of other rules"]);
  });

  it("removing or weakening refuse / approval rules is guarded", () => {
    expect(reasons(DEFAULTS, clone().filter((r) => r.id !== "refuse-unconfirmed"))).toEqual(["refuse-unconfirmed: removes a refuse rule"]);
    expect(reasons(DEFAULTS, clone().filter((r) => r.id !== "four-eyes"))).toEqual(["four-eyes: removes an approval rule"]);
    const weak = clone();
    rule(weak, "four-eyes").require = { roles: ["approver"], count: 1, exclude_initiator: false };
    expect(reasons(DEFAULTS, weak)).toEqual(["four-eyes: weakens the approval requirement"]);
    const turned = clone();
    rule(turned, "refuse-unconfirmed").require = { roles: ["approver"], count: 1, exclude_initiator: false };
    expect(reasons(DEFAULTS, turned)).toContain("refuse-unconfirmed: turns a refuse rule into an approval rule");
    const narrowed = clone();
    rule(narrowed, "four-eyes").when = "cs.incremental_cost_cents > 2000000";
    expect(reasons(DEFAULTS, narrowed)).toEqual(["four-eyes: narrows when the rule applies"]);
  });

  it("widening an approval rule that runs before a stricter rule is guarded", () => {
    // Regression (M5 review): the port missed this branch of the engine's guarded_changes, so a
    // tenant admin saw no lock (and the save then failed with 403 guarded_change).
    const withFastLane = clone();
    withFastLane.push({ id: "fast-lane", description: "One approver", when: "cs.lines <= 3", require: { roles: ["approver"], count: 1, exclude_initiator: false }, priority: 15 });
    const widened = structuredClone(withFastLane);
    rule(widened, "fast-lane").when = "cs.lines <= 30";
    expect(reasons(withFastLane, widened)).toEqual(["fast-lane: widens a rule that takes precedence over the stricter rule four-eyes"]);
    const narrowedLane = structuredClone(withFastLane);
    rule(narrowedLane, "fast-lane").when = "cs.lines <= 2";
    expect(reasons(withFastLane, narrowedLane)).toEqual(["fast-lane: narrows when the rule applies"]);
    // widening the last approval rule takes nothing away from stricter ones
    const wideDefault = clone();
    const tail = structuredClone(wideDefault);
    rule(wideDefault, "default").when = "cs.lines <= 100";
    rule(tail, "default").when = "cs.lines <= 1000";
    expect(reasons(wideDefault, tail)).toEqual([]);
  });

  it("a new approval rule ahead of a refuse or stricter rule is guarded", () => {
    const ahead = clone();
    ahead.push({ id: "fast-lane", description: "One approver", when: "cs.lines <= 3", require: { roles: ["approver"], count: 1, exclude_initiator: false }, priority: 5 });
    expect(reasons(DEFAULTS, ahead)).toEqual([
      "fast-lane: would take precedence over the refuse rule refuse-unconfirmed",
      "fast-lane: would take precedence over the stricter rule four-eyes",
    ]);
  });
});

describe("validateRules", () => {
  it("accepts the defaults and reports duplicates, bad ids and empty fields", () => {
    expect(validateRules(DEFAULTS)).toEqual([]);
    const bad = clone();
    bad[1]!.id = "refuse-unconfirmed";
    bad[2]!.priority = 10;
    bad[3]!.description = " ";
    bad[4]!.when = "";
    bad[0]!.id = "Bad Id";
    const fields = validateRules(bad).map((p) => `${p.ruleIndex}:${p.field}`);
    expect(fields).toEqual(expect.arrayContaining(["0:id", "2:priority", "3:description", "4:when"]));
    expect(validateRules([])).toEqual([{ ruleIndex: null, field: "rules", message: "Keep at least one rule." }]);
    const q = clone();
    q[1]!.require = { roles: [], count: 0, exclude_initiator: true };
    expect(validateRules(q).filter((p) => p.field === "require")).toHaveLength(2);
  });
});

describe("celHints", () => {
  it("the default conditions read clean", () => {
    for (const r of DEFAULTS) expect(celHints(r.when), r.when).toEqual([]);
  });

  it("spots common slips", () => {
    expect(celHints("")).toEqual(["Enter a condition, e.g. cs.lines <= 10 (or true to always match)."]);
    expect(celHints("cs.lines = 10")).toContain("Use == to compare (a single = is not an operator).");
    expect(celHints("cs.lines <= 10 and cs.units <= 5")).toContain("CEL uses &&, || and ! instead of and / or / not.");
    expect(celHints("cs.lines <= 10 & cs.units <= 5")).toContain("Use && and || to combine conditions.");
    expect(celHints("(cs.lines <= 10")).toContain('"(" is not closed.');
    expect(celHints('case.mode == "whatif"')).toContain('case.mode is one of "actual", "what_if" (not "whatif").');
    expect(celHints("cs.qty > 1")[0]).toMatch(/^Unknown field cs.qty\./);
    expect(celHints("lines > 1")).toContain("Unknown name lines: conditions read cs., case. or initiator.");
    expect(celHints("cs > 1")[0]).toMatch(/^cs alone is an object/);
    expect(celHints("size(cs.ops) > 2 && cs.ops.exists(x, x.startsWith(\"po.\"))")).toEqual([]);
    expect(celHints('case.origin == "chat" && "a = b" == "c"')).toEqual([]);
  });
});

describe("test bench helpers", () => {
  it("presets are valid contexts; invalid numbers are reported", () => {
    for (const p of CONTEXT_PRESETS) expect(checkContext(p.context).ok, p.id).toBe(true);
    const bad = structuredClone(CONTEXT_PRESETS[0]!.context) as unknown as { cs: { lines: number } };
    bad.cs.lines = 1.5;
    const out = checkContext(bad);
    expect(out.ok).toBe(false);
    expect(!out.ok && out.message).toMatch(/^cs\.lines: /);
  });

  it("parseList / ordered / requirementText", () => {
    expect(parseList(" po.create, transfer_order.create  po.create ")).toEqual(["po.create", "transfer_order.create"]);
    expect(ordered([{ priority: 30 }, { priority: 10 }]).map((r) => r.priority)).toEqual([10, 30]);
    expect(requirementText("refuse")).toBe("Refuse (no change set is compiled)");
    expect(requirementText("auto")).toBe("Apply automatically");
    expect(requirementText({ roles: ["approver", "compliance"], count: 2, exclude_initiator: true })).toBe(
      "2 different people: Approver and Compliance (not the person who started it)",
    );
    expect(requirementText({ roles: ["approver", "compliance"], count: 1, exclude_initiator: false })).toBe("1 person: Approver or Compliance");
  });
});
