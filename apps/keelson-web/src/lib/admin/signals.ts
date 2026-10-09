/**
 * Signals admin (`docs/contracts/110` §5): the tenant's view of the Signal Hub — sources
 * (subscription on/off, cadence, licence), watch regions (extra polygons / a radius around a named
 * node) and alert rules. The engine proxies these to the hub with its service JWT; shapes are read
 * tolerantly (the hub's own `/v1/sources` and `/v1/tenants/{t}/alert-rules` bodies are accepted too).
 */
import { signalsAlertRuleSchema, type SignalsAlertRule } from "@keelson/contracts";
import { z } from "zod";

// ───────────────────────────── sources ─────────────────────────────

const iso = z.string().nullable().optional();

/**
 * `GET /api/admin/signals/sources` row: the hub's feed status (cadence and licence when the hub
 * reports them) plus this tenant's subscription: on / off and an optional cadence override
 * (`cadence_s`; `null` = the source's default). M8 (`docs/contracts/124` §4) adds the catalogue
 * facts: grouping `kind`, `provenance`, licence class, attribution, an https terms link, `opt_in`
 * (off until the workspace subscribes), `polled` (enabled on this installation), coverage and a
 * description. Every one is optional: an older engine or hub simply omits them.
 */
export const signalSourceSchema = z
  .object({
    id: z.string().optional(),
    connector_id: z.string().optional(),
    title: z.string().nullable().optional(),
    name: z.string().nullable().optional(),
    kind: z.string().nullable().optional(),
    enabled: z.boolean().default(true),
    /** This tenant's cadence override (`null` = the source's default). */
    cadence_s: z.number().int().positive().nullable().optional(),
    /** The hub's default and the cadence it actually polls at (the fastest subscriber wins). */
    default_cadence_s: z.number().int().positive().nullable().optional(),
    effective_cadence_s: z.number().int().positive().nullable().optional(),
    /** The source's own cadence limits (M8): the editor offers only cadences inside them. */
    min_cadence_s: z.number().int().positive().nullable().optional(),
    max_cadence_s: z.number().int().positive().nullable().optional(),
    licence: z.string().nullable().optional(),
    license: z.string().nullable().optional(),
    attribution: z.string().nullable().optional(),
    provenance: z.string().nullable().optional(),
    licence_class: z.string().nullable().optional(),
    /** Rendered only through {@link safeTermsUrl} (https, plain host). */
    terms_url: z.string().nullable().optional(),
    opt_in: z.boolean().nullable().optional(),
    polled: z.boolean().nullable().optional(),
    paused: z.boolean().nullable().optional(),
    authority_bp: z.number().int().nonnegative().nullable().optional(),
    coverage: z.string().nullable().optional(),
    description: z.string().nullable().optional(),
    last_polled_at: iso,
    last_status: z.string().nullable().optional(),
    last_error: z.string().nullable().optional(),
    consecutive_failures: z.number().int().nonnegative().nullable().optional(),
    items_total: z.number().int().nonnegative().nullable().optional(),
    next_poll_at: iso,
  })
  .loose()
  .transform((s) => {
    const id = s.id ?? s.connector_id ?? "";
    return { ...s, id, name: s.title || s.name || id, licence: s.licence ?? s.license ?? null };
  })
  .refine((s) => s.id !== "", { message: "source without id" });
export type SignalSource = z.infer<typeof signalSourceSchema>;

function sourceRows(items: unknown[]): SignalSource[] {
  return items.flatMap((i) => {
    const p = signalSourceSchema.safeParse(i);
    return p.success ? [p.data] : [];
  });
}

/**
 * `{items, hub_error}` (engine), `{sources}` (hub shape) or a bare array; malformed rows skipped.
 * `hubError` = the hub could not be asked (the tenant's own subscriptions are still listed).
 */
export const signalSourceListSchema = z
  .union([
    z.array(z.unknown()).transform((items) => ({ items, hub_error: null as string | null })),
    z.object({ items: z.array(z.unknown()), hub_error: z.string().nullable().optional() }).loose(),
    z
      .object({ sources: z.array(z.unknown()) })
      .loose()
      .transform((v) => ({ items: v.sources, hub_error: null as string | null })),
  ])
  .transform((v) => ({ sources: sourceRows(v.items), hubError: v.hub_error ?? null }));
