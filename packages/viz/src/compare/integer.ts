import type { CompareOption } from "@keelson/contracts";

/** `CompareOption.integer` (M8, `125-m8-optimizer-cpsat.md`): how an option's whole units were found. */
export type CompareInteger = NonNullable<CompareOption["integer"]>;
export type IntegerFallbackReason = NonNullable<CompareInteger["fallback_reason"]>;

/** Plain-language reasons for an LP-rounding fallback (the CP-SAT stage ran but was not used). */
export const INTEGER_FALLBACK_TEXT: Record<IntegerFallbackReason, string> = {
  no_candidates: "nothing to re-source",
  no_improvement: "found nothing better",
  simulator_not_better: "its units simulated worse",
  model_too_large: "fallback: model too large",
  objective_overflow: "fallback: numbers too large",
  time_limit_no_solution: "fallback: time limit",
  infeasible: "fallback: no feasible plan",
  model_invalid: "fallback: invalid model",
  wall_clock_guard: "fallback: time guard",
  budget_exhausted: "fallback: solver budget used",
  solver_error: "fallback: solver error",
};

/** Basis points as a percentage with one decimal ("0.4 %"; under 0.1 % reads "< 0.1 %"). */
function gapText(bp: number): string {
  if (bp > 0 && bp < 10) return "< 0.1 %";
  return `${(bp / 100).toLocaleString("en-GB", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} %`;
}

/**
 * The muted caption under an option's levers in the scorecard: "Whole units: CP-SAT, proven
 * optimal", "Whole units: CP-SAT, best found (gap 0.4 %)" or "Whole units: rounded from the LP
 * (CP-SAT fallback: time limit)". `null` for options without the note (``integer_stage="repair"``).
 */
export function integerNote(integer: CompareOption["integer"]): string | null {
  if (!integer) return null;
  switch (integer.method) {
    case "cpsat_optimal":
      return "Whole units: CP-SAT, proven optimal";
    case "cpsat_feasible":
      return integer.gap_bp != null && integer.gap_bp > 0
        ? `Whole units: CP-SAT, best found (gap ${gapText(integer.gap_bp)})`
        : "Whole units: CP-SAT, best found";
    case "lp_rounding":
      return integer.fallback_reason
        ? `Whole units: rounded from the LP (CP-SAT ${INTEGER_FALLBACK_TEXT[integer.fallback_reason]})`
        : "Whole units: rounded from the LP";
    default:
      return null;
  }
}
