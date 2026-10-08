/**
 * Tier-A connector mappings (`docs/contracts/110` §4): the **read mapping** (scn entity ←
 * vendor read tool + field expressions) and the **action mapping** (canonical op → vendor tool +
 * argument template + read-back), edited as YAML or JSON in the admin.
 *
 * Text is parsed with the `yaml` package (YAML 1.2: JSON is valid YAML; no custom tags, aliases
 * capped) and validated against the generated contract schemas, so a mistake is underlined at its
 * line before anything is sent. Expressions are only *looked at* here (unknown functions,
 * unbalanced brackets) — never evaluated; the engine parses them with its own safe language.
 */
import {
  MAPPING_EXPRESSION_FUNCTIONS,
  connectorsCapabilitiesSchema,
  mappingActionSchema,
  mappingDryRunResultSchema,
  mappingReadSchema,
  scnSnapshotLabelSchema,
  type ConnectorsCapabilities,
  type MappingAction,
  type MappingDryRunResult,
  type MappingRead,
} from "@keelson/contracts";
import { LineCounter, isMap, isNode, isPair, isScalar, isSeq, parseDocument, stringify, type Document } from "yaml";
import { z } from "zod";
import type { ConnectorTool } from "./types";

export type MappingKind = "read" | "action";
export type MappingFormat = "yaml" | "json";

export const MAPPING_KINDS: readonly MappingKind[] = ["read", "action"];

/** Mapping text larger than this is refused before parsing (the engine's body limit is the authority). */
export const MAX_MAPPING_CHARS = 200_000;

export type Diagnostic = {
  from: number;
  to: number;
  severity: "error" | "warning";
  message: string;
  /** 1-based line for lists outside the editor. */
  line: number;
};

export type Checked<T> = { value: T | null; diagnostics: Diagnostic[] };

// ───────────────────────────── server envelopes ─────────────────────────────

/**
 * `GET /api/admin/connectors/{slug}/mappings/{kind}`: the latest version (`version` / `mapping`
 * `null` when there is none yet) and `problems` = the approved-tool checks against today's tool
 * classification (a tool reclassified since the save shows up here).
 */
export const mappingEnvelopeSchema = z
  .object({
    connector: z.string().optional(),
    kind: z.enum(["read", "action"]).optional(),
    version: z.number().int().positive().nullable().optional().transform((v) => v ?? null),
    mapping: z.record(z.string(), z.unknown()).nullable().optional().transform((v) => v ?? null),
    summary: z.string().nullable().optional(),
    created_by: z.string().nullable().optional(),
    created_at: z.string().nullable().optional(),
    problems: z.array(z.string()).catch([]).default([]),
  })
  .loose();
export type MappingEnvelope = z.infer<typeof mappingEnvelopeSchema>;

/** `PUT …/mappings/{kind}` answer: the stored row (`unchanged: true` = same as the latest version). */
export const mappingSavedSchema = z
  .object({ version: z.number().int().positive(), unchanged: z.boolean().optional(), summary: z.string().nullable().optional() })
  .loose();
export type MappingSaved = z.infer<typeof mappingSavedSchema>;

export const dryRunResultSchema = mappingDryRunResultSchema.strip();
export type DryRunResult = MappingDryRunResult;

export const capabilitiesSchema = connectorsCapabilitiesSchema.strip();
export type Capabilities = ConnectorsCapabilities;

/** `POST …/mappings/read/test`: one page per source, mapped (nothing stored). */
export const readPreviewSchema = z
  .object({
    ok: z.boolean(),
    error: z.string().nullable().optional(),
    entities: z
      .array(
        z
          .object({
            entity: z.string(),
            where: z.string().nullable().optional(),
            rows: z.number().int().nonnegative().default(0),
            sample: z.array(z.record(z.string(), z.unknown())).catch([]).default([]),
            error: z.string().nullable().optional(),
          })
          .loose(),
      )
      .catch([])
      .default([]),
    pages: z.number().int().nonnegative().nullable().optional(),
    rows_seen: z.number().int().nonnegative().nullable().optional(),
    truncated: z.array(z.string()).catch([]).default([]),
  })
  .loose();