export type SignalSourceList = z.infer<typeof signalSourceListSchema>;

/** `PUT …/signals/sources/{id}` answer: the stored subscription and whether the hub took it. */
export const sourceSavedSchema = z
  .object({
    id: z.string(),
    enabled: z.boolean(),
    cadence_s: z.number().int().positive().nullable().optional(),
    hub_synced: z.boolean().optional(),
    hub_error: z.string().nullable().optional(),
  })
  .loose();
export type SourceSaved = z.infer<typeof sourceSavedSchema>;

/** `PUT …/signals/sources/{id}` body: at least one field; `cadence_s: null` = back to the source's default. */
export type SourceSettingPatch = { enabled?: boolean | undefined; cadence_s?: number | null | undefined };

/** Engine `signals_admin.CADENCE_RANGE` (seconds). */
export const CADENCE_RANGE = { min: 60, max: 86_400 } as const;
/** Cadences offered in the editor (a stored value outside the list is shown too). */
export const CADENCE_CHOICES: readonly number[] = [60, 300, 900, 1800, 3600, 6 * 3600, 12 * 3600, 86_400];

export function cadenceLabel(seconds: number | null | undefined): string {
  if (!seconds) return "—";
  if (seconds % 3600 === 0) return `every ${seconds / 3600} h`;
  if (seconds % 60 === 0) return `every ${seconds / 60} min`;
  return `every ${seconds} s`;
}

/**
 * Select options for one source: "" = the source's default, then the allowed cadences — only those
 * inside the source's own limits when the hub reports them (`min_cadence_s` / `max_cadence_s`, M8;
 * the hub would clamp any other wish). A stored value is always offered, so the select can show it.
 */
export function cadenceOptions(
  s: Pick<SignalSource, "cadence_s" | "default_cadence_s"> & Partial<Pick<SignalSource, "min_cadence_s" | "max_cadence_s">>,
): { value: string; label: string }[] {
  const def = s.default_cadence_s ? `Default (${cadenceLabel(s.default_cadence_s)})` : "Default";
  const lo = s.min_cadence_s ?? CADENCE_RANGE.min;
  const hi = s.max_cadence_s ?? CADENCE_RANGE.max;
  const inLimits = CADENCE_CHOICES.filter((v) => v >= lo && v <= hi);
  const values = new Set(inLimits.length > 0 ? inLimits : CADENCE_CHOICES);
  if (s.cadence_s && s.cadence_s >= CADENCE_RANGE.min && s.cadence_s <= CADENCE_RANGE.max) values.add(s.cadence_s);
  return [{ value: "", label: def }, ...[...values].sort((a, b) => a - b).map((v) => ({ value: String(v), label: cadenceLabel(v) }))];
}

/** Select value → `cadence_s` (`null` = default); anything outside the engine's range → `undefined` (not sendable). */
export function cadenceFromOption(value: string): number | null | undefined {
  if (value === "") return null;
  const n = Number(value);
  return Number.isInteger(n) && n >= CADENCE_RANGE.min && n <= CADENCE_RANGE.max ? n : undefined;
}

/** Catalogue grouping of a source (`kind`), as a short badge label. */
export const SOURCE_KIND_LABEL: Readonly<Record<string, string>> = {
  hazard: "Hazard",
  weather: "Weather",
  regulatory: "Regulatory",
  news: "News",
};

/** Below this authority an inference source is labelled "low authority" (GDELT: 4000). */
export const LOW_AUTHORITY_BP = 5000;

/** The provenance chip: deterministic `Fact`, or `Inference` (with "low authority" when it is). */
export function provenanceLabel(s: Pick<SignalSource, "provenance" | "authority_bp">): string | null {
  if (s.provenance === "fact") return "Fact";
  if (s.provenance === "inference") return typeof s.authority_bp === "number" && s.authority_bp < LOW_AUTHORITY_BP ? "Inference · low authority" : "Inference";
  return null;
}

