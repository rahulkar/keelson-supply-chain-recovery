import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import { VALUE_UNITS, knownBlockEnvelopeSchema, parseBlockProps, scenarioComparePropsSchema } from "../src";

// Fixtures mirror packages/contracts-py/tests/test_m8_co2_robustness.py (docs/contracts/126).
const vizFixtures = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../viz/fixtures");
const read = (name: string) => JSON.parse(fs.readFileSync(path.join(vizFixtures, name), "utf8")) as Record<string, unknown>;

const card = {
  fill_rate_bp: 9650,
  lost_units: 420,
  lost_margin_cents: 840_000,
  incremental_cost_cents: 183_000,
  cost_breakdown: { transfer: 63_000, expedite_premium: 120_000, nearshore_premium: 0 },
  net_benefit_cents: 1_517_000,
  shortage_days: 12,
  first_breach_shift_days: 7,
  tts_gain_days: 5,
  changes: 6,
};

describe("M8 CO₂e and robustness blocks", () => {
  it("kg_co2e is a value unit", () => {
    expect(VALUE_UNITS).toContain("kg_co2e");
  });

  it("the robust scenario_compare and kpi_strip fixtures round-trip through the strict envelope", () => {
    for (const name of ["scenario_compare.robust.json", "kpi_strip.robust.json"]) {
      const env = read(name);
      const parsed = knownBlockEnvelopeSchema.safeParse(env);
      expect(parsed.success, name).toBe(true);
      const props = parseBlockProps(env as never);
      expect(props.ok, name).toBe(true);
    }
    const compare = scenarioComparePropsSchema.parse(read("scenario_compare.robust.json")["props"]);
    expect(compare.profiles.map((p) => p.id)).toEqual(["net-benefit", "cost-first", "service-first", "low-carbon"]);
    expect(compare.options.reduce((s, o) => s + (o.win_rate_bp ?? 0), 0)).toBe(10_000);
    expect(compare.options[1]!.scorecard.incremental_co2e_kg).toBe(22_932);
    expect(compare.options[1]!.scorecard.bands!.net_benefit_cents.p10).toBe(14_775_428);
    expect(compare.uncertainty!.samples).toBe(64);
    expect(compare.emissions!.indicative).toBe(true);
  });

  it("every pre-M8 fixture still parses (new fields default to empty / null)", () => {
    const names = fs.readdirSync(vizFixtures).filter((n) => n.endsWith(".json") && !n.includes(".data.") && !n.startsWith("invalid.") && !n.startsWith("unknown."));
    expect(names.length).toBeGreaterThan(30);
    for (const name of names) {
      const env = read(name);
      expect(parseBlockProps(env as never).ok, name).toBe(true);
    }
    const plain = scenarioComparePropsSchema.parse(read("scenario_compare.json")["props"]);
    expect(plain.profiles).toEqual([]);
    expect(plain.uncertainty).toBeNull();
    expect(plain.emissions).toBeNull();
    expect(plain.options[0]!.scores).toEqual([]);
    expect(plain.options[0]!.win_rate_bp).toBeNull();
    expect(plain.options[0]!.scorecard.bands).toBeNull();
  });

  it("rejects inconsistent CO₂e and bands", () => {
    const option = { id: "opt-A", title: "A", levers: [], scorecard: card, recommended: false, guided_only: false, pareto: true };
    const props = (over: Record<string, unknown>, scorecard: Record<string, unknown> = {}) =>
      scenarioComparePropsSchema.safeParse({ options: [{ ...option, scorecard: { ...card, ...scorecard }, ...over }] });
    expect(props({}).success).toBe(true);
    expect(props({ win_rate_bp: 10_001 }).success).toBe(false);
    expect(props({ scores: [{ profile_id: "Bad Id", score_cents: 1, rank: 1 }] }).success).toBe(false);
    expect(props({}, { incremental_co2e_kg: 1.5 }).success).toBe(false);
    expect(props({}, { bands: { samples: 0 } }).success).toBe(false);
    const kpi = (band: unknown) =>
      parseBlockProps({ kind: "kpi_strip", v: 1, props: { items: [{ key: "x", label: "X", value: 1, unit: "kg_co2e", prov: "simulated", preliminary: false, band }] } });
    expect(kpi({ p10: 1, p90: 2, samples: 64 }).ok).toBe(true);
    expect(kpi({ p10: 1, p90: 2, samples: 0 }).ok).toBe(false);
    expect(kpi(null).ok).toBe(true);
  });
});
