import { describe, expect, it } from "vitest";
import { baseSpec, makeAdminServer } from "../../../test/adminMock";
import {
  blockedFactoriesMessage,
  blockedFactoriesRemoved,
  integerStage,
  parseFactoryCodes,
  planningIssues,
  setAltSourcing,
  setIntegerStage,
} from "./planning";
import { applyMergePatch, mergePatch, normalizeSpec, validateDraft } from "./spec";
import { agentSpecSchema, catalogSchema, type AgentSpec } from "./types";

const spec = (): AgentSpec => agentSpecSchema.parse(baseSpec());
const catalog = () => catalogSchema.parse(makeAdminServer().catalog);

describe("M8 planning solver settings (125)", () => {
  it("parses the engine's planning object (loose: other planning keys pass through)", () => {
    const s = agentSpecSchema.parse({
      ...baseSpec(),
      planning: {
        default_delay_days: 7,
        solver_integer_stage: "cpsat",
        alt_sourcing: { moq_units: 300, order_multiple_units: 6, moq_basis: "style", blocked_factories: ["F-MA-01"], unit_cost_premium_bp: 2500 },
      },
    });
    expect(integerStage(s)).toBe("cpsat");
    expect(s.planning?.alt_sourcing?.order_multiple_units).toBe(6);
    expect((s.planning as Record<string, unknown>)["default_delay_days"]).toBe(7);
    expect(agentSpecSchema.safeParse({ ...baseSpec(), planning: { solver_integer_stage: "mip" } }).success).toBe(false);
    expect(integerStage(spec())).toBe("repair"); // omitted = the default
  });

  it("merge-patches only the changed settings; defaults of the M8 settings drop the key", () => {
    const base = spec();
    const draft = structuredClone(base);
    setIntegerStage(draft, "cpsat");
    setAltSourcing(draft, "order_multiple_units", 12);
    setAltSourcing(draft, "moq_basis", "style");
    setAltSourcing(draft, "share_factory_capacity", true);
    const patch = mergePatch(base, draft);
    expect(patch).toEqual({
      planning: { solver_integer_stage: "cpsat", alt_sourcing: { order_multiple_units: 12, moq_basis: "style", share_factory_capacity: true } },
    });
    expect(applyMergePatch(base, patch)).toEqual(draft);
    // back to the defaults: nothing left to save
    setIntegerStage(draft, "repair");
    setAltSourcing(draft, "order_multiple_units", 1);
    setAltSourcing(draft, "moq_basis", "po");
    setAltSourcing(draft, "share_factory_capacity", false);
    expect(draft.planning).toEqual({ default_delay_days: 7, alt_sourcing: {} });
    expect(mergePatch(normalizeSpec(base), normalizeSpec(draft))).toEqual({});
    // a cleared number goes back to the engine default (null in the patch)
    const withMoq = agentSpecSchema.parse({ ...baseSpec(), planning: { alt_sourcing: { moq_units: 300, blocked_factories: [] } } });
    const cleared = structuredClone(withMoq);
    setAltSourcing(cleared, "moq_units", undefined);
    expect(mergePatch(normalizeSpec(withMoq), normalizeSpec(cleared))).toEqual({ planning: { alt_sourcing: { moq_units: null } } });
  });

  it("validateDraft rejects a case pack below 1 and other bad numbers", () => {
    const draft = spec();
    setAltSourcing(draft, "order_multiple_units", 0);
    setAltSourcing(draft, "moq_units", -5);
    setAltSourcing(draft, "max_orders_per_source", 53);
    const issues = validateDraft(draft, catalog(), spec());
    expect(issues["planning.alt_sourcing.order_multiple_units"]).toBe("A whole number from 1 to 100,000.");
    expect(issues["planning.alt_sourcing.moq_units"]).toBe("A whole number of at least 0.");
    expect(issues["planning.alt_sourcing.max_orders_per_source"]).toBe("A whole number from 1 to 52.");
    setAltSourcing(draft, "order_multiple_units", 1.5);
    expect(planningIssues(draft)["planning.alt_sourcing.order_multiple_units"]).toBeDefined();
    const ok = spec();
    setAltSourcing(ok, "order_multiple_units", 12);
    expect(planningIssues(ok)).toEqual({});
  });

  it("removing a blocked factory is a guarded change; adding one is not", () => {
    const base = agentSpecSchema.parse({ ...baseSpec(), planning: { alt_sourcing: { blocked_factories: ["F-MA-01", "F-TR-01"] } } });
    const draft = structuredClone(base);
    setAltSourcing(draft, "blocked_factories", ["F-TR-01", "F-BD-02"]);
    expect(blockedFactoriesRemoved(base, draft)).toEqual(["F-MA-01"]);
    expect(blockedFactoriesMessage(["F-MA-01"])).toBe("Compliance block: removing F-MA-01 lets the planner buy from it");
    setAltSourcing(draft, "blocked_factories", ["F-MA-01", "F-TR-01", "F-BD-02"]);
    expect(blockedFactoriesRemoved(base, draft)).toEqual([]);
    expect(parseFactoryCodes(" F-MA-01,F-TR-01  F-MA-01 ,, ")).toEqual(["F-MA-01", "F-TR-01"]);
  });
});
