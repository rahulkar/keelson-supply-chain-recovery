/**
 * M8 Evals UI (`docs/contracts/128`): in-app eval runs, run history, per-case drill-down, compare
 * and trends under `/api/admin/evals/*` (platform admins only).
 *
 * Engine answers are parsed with the generated contract schemas (`evals*Schema`; unknown top-level
 * fields stripped so an additive engine field never breaks the page). Ratios are basis points,
 * latencies milliseconds, cost integer micro-USD: this module turns them into text. Everything an
 * eval reports may quote model output (check details, error texts): render it as plain text only.
 */
import {
  CONFIRM_LIVE_TEXT,
  EVAL_METRIC_META,
  evalRunCreateProblem,
  evalsCaseResultSchema,
  evalsCompareSchema,
  evalsRunDetailSchema,
  evalsRunEventSchema,
  evalsRunSchema,
  evalsStatusSchema,
  evalsTrendsSchema,
  isLiveMode,
  type EvalCaseStatus,
  type EvalMetric,
  type EvalMode,
  type EvalRunMode,
  type EvalRunStatus,
  type EvalsCaseResult,
  type EvalsRun,
  type EvalsRunCreate,
  type EvalsRunDetail,
  type EvalsStatus,
  type EvalsThreshold,
  type EvalsTrends,
} from "@keelson/contracts";
import type { BadgeTone } from "@keelson/ui";
import type { TrendPoint } from "@keelson/viz";
import { z } from "zod";

/** Progress polling while a run is queued or running. */
export const EVALS_POLL_MS = 2000;
/** The engine's default `EVALS_MAX_IMPORT_BYTES`; the server stays authoritative. */
export const EVALS_IMPORT_MAX_BYTES = 8 * 1024 * 1024;
/** How the runner is started (the status card shows it when the runner is offline). */
export const RUNNER_START_HINT = "docker compose --profile evals up -d evals-runner";

// ───────────────────────────── schemas ─────────────────────────────

export const evalStatusSchema = evalsStatusSchema.strip();
export const evalRunSchema = evalsRunSchema.strip();
export const evalRunDetailSchema = evalsRunDetailSchema.strip();
export const evalCaseResultSchema = evalsCaseResultSchema.strip();
export const evalCompareSchema = evalsCompareSchema.strip();
export const evalTrendsSchema = evalsTrendsSchema.strip();

export const evalRunListSchema = z.object({ items: z.array(evalRunSchema), next_before: z.string().nullable() }).loose();
export type EvalRunList = z.infer<typeof evalRunListSchema>;

export const evalEventsSchema = z
  .object({
    items: z.array(evalsRunEventSchema.strip()),
    next_after: z.number().int().nonnegative(),
    status: evalsRunSchema.shape.status,
    progress: evalsRunSchema.shape.progress,
    counters: evalsRunSchema.shape.counters,
  })
  .loose();
export type EvalEvents = z.infer<typeof evalEventsSchema>;

export const importAnswerSchema = z.object({ run_id: z.string(), duplicate: z.boolean() }).loose();
export type ImportAnswer = z.infer<typeof importAnswerSchema>;

export const folderImportSchema = z
  .object({
    imported: z.array(z.object({ file: z.string(), run_id: z.string() }).loose()),
    duplicates: z.number().int().nonnegative(),
    errors: z.array(z.object({ file: z.string(), error: z.string() }).loose()),
  })
  .loose();
export type FolderImport = z.infer<typeof folderImportSchema>;

const fileSchema = z
  .object({
    run: z.string().nullable().optional(),
    file: z.string(),
    error: z.string().nullable().optional(),
    schema: z.string().nullable().optional(),
    started_at: z.string().nullable().optional(),
    summary: z.record(z.string(), z.unknown()).catch({}).default({}),
    cases: z.array(z.unknown()).catch([]).default([]),
    imported_run_id: z.string().nullable().optional(),
  })
  .loose();
export const evalFilesSchema = z
  .object({ items: z.array(fileSchema).catch([]).default([]), directory_available: z.boolean().default(true) })
  .loose();
export type EvalFile = z.infer<typeof fileSchema>;
export type EvalFiles = z.infer<typeof evalFilesSchema>;

export type EvalStatusData = EvalsStatus;
export type EvalRun = EvalsRun;
export type EvalRunDetail = EvalsRunDetail;
export type EvalCaseRow = EvalsRunDetail["cases"][number];
export type EvalCaseResult = EvalsCaseResult;
export type EvalTurn = EvalsCaseResult["turns"][number];
export type EvalTrendsData = EvalsTrends;

// ───────────────────────────── vocabulary ─────────────────────────────

type Meta = { label: string; tone: BadgeTone };

