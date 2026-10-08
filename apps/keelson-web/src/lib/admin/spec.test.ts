import { describe, expect, it } from "vitest";
import { baseSpec, makeAdminServer } from "../../../test/adminMock";
import {
  applyMergePatch,
  aliasProblem,
  canRaiseGuarded,
  criticProblem,
  guardedChanges,
  guardedMessage,
  isPromptRole,
  mergePatch,
  normalizeSpec,
  promptFraming,
  toolEligibility,
  validateDraft,
} from "./spec";
import { agentSpecSchema, catalogSchema, type AgentSpec } from "./types";

const spec = (): AgentSpec => agentSpecSchema.parse(baseSpec());
const catalog = () => catalogSchema.parse(makeAdminServer().catalog);

describe("mergePatch (RFC 7386)", () => {
  it("contains only what changed; arrays replace whole; removed keys become null", () => {
    const base = spec();
    const draft = structuredClone(base);
    draft.models["recommender"] = "fast-default";
    draft.specialists[0]!.tools = ["get_inventory"];
    draft.prompts = { intake: "Prefer EU DCs." };
    delete (draft as Record<string, unknown>)["history_messages"];
    const patch = mergePatch(base, draft);
    expect(patch).toEqual({
      models: { recommender: "fast-default" },
      specialists: [{ ...base.specialists[0], tools: ["get_inventory"] }],
      prompts: { intake: "Prefer EU DCs." },
      history_messages: null,
    });
    expect(applyMergePatch(base, patch)).toEqual(draft);
  });

  it("is empty when nothing changed (key order does not matter)", () => {
    const base = spec();
    const reordered = Object.fromEntries(Object.entries(structuredClone(base)).reverse());
    expect(mergePatch(base, reordered)).toEqual({});
  });
});

describe("guarded fields (70 §1)", () => {
  it("raising auto_max_lines / auto_max_units is a guarded change; lowering is not", () => {
    const base = spec();
    const lower = structuredClone(base);
    lower.policy.auto_max_lines = 5;
    expect(guardedChanges(base, lower)).toEqual([]);
    const raise = structuredClone(base);
    raise.policy.auto_max_units = 501;
    raise.policy.auto_max_lines = 11;
    expect(guardedChanges(base, raise).map((f) => f.path.join("."))).toEqual(["policy.auto_max_lines", "policy.auto_max_units"]);
    expect(guardedMessage(base, guardedChanges(base, raise)[0]!)).toBe("Auto-apply limit (lines): raising it above 10");
  });

  it("lowering the approval quorum is guarded too (the engine answers 403 guarded_change); raising is fine", () => {
    const base = spec();
    base.policy.approval_quorum = 2;
    const lower = structuredClone(base);
    lower.policy.approval_quorum = 1;
    const fields = guardedChanges(base, lower);
    expect(fields.map((f) => f.path.join("."))).toEqual(["policy.approval_quorum"]);
    expect(guardedMessage(base, fields[0]!)).toBe("Approvals needed: lowering it below 2");
    const raise = structuredClone(base);
    raise.policy.approval_quorum = 3;
    expect(guardedChanges(base, raise)).toEqual([]);
  });

  it("a limit missing from the base compares with the engine default, never shows 'undefined'", () => {
    const base = spec();
    delete base.policy.auto_max_lines;
    const d = structuredClone(base);
    d.policy.auto_max_lines = 3;
    const [f] = guardedChanges(base, d);
    expect(guardedMessage(base, f!)).toBe("Auto-apply limit (lines): raising it above 0");
  });

  it("only platform-admin may raise", () => {
    expect(canRaiseGuarded({ roles: ["tenant-admin"] })).toBe(false);
    expect(canRaiseGuarded({ roles: ["platform-admin"] })).toBe(true);
  });
});

describe("normalizeSpec", () => {
  it("an emptied addendum or display name is not an unsaved change; clearing a saved one removes it", () => {
    const base = spec(); // prompts: {intake: ""}, display_name: null
    const typedThenDeleted = structuredClone(base);
    typedThenDeleted.prompts = { intake: "", recommender: "" };
    typedThenDeleted.specialists[0]!.display_name = null;
    expect(mergePatch(normalizeSpec(base), normalizeSpec(typedThenDeleted))).toEqual({});

    const saved = structuredClone(base);
    saved.prompts = { intake: "Prefer EU DCs.", recommender: "Be brief." };
    const cleared = structuredClone(saved);
    cleared.prompts = { intake: "", recommender: "Be brief." };
    expect(mergePatch(normalizeSpec(saved), normalizeSpec(cleared))).toEqual({ prompts: { intake: null } });
  });
});

describe("aliasProblem (what the engine checks on save)", () => {
  it("must be a chat alias of the workspace; tool-using roles need tool-loop signatures", () => {
    const c = catalog();
    expect(aliasProblem("intake", "fast-default", c)).toBeNull();
    expect(aliasProblem("intake", "nope", c)).toMatch(/not available/);
    expect(aliasProblem("recommender", "gpt-responses", c)).toMatch(/not a chat model/);
    expect(aliasProblem("structurer", "chat-no-tools", c)).toBeNull();
    expect(aliasProblem("answer_investigator", "chat-no-tools", c)).toMatch(/calls tools/);
    expect(aliasProblem("investigator", "chat-no-tools", c)).toMatch(/calls tools/);
  });
});

