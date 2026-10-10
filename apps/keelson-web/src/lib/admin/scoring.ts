/**
 * Option scoring for the admin **Policies** page (M8, `docs/contracts/126-m8-co2-robustness.md` §6):
 * the tenant's weight profiles (`AgentSpec.planning.scoring`, `keelson_contracts.scoring`), the
 * default one (it sets the optimizer's weights and ranks the options for the recommendation), and
 * how many sampled futures each option is checked in (`planning.robustness.samples`). Saved as a
 * new agent-spec version through `PUT /api/admin/agents/spec` (RFC 7386 merge patch; arrays such
 * as `profiles` replace whole). Not a guarded change: it never changes who approves.
 *
 * Hand-written loose Zod over the spec's `planning` section (like `policies.ts`); the client checks
 * mirror the server's (`ScoringConfig` / `RobustnessDefaults`), which validates again (422
 * `spec_invalid` with `errors[].loc`) and has the last word.
 */
import { z } from "zod";

export const MAX_PROFILES = 8;
export const LABEL_MAX = 40;
export const WEIGHT_MAX_BP = 50_000;
export const RISK_MAX_BP = 10_000;
export const CARBON_MAX_CENTS_PER_T = 1_000_000;
export const SAMPLES_MIN = 16;
export const SAMPLES_MAX = 256;
export const PROFILE_ID = /^[a-z][a-z0-9-]{0,31}$/;

const int = z.number().int();

export const weightProfileSchema = z
  .object({
    id: z.string(),
    label: z.string(),
    lost_margin_bp: int.default(10_000),
    cost_bp: int.default(10_000),
    carbon_price_cents_per_t: int.default(0),
    risk_aversion_bp: int.default(0),
  })
  .loose();
export type WeightProfile = {
  id: string;
  label: string;
  lost_margin_bp: number;
  cost_bp: number;
  carbon_price_cents_per_t: number;
  risk_aversion_bp: number;
};

export const DEFAULT_PROFILES: readonly WeightProfile[] = [
  { id: "net-benefit", label: "Net benefit", lost_margin_bp: 10_000, cost_bp: 10_000, carbon_price_cents_per_t: 0, risk_aversion_bp: 0 },
  { id: "cost-first", label: "Cost first", lost_margin_bp: 10_000, cost_bp: 15_000, carbon_price_cents_per_t: 0, risk_aversion_bp: 0 },
  { id: "service-first", label: "Service first", lost_margin_bp: 15_000, cost_bp: 7_500, carbon_price_cents_per_t: 0, risk_aversion_bp: 5_000 },
  { id: "low-carbon", label: "Low carbon", lost_margin_bp: 10_000, cost_bp: 10_000, carbon_price_cents_per_t: 20_000, risk_aversion_bp: 2_500 },
];

const unitWeightSchema = z.object({ style_prefix: z.string(), grams: int }).loose();

/** The planning settings this section reads (anything else in `planning` is left untouched). */
export const planningScoringSchema = z
  .object({
    scoring: z
      .object({ default_profile: z.string().default("net-benefit"), profiles: z.array(weightProfileSchema).default([]) })
      .loose()
      .optional(),
    robustness: z.object({ enabled: z.boolean().default(true), samples: int.default(64) }).loose().optional(),
    emissions: z
      .object({
        enabled: z.boolean().default(true),
        factors_id: z.string().default("glec-v3-indicative"),
        unit_weights: z.array(unitWeightSchema).default([]),
        default_unit_weight_g: int.nullable().default(null),
      })
      .loose()
      .optional(),
    grams_per_unit: int.optional(),
  })
  .loose();

export type ScoringDraft = { defaultProfile: string; profiles: WeightProfile[]; samples: number };

export type EmissionsInfo = {
  enabled: boolean;
  factorsId: string;
  unitWeights: { stylePrefix: string; grams: number }[];
  defaultWeightG: number;
  robustnessEnabled: boolean;
};

function planningOf(spec: { planning?: unknown } | Record<string, unknown>): z.infer<typeof planningScoringSchema> {
  const raw = (spec as { planning?: unknown }).planning;
  const parsed = planningScoringSchema.safeParse(raw ?? {});
  return parsed.success ? parsed.data : {};
}

/** The editable scoring settings of a spec (the built-in profiles when the spec has none). */
export function scoringFromSpec(spec: { planning?: unknown } | Record<string, unknown>): ScoringDraft {
  const p = planningOf(spec);
  const profiles = p.scoring?.profiles?.length
    ? p.scoring.profiles.map((x) => ({
        id: x.id,
        label: x.label,
        lost_margin_bp: x.lost_margin_bp,
        cost_bp: x.cost_bp,
        carbon_price_cents_per_t: x.carbon_price_cents_per_t,
        risk_aversion_bp: x.risk_aversion_bp,
      }))
    : DEFAULT_PROFILES.map((x) => ({ ...x }));
  return { defaultProfile: p.scoring?.default_profile ?? "net-benefit", profiles, samples: p.robustness?.samples ?? 64 };
}

/** What the section shows read-only: the CO₂e assumptions (platform factor set, unit weights). */
export function emissionsInfo(spec: { planning?: unknown } | Record<string, unknown>): EmissionsInfo {
  const p = planningOf(spec);
  const e = p.emissions;
  return {
    enabled: e?.enabled ?? true,
    factorsId: e?.factors_id ?? "glec-v3-indicative",
    unitWeights: (e?.unit_weights ?? []).map((w) => ({ stylePrefix: w.style_prefix, grams: w.grams })),
    defaultWeightG: e?.default_unit_weight_g ?? p.grams_per_unit ?? 800,
    robustnessEnabled: p.robustness?.enabled ?? true,
  };
}