const TERMS_URL_RE = /^https:\/\/[A-Za-z0-9.-]+(:\d{1,5})?\/[^\s"'<>\\]*$/;

/** A terms link as it may be rendered: absolute `https:` with a plain host, else `null` (never `javascript:`). */
export function safeTermsUrl(url: string | null | undefined): string | null {
  if (!url || url.length > 500 || !TERMS_URL_RE.test(url)) return null;
  try {
    const u = new URL(url);
    return u.protocol === "https:" && !u.username && !u.password ? u.href : null;
  } catch {
    return null;
  }
}

/** Note under the Subscribed switch: not enabled on this installation, or an opt-in source waiting for a subscriber. */
export function sourceSwitchNote(s: Pick<SignalSource, "polled" | "opt_in" | "enabled" | "paused">): string | null {
  if (s.polled === false) return "Not enabled on this installation (platform admin: SIGNAL_ADAPTERS)";
  if (s.opt_in && !s.enabled && s.paused !== false) return "Starts polling when a workspace subscribes";
  return null;
}

// ───────────────────────────── network codes (radius regions) ─────────────────────────────

/**
 * `GET /api/admin/signals/network`: `{nodes: {type: [codes]}}` of the tenant network the engine
 * pushes to the hub (radius watch regions are drawn around one of them). Codes that don't match
 * the engine's `NodeRef` pattern are dropped; unknown types are ignored.
 */
export const networkCodesSchema = z
  .object({ nodes: z.record(z.string(), z.array(z.unknown()).catch([])).catch({}) })
  .loose()
  .transform((v) => {
    const out: Partial<Record<RegionNodeType, string[]>> = {};
    for (const t of REGION_NODE_TYPES) {
      const codes = (v.nodes[t] ?? []).filter((c): c is string => typeof c === "string" && c.length <= 120 && NODE_CODE_PATTERN.test(c));
      if (codes.length) out[t] = [...new Set(codes)].slice(0, 2000);
    }
    return out;
  });
export type NetworkCodes = z.infer<typeof networkCodesSchema>;

export const SOURCE_STATUS_TONE: Record<string, "success" | "warning" | "danger" | "neutral"> = {
  ok: "success",
  not_modified: "success",
  error: "danger",
  backoff: "warning",
};

/** Person-facing text for the engine's `hub_error` codes (`null` = nothing to say). */
export function hubErrorText(code: string | null | undefined): string | null {
  if (!code) return null;
  if (code === "signal_hub_not_configured") return "The Signal Hub is not configured for this installation.";
  if (code === "signal_hub_unsupported") return "The Signal Hub doesn't support this setting yet: it is stored and sent again later.";
  return "The Signal Hub couldn't be reached: the setting is stored and sent again after the next sync.";
}

// ───────────────────────────── watch regions ─────────────────────────────

/** Engine `signals_admin.WatchRegion` ids (at most 40 characters). */
export const REGION_ID_PATTERN = /^[a-z][a-z0-9_-]{0,39}$/;
export const MAX_WATCH_REGIONS = 50;
/** Points the editor accepts (the engine allows up to 2,000 positions per polygon). */
export const MAX_POLYGON_POINTS = 500;
export const RADIUS_KM = { min: 1, max: 2000 } as const;
export const REGION_NODE_TYPES = ["supplier", "factory", "port", "dc", "store"] as const;
export type RegionNodeType = (typeof REGION_NODE_TYPES)[number];
/** Engine `NodeRef.code`. */
export const NODE_CODE_PATTERN = /^[A-Za-z0-9][A-Za-z0-9_.:/#>-]*$/;

const lonLat = z.tuple([z.number().gte(-180).lte(180), z.number().gte(-90).lte(90)]);

export const watchRegionSchema = z
  .object({
    id: z.string().regex(REGION_ID_PATTERN),
    name: z.string().min(1).max(120),
    kind: z.enum(["radius", "polygon"]),
    /** GeoJSON `Polygon` (WGS84, closed rings) for `kind: polygon`. */
    polygon: z
      .object({ type: z.literal("Polygon"), coordinates: z.array(z.array(lonLat).min(4)).min(1) })
      .nullable()
      .optional(),
    /** A named site of the tenant network for `kind: radius`. */
    node: z
      .object({ type: z.enum(REGION_NODE_TYPES), code: z.string().min(1).max(120) })
      .nullable()
      .optional(),
    radius_km: z.number().int().gte(RADIUS_KM.min).lte(RADIUS_KM.max).nullable().optional(),
    enabled: z.boolean().default(true),
  })
  .strip();
export type WatchRegion = z.infer<typeof watchRegionSchema>;

/** `{items}` (engine), `{regions}` (hub shape) or a bare array; malformed rows skipped. */
export const watchRegionListSchema = z
  .union([z.array(z.unknown()), z.object({ items: z.array(z.unknown()) }).loose(), z.object({ regions: z.array(z.unknown()) }).loose()])
  .transform((v) => {
    const items = (Array.isArray(v) ? v : Array.isArray((v as { items?: unknown }).items) ? (v as { items: unknown[] }).items : (v as { regions: unknown[] }).regions) as unknown[];
    return items.flatMap((i) => {
      const p = watchRegionSchema.safeParse(i);
      return p.success ? [p.data] : [];
    });
  });

/** The region as the engine stores it: only the fields of its kind (the engine forbids the others). */
export function regionBody(r: WatchRegion): Record<string, unknown> {
  const base = { id: r.id, name: r.name, kind: r.kind, enabled: r.enabled };
  return r.kind === "polygon" ? { ...base, polygon: r.polygon } : { ...base, node: r.node, radius_km: r.radius_km };
}

/**
 * "lon, lat" per line (or a GeoJSON Polygon / its coordinates) → the outer ring. Errors name
 * the line. The ring is closed (GeoJSON); at least three distinct points.
 */
export function parsePolygon(text: string): { ring: [number, number][] } | { error: string } {
  const t = text.trim();
  if (!t) return { error: "Enter at least three points." };
  let points: unknown;
  if (t.startsWith("{") || t.startsWith("[")) {
    try {
      const json: unknown = JSON.parse(t);
      const coords = json && typeof json === "object" && !Array.isArray(json) ? (json as { coordinates?: unknown }).coordinates : json;
      points = Array.isArray(coords) && Array.isArray(coords[0]) && Array.isArray((coords[0] as unknown[])[0]) ? coords[0] : coords;
    } catch {
      return { error: "That isn't valid JSON." };
    }
  } else {
    const out: [number, number][] = [];
    const lines = t.split(/\r?\n/).filter((l) => l.trim());
    for (let i = 0; i < lines.length; i++) {
      const parts = lines[i]!.split(/[\s,;]+/).filter(Boolean).map(Number);
      if (parts.length !== 2 || parts.some((n) => !Number.isFinite(n))) return { error: `Line ${i + 1}: write "longitude, latitude".` };
      out.push([parts[0]!, parts[1]!]);
    }
    points = out;
  }
  const ring = z.array(lonLat).safeParse(points);
  if (!ring.success) return { error: "Each point is [longitude, latitude] with longitude −180…180 and latitude −90…90." };
  const pts = ring.data;
  const first = pts[0];
  const last = pts.at(-1);
  const open = first && last && (first[0] !== last[0] || first[1] !== last[1]) ? [...pts, first] : pts;
  const distinct = new Set(open.map((p) => `${p[0]},${p[1]}`)).size;
  if (distinct < 3) return { error: "Enter at least three different points." };
  if (open.length > MAX_POLYGON_POINTS + 1) return { error: `At most ${MAX_POLYGON_POINTS} points.` };
  return { ring: open };
}

/** A free id derived from the name (`cat-lai-port`, `cat-lai-port-2`, …; at most 40 characters). */
export function regionIdFromName(name: string, taken: readonly string[]): string {
  const base =
    name
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-+|-+$/g, "")
      .slice(0, 32)
      .replace(/-+$/, "") || "region";
  const start = /^[a-z]/.test(base) ? base : `r-${base}`.slice(0, 32);
  let id = start;
  for (let n = 2; taken.includes(id); n++) id = `${start}-${n}`;
  return id;
}

export const NODE_TYPE_LABEL: Record<RegionNodeType, string> = { supplier: "Supplier", factory: "Factory", port: "Port", dc: "DC", store: "Store" };

export function regionSummary(r: WatchRegion): string {
  if (r.kind === "radius") return r.node ? `${r.radius_km ?? "?"} km around ${NODE_TYPE_LABEL[r.node.type].toLowerCase()} ${r.node.code}` : "Radius (no site)";
  const ring = r.polygon?.coordinates[0] ?? [];
  const closed = ring.length > 1 && ring[0]![0] === ring.at(-1)![0] && ring[0]![1] === ring.at(-1)![1];
  return `Polygon with ${ring.length - (closed ? 1 : 0)} points`;
}

// ───────────────────────────── alert rules ─────────────────────────────

export type AlertRule = SignalsAlertRule;
export const alertRuleSchema = signalsAlertRuleSchema.strip();

export const alertRulesSchema = z
  .object({ rules: z.array(z.unknown()), custom: z.boolean().optional(), hub_synced: z.boolean().optional() })
  .loose()
  .transform((v) => ({
    custom: v.custom ?? true,
    rules: v.rules.flatMap((r) => {
      const p = alertRuleSchema.safeParse(r);
      return p.success ? [p.data] : [];
    }),
    /** `false` after a save the hub didn't take yet (stored; pushed again after the next sync). */
    hubSynced: v.hub_synced ?? null,
  }));
export type AlertRules = z.infer<typeof alertRulesSchema>;

export const ALERT_ACTION_LABELS: Record<AlertRule["action"], string> = {
  alert: "Alert",
  alert_and_draft_case: "Alert and draft a case",
  ignore: "Ignore",
};

export const SIGNAL_KIND_OPTIONS = signalsAlertRuleSchema.shape.when.shape.kinds.element.options;
export const NODE_TYPE_OPTIONS = signalsAlertRuleSchema.shape.when.shape.node_types.element.options;

export const RULE_ID_PATTERN = /^[a-z][a-z0-9_-]*$/;

/** Basis points ↔ whole percent for the editor (contract values are integers 0…10,000). */
export const bpToPct = (bp: number) => Math.round(bp) / 100;
export const pctToBp = (pct: number) => Math.round(pct * 100);

export function newRule(existing: readonly AlertRule[]): AlertRule {
  let n = existing.length + 1;
  while (existing.some((r) => r.id === `rule-${n}`)) n++;
  return { id: `rule-${n}`, action: "alert", hysteresis_bp: 500, when: { kinds: [], node_types: [], min_severity_bp: 5000, min_confidence_bp: 5000, min_exposed_nodes: 1 } };
}

/** Problems per rule index (empty = can be saved). */
export function ruleProblems(rules: readonly AlertRule[]): Record<number, string[]> {
  const out: Record<number, string[]> = {};
  const add = (i: number, m: string) => (out[i] ??= []).push(m);
  const seen = new Map<string, number>();
  rules.forEach((r, i) => {
    if (!RULE_ID_PATTERN.test(r.id) || r.id.length > 64) add(i, "Id: lower-case letters, digits, - and _ (starts with a letter).");
    if (seen.has(r.id)) add(i, `Id "${r.id}" is used twice.`);
    seen.set(r.id, i);
    const bp = (v: number, label: string) => {
      if (!Number.isInteger(v) || v < 0 || v > 10_000) add(i, `${label}: between 0 % and 100 %.`);
    };
    bp(r.when.min_severity_bp, "Minimum severity");
    bp(r.when.min_confidence_bp, "Minimum confidence");
    bp(r.hysteresis_bp, "Hysteresis");
    if (!Number.isInteger(r.when.min_exposed_nodes) || r.when.min_exposed_nodes < 0) add(i, "Minimum exposed sites: a whole number, 0 or more.");
  });
  return out;
}
