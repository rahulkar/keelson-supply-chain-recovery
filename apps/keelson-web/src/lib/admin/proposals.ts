/**
 * Proposed tier-A mappings (`docs/contracts/121` §3): the engine samples a connector's approved
 * read tools, keeps only shape statistics, and proposes read / action mapping drafts — rule-based,
 * optionally refined by the AI. A proposal is never active: the admin opens a draft in the mapping
 * editor (as YAML with one comment per field: confidence · origin · why), previews it and saves it
 * like any edit, with `proposal_id` (an action mapping from a proposal then needs a second admin).
 *
 * Every text in a proposal that came from the vendor or the model (rationales, notes) is shown as
 * plain text only, and only inside YAML comments in the editor (never as keys or values).
 */
import {
  confidencePercent,
  mappingProposalSchema,
  type MappingProposal,
  type ProposalNoteCode,
  type ProposalOrigin,
} from "@keelson/contracts";
import { Document, isMap, isScalar, isSeq, type Scalar } from "yaml";
import { z } from "zod";

export type Proposal = MappingProposal;
export type ProposedEntity = NonNullable<Proposal["read"]>["entities"][number];
export type ProposedField = ProposedEntity["fields"][number];
export type ProposedOp = NonNullable<Proposal["action"]>["ops"][number];
export type ProposalNote = NonNullable<Proposal["notes"]>[number];
export type SourceProfile = Proposal["profiles"][number];

export const proposalSchema = mappingProposalSchema;

/** `GET …/mappings/proposals` row (newest first). */
export const proposalSummarySchema = z
  .object({
    id: z.string(),
    status: z.enum(["ready", "accepted", "discarded"]).catch("ready"),
    proposer: z.string(),
    model_alias: z.string().nullable().optional(),
    kinds: z.array(z.string()).catch([]).default([]),
    created_by: z.string().nullable().optional(),
    created_at: z.string().nullable().optional(),
    entities: z.number().int().nonnegative().catch(0).default(0),
    complete_entities: z.number().int().nonnegative().catch(0).default(0),
    ops: z.number().int().nonnegative().catch(0).default(0),
    notes: z.number().int().nonnegative().catch(0).default(0),
  })
  .loose();
export type ProposalSummary = z.infer<typeof proposalSummarySchema>;

export const proposalListSchema = z
  .object({ items: z.array(z.unknown()).catch([]) })
  .loose()
  .transform((v) =>
    v.items.flatMap((i) => {
      const p = proposalSummarySchema.safeParse(i);
      return p.success ? [p.data] : [];
    }),
  );

/** `GET /api/admin/mappings/proposer`: the installation's proposer mode and whether the AI step can run. */
export const proposerStatusSchema = z
  .object({
    mode: z.enum(["auto", "heuristic", "off"]).catch("heuristic"),
    enabled: z.boolean().catch(true),
    alias: z.string().nullable().optional(),
    llm: z
      .object({ available: z.boolean().catch(false), reason: z.string().nullable().optional(), message: z.string().nullable().optional() })
      .loose()
      .catch({ available: false, reason: null, message: null }),
  })
  .loose();
export type ProposerStatus = z.infer<typeof proposerStatusSchema>;

export type ProposeBody = { kinds: ("read" | "action")[]; proposer: "auto" | "heuristic"; sample: boolean };

export const ORIGIN_LABELS: Record<ProposalOrigin, string> = { heuristic: "Rules", llm: "AI", both: "Rules + AI" };

export function originLabel(origin: string): string {
  return (ORIGIN_LABELS as Record<string, string>)[origin] ?? origin;
}

/** Short, person-facing label per note code (the engine's own message is shown next to it). */
export const NOTE_LABELS: Record<ProposalNoteCode, string> = {
  llm_disabled: "AI refinement off",
  llm_unavailable: "AI unavailable",
  llm_failed: "AI step failed",
  replay_miss: "No recorded AI answer",
  llm_output_dropped: "AI suggestion dropped",
  injection_suspected: "Instructions found in the data",
  description_flagged: "Tool description withheld",
  sample_value_withheld: "Free text withheld",
  sampling_truncated: "Sampling cut short",
  schema_only: "Tool catalogue only",
  required_column_unmapped: "Required column missing",
  entity_unmapped: "Entity not found",
  op_unmapped: "Change not mappable",
  path_not_in_sample: "Field not in the sample",
  type_mismatch: "Type mismatch",
  generic_update_tool: "Generic update tool",
  source_unused: "Read not used",
  key_dropped: "Field names withheld",
  sample_failed: "Sampling failed",
};

export function noteLabel(code: string): string {
  return (NOTE_LABELS as Record<string, string>)[code] ?? code.replace(/_/g, " ");
}

const AI_FALLBACK: ReadonlySet<string> = new Set(["llm_unavailable", "llm_failed", "replay_miss", "injection_suspected"]);

/** The one-line banner of a proposal: who proposed it and, when the AI step did not run, why. */
export function proposalHeadline(p: Pick<Proposal, "proposer" | "notes" | "model_alias">): string {
  const fallback = (p.notes ?? []).find((n) => AI_FALLBACK.has(n.code));
  if (p.proposer === "llm+heuristic") return `Rules refined by the AI${p.model_alias ? ` (${p.model_alias})` : ""}.`;
  if (fallback) return `${noteLabel(fallback.code)}: rule-based suggestions.`;
  return "Rule-based suggestions (no AI step).";
}

