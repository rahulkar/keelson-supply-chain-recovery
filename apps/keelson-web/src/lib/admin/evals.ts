/**
 * Evals results (`docs/contracts/110` §6): `GET /api/admin/evals` (platform admins) lists the
 * latest `evals/results/<UTC stamp>.json` files (schema `keelson-evals/1`, written by
 * `evals/run_live.py`; mounted read-only) with per-case scores. M8 (`docs/contracts/128`): the Evals page
 * shows this listing as its "Results folder" (`GET /api/admin/evals/files` adds `imported_run_id`);
 * runs, history and drill-down live in `./evalRuns.ts`.
 *
 * The engine answers `{items: [{run, file, error, schema, started_at, finished_at, summary,
 * cases: [{id, title, tags, status, duration_s, checks, checks_failed, scores}]}],
 * directory_available}` where `checks` / `checks_failed` are counts. A raw results file (cases
 * with `checks: [{name, ok, soft}]` and `metrics`) is read too.
 */
import { z } from "zod";

const checkSchema = z
  .object({ name: z.string(), ok: z.boolean(), detail: z.string().nullable().optional(), soft: z.boolean().optional() })
  .loose();
export type EvalCheck = z.infer<typeof checkSchema>;

const num = z.number().finite();

const caseSchema = z
  .object({
    id: z.string(),
    title: z.string().nullable().optional(),
    tags: z.array(z.string()).catch([]).default([]),
    status: z.string().catch("unknown").default("unknown"),
    error: z.string().nullable().optional(),
    duration_s: num.nullable().optional().catch(null),
    /** A count (engine summary) or the raw check list (results file). */
    checks: z.union([z.number().int().nonnegative(), z.array(checkSchema)]).catch([]).default([]),
    checks_failed: z.number().int().nonnegative().nullable().optional().catch(null),
    turns: z
      .array(z.object({ id: z.string().optional(), checks: z.array(checkSchema).catch([]).default([]) }).loose())
      .catch([])
      .default([]),
    scores: z.record(z.string(), z.unknown()).catch({}).default({}),
    metrics: z.record(z.string(), z.unknown()).catch({}).default({}),
  })
  .loose()
  .transform((c) => {
    const list = Array.isArray(c.checks) ? [...c.turns.flatMap((t) => t.checks), ...c.checks] : null;
    const total = list ? list.length : (c.checks as number);
    const failedList = list ? list.filter((x) => !x.ok && !x.soft) : [];
    const failed = list ? failedList.length : Math.min(total, c.checks_failed ?? 0);
    const scores: Record<string, number> = {};
    for (const [k, v] of Object.entries({ ...c.metrics, ...c.scores })) if (typeof v === "number" && Number.isFinite(v)) scores[k] = v;
    return {
      id: c.id,
      title: c.title ?? null,
      tags: c.tags,
      status: c.status,
      error: c.error ?? null,
      duration_s: c.duration_s ?? null,
      checkList: list,
      checksTotal: total,
      checksFailed: failed,
      failedChecks: failedList,
      softChecks: list ? list.filter((x) => !x.ok && x.soft) : [],
      scores,
    };
  });
export type EvalCase = z.infer<typeof caseSchema>;

const runSchema = z
  .object({
    run: z.string().nullable().optional(),
    file: z.string().nullable().optional(),
    name: z.string().nullable().optional(),
    error: z.string().nullable().optional(),
    schema: z.string().nullable().optional(),
    started_at: z.string().nullable().optional(),
    finished_at: z.string().nullable().optional(),
    summary: z.record(z.string(), z.unknown()).catch({}).default({}),
    cases: z.array(z.unknown()).catch([]).default([]),
  })
  .loose()
  .transform((r) => ({
    ...r,
    error: r.error ?? null,
    cases: r.cases.flatMap((c) => {
      const p = caseSchema.safeParse(c);
      return p.success ? [p.data] : [];
    }),
    key: r.file ?? r.run ?? r.name ?? r.started_at ?? "run",
  }));
export type EvalRun = z.infer<typeof runSchema>;

export type EvalList = { runs: EvalRun[]; directoryAvailable: boolean };

function runs(items: unknown[]): EvalRun[] {
  return items.flatMap((i) => {
    const p = runSchema.safeParse(i);
    return p.success ? [p.data] : [];
  });
}