export type ReadPreview = z.infer<typeof readPreviewSchema>;

const isoOrNull = z.string().nullable().optional();

/** One canonical snapshot (a sync attempt): exported (`consistent`) or crawled (tier A, advisory). */
export const snapshotSchema = z
  .object({
    id: z.string(),
    status: z.string(),
    as_of: isoOrNull,
    reason: z.string().nullable().optional(),
    row_counts: z.record(z.string(), z.number()).catch({}).default({}),
    rows: z.number().nullable().optional(),
    duration_ms: z.number().nullable().optional(),
    error: z.string().nullable().optional(),
    created_at: isoOrNull,
    ready_at: isoOrNull,
    consistency: scnSnapshotLabelSchema.shape.consistency.catch("consistent").default("consistent"),
    as_of_range: z.object({ from: z.string(), to: z.string() }).nullable().catch(null).optional(),
    crawl: z
      .object({
        pages: z.number().int().nonnegative().optional(),
        rows_seen: z.number().int().nonnegative().optional(),
        truncated: z.array(z.string()).catch([]).default([]),
      })
      .loose()
      .nullable()
      .catch(null)
      .optional(),
  })
  .loose();
export type Snapshot = z.infer<typeof snapshotSchema>;

/** `GET …/connectors/{slug}/snapshots`: the current snapshot, the trigger state and the latest attempts (newest first). */
export const snapshotHistorySchema = z
  .object({
    current: snapshotSchema.nullable().catch(null).optional(),
    state: z.record(z.string(), z.unknown()).nullable().catch(null).optional(),
    items: z.array(z.unknown()).catch([]).default([]),
  })
  .loose()
  .transform((v) => ({
    current: v.current ?? null,
    state: v.state ?? null,
    items: v.items.flatMap((i) => {
      const p = snapshotSchema.safeParse(i);
      return p.success ? [p.data] : [];
    }),
  }));
export type SnapshotHistory = z.infer<typeof snapshotHistorySchema>;

/** `POST …/connectors/{slug}/sync` (202). */
export const syncQueuedSchema = z.object({ status: z.string() }).loose();
export type SyncQueued = z.infer<typeof syncQueuedSchema>;

export const SNAPSHOT_STATUS_LABELS: Record<string, { label: string; tone: "success" | "info" | "danger" | "neutral" | "warning" }> = {
  loading: { label: "Loading", tone: "info" },
  ready: { label: "Ready", tone: "success" },
  failed: { label: "Failed", tone: "danger" },
  superseded: { label: "Replaced", tone: "neutral" },
};

/** The latest attempt still loading (a crawl in progress), if any. */
export function loadingSnapshot(h: SnapshotHistory): Snapshot | null {
  return h.items.find((s) => s.status === "loading") ?? null;
}

/**
 * A requested (or due) sync the worker has not started yet: the trigger state's `due_at`, with no
 * attempt loading (engine `GET …/crawl` reports this as `queued`). `null` when nothing is queued.
 */
export function queuedSince(h: SnapshotHistory): string | null {
  if (loadingSnapshot(h)) return null;
  const due = h.state?.["due_at"];
  return typeof due === "string" && due ? due : null;
}

// ───────────────────────────── parsing & validation ─────────────────────────────

function pathLabel(path: readonly PropertyKey[]): string {
  return path.reduce<string>((s, k) => (typeof k === "number" ? `${s}[${k}]` : s ? `${s}.${String(k)}` : String(k)), "");
}

