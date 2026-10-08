import { describe, expect, it } from "vitest";
import {
  CONFIRM_LIVE_TEXT,
  EVAL_CASE_STATUSES,
  EVAL_LIVE_MODES,
  EVAL_METRICS,
  EVAL_METRIC_META,
  EVAL_MODES,
  EVAL_RUN_MODES,
  EVAL_RUN_STATUSES,
  evalRunCreateProblem,
  evalsCaseResultSchema,
  evalsCompareSchema,
  evalsRunCreateSchema,
  evalsRunEventSchema,
  evalsRunSchema,
  evalsStatusSchema,
  evalsThresholdSchema,
  evalsTrendsSchema,
  isLiveMode,
} from "../src";

// Shapes mirror packages/contracts-py/tests/test_evals.py and the engine's answers (128 §3).
const RUN_ID = "0199c1a2-7b3c-7d4e-8f50-0123456789ab";
const T0 = "2026-10-08T09:00:00Z";
const STAT = { n: 0, p50_ms: null, max_ms: null };
const SUMMARY = {
  cases: 2, passed: 1, failed: 0, errors: 1, skipped: 0, cancelled: 0, checks: 12, checks_failed: 1, soft_failed: 1,
  pass_rate_bp: 5000, adversarial_cases: 1, adversarial_pass_rate_bp: 10000, entity_recall_mean_bp: 7500,
  numeric_grounding_mean_bp: 10000, numeric_grounding_min_bp: 10000, trajectory_recall_mean_bp: 6667,
  latency: { first_stage: { n: 2, p50_ms: 480, max_ms: 650 }, exposure: STAT, options: STAT, total: STAT },
  tokens_total: 42000, tokens_per_case_mean: 21000, tokens_source: "engine", cost_microusd_total: 12300,
  cost_per_case_mean_microusd: 6150, llm_calls_live: 4, llm_calls_replayed: 2, replay_misses: 1,
};

export const run = {
  id: RUN_ID, tenant: "demo-retail", source: "in_app", mode: "replay", status: "succeeded",
  case_ids: ["trim_delay_actual", "store_shortage_question"],
  requested_by: { sub: "sub-ava", username: "ava", name: "Ava Lindqvist" }, note: null, budget: null,
  counters: { tokens_live: 0, tokens_replayed: 81000, llm_calls_live: 0, llm_calls_replayed: 9, replay_misses: 1, cost_microusd: 0 },
  cancel_requested: false, suite_hash: "a".repeat(64), summary: SUMMARY,
  thresholds: [{ metric: "pass_rate_bp", op: "min", value: 8000, modes: ["replay", "record_or_replay", "record", "unknown"] }],
  threshold_results: [{ metric: "pass_rate_bp", op: "min", value: 8000, actual: 5000, outcome: "breach" }],
  breaches: 1, doc_schema: null, file_name: null, target: null, error: null,
  created_at: T0, started_at: T0, finished_at: "2026-10-08T09:04:00Z",
  progress: { cases_done: 2, cases_total: 2, current_case: null, current_turn: null },
};

export const caseResult = {
  case_id: "trim_delay_actual", title: "Trim supplier delay (actual)", tags: ["golden"], status: "fail",
  keelson_case_id: "01a1158f-c62f-739e-9de2-30ed866c613f", error: null, error_code: null, duration_ms: 231400,
  turns: [{
    id: "report", kind: "message", run_ids: ["01a1158f-c677-74c5-a72e-edfe1c28b344"],
    timings: { first_stage_ms: 650, exposure_ms: 20970, options_ms: 226000, total_ms: 226210 },
    budgets: { first_stage_ms: 2000, exposure_ms: 30000, options_ms: 180000, total_ms: null },
    plan_version: 1, skipped: null,
    checks: [{ name: "report.trajectory", ok: false, soft: true, detail: "missing ['playbook:*']" }],
    entity_recall_bp: 7500, numeric_grounding_bp: 10000, numeric_grounding_min_bp: 10000, narrative_numbers: 2,
    trajectory_recall_bp: 6667, tool_calls: 3, agents_started: ["sourcing"],
    trajectory: {
      required: ["sourcing:*", "playbook:*"], matched: ["sourcing:*"], missing: ["playbook:*"], forbidden: ["*:apply_*"],
      forbidden_hits: [], calls: [{ agent: "sourcing", tool: "list_purchase_orders", n: 2 }], total_calls: 2,
      max_calls: null, recall_bp: 5000, min_recall_bp: 10000,
    },
    entities: { golden: ["UK1", "NL1"], missing: ["NL1"], recall_bp: 5000, min_recall_bp: 8000 },
    usage: { runs: 1, total: { calls: 3, replayed: 1, prompt_tokens: 41600, completion_tokens: 400, reasoning_tokens: 120, total_tokens: 42000, latency_ms: 5400, cost_microusd: 12300 }, agents: {} },
    final_code: null, extra: { unverified_note: false, options: ["opt-A", "do-nothing"] },
  }],
  checks: [], entity_recall_bp: 7500, numeric_grounding_bp: 10000, trajectory_recall_bp: 6667, guard_flagged: null,
  tool_calls: 3, usage: null, tokens_gateway: 400, engine: null,
};

