/**
 * M8 Evals UI vocabularies and small helpers (docs/contracts/128; contracts-py `keelson_contracts.evals`).
 * Schemas are generated (`evals*Schema` in ./generated); this module derives friendly names and mirrors
 * the pure rules the admin UI shows before the engine answers (the engine stays authoritative).
 */
import {
  evalsCaseResultSchema,
  evalsRunSchema,
  evalsStatusSchema,
  evalsThresholdSchema,
  type EvalsRunCreate,
} from "./generated";

/** `replay` (recorded answers only, no provider call) | `record_or_replay` | `record`. */
export const EVAL_MODES = evalsStatusSchema.shape.modes.element.shape.mode.options;
export type EvalMode = (typeof EVAL_MODES)[number];

/** The modes that call the model provider (platform admins, typed confirmation, budget). */
export const EVAL_LIVE_MODES = ["record_or_replay", "record"] as const satisfies readonly EvalMode[];

/** A run's mode; imported CLI files that do not say how they ran are `unknown`. */
export const EVAL_RUN_MODES = evalsRunSchema.shape.mode.options;
export type EvalRunMode = (typeof EVAL_RUN_MODES)[number];

/** `queued` | `running` | `succeeded` | `failed` | `cancelled`. */
export const EVAL_RUN_STATUSES = evalsRunSchema.shape.status.options;
export type EvalRunStatus = (typeof EVAL_RUN_STATUSES)[number];

/** `pass` | `fail` | `error` | `skipped` | `cancelled`. */
export const EVAL_CASE_STATUSES = evalsCaseResultSchema.shape.status.options;
export type EvalCaseStatus = (typeof EVAL_CASE_STATUSES)[number];

/** Run metrics that carry targets (thresholds). */
export const EVAL_METRICS = evalsThresholdSchema.shape.metric.options;
export type EvalMetric = (typeof EVAL_METRICS)[number];

export type EvalMetricUnit = "bp" | "ms" | "tokens" | "microusd";

/** Label, unit and the better direction (`min`: higher is better) of each metric (contracts-py `METRIC_OPS`). */
export const EVAL_METRIC_META = {
  pass_rate_bp: { label: "Pass rate", unit: "bp", op: "min" },
  adversarial_pass_rate_bp: { label: "Adversarial pass rate", unit: "bp", op: "min" },
  entity_recall_mean_bp: { label: "Entity recall (mean)", unit: "bp", op: "min" },
  numeric_grounding_min_bp: { label: "Numeric grounding (min)", unit: "bp", op: "min" },
  trajectory_recall_mean_bp: { label: "Trajectory recall (mean)", unit: "bp", op: "min" },
  first_stage_p50_ms: { label: "First stage (p50)", unit: "ms", op: "max" },
  exposure_max_ms: { label: "Exposure (max)", unit: "ms", op: "max" },
  options_max_ms: { label: "Options (max)", unit: "ms", op: "max" },
  tokens_per_case_mean: { label: "Tokens per case", unit: "tokens", op: "max" },
  cost_per_case_mean_microusd: { label: "Cost per case", unit: "microusd", op: "max" },
} as const satisfies Record<EvalMetric, { label: string; unit: EvalMetricUnit; op: "min" | "max" }>;

/** What an admin types to start a live run. */
export const CONFIRM_LIVE_TEXT = "RUN LIVE";

/** `RUN_ERROR` codes that end an eval case early → the case's `error_code` (contracts-py `EVAL_STOP_CODES`). */
export const EVAL_STOP_CODES = {
  LLM_REPLAY_MISS: "replay_miss",
  EVAL_BUDGET_EXHAUSTED: "budget_exhausted",
  BUDGET_EXCEEDED: "budget_exhausted",
  EVAL_CANCELLED: "cancelled",
} as const;

export function isLiveMode(mode: string): mode is (typeof EVAL_LIVE_MODES)[number] {
  return (EVAL_LIVE_MODES as readonly string[]).includes(mode);
}

/**
 * Mirror of contracts-py `eval_run_create_error`: the API's 422 code for a live request without its
 * typed confirmation or budget, or above the installation's caps (`null` = fine).
 */
export function evalRunCreateProblem(
  req: Pick<EvalsRunCreate, "mode" | "confirm_live" | "budget">,
  caps: { max_tokens: number; max_cost_microusd: number },
): "confirm_required" | "budget_required" | "budget_too_high" | null {
  if (!isLiveMode(req.mode ?? "replay")) return null;
  if (req.confirm_live !== CONFIRM_LIVE_TEXT) return "confirm_required";
  if (!req.budget) return "budget_required";
  if (req.budget.max_tokens > caps.max_tokens || req.budget.max_cost_microusd > caps.max_cost_microusd) return "budget_too_high";
  return null;
}