/** Newest first (by `started_at`, then file name, which is a UTC stamp). */
export const evalsResponseSchema = z.unknown().transform((v, ctx): EvalList => {
  let list: EvalRun[] | null = null;
  let directoryAvailable = true;
  if (Array.isArray(v)) list = runs(v);
  else if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    if (o["directory_available"] === false) directoryAvailable = false;
    if (Array.isArray(o["items"])) list = runs(o["items"]);
    else if (Array.isArray(o["runs"])) list = runs([...(o["latest"] ? [o["latest"]] : []), ...o["runs"]]);
    else if (Array.isArray(o["cases"])) list = runs([o]);
  }
  if (list === null) {
    ctx.addIssue({ code: "custom", message: "unexpected evals shape" });
    return z.NEVER;
  }
  const seen = new Set<string>();
  return {
    directoryAvailable,
    runs: list
      .filter((r) => (seen.has(r.key) ? false : (seen.add(r.key), true)))
      .sort((a, b) => (b.started_at ?? "").localeCompare(a.started_at ?? "") || b.key.localeCompare(a.key)),
  };
});

/** "12 / 14 checks" (soft failures count as passed but are listed when the file names them). */
export function caseScore(c: EvalCase): { passed: number; total: number; failed: EvalCheck[]; soft: EvalCheck[] } {
  return { passed: c.checksTotal - c.checksFailed, total: c.checksTotal, failed: c.failedChecks, soft: c.softChecks };
}

export const EVAL_STATUS: Record<string, { label: string; tone: "success" | "danger" | "warning" | "neutral" }> = {
  pass: { label: "Pass", tone: "success" },
  passed: { label: "Pass", tone: "success" },
  fail: { label: "Fail", tone: "danger" },
  failed: { label: "Fail", tone: "danger" },
  error: { label: "Error", tone: "warning" },
  skipped: { label: "Skipped", tone: "neutral" },
};

/** A metric as text: ratios as %, counts with separators, `null` → "—". */
export function metricText(key: string, v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "number") {
    if (/(recall|grounding|rate|ratio|precision|score)/.test(key) && v >= 0 && v <= 1) return `${Math.round(v * 1000) / 10} %`;
    return new Intl.NumberFormat("en-GB", { maximumFractionDigits: 2 }).format(v);
  }
  if (typeof v === "string" || typeof v === "boolean") return String(v);
  return "…";
}

/** "numeric_grounding" → "Numeric grounding". */
export function metricLabel(key: string): string {
  const t = key.replace(/_/g, " ").trim();
  return t ? t[0]!.toUpperCase() + t.slice(1) : key;
}

export type SummaryTile = { key: string; label: string; value: string };

/** The run summary as tiles (cases, passed, failed, errors, checks, grounding, recall, tokens). */
export function summaryTiles(run: EvalRun): SummaryTile[] {
  const s = run.summary;
  const n = (k: string) => (typeof s[k] === "number" ? (s[k] as number) : null);
  const tiles: SummaryTile[] = [];
  const add = (key: string, label: string, value: number | null) => {
    if (value !== null) tiles.push({ key, label, value: metricText(key, value) });
  };
  const isPass = (c: EvalCase) => c.status === "pass" || c.status === "passed";
  add("cases", "Cases", n("cases") ?? run.cases.length);
  add("passed", "Passed", n("passed") ?? run.cases.filter(isPass).length);
  add("failed", "Failed", n("failed"));
  add("errors", "Errors", n("errors"));
  const checks = n("checks");
  const checksFailed = n("checks_failed");
  if (checks !== null) tiles.push({ key: "checks", label: "Checks passed", value: `${metricText("n", checks - (checksFailed ?? 0))} / ${metricText("n", checks)}` });
  add("numeric_grounding_mean", "Numeric grounding", n("numeric_grounding_mean"));
  add("entity_recall_mean", "Entity recall", n("entity_recall_mean"));
  add("tokens_per_case_mean", "Tokens per case", n("tokens_per_case_mean"));
  return tiles;
}

/** Score columns across a run's cases (stable order: first seen). */
export function scoreKeys(run: EvalRun, max = 4): string[] {
  const keys: string[] = [];
  for (const c of run.cases) for (const k of Object.keys(c.scores)) if (!keys.includes(k)) keys.push(k);
  return keys.slice(0, max);
}