describe("M8 eval vocabularies", () => {
  it("derives the enums from the generated schemas", () => {
    expect(EVAL_MODES).toEqual(["replay", "record_or_replay", "record"]);
    expect(EVAL_RUN_MODES).toEqual(["replay", "record_or_replay", "record", "unknown"]);
    expect(EVAL_RUN_STATUSES).toEqual(["queued", "running", "succeeded", "failed", "cancelled"]);
    expect(EVAL_CASE_STATUSES).toEqual(["pass", "fail", "error", "skipped", "cancelled"]);
    expect([...EVAL_METRICS].sort()).toEqual(Object.keys(EVAL_METRIC_META).sort());
    expect(EVAL_LIVE_MODES.every((m) => EVAL_MODES.includes(m))).toBe(true);
    expect(isLiveMode("replay")).toBe(false);
    expect(isLiveMode("record")).toBe(true);
  });

  it("mirrors the engine's live-run request rules", () => {
    const caps = { max_tokens: 3_000_000, max_cost_microusd: 5_000_000 };
    const budget = { max_tokens: 100_000, max_cost_microusd: 200_000 };
    expect(evalRunCreateProblem({ mode: "replay", confirm_live: null, budget: null }, caps)).toBeNull();
    expect(evalRunCreateProblem({ mode: "record_or_replay", confirm_live: "run live", budget }, caps)).toBe("confirm_required");
    expect(evalRunCreateProblem({ mode: "record", confirm_live: CONFIRM_LIVE_TEXT, budget: null }, caps)).toBe("budget_required");
    expect(evalRunCreateProblem({ mode: "record", confirm_live: CONFIRM_LIVE_TEXT, budget: { max_tokens: 3_000_001, max_cost_microusd: 1 } }, caps)).toBe("budget_too_high");
    expect(evalRunCreateProblem({ mode: "record", confirm_live: CONFIRM_LIVE_TEXT, budget }, caps)).toBeNull();
  });
});

describe("M8 eval schemas", () => {
  it("parse the engine's shapes and stay strict", () => {
    expect(evalsRunSchema.parse(run).summary?.pass_rate_bp).toBe(5000);
    expect(evalsRunSchema.safeParse({ ...run, surprise: 1 }).success).toBe(false);
    const parsed = evalsCaseResultSchema.parse(caseResult);
    expect(parsed.turns[0]!.trajectory?.missing).toEqual(["playbook:*"]);
    expect(evalsCaseResultSchema.safeParse({ ...caseResult, duration_ms: 1.5 }).success).toBe(false);
    expect(evalsCaseResultSchema.safeParse({ ...caseResult, status: "passed" }).success).toBe(false);
    expect(evalsRunEventSchema.safeParse({ seq: 1, type: "turn_started", case_id: "trim_delay_actual", turn_id: "report", data: { kind: "message" }, at: T0 }).success).toBe(true);
    expect(evalsRunEventSchema.safeParse({ seq: 0, type: "turn_started", case_id: null, turn_id: null, data: {}, at: T0 }).success).toBe(false);
    expect(evalsThresholdSchema.safeParse({ metric: "nope", op: "min", value: 1, modes: ["replay"] }).success).toBe(false);
  });

  it("run create: replay needs the Merxa reset acknowledgement", () => {
    expect(evalsRunCreateSchema.parse({ acknowledge_reset: true }).mode).toBe("replay");
    expect(evalsRunCreateSchema.safeParse({ acknowledge_reset: false }).success).toBe(false);
    expect(evalsRunCreateSchema.safeParse({ acknowledge_reset: true, case_ids: [] }).success).toBe(false);
  });

  it("status, compare and trends", () => {
    const status = {
      tenant: "demo-retail",
      runner: { configured: true, online: false, runner_id: null, version: null, last_seen_at: null, suite_hash: null },
      suite: null,
      modes: [
        { mode: "replay", available: false, reason: "runner_offline", message: "The eval runner is offline." },
        { mode: "record_or_replay", available: false, reason: "live_disabled", message: null },
        { mode: "record", available: false, reason: "live_disabled", message: null },
      ],
      process_replay_mode: "record_or_replay", live_caps: { max_tokens: 3_000_000, max_cost_microusd: 5_000_000 },
      active_run: null, recordings: { rows: 0, bytes: 0, newest_at: null, agents: {} },
    };
    expect(evalsStatusSchema.parse(status).modes[0]!.reason).toBe("runner_offline");
    const delta = { base: 5000, head: 10000, delta: 5000, better: true };
    const compare = {
      base: run, head: { ...run, id: "0199c1a2-7b3c-7d4e-8f50-0123456789ac" }, summary_deltas: { pass_rate_bp: delta },
      cases: [{ case_id: "adversarial_kb_doc", title: "KB", base_status: "fail", head_status: "pass", change: "fixed", metric_deltas: {}, checks_newly_failed: [], checks_newly_passed: ["case.guard_flagged"] }],
    };
    expect(evalsCompareSchema.parse(compare).cases[0]!.change).toBe("fixed");
    const trends = {
      runs: [{ id: RUN_ID, at: T0, mode: "unknown", source: "import", status: "succeeded", metrics: { pass_rate_bp: 0, options_max_ms: null }, breaches: 3, tokens_source: "gateway" }],
      cases: [{ case_id: "adversarial_kb_doc", title: "KB", statuses: ["fail"] }],
      thresholds: [],
    };
    expect(evalsTrendsSchema.parse(trends).runs[0]!.metrics["options_max_ms"]).toBeNull();
  });
});
