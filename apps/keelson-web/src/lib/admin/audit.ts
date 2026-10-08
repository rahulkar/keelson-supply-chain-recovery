/**
 * Audit categories (`docs/contracts/110` §6): the audit page filters across runs, approvals,
 * change sets, admin changes and signals. The engine is asked with `category=` (it may tag rows
 * with `category`); rows without the tag are classified here by their action name.
 */
import { z } from "zod";
import type { AuditEntry } from "./types";

export const AUDIT_CATEGORIES = ["runs", "approvals", "change_sets", "admin", "signals"] as const;
export type AuditCategory = (typeof AUDIT_CATEGORIES)[number];

export const AUDIT_CATEGORY_LABELS: Record<AuditCategory, string> = {
  runs: "Runs and cases",
  approvals: "Approvals",
  change_sets: "Change sets",
  admin: "Admin changes",
  signals: "Signals and alerts",
};

const PREFIXES: Record<AuditCategory, readonly string[]> = {
  runs: ["run.", "case.", "message.", "demo.reset"],
  approvals: ["approval.", "interrupt.", "decision.", "change_request."],
  change_sets: ["change_set.", "commit.", "rollback.", "checklist.", "ledger."],
  signals: ["alert.", "signal.", "signals.", "watch_region.", "alert_rule.", "demo.pack"],
  admin: [
    "agent_spec.",
    "policy.",
    "policies.",
    "connector",
    "tool.",
    "gateway.",
    "kb.",
    "tenant.",
    "user.",
    "console.",
    "mapping.",
    "admin.",
    "auth.",
    "demo.login_as",
    "untrusted_text.",
  ],
};

export function isAuditCategory(v: unknown): v is AuditCategory {
  return typeof v === "string" && (AUDIT_CATEGORIES as readonly string[]).includes(v);
}

/** The row's category: the engine's tag when it sent one, else by action prefix (approvals before admin: `change_request.*`). */
export function categoryOf(entry: Pick<AuditEntry, "action"> & { category?: unknown }): AuditCategory | null {
  if (isAuditCategory(entry.category)) return entry.category;
  for (const cat of ["approvals", "change_sets", "runs", "signals", "admin"] as const) {
    if (PREFIXES[cat].some((p) => entry.action.startsWith(p))) return cat;
  }
  return null;
}

/** `YYYY-MM-DD` inclusive day bounds (UTC) on the row time; rows without a time pass only an empty range. */
export function inDateRange(iso: string | null | undefined, from: string, to: string): boolean {
  if (!from && !to) return true;
  if (!iso) return false;
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return false;
  if (from && t < Date.parse(`${from}T00:00:00Z`)) return false;
  if (to && t >= Date.parse(`${to}T00:00:00Z`) + 86_400_000) return false;
  return true;
}

// ───────────────────────────── activity timeline (GET /api/admin/audit/activity) ─────────────────────────────

/** Filters of the activity timeline (all optional; the engine validates again). */
export type ActivityQuery = {
  limit?: number | undefined;
  categories?: readonly AuditCategory[] | undefined;
  /** Opaque cursor from the previous page (`nextBefore`). */
  before?: string | null | undefined;
  /** Exact username. */
  actor?: string | undefined;
  case_id?: string | undefined;
  /** Action prefix, e.g. `run.` or `connector.mapping`. */
  action?: string | undefined;
  /** ISO times with an offset (inclusive / exclusive). */
  since?: string | undefined;
  until?: string | undefined;
};

export const activityEntrySchema = z
  .object({
    key: z.string(),
    category: z.string().nullable().optional(),
    at: z.string(),
    actor: z.string().nullable().optional(),
    action: z.string(),
    target: z.string().nullable().optional(),
    case_id: z.string().nullable().optional(),
    detail: z.record(z.string(), z.unknown()).nullable().catch(null).optional(),
  })
  .loose()
  .transform((e) => ({ ...e, category: categoryOf({ action: e.action, category: e.category }), actorName: e.actor || "system" }));
export type ActivityEntry = z.infer<typeof activityEntrySchema>;

/** `{items, next_before}`; malformed rows are skipped (one bad row never hides the page). */
export const activityPageSchema = z
  .object({ items: z.array(z.unknown()), next_before: z.string().nullable().optional() })
  .loose()
  .transform((v) => ({
    items: v.items.flatMap((i) => {
      const p = activityEntrySchema.safeParse(i);
      return p.success ? [p.data] : [];
    }),
    nextBefore: v.next_before ?? null,
  }));
export type ActivityPage = z.infer<typeof activityPageSchema>;

/** `YYYY-MM-DD` day bounds (UTC) → `since` / `until` query values (`until` exclusive: the next day). */
export function dayBounds(from: string, to: string): { since?: string; until?: string } {
  const out: { since?: string; until?: string } = {};
  const day = /^\d{4}-\d{2}-\d{2}$/;
  if (day.test(from) && !Number.isNaN(Date.parse(`${from}T00:00:00Z`))) out.since = `${from}T00:00:00+00:00`;
  if (day.test(to)) {
    const t = Date.parse(`${to}T00:00:00Z`);
    if (!Number.isNaN(t)) out.until = new Date(t + 86_400_000).toISOString().replace(".000Z", "+00:00");
  }
  return out;
}

/** A short line for the row's `detail` (run status, approval reason, change-set lines, before → after). */
export function activityDetail(e: Pick<ActivityEntry, "category" | "detail">): string | null {
  const d = e.detail;
  if (!d) return null;
  const str = (k: string) => (typeof d[k] === "string" && d[k] ? String(d[k]) : null);
  const num = (k: string) => (typeof d[k] === "number" ? Number(d[k]) : null);
  switch (e.category) {
    case "runs":
      return [str("kind"), str("status"), str("agent_spec_version") ? `spec ${str("agent_spec_version")}` : null, str("error")].filter(Boolean).join(" · ") || null;
    case "approvals":
      return [str("role") ? `as ${str("role")}` : null, str("plan_version") ? `plan ${str("plan_version")}` : null, str("reason") ? `“${str("reason")}”` : null].filter(Boolean).join(" · ") || null;
    case "change_sets": {
      const lines = num("lines");
      return [str("connector"), lines !== null ? `${lines} ${lines === 1 ? "line" : "lines"}` : null, str("ext_code")].filter(Boolean).join(" · ") || null;
    }
    case "signals":
      return d["auto"] === true ? "opened automatically" : str("event_type");
    default:
      return null;
  }
}
