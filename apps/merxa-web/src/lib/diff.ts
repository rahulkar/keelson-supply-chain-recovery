/**
 * Before/after diff of a change-set item (`before` / `after` written by `:validate`).
 * Nested objects flatten to dotted paths; arrays of records are keyed by `sku` or
 * `line_no` so transfer-order lines line up per SKU.
 */

export type DiffRow = {
  /** Stable key, e.g. `lines[PUF-W-BLK-M].from_atp`. */
  path: string;
  /** Human label, e.g. "PUF-W-BLK-M · from ATP". */
  label: string;
  before: unknown;
  after: unknown;
  changed: boolean;
};

type Flat = Map<string, { label: string; value: unknown }>;

const FIELD_LABELS: Record<string, string> = {
  qty: "qty",
  qty_ordered: "qty ordered",
  qty_received: "qty received",
  qty_open: "qty open",
  from_atp: "from-DC ATP",
  to_in_transit: "to-DC in transit",
  eta_dc: "ETA DC",
  eta_date: "ETA",
  ship_date: "ship date",
  ship_week: "ship week",
  dc_code: "DC",
  store_code: "store",
  from_dc: "from DC",
  to_dc: "to DC",
  version: "version",
};

function fieldLabel(key: string): string {
  return FIELD_LABELS[key] ?? key.replace(/_/g, " ");
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function recordKey(v: Record<string, unknown>, index: number): string {
  if (typeof v.sku === "string") return v.sku;
  if (typeof v.line_no === "number") return `line ${v.line_no}`;
  if (typeof v.code === "string") return v.code;
  return `#${index + 1}`;
}

function flatten(value: unknown, path: string, label: string, out: Flat): void {
  if (Array.isArray(value)) {
    if (value.every(isRecord)) {
      value.forEach((rec, i) => {
        const key = recordKey(rec, i);
        for (const [k, v] of Object.entries(rec)) {
          if (k === "sku" || k === "line_no") continue;
          flatten(v, `${path}[${key}].${k}`, `${key} · ${fieldLabel(k)}`, out);
        }
      });
      return;
    }
    out.set(path, { label, value: value.join(", ") });
    return;
  }
  if (isRecord(value)) {
    for (const [k, v] of Object.entries(value)) {
      const childPath = path ? `${path}.${k}` : k;
      // Nested headers (e.g. after.transfer_order.*) keep their parent as a prefix.
      const childLabel = path && !path.endsWith("]") ? `${label} ${fieldLabel(k)}` : fieldLabel(k);
      flatten(v, childPath, childLabel, out);
    }
    return;
  }
  out.set(path, { label, value });
}

function flat(value: unknown): Flat {
  const out: Flat = new Map();
  if (value === null || value === undefined) return out;
  flatten(value, "", "", out);
  return out;
}

function same(a: unknown, b: unknown): boolean {
  return JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
}

/**
 * Field-level diff. Rows keep the order in which fields first appear (before, then any
 * fields only present after). Fields equal on both sides are kept with `changed: false`
 * so reviewers see the context (SKU, store, …); `onlyChanged` drops them.
 */
export function diffItem(before: unknown, after: unknown, opts: { onlyChanged?: boolean } = {}): DiffRow[] {
  const b = flat(before);
  const a = flat(after);
  const paths = [...b.keys(), ...[...a.keys()].filter((k) => !b.has(k))];
  const rows: DiffRow[] = [];
  for (const path of paths) {
    const bv = b.get(path);
    const av = a.get(path);
    // `transfer_order: null` before vs. a nested header after: the children carry the diff.
    if ((bv?.value ?? null) === null && (av?.value ?? null) === null && paths.some((p) => p.startsWith(`${path}.`))) {
      continue;
    }
    const row: DiffRow = {
      path,
      label: (av ?? bv)?.label || path,
      before: bv?.value ?? null,
      after: av?.value ?? null,
      changed: !same(bv?.value, av?.value),
    };
    if (opts.onlyChanged && !row.changed) continue;
    rows.push(row);
  }
  return rows;
}

/**
 * One-line summary of an audit row's change: "reserved 120 → 220; atp 300 → 200".
 * `format` renders individual values (defaults to JSON-ish strings).
 */
export function summarizeChange(
  before: unknown,
  after: unknown,
  format: (v: unknown) => string = (v) => (v === null || v === undefined ? "∅" : typeof v === "string" ? v : JSON.stringify(v)),
  maxParts = 6,
): string {
  const rows = diffItem(before, after, { onlyChanged: true }).filter((r) => !r.path.endsWith("version"));
  if (rows.length === 0) return "";
  const parts = rows.slice(0, maxParts).map((r) => `${r.label} ${format(r.before)} → ${format(r.after)}`);
  if (rows.length > maxParts) parts.push(`+${rows.length - maxParts} more`);
  return parts.join("; ");
}

/** Numeric delta for a diff row when both sides are integers, else null. */
export function numericDelta(row: Pick<DiffRow, "before" | "after">): number | null {
  const { before, after } = row;
  if (typeof after !== "number") return null;
  if (before === null || before === undefined) return after;
  return typeof before === "number" ? after - before : null;
}