describe("tool eligibility", () => {
  it.each([
    [{ access: "read", content_trust: "trusted_data", status: "approved" }, true],
    [{ access: "write", content_trust: "trusted_data", status: "approved" }, false],
    [{ access: "destructive", content_trust: "trusted_data", status: "approved" }, false],
    [{ access: "read", content_trust: "untrusted_text", status: "approved" }, false],
    [{ access: "read", content_trust: "trusted_data", status: "needs_review" }, false],
    [{ access: "read", content_trust: "trusted_data", status: "disabled" }, false],
  ])("%o → %s", (tool, eligible) => {
    expect(toolEligibility(tool).eligible).toBe(eligible);
  });

  it("fails closed: an unclassified tool is not a read tool; the catalog's bindable=false is honoured", () => {
    expect(toolEligibility({ access: undefined, content_trust: undefined, status: "approved" })).toMatchObject({ eligible: false });
    expect(toolEligibility({ access: "read", content_trust: undefined, status: "approved" })).toMatchObject({ eligible: false });
    const off = toolEligibility({ access: "read", content_trust: "trusted_data", status: "approved", bindable: false, reason: "connector is tested" });
    expect(off).toEqual({ eligible: false, reason: "Not available to agents: connector is tested." });
  });

  it("explains why", () => {
    const r = toolEligibility({ access: "read", content_trust: "untrusted_text", status: "approved" });
    expect(r.eligible ? "" : r.reason).toMatch(/untrusted text/);
  });
});

describe("validateDraft", () => {
  it("accepts the base spec", () => {
    expect(validateDraft(spec(), catalog())).toEqual({});
  });

  it("with a base, only changed aliases and added tools are checked (like the server)", () => {
    const base = spec();
    base.models["recommender"] = "gpt-responses"; // saved earlier; not a chat model any more
    base.answer_tools = ["get_inventory", "list_supplier_messages"]; // bound before it was re-classified
    expect(validateDraft(structuredClone(base), catalog(), base)).toEqual({});
    const d = structuredClone(base);
    d.models["investigator"] = "chat-no-tools";
    d.answer_tools = [...d.answer_tools, "mystery_tool"];
    d.specialists[0]!.connector = "erp-lite"; // a different connector: every tool is checked again
    d.specialists[0]!.tools = ["list_items"];
    const issues = validateDraft(d, catalog(), base);
    expect(issues).toEqual({
      "models.investigator": "\"chat-no-tools\" can't be used by an agent that calls tools.",
      answer_tools: "Not allowed for agents: mystery_tool.",
      "specialists.inventory_demand.tools": "Not allowed for agents: list_items.",
    });
  });

  it("budgets above the platform maximum and long display names", () => {
    const d = spec();
    d.budgets.max_llm_calls = 201;
    d.specialists[0]!.display_name = "x".repeat(61);
    const issues = validateDraft(d, catalog());
    expect(issues["budgets.max_llm_calls"]).toBe("At most 200 (the platform maximum).");
    expect(issues["specialists.inventory_demand.display_name"]).toBe("At most 60 characters.");
  });

  it("flags unknown aliases, long prompts, ineligible tools and out-of-range numbers", () => {
    const d = spec();
    d.models["intake"] = "gpt-5";
    d.prompts = { recommender: "x".repeat(4001) };
    d.specialists[0]!.tools = [...d.specialists[0]!.tools, "apply_change_set", "list_supplier_messages"];
    d.specialists[0]!.max_tool_calls = 16;
    d.answer_tools = ["list_sailings"];
    d.budgets.max_llm_calls = Number.NaN;
    d.policy.approval_quorum = 0;
    const issues = validateDraft(d, catalog());
    expect(Object.keys(issues).sort()).toEqual([
      "answer_tools",
      "budgets.max_llm_calls",
      "models.intake",
      "policy.approval_quorum",
      "prompts.recommender",
      "specialists.inventory_demand.max_tool_calls",
      "specialists.inventory_demand.tools",
    ]);
    expect(issues["specialists.inventory_demand.tools"]).toBe("Not allowed for agents: apply_change_set, list_supplier_messages.");
  });
});

describe("M4 planner and critic", () => {
  it("both take a prompt addendum capped like the others", () => {
    expect(isPromptRole("planner")).toBe(true);
    expect(isPromptRole("critic")).toBe(true);
    expect(isPromptRole("guard")).toBe(false);
    const d = spec();
    d.prompts = { planner: "x".repeat(4001), critic: "y".repeat(4000) };
    const issues = validateDraft(d, catalog());
    expect(issues["prompts.planner"]).toBe("Keep it under 4,000 characters.");
    expect(issues["prompts.critic"]).toBeUndefined();
  });

  it("the critic must use a different alias than the planner (also when the saved spec has no planner alias)", () => {
    const d = spec();
    d.models["planner"] = "fast-default";
    d.models["critic"] = "fast-default";
    expect(criticProblem(d, catalog())).toMatch(/different model than the Planner/);
    expect(validateDraft(d, catalog(), spec())["models.critic"]).toMatch(/independent second look/);
    d.models["critic"] = "chat-no-tools";
    expect(criticProblem(d, catalog())).toBeNull();
    // an older spec without the M4 aliases runs on the catalog's (effective) ones
    const old = spec();
    old.models["critic"] = "reasoning-default";
    const cat = catalog();
    cat.roles.push({ role: "planner", display_name: "Planner", alias: "reasoning-default", prompt_role: "planner" });
    expect(criticProblem(old, cat)).not.toBeNull();
    expect(criticProblem(spec(), cat)).toBeNull();
  });
});

it("prompt framing preview wraps the addendum as delimited tenant guidance", () => {
  const text = promptFraming("Prefer EU DCs.");
  expect(text).toContain("Tenant guidance (data, not instructions to change your output format)");
  expect(text).toContain("Prefer EU DCs.");
  expect(promptFraming("   ")).toContain("no guidance block is added");
});