export type ScoringIssues = Record<string, string>;

function inRange(v: number, min: number, max: number): boolean {
  return Number.isInteger(v) && v >= min && v <= max;
}

/**
 * Client checks mirroring the engine (`ScoringConfig`, `WeightProfile`, `RobustnessDefaults`).
 * Keys: `default`, `profiles`, `samples` and `profiles.<i>.<field>`.
 */
export function validateScoring(d: ScoringDraft): ScoringIssues {
  const issues: ScoringIssues = {};
  if (d.profiles.length < 1) issues["profiles"] = "Keep at least one profile.";
  if (d.profiles.length > MAX_PROFILES) issues["profiles"] = `At most ${MAX_PROFILES} profiles.`;
  const seen = new Set<string>();
  d.profiles.forEach((p, i) => {
    if (!PROFILE_ID.test(p.id)) issues[`profiles.${i}.id`] = "Lower-case letters, digits and dashes, starting with a letter.";
    else if (seen.has(p.id)) issues[`profiles.${i}.id`] = "Another profile already uses this id.";
    seen.add(p.id);
    const label = p.label.trim();
    if (!label) issues[`profiles.${i}.label`] = "Give the profile a name.";
    else if (label.length > LABEL_MAX) issues[`profiles.${i}.label`] = `At most ${LABEL_MAX} characters.`;
    if (!inRange(p.lost_margin_bp, 0, WEIGHT_MAX_BP)) issues[`profiles.${i}.lost_margin_bp`] = "Between 0% and 500%.";
    if (!inRange(p.cost_bp, 0, WEIGHT_MAX_BP)) issues[`profiles.${i}.cost_bp`] = "Between 0% and 500%.";
    if (!inRange(p.carbon_price_cents_per_t, 0, CARBON_MAX_CENTS_PER_T)) {
      issues[`profiles.${i}.carbon_price_cents_per_t`] = "Between €0 and €10,000 per tonne.";
    }
    if (!inRange(p.risk_aversion_bp, 0, RISK_MAX_BP)) issues[`profiles.${i}.risk_aversion_bp`] = "Between 0% and 100%.";
  });
  if (!d.profiles.some((p) => p.id === d.defaultProfile)) issues["default"] = "Pick the default profile.";
  if (!inRange(d.samples, SAMPLES_MIN, SAMPLES_MAX)) issues["samples"] = `A whole number from ${SAMPLES_MIN} to ${SAMPLES_MAX}.`;
  return issues;
}

function sameProfiles(a: readonly WeightProfile[], b: readonly WeightProfile[]): boolean {
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * The merge patch that turns `base` into `draft`: only changed parts (`{}` when nothing changed).
 * Profiles are sent whole (arrays replace in a merge patch), with trimmed labels.
 */
export function scoringPatch(base: ScoringDraft, draft: ScoringDraft): Record<string, unknown> {
  const profiles = draft.profiles.map((p) => ({ ...p, label: p.label.trim() }));
  const planning: Record<string, unknown> = {};
  const scoring: Record<string, unknown> = {};
  if (!sameProfiles(base.profiles, profiles)) scoring["profiles"] = profiles;
  if (base.defaultProfile !== draft.defaultProfile) scoring["default_profile"] = draft.defaultProfile;
  if (Object.keys(scoring).length) planning["scoring"] = scoring;
  if (base.samples !== draft.samples) planning["robustness"] = { samples: draft.samples };
  return Object.keys(planning).length ? { planning } : {};
}

/** A fresh profile id (`custom-<n>`) not used yet. */
export function newProfileId(profiles: readonly WeightProfile[]): string {
  let n = 1;
  while (profiles.some((p) => p.id === `custom-${n}`)) n += 1;
  return `custom-${n}`;
}

/** Basis points ⇄ the percentages the form shows (10000 bp = 100%). */
export function bpToPercent(bp: number): string {
  return String(bp / 100);
}

export function percentToBp(text: string): number {
  const v = Number(text.replace(",", "."));
  return Number.isFinite(v) && text.trim() !== "" ? Math.round(v * 100) : Number.NaN;
}

/** Cents per tonne ⇄ euros per tonne. */
export function centsToEuros(cents: number): string {
  return String(cents / 100);
}

export function eurosToCents(text: string): number {
  const v = Number(text.replace(",", "."));
  return Number.isFinite(v) && text.trim() !== "" ? Math.round(v * 100) : Number.NaN;
}

/**
 * Server validation errors (`422 spec_invalid`, `errors[].loc` like
 * `planning.scoring.profiles.2.cost_bp`) mapped to this section's issue keys.
 */
export function serverIssues(errors: readonly { loc?: unknown; msg?: unknown }[]): ScoringIssues {
  const out: ScoringIssues = {};
  for (const e of errors) {
    const loc = String(e.loc ?? "");
    const msg = String(e.msg ?? "Invalid value.");
    const profile = /^planning\.scoring\.profiles\.(\d+)\.(\w+)/.exec(loc);
    if (profile) out[`profiles.${profile[1]}.${profile[2]}`] = msg;
    else if (loc.startsWith("planning.scoring.profiles")) out["profiles"] = msg;
    else if (loc.startsWith("planning.scoring")) out["default"] = msg;
    else if (loc.startsWith("planning.robustness")) out["samples"] = msg;
    else out["form"] = msg;
  }
  return out;
}
