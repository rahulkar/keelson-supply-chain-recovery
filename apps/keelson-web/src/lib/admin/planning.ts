/**
 * Planning solver settings of the agent spec (M8, `docs/contracts/125-m8-optimizer-cpsat.md` §7):
 * how the optimizer finds whole units (`planning.solver_integer_stage`) and the sourcing terms of
 * the alternate sources derived from the snapshot (`planning.alt_sourcing`). The engine omits each
 * setting from the spec while it has its default, so a missing key reads as that default.
 */
import type { AgentSpec } from "./types";

export type IntegerStage = "repair" | "cpsat";
export type MoqBasis = "po" | "style";

export const INTEGER_STAGE_OPTIONS: { value: IntegerStage; label: string }[] = [
  { value: "repair", label: "LP rounding (default)" },
  { value: "cpsat", label: "CP-SAT whole units" },
];

export const MOQ_BASIS_OPTIONS: { value: MoqBasis; label: string }[] = [
  { value: "po", label: "Per purchase order" },
  { value: "style", label: "Per style-colour" },
];

/** Engine defaults (`runtime/spec.py` `PlanningDefaults` / `AltSourcingDefaults`). */
export const PLANNING_DEFAULTS = {
  solver_integer_stage: "repair" as IntegerStage,
  moq_units: 300,
  order_multiple_units: 1,
  moq_basis: "po" as MoqBasis,
  share_factory_capacity: false,
};

export type AltSourcing = NonNullable<NonNullable<AgentSpec["planning"]>["alt_sourcing"]>;

export function altSourcing(spec: AgentSpec): AltSourcing {
  return spec.planning?.alt_sourcing ?? {};
}

export function integerStage(spec: AgentSpec): IntegerStage {
  return spec.planning?.solver_integer_stage ?? PLANNING_DEFAULTS.solver_integer_stage;
}

/** Settings the engine omits from the spec while they have their default (M8 fields). */
const OMITTED_AT_DEFAULT: Partial<Record<keyof AltSourcing, unknown>> = {
  order_multiple_units: PLANNING_DEFAULTS.order_multiple_units,
  moq_basis: PLANNING_DEFAULTS.moq_basis,
  share_factory_capacity: PLANNING_DEFAULTS.share_factory_capacity,
  max_orders_per_source: null,
};

/**
 * Set one `planning.alt_sourcing` setting on the draft (creating the objects when missing).
 * `undefined` removes the key (the merge patch sends `null`: back to the engine default); an M8
 * setting equal to its default is removed too, as the engine stores it, so toggling a setting
 * back is not an unsaved change.
 */
export function setAltSourcing<K extends keyof AltSourcing>(d: AgentSpec, key: K, value: AltSourcing[K] | undefined): void {
  const alt: AltSourcing = { ...(d.planning?.alt_sourcing ?? {}) };
  if (value === undefined || (key in OMITTED_AT_DEFAULT && OMITTED_AT_DEFAULT[key] === value)) delete alt[key];
  else alt[key] = value;
  d.planning = { ...(d.planning ?? {}), alt_sourcing: alt };
}

export function setIntegerStage(d: AgentSpec, stage: IntegerStage): void {
  const planning = { ...(d.planning ?? {}) };
  if (stage === PLANNING_DEFAULTS.solver_integer_stage) delete planning.solver_integer_stage;
  else planning.solver_integer_stage = stage;
  d.planning = planning;
}

/**
 * Part of `normalizeSpec`: an empty `planning.alt_sourcing` / `planning` object means "no
 * settings" (a setting changed and changed back on a spec without them is not an unsaved change).
 */
export function normalizePlanning(spec: AgentSpec): void {
  const planning = spec.planning;
  if (!planning) return;
  if (planning.alt_sourcing && Object.keys(planning.alt_sourcing).length === 0) delete planning.alt_sourcing;
  if (Object.keys(planning).length === 0) delete spec.planning;
}

/** "F-MA-01, F-TR-01 ,," → ["F-MA-01", "F-TR-01"] (trimmed, unique, in order; codes are case-sensitive). */
export function parseFactoryCodes(text: string): string[] {
  const out: string[] = [];
  for (const part of text.split(/[,\s]+/)) {
    const code = part.trim();
    if (code && !out.includes(code)) out.push(code);
  }
  return out;
}

/**
 * Factories the draft takes off the compliance block list: the planner could then buy from them,
 * so the engine treats it as a guarded change (a second admin approves; adding is never guarded).
 */
export function blockedFactoriesRemoved(base: AgentSpec, draft: AgentSpec): string[] {
  const now = new Set(altSourcing(draft).blocked_factories ?? []);
  return (altSourcing(base).blocked_factories ?? []).filter((f) => !now.has(f));
}

export function blockedFactoriesMessage(removed: readonly string[]): string {
  return `Compliance block: removing ${removed.join(", ")} lets the planner buy from ${removed.length === 1 ? "it" : "them"}`;
}

/** Client checks mirroring the engine's spec model (it validates again and has the last word). */
export function planningIssues(draft: AgentSpec): Record<string, string> {
  const issues: Record<string, string> = {};
  const alt = altSourcing(draft);
  const int = (key: string, v: unknown, min: number, max?: number) => {
    if (v === undefined || v === null) return;
    if (typeof v !== "number" || !Number.isInteger(v) || v < min || (max !== undefined && v > max)) {
      issues[key] =
        max === undefined
          ? `A whole number of at least ${min.toLocaleString("en-GB")}.`
          : `A whole number from ${min.toLocaleString("en-GB")} to ${max.toLocaleString("en-GB")}.`;
    }
  };
  int("planning.alt_sourcing.moq_units", alt.moq_units, 0);
  int("planning.alt_sourcing.order_multiple_units", alt.order_multiple_units, 1, 100_000);
  int("planning.alt_sourcing.max_orders_per_source", alt.max_orders_per_source, 1, 52);
  return issues;
}