export const RUN_STATUS: Record<EvalRunStatus, Meta> = {
  queued: { label: "Queued", tone: "info" },
  running: { label: "Running", tone: "accent" },
  succeeded: { label: "Finished", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export const CASE_STATUS: Record<EvalCaseStatus, Meta> = {
  pass: { label: "Pass", tone: "success" },
  fail: { label: "Fail", tone: "danger" },
  error: { label: "Error", tone: "warning" },
  skipped: { label: "Skipped", tone: "neutral" },
  cancelled: { label: "Cancelled", tone: "neutral" },
};

export const MODE_META: Record<EvalRunMode, Meta & { description: string }> = {
  replay: { label: "Replay", tone: "neutral", description: "Recorded model answers only. A request without a recording stops the case; nothing calls the model provider." },
  record_or_replay: { label: "Live · record or replay", tone: "warning", description: "Recorded answers where they exist, the model provider for the rest (recorded for next time). Costs money." },
  record: { label: "Live · record", tone: "warning", description: "Every request goes to the model provider and is recorded again. Costs the most." },
  unknown: { label: "Unknown mode", tone: "neutral", description: "An imported command-line run that does not say how the engine answered." },
};

export const CHANGE_META: Record<string, Meta> = {
  fixed: { label: "Fixed", tone: "success" },
  regressed: { label: "Regressed", tone: "danger" },
  changed: { label: "Changed", tone: "warning" },
  unchanged: { label: "Same", tone: "neutral" },
  new: { label: "New", tone: "info" },
  removed: { label: "Removed", tone: "neutral" },
};

export const OUTCOME_META: Record<string, Meta> = {
  ok: { label: "Met", tone: "success" },
  breach: { label: "Missed", tone: "danger" },
  not_measured: { label: "Not measured", tone: "neutral" },
  not_applicable: { label: "Not for this mode", tone: "neutral" },
};

const ERROR_CODES: Record<string, string> = {
  replay_miss: "A model request had no recording (replay miss)",
  budget_exhausted: "The run's budget was used up",
  cancelled: "Cancelled",
  needs_live_embeddings: "Needs live embeddings (not replayed)",
};

/** A case's `error_code` in words (unknown codes as they are). */
export function errorCodeText(code: string | null | undefined): string | null {
  if (!code) return null;
  return ERROR_CODES[code] ?? code.replace(/_/g, " ");
}

/** The checks of the adversarial outcomes matrix (case-level safety checks). */
export const ADVERSARIAL_CHECKS = [
  { key: "guard_flagged", label: "Flagged by the guard" },
  { key: "no_write_tool_calls", label: "No write tool called" },
  { key: "no_write_tools_bound", label: "No write tool bound" },
  { key: "no_untrusted_tool_calls", label: "No raw untrusted tool" },
  { key: "no_commit_without_human", label: "No commit without a human" },
  { key: "narrative_excludes", label: "Injected text kept out" },
  { key: "narrative_code_limit", label: "Codes within limits" },
] as const;

export function isActive(status: EvalRunStatus): boolean {
  return status === "queued" || status === "running";
}

export function isAdversarial(tags: readonly string[]): boolean {
  return tags.includes("adversarial");
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
export function isUuid(value: string | null | undefined): value is string {
  return !!value && UUID.test(value);
}

// ───────────────────────────── numbers as text ─────────────────────────────

const intFmt = new Intl.NumberFormat("en-GB");

export function bpText(bp: number | null | undefined): string {
  if (typeof bp !== "number") return "—";
  const pct = bp / 100;
  return `${Number.isInteger(pct) ? pct.toFixed(0) : pct.toFixed(1)} %`;
}

export function msText(ms: number | null | undefined): string {
  if (typeof ms !== "number") return "—";
  if (ms < 1000) return `${ms} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)} s`;
  const min = Math.floor(ms / 60_000);
  const s = Math.round((ms % 60_000) / 1000);
  return s ? `${min} min ${s} s` : `${min} min`;
}

export function tokensText(n: number | null | undefined): string {
  return typeof n === "number" ? intFmt.format(n) : "—";
}

/** Integer micro-USD as dollars ("$0.0123", "$12.40"). */
export function usdText(microusd: number | null | undefined): string {
  if (typeof microusd !== "number") return "—";
  const usd = microusd / 1_000_000;
  return `$${usd >= 1 ? usd.toFixed(2) : usd.toFixed(4)}`;
}

export function metricText(metric: EvalMetric, value: number | null | undefined): string {
  switch (EVAL_METRIC_META[metric].unit) {
    case "bp":
      return bpText(value);
    case "ms":
      return msText(value);
    case "microusd":
      return usdText(value);
    default:
      return tokensText(value);
  }
}

/** "≥ 80 %", "≤ 3 min" */
export function thresholdText(t: Pick<EvalsThreshold, "metric" | "op" | "value">): string {
  return `${t.op === "min" ? "≥" : "≤"} ${metricText(t.metric, t.value)}`;
}

/** A metric delta as signed text in the metric's unit ("+50 %", "−1.2 s"). */
export function deltaText(metric: EvalMetric | string, delta: number | null | undefined): string {
  if (typeof delta !== "number") return "—";
  if (delta === 0) return "No change";
  const sign = delta > 0 ? "+" : "−";
  const abs = Math.abs(delta);
  const meta = (EVAL_METRIC_META as Record<string, { unit: string } | undefined>)[metric];
  const unit = meta?.unit ?? (metric.endsWith("_bp") ? "bp" : metric.endsWith("_ms") ? "ms" : "tokens");
  const text = unit === "bp" ? bpText(abs) : unit === "ms" ? msText(abs) : unit === "microusd" ? usdText(abs) : tokensText(abs);
  return `${sign}${text}`;
}

// ───────────────────────────── runs ─────────────────────────────

export function progressText(run: Pick<EvalRun, "progress" | "status">): string {
  const p = run.progress;
  const base = `${p.cases_done} of ${p.cases_total} case${p.cases_total === 1 ? "" : "s"}`;
  if (run.status !== "running" || !p.current_case) return base;
  return `${base} · now ${p.current_case}${p.current_turn ? ` (turn ${p.current_turn})` : ""}`;
}

/** "3 / 4 passed" from a run's summary (`—` before it finished). */
export function passedText(run: Pick<EvalRun, "summary">): string {
  const s = run.summary;
  return s ? `${s.passed} / ${s.cases}` : "—";
}

/** Cost per case of the latest finished live run (the new-run dialog's estimate base). */
export function lastLiveCostPerCase(runs: readonly EvalRun[]): number | null {
  const live = runs.find((r) => isLiveMode(r.mode) && r.summary?.cost_per_case_mean_microusd != null);
  return live?.summary?.cost_per_case_mean_microusd ?? null;
}

// ───────────────────────────── new run ─────────────────────────────

export type NewRunDraft = {
  mode: EvalMode;
  caseIds: string[];
  acknowledged: boolean;
  confirm: string;
  maxTokens: string;
  /** Dollars as typed ("2.50"); converted to integer micro-USD. */
  maxCostUsd: string;
  note: string;
};

/** Typed dollars → integer micro-USD (`null` = not a positive amount). */
export function usdToMicro(text: string): number | null {
  const t = text.trim().replace(/^\$/, "");
  if (!/^\d+(\.\d{1,6})?$/.test(t)) return null;
  const [whole = "0", frac = ""] = t.split(".");
  const micro = Number(whole) * 1_000_000 + Number(frac.padEnd(6, "0"));
  return Number.isSafeInteger(micro) && micro > 0 ? micro : null;
}

/** The request body of a draft; `problem` = why it cannot be sent yet (button stays disabled). */
export function newRunRequest(
  draft: NewRunDraft,
  caps: { max_tokens: number; max_cost_microusd: number },
  allCaseIds: readonly string[],
): { body: EvalsRunCreate | null; problem: string | null } {
  if (draft.caseIds.length === 0) return { body: null, problem: "Choose at least one case." };
  if (!draft.acknowledged) return { body: null, problem: "Acknowledge that each case resets Merxa's demo data." };
  const live = isLiveMode(draft.mode);
  const tokens = /^\d+$/.test(draft.maxTokens.trim()) ? Number(draft.maxTokens.trim()) : null;
  const cost = usdToMicro(draft.maxCostUsd);
  const budget = live && tokens && tokens > 0 && cost ? { max_tokens: tokens, max_cost_microusd: cost } : null;
  const body: EvalsRunCreate = {
    mode: draft.mode,
    acknowledge_reset: true,
    case_ids: draft.caseIds.length === allCaseIds.length && allCaseIds.every((c) => draft.caseIds.includes(c)) ? null : [...draft.caseIds],
    confirm_live: live ? draft.confirm : null,
    budget,
    note: draft.note.trim() ? draft.note.trim().slice(0, 200) : null,
  };
  if (live && (tokens === null || tokens <= 0 || cost === null)) return { body: null, problem: "Set a token and a cost budget." };
  const problem = evalRunCreateProblem(body, caps);
  if (problem === "confirm_required") return { body: null, problem: `Type ${CONFIRM_LIVE_TEXT} to confirm a live run.` };
  if (problem === "budget_too_high") return { body: null, problem: `The budget is above this installation's caps (${tokensText(caps.max_tokens)} tokens, ${usdText(caps.max_cost_microusd)}).` };
  if (problem === "budget_required") return { body: null, problem: "Set a token and a cost budget." };
  return { body, problem: null };
}

// ───────────────────────────── import ─────────────────────────────

/**
 * Early checks of a results file before upload (the engine validates it fully): JSON, an object
 * with a `keelson-evals/1|2` schema and 1–100 cases.
 */
export function parseResultsFile(text: string): { doc: Record<string, unknown> } | { error: string } {
  let doc: unknown;
  try {
    doc = JSON.parse(text);
  } catch {
    return { error: "The file is not JSON." };
  }
  if (!doc || typeof doc !== "object" || Array.isArray(doc)) return { error: "The file is not a results document." };
  const o = doc as Record<string, unknown>;
  if (o["schema"] !== "keelson-evals/1" && o["schema"] !== "keelson-evals/2") {
    return { error: "Not an eval results file (schema keelson-evals/1 or /2 expected)." };
  }
  const cases = o["cases"];
  if (!Array.isArray(cases) || cases.length === 0) return { error: "The file has no cases." };
  if (cases.length > 100) return { error: "The file has more than 100 cases." };
  return { doc: o };
}

// ───────────────────────────── drill-down ─────────────────────────────

export type CheckCell = "ok" | "fail" | "soft" | "none";

/** One adversarial case's case-level safety checks (`case.<check>`). */
export function adversarialCells(result: EvalCaseResult): Record<(typeof ADVERSARIAL_CHECKS)[number]["key"], CheckCell> {
  const byName = new Map(result.checks.map((c) => [c.name, c]));
  const out = {} as Record<(typeof ADVERSARIAL_CHECKS)[number]["key"], CheckCell>;
  for (const { key } of ADVERSARIAL_CHECKS) {
    const c = byName.get(`case.${key}`);
    out[key] = !c ? "none" : c.ok ? "ok" : c.soft ? "soft" : "fail";
  }
  return out;
}

/** Checks of a turn (or of the case), failed hard checks first, then soft failures, then passes. */
export function orderedChecks<T extends { ok: boolean; soft?: boolean | undefined }>(checks: readonly T[]): T[] {
  const rank = (c: T) => (c.ok ? 2 : c.soft ? 1 : 0);
  return [...checks].sort((a, b) => rank(a) - rank(b));
}

export type LatencyRow = { stage: string; label: string; ms: number | null; budget: number | null };

const STAGES = [
  ["first_stage", "First stage"],
  ["exposure", "Exposure"],
  ["options", "Options"],
  ["total", "Turn total"],
] as const;

export function latencyRows(turn: EvalTurn): LatencyRow[] {
  return STAGES.map(([stage, label]) => ({
    stage,
    label,
    ms: turn.timings?.[`${stage}_ms`] ?? null,
    budget: turn.budgets?.[`${stage}_ms`] ?? null,
  })).filter((r) => r.ms !== null || r.budget !== null);
}

export type UsageRow = { agent: string; calls: number; replayed: number; prompt: number; completion: number; reasoning: number; total: number; latencyMs: number; cost: number | null };

export function usageRows(usage: EvalCaseResult["usage"]): UsageRow[] {
  if (!usage) return [];
  return Object.entries(usage.agents ?? {})
    .map(([agent, r]) => ({
      agent,
      calls: r.calls ?? 0,
      replayed: r.replayed ?? 0,
      prompt: r.prompt_tokens ?? 0,
      completion: r.completion_tokens ?? 0,
      reasoning: r.reasoning_tokens ?? 0,
      total: r.total_tokens ?? 0,
      latencyMs: r.latency_ms ?? 0,
      cost: r.cost_microusd ?? null,
    }))
    .sort((a, b) => b.total - a.total || a.agent.localeCompare(b.agent));
}

// ───────────────────────────── trends ─────────────────────────────

const shortWhen = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false, timeZone: "UTC" });

/** "7 Oct, 08:08" (UTC) for chart axes. */
export function runLabel(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : shortWhen.format(d);
}

export const TREND_METRICS: readonly EvalMetric[] = [
  "pass_rate_bp",
  "entity_recall_mean_bp",
  "numeric_grounding_min_bp",
  "trajectory_recall_mean_bp",
  "first_stage_p50_ms",
  "options_max_ms",
  "tokens_per_case_mean",
  "cost_per_case_mean_microusd",
];

/** One metric over the trend's runs + the target to draw (when it applies to the shown modes). */
export function trendSeries(
  trends: EvalTrendsData,
  metric: EvalMetric,
  tab: "all" | "replay" | "live",
): { points: TrendPoint[]; threshold: { value: number; label: string } | undefined } {
  const points = trends.runs.map((r) => ({ label: runLabel(r.at), value: r.metrics[metric] ?? null }));
  const t = trends.thresholds.find((x) => x.metric === metric);
  const applies =
    t !== undefined && (tab === "replay" ? t.modes.includes("replay") : tab === "live" ? t.modes.some((m) => m !== "replay") : true);
  return { points, threshold: t && applies ? { value: t.value, label: thresholdText(t) } : undefined };
}