/** The node at `path`, or the deepest existing ancestor (a missing key points at its parent). */
function rangeAt(doc: Document, path: readonly PropertyKey[]): [number, number] {
  for (let n = path.length; n >= 0; n--) {
    const sub = path.slice(0, n);
    const node: unknown = n === 0 ? doc.contents : doc.getIn(sub as unknown[], true);
    if (isNode(node) && node.range) {
      // A collection's range spans all its lines; point at its key instead when we can.
      if ((isMap(node) || isSeq(node)) && n > 0) {
        const parent: unknown = n === 1 ? doc.contents : doc.getIn(sub.slice(0, -1) as unknown[], true);
        if (isMap(parent)) {
          const pair = parent.items.find((p) => isPair(p) && isScalar(p.key) && p.key.value === sub[n - 1]);
          if (pair && isScalar(pair.key) && pair.key.range) return [pair.key.range[0], pair.key.range[1]];
        }
      }
      return [node.range[0], Math.max(node.range[0] + 1, node.range[1])];
    }
  }
  return [0, 1];
}

function lineOf(counter: LineCounter, offset: number): number {
  return counter.linePos(offset).line;
}

/** Parse YAML / JSON text (JSON is YAML 1.2) into a plain value with error positions. */
export function parseMappingText(text: string): { doc: Document | null; value: unknown; counter: LineCounter; diagnostics: Diagnostic[] } {
  const counter = new LineCounter();
  if (text.length > MAX_MAPPING_CHARS) {
    return { doc: null, value: null, counter, diagnostics: [{ from: 0, to: 1, severity: "error", message: `The mapping is too long (over ${MAX_MAPPING_CHARS.toLocaleString("en-GB")} characters).`, line: 1 }] };
  }
  const doc = parseDocument(text, { lineCounter: counter, uniqueKeys: true, prettyErrors: false, strict: true });
  const diagnostics: Diagnostic[] = [];
  for (const e of doc.errors) {
    const [from, to] = e.pos;
    diagnostics.push({ from, to: Math.max(from + 1, to), severity: "error", message: e.message.split("\n")[0] ?? "Syntax error", line: lineOf(counter, from) });
  }
  for (const w of doc.warnings) {
    const [from, to] = w.pos;
    diagnostics.push({ from, to: Math.max(from + 1, to), severity: "warning", message: w.message.split("\n")[0] ?? "Warning", line: lineOf(counter, from) });
  }
  if (doc.errors.length) return { doc, value: null, counter, diagnostics };
  let value: unknown = null;
  try {
    value = doc.toJS({ maxAliasCount: 50 });
  } catch (err) {
    diagnostics.push({ from: 0, to: 1, severity: "error", message: err instanceof Error ? err.message : "Couldn't read the mapping.", line: 1 });
  }
  return { doc, value, counter, diagnostics };
}

/**
 * A light look at one mapping expression (never evaluated): known function names, balanced
 * brackets and quotes, no `__` names. `null` = nothing to say.
 */