/** `list_items(type=store)` — how a proposed read is named. */
export function sourceLabel(src: { tool: string; args?: Record<string, unknown> | undefined }): string {
  const args = Object.entries(src.args ?? {})
    .filter(([, v]) => v === null || ["string", "number", "boolean"].includes(typeof v))
    .map(([k, v]) => `${k}=${String(v)}`)
    .join(", ");
  return `${src.tool}(${args})`;
}

// ───────────────────────────── drafts as editor text ─────────────────────────────

/** Vendor / model text for a YAML comment: one line, no control characters, bounded. */
export function commentText(text: string, max = 160): string {
  const flat = Array.from(text, (ch) => (/[\p{Cc}\p{Cf}\p{Zl}\p{Zp}]/u.test(ch) ? " " : ch)).join("");
  const one = flat.replace(/\s+/g, " ").trim();
  return one.length > max ? `${one.slice(0, max - 1)}…` : one;
}

function fieldComment(f: { confidence_bp: number; origin: string; rationale: string }): string {
  const why = commentText(f.rationale);
  return ` ${confidencePercent(f.confidence_bp)} · ${originLabel(f.origin)}${why ? ` · ${why}` : ""}`;
}

/** Selected parts of a proposal (default: every complete entity / every op). */
export type DraftSelection = { entities: ReadonlySet<number>; ops: ReadonlySet<string> };

export function defaultSelection(p: Proposal): DraftSelection {
  const entities = new Set<number>();
  (p.read?.entities ?? []).forEach((e, i) => {
    if (!(e.missing_required ?? []).length) entities.add(i);
  });
  return { entities, ops: new Set((p.action?.ops ?? []).map((o) => o.mapping.op)) };
}

function usedTables(expressions: readonly string[]): Set<string> {
  const out = new Set<string>();
  for (const e of expressions) {
    for (const m of e.matchAll(/\b(?:lookup|field)\s*\(\s*(['"])([a-z][a-z0-9_]{0,39})\1/g)) out.add(m[2]!);
  }
  return out;
}

/**
 * The selected read draft as YAML for the editor: each field carries a trailing comment
 * `NN% · origin · why`; required columns nobody could map are written as `null` with a comment, so
 * the editor's contract check points at exactly what to fill in. Lookups used by the selected
 * entities are included. `null` when nothing is selected.
 */
export function readDraftText(p: Proposal, selection: DraftSelection): string | null {
  const chosen = (p.read?.entities ?? []).filter((_e, i) => selection.entities.has(i));
  if (!chosen.length) return null;
  const tables = usedTables(chosen.flatMap((e) => [...e.fields.map((f) => f.expression), ...(e.group_by ?? [])]));
  const lookups = Object.fromEntries((p.read?.lookups ?? []).filter((lk) => tables.has(lk.name)).map((lk) => [lk.name, lk.table]));
  const entities = chosen.map((e) => {
    const fields: Record<string, string | null> = {};
    for (const f of e.fields) fields[f.column] = f.expression;
    for (const c of e.missing_required ?? []) fields[c] = null;
    return { entity: e.entity, source: e.source, ...(e.group_by?.length ? { group_by: e.group_by } : {}), ...(e.dedupe ? { dedupe: true } : {}), fields };
  });
  const doc = new Document({ kind: "read", entities, ...(Object.keys(lookups).length ? { lookups } : {}) });
  doc.commentBefore = ` Proposed read mapping (${proposalHeadline(p).replace(/\.$/, "")}). Review every field, preview, then save.`;
  chosen.forEach((e, i) => {
    const node = doc.getIn(["entities", i], true);
    if (isMap(node)) node.commentBefore = ` ${e.entity} from ${sourceLabel(e.source)} · ${confidencePercent(e.confidence_bp)} · ${originLabel(e.origin)}`;
    const fields = doc.getIn(["entities", i, "fields"], true);
    if (!isMap(fields)) return;
    for (const pair of fields.items) {
      const key = isScalar(pair.key) ? String(pair.key.value) : "";
      const value = pair.value as Scalar | null;
      if (!isScalar(value)) continue;
      const f = e.fields.find((x) => x.column === key);
      value.comment = f ? fieldComment(f) : " required: nothing in the sample fits — fill it in";
    }
  });
  for (const lk of p.read?.lookups ?? []) {
    const node = doc.getIn(["lookups", lk.name], true);
    if (isMap(node)) node.commentBefore = ` ${lk.table.kind} lookup · ${confidencePercent(lk.confidence_bp)} · ${originLabel(lk.origin)}`;
  }
  return doc.toString({ lineWidth: 0 });
}

/** The selected action draft as YAML (a comment per op: confidence · origin · why). `null` when none. */
export function actionDraftText(p: Proposal, selection: DraftSelection): string | null {
  const chosen = (p.action?.ops ?? []).filter((o) => selection.ops.has(o.mapping.op));
  if (!chosen.length) return null;
  const doc = new Document({ kind: "action", ops: chosen.map((o) => o.mapping) });
  doc.commentBefore = " Proposed action mapping: saving it needs a second admin (four-eyes). Dry-run every op first.";
  const ops = doc.get("ops", true);
  if (isSeq(ops)) {
    ops.items.forEach((node, i) => {
      const o = chosen[i]!;
      if (isMap(node)) node.commentBefore = `${fieldComment(o)}${o.generic ? " · generic update tool: check the vendor's rules" : ""}`;
    });
  }
  return doc.toString({ lineWidth: 0 });
}
