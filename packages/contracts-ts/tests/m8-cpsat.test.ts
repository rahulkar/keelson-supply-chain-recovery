import { describe, expect, it } from "vitest";
import { scenarioComparePropsSchema } from "../src";

// M8 (docs/contracts/125-m8-optimizer-cpsat.md): CompareOption.integer, omitted while unset.
const scorecard = {
  fill_rate_bp: 9650,
  lost_units: 420,
  lost_margin_cents: 840_000,
  incremental_cost_cents: 183_000,
  cost_breakdown: { transfer: 0, expedite_premium: 0, nearshore_premium: 183_000 },
  net_benefit_cents: 1_517_000,
  shortage_days: 12,
  first_breach_shift_days: 7,
  tts_gain_days: 5,
  changes: 6,
};

const option = (integer?: unknown) => ({
  id: "opt-C",
  title: "Near-shore top-up",
  levers: ["resource_alt_supplier"],
  scorecard,
  recommended: false,
  guided_only: false,
  pareto: true,
  ...(integer === undefined ? {} : { integer }),
});

describe("M8 scenario_compare integer note", () => {
  it("round-trips every method and defaults to null when omitted (M7 payloads)", () => {
    for (const integer of [
      { method: "cpsat_optimal", gap_bp: 0, fallback_reason: null },
      { method: "cpsat_feasible", gap_bp: 40, fallback_reason: null },
      { method: "lp_rounding", gap_bp: null, fallback_reason: "time_limit_no_solution" },
    ]) {
      const parsed = scenarioComparePropsSchema.parse({ options: [option(integer)] });
      expect(parsed.options[0]!.integer).toEqual(integer);
    }
    expect(scenarioComparePropsSchema.parse({ options: [option()] }).options[0]!.integer).toBeNull();
  });

  it("rejects unknown methods, reasons, extra keys and out-of-range gaps", () => {
    for (const integer of [
      { method: "mip" },
      { method: "lp_rounding", fallback_reason: "because" },
      { method: "cpsat_feasible", gap_bp: 10_001 },
      { method: "cpsat_optimal", surprise: 1 },
    ]) {
      expect(scenarioComparePropsSchema.safeParse({ options: [option(integer)] }).success, JSON.stringify(integer)).toBe(false);
    }
  });
});