export function expressionProblem(expr: string): string | null {
  let depth = 0;
  let quote: string | null = null;
  for (let i = 0; i < expr.length; i++) {
    const ch = expr[i]!;
    if (quote) {
      if (ch === "\\") i++;
      else if (ch === quote) quote = null;
      continue;
    }
    if (ch === "'" || ch === '"') quote = ch;
    else if (ch === "(") depth++;
    else if (ch === ")" && --depth < 0) return "A closing bracket has no opening one.";
  }
  if (quote) return "A quoted text is not closed.";
  if (depth > 0) return "A bracket is not closed.";
  const known = MAPPING_EXPRESSION_FUNCTIONS as Record<string, readonly [number, number]>;
  // Function calls outside quoted text.
  const bare = expr.replace(/'(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*"/g, "''");
  for (const m of bare.matchAll(/([A-Za-z_][A-Za-z0-9_]*)\s*\(/g)) {
    const name = m[1]!;
    if (!Object.hasOwn(known, name)) return `Unknown function "${name}" (use ${Object.keys(known).join(", ")}).`;
  }
  if (/__/.test(bare)) return "Names with a double underscore are not allowed.";
  return null;
}

type ToolInfo = Pick<ConnectorTool, "name" | "access" | "content_trust" | "status">;

function toolDiagnostics(doc: Document, counter: LineCounter, kind: MappingKind, value: MappingRead | MappingAction, tools: readonly ToolInfo[] | null): Diagnostic[] {
  const out: Diagnostic[] = [];
  const at = (path: PropertyKey[], severity: Diagnostic["severity"], message: string) => {
    const [from, to] = rangeAt(doc, path);
    out.push({ from, to, severity, message, line: lineOf(counter, from) });
  };
  const byName = tools ? new Map(tools.map((t) => [t.name, t])) : null;
  const checkRead = (path: PropertyKey[], name: string, why: string) => {
    if (!byName) return;
    const t = byName.get(name);
    if (!t) at(path, "error", `"${name}" is not a tool of this connector.`);
    else if (t.access !== "read") at(path, "error", `"${name}" is a ${t.access} tool: ${why} must use read tools.`);
    else if (t.content_trust === "untrusted_text") at(path, "error", `"${name}" returns untrusted text: crawled rows must come from trusted-data tools.`);
    else if (t.status !== "approved") at(path, "warning", `"${name}" is not approved yet (Tools table).`);
  };
  if (kind === "read") {
    const read = value as MappingRead;
    read.entities.forEach((e, i) => {
      checkRead(["entities", i, "source", "tool"], e.source.tool, "the crawl");
      for (const [col, expr] of Object.entries(e.fields)) {
        const p = expressionProblem(expr);
        if (p) at(["entities", i, "fields", col], "error", `${e.entity}.${col}: ${p}`);
      }
    });
    for (const [name, lk] of Object.entries(read.lookups ?? {})) {
      if (lk.kind === "source") {
        checkRead(["lookups", name, "source", "tool"], lk.source.tool, "lookups");
        for (const k of ["key", "value"] as const) {
          const p = expressionProblem(lk[k]);
          if (p) at(["lookups", name, k], "error", `lookup ${name}.${k}: ${p}`);
        }
      }
    }
  } else {
    const action = value as MappingAction;
    const seen = new Set<string>();
    action.ops.forEach((op, i) => {
      if (seen.has(op.op)) at(["ops", i, "op"], "error", `"${op.op}" is mapped twice.`);
      seen.add(op.op);
      if (byName && !byName.has(op.tool)) at(["ops", i, "tool"], "error", `"${op.tool}" is not a tool of this connector.`);
      if (op.readback) checkRead(["ops", i, "readback", "tool"], op.readback.tool, "read-backs");
      if (op.idempotency === "none") at(["ops", i, "idempotency"], "warning", `${op.op}: idempotency "none" is never retried automatically; a failed call becomes a guided step.`);
      if (op.idempotency !== "enforced" && !op.readback) at(["ops", i], "warning", `${op.op}: add a read-back so an uncertain call can be checked before any retry.`);
    });
  }
  return out;
}

/**
 * Parse + validate a mapping text: syntax errors, contract shape (`mappingReadSchema` /
 * `mappingActionSchema`, kind must match), expression look-over and — when `tools` are known —
 * tool checks (read mapping sources / lookups / read-backs must be approved trusted read tools).
 */
export function checkMapping(kind: MappingKind, text: string, tools: readonly ToolInfo[] | null = null): Checked<MappingRead | MappingAction> {
  const parsed = parseMappingText(text);
  if (!parsed.doc || parsed.value === null || parsed.diagnostics.some((d) => d.severity === "error")) {
    if (parsed.doc && !text.trim()) return { value: null, diagnostics: [{ from: 0, to: 1, severity: "error", message: "The mapping is empty.", line: 1 }] };
    return { value: null, diagnostics: parsed.diagnostics };
  }
  const { doc, counter } = parsed;
  const diagnostics = [...parsed.diagnostics];
  if (!parsed.value || typeof parsed.value !== "object" || Array.isArray(parsed.value)) {
    return { value: null, diagnostics: [...diagnostics, { from: 0, to: Math.max(1, Math.min(text.length, 40)), severity: "error", message: "A mapping is an object (key: value pairs).", line: 1 }] };
  }
  const declared = (parsed.value as Record<string, unknown>)["kind"];
  if (declared !== undefined && declared !== kind) {
    const [from, to] = rangeAt(doc, ["kind"]);
    diagnostics.push({ from, to, severity: "error", message: `This is the ${kind} mapping: "kind" must be "${kind}".`, line: lineOf(counter, from) });
    return { value: null, diagnostics };
  }
  const schema = kind === "read" ? mappingReadSchema : mappingActionSchema;
  const result = schema.safeParse(parsed.value);
  if (!result.success) {
    for (const issue of result.error.issues.slice(0, 50)) {
      const [from, to] = rangeAt(doc, issue.path);
      const where = pathLabel(issue.path);
      diagnostics.push({ from, to, severity: "error", message: where ? `${where}: ${issue.message}` : issue.message, line: lineOf(counter, from) });
    }
    return { value: null, diagnostics };
  }
  diagnostics.push(...toolDiagnostics(doc, counter, kind, result.data, tools));
  return { value: diagnostics.some((d) => d.severity === "error") ? null : result.data, diagnostics };
}

/**
 * Saved mapping → editor text. The engine stores the validated model (JSON), not the admin's
 * text, so comments are not kept: the text is rendered in the chosen format.
 */
export function mappingText(env: MappingEnvelope | null, format: MappingFormat): string {
  if (!env || !env.mapping) return "";
  return format === "json" ? `${JSON.stringify(env.mapping, null, 2)}
` : stringify(env.mapping, { lineWidth: 0 });
}

/** Convert the editor text between YAML and JSON (only when it parses; otherwise `null`). */
export function convertText(text: string, to: MappingFormat): string | null {
  const parsed = parseMappingText(text);
  if (parsed.value === null || parsed.diagnostics.some((d) => d.severity === "error")) return null;
  return to === "json" ? `${JSON.stringify(parsed.value, null, 2)}\n` : stringify(parsed.value, { lineWidth: 0 });
}

/** Starter text for a connector without a mapping yet (a minimal valid example to edit). */
export function mappingTemplate(kind: MappingKind): string {
  return kind === "read"
    ? `# Read mapping: each scn entity is filled by paging one read tool.
kind: read
entities:
  - entity: location
    source:
      tool: list_items
      args: { type: location }
      page_param: page
      page_size: 100
      items_path: $.items
      cursor_path: null
      max_pages: 50
    fields:
      code: $.key
      name: $.fields.name
      type: $.fields.kind
`
    : `# Action mapping: each canonical op calls one vendor tool; unmapped ops are guided.
kind: action
ops:
  - op: transfer_order.create
    tool: create_transfer_order
    args:
      from_dc: "{{payload.from_dc}}"
      to_dc: "{{payload.to_dc}}"
      lines: "{{payload.lines}}"
    idempotency: best_effort
    request_id_arg: null
    readback:
      tool: list_items
      args: { type: transfer_order }
      items_path: $.items
      match: { from_dc: "{{payload.from_dc}}", to_dc: "{{payload.to_dc}}" }
`;
}

/** The ops of an action mapping (for the dry-run picker); `[]` when it doesn't validate. */
export function mappedOps(value: MappingRead | MappingAction | null): string[] {
  if (!value || !("ops" in value)) return [];
  return value.ops.map((o) => o.op);
}

/** `{…}` sample payload → object, or the reason it isn't one. */
export function parsePayload(text: string): { value: Record<string, unknown> } | { error: string } {
  if (!text.trim()) return { value: {} };
  try {
    const v: unknown = JSON.parse(text);
    if (!v || typeof v !== "object" || Array.isArray(v)) return { error: "The sample payload must be a JSON object." };
    return { value: v as Record<string, unknown> };
  } catch {
    return { error: "The sample payload isn't valid JSON." };
  }
}
