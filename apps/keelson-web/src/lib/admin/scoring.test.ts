import { describe, expect, it } from "vitest";
import {
  DEFAULT_PROFILES,
  MAX_PROFILES,
  bpToPercent,
  centsToEuros,
  emissionsInfo,
  eurosToCents,
  newProfileId,
  percentToBp,
  scoringFromSpec,
  scoringPatch,
  serverIssues,
  validateScoring,
  type ScoringDraft,
} from "./scoring";

/** The spec's planning section as the engine serves it (``GET /api/admin/agents/spec``). */
const SPEC = {
  planning: {
    solver_seed: 7,
    grams_per_unit: 800,
    scoring: { default_profile: "net-benefit", profiles: DEFAULT_PROFILES.map((p) => ({ ...p })) },
    robustness: { enabled: true, samples: 64, seed: null },
    emissions: {
      enabled: true,
      factors_id: "glec-v3-indicative",
      unit_weights: [{ style_prefix: "PUF", grams: 1100 }],
      default_unit_weight_g: null,
    },
  },
};

const draft = (): ScoringDraft => structuredClone(scoringFromSpec(SPEC));

describe("option scoring (M8)", () => {
  it("reads the spec, falling back to the built-in profiles for older specs", () => {
    const d = draft();
    expect(d.defaultProfile).toBe("net-benefit");
    expect(d.profiles.map((p) => p.id)).toEqual(["net-benefit", "cost-first", "service-first", "low-carbon"]);
    expect(d.samples).toBe(64);
    const old = scoringFromSpec({ planning: { solver_seed: 7 } });
    expect(old.profiles).toEqual(DEFAULT_PROFILES);
    expect(old.samples).toBe(64);
    expect(scoringFromSpec({})).toEqual(old);
    expect(emissionsInfo(SPEC)).toEqual({
      enabled: true,
      factorsId: "glec-v3-indicative",
      unitWeights: [{ stylePrefix: "PUF", grams: 1100 }],
      defaultWeightG: 800,
      robustnessEnabled: true,
    });
  });

  it("validates like the engine (ScoringConfig, WeightProfile, RobustnessDefaults)", () => {
    expect(validateScoring(draft())).toEqual({});
    const d = draft();
    d.profiles[1]!.cost_bp = 50_001;
    d.profiles[2]!.risk_aversion_bp = -1;
    d.profiles[3]!.carbon_price_cents_per_t = Number.NaN;
    d.profiles[0]!.label = "  ";
    d.samples = 8;
    expect(validateScoring(d)).toEqual({
      "profiles.0.label": "Give the profile a name.",
      "profiles.1.cost_bp": "Between 0% and 500%.",
      "profiles.2.risk_aversion_bp": "Between 0% and 100%.",
      "profiles.3.carbon_price_cents_per_t": "Between €0 and €10,000 per tonne.",
      samples: "A whole number from 16 to 256.",
    });
    const dup = draft();
    dup.profiles[1]!.id = "net-benefit";
    expect(validateScoring(dup)["profiles.1.id"]).toBe("Another profile already uses this id.");
    const badId = draft();
    badId.profiles[1]!.id = "Cost First";
    expect(validateScoring(badId)["profiles.1.id"]).toMatch(/Lower-case/);
    const noDefault = draft();
    noDefault.defaultProfile = "gone";
    expect(validateScoring(noDefault)["default"]).toBe("Pick the default profile.");
    const many = draft();
    many.profiles = Array.from({ length: MAX_PROFILES + 1 }, (_, i) => ({ ...DEFAULT_PROFILES[0]!, id: `p${i}` }));
    many.defaultProfile = "p0";
    expect(validateScoring(many)["profiles"]).toBe("At most 8 profiles.");
    const long = draft();
    long.profiles[0]!.label = "x".repeat(41);
    expect(validateScoring(long)["profiles.0.label"]).toBe("At most 40 characters.");
  });

  it("builds a minimal merge patch (profiles replace whole, labels trimmed)", () => {
    const base = draft();
    expect(scoringPatch(base, draft())).toEqual({});
    const d = draft();
    d.defaultProfile = "low-carbon";
    expect(scoringPatch(base, d)).toEqual({ planning: { scoring: { default_profile: "low-carbon" } } });
    d.samples = 128;
    d.profiles[3]!.carbon_price_cents_per_t = 12_000;
    d.profiles.push({ id: "board", label: " Board view ", lost_margin_bp: 12_000, cost_bp: 10_000, carbon_price_cents_per_t: 5_000, risk_aversion_bp: 3_000 });
    const patch = scoringPatch(base, d) as { planning: { scoring: { profiles: { id: string; label: string }[]; default_profile: string }; robustness: unknown } };
    expect(Object.keys(patch.planning).sort()).toEqual(["robustness", "scoring"]);
    expect(patch.planning.robustness).toEqual({ samples: 128 });
    expect(patch.planning.scoring.default_profile).toBe("low-carbon");
    expect(patch.planning.scoring.profiles).toHaveLength(5);
    expect(patch.planning.scoring.profiles[4]).toEqual({ id: "board", label: "Board view", lost_margin_bp: 12_000, cost_bp: 10_000, carbon_price_cents_per_t: 5_000, risk_aversion_bp: 3_000 });
    expect(patch.planning.scoring.profiles[3]!).toMatchObject({ id: "low-carbon", carbon_price_cents_per_t: 12_000 });
  });

  it("converts the form's percentages and euros exactly", () => {
    expect(bpToPercent(10_000)).toBe("100");
    expect(bpToPercent(2_550)).toBe("25.5");
    expect(percentToBp("12.5")).toBe(1_250);
    expect(percentToBp("12,25")).toBe(1_225);
    expect(percentToBp("")).toBeNaN();
    expect(percentToBp("abc")).toBeNaN();
    expect(centsToEuros(20_000)).toBe("200");
    expect(eurosToCents("200")).toBe(20_000);
    expect(eurosToCents("199.99")).toBe(19_999);
    expect(newProfileId(DEFAULT_PROFILES)).toBe("custom-1");
    expect(newProfileId([...DEFAULT_PROFILES, { ...DEFAULT_PROFILES[0]!, id: "custom-1" }])).toBe("custom-2");
  });

  it("maps the engine's 422 locations to the fields", () => {
    expect(
      serverIssues([
        { loc: "planning.scoring.profiles.2.cost_bp", msg: "Input should be less than or equal to 50000" },
        { loc: "planning.scoring", msg: "Value error, default_profile 'x' is not one of the profiles" },
        { loc: "planning.robustness.samples", msg: "Input should be greater than or equal to 16" },
        { loc: "planning.scoring.profiles", msg: "List should have at most 8 items" },
        { loc: "planning.emissions.unit_weights.0.style_prefix", msg: "String should match pattern" },
      ]),
    ).toEqual({
      "profiles.2.cost_bp": "Input should be less than or equal to 50000",
      default: "Value error, default_profile 'x' is not one of the profiles",
      samples: "Input should be greater than or equal to 16",
      profiles: "List should have at most 8 items",
      form: "String should match pattern",
    });
  });
});
