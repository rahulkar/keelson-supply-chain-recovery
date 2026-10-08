/**
 * Structural diff of two JSON values, for "what changed between versions" views.
 *
 * - objects are compared key by key (paths like `policy.auto_max_lines`);
 * - arrays of objects that all carry an identity key (`slug`, `id`, `name`, `alias`, `key`) are
 *   matched by it (`specialists[inventory_demand].tools`), other arrays by position — except
 *   arrays of primitives, which are compared as a whole and report `added` / `removed` members
 *   (tool allowlists read as "+ list_items, − get_entity", not as shifted indexes).
 */
export type JsonValue = null | boolean | number | string | JsonValue[] | { [key: string]: JsonValue };

export type DiffKind = "added" | "removed" | "changed";

export type DiffEntry = {
  /** Human path (`a.b[slug].c`, `[0]` for positional items). */
  path: string;
  kind: DiffKind;
  before?: unknown;
  after?: unknown;
  /** For arrays of primitives: members only in `after` / only in `before`. */
  added?: unknown[] | undefined;
  removed?: unknown[] | undefined;
};

const ID_KEYS = ["slug", "id", "name", "alias", "key"] as const;

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isPrimitive(v: unknown): boolean {
  return v === null || ["string", "number", "boolean"].includes(typeof v);
}

/** Deep equality for JSON-like values (key order ignored). */
export function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => jsonEqual(x, b[i]));
  if (isPlainObject(a) && isPlainObject(b)) {
    const ka = Object.keys(a).filter((k) => a[k] !== undefined);
    const kb = Object.keys(b).filter((k) => b[k] !== undefined);
    return ka.length === kb.length && ka.every((k) => Object.prototype.hasOwnProperty.call(b, k) && jsonEqual(a[k], b[k]));
  }
  return false;
}

function joinPath(base: string, key: string): string {
  return /^[A-Za-z_$][\w$]*$/.test(key) ? (base ? `${base}.${key}` : key) : `${base}[${JSON.stringify(key)}]`;
}

function identityKey(before: unknown[], after: unknown[]): string | null {
  const items = [...before, ...after];
  if (items.length === 0 || !items.every(isPlainObject)) return null;
  for (const k of ID_KEYS) {
    const values = (items as Record<string, unknown>[]).map((o) => o[k]);
    const unique = (arr: unknown[]) => new Set((arr as Record<string, unknown>[]).map((o) => o[k])).size === arr.length;
    if (values.every((v) => typeof v === "string" || typeof v === "number") && unique(before) && unique(after)) return k;
  }
  return null;
}

function walk(before: unknown, after: unknown, path: string, out: DiffEntry[]): void {
  if (jsonEqual(before, after)) return;
  if (before === undefined) {
    out.push({ path, kind: "added", after });
    return;
  }
  if (after === undefined) {
    out.push({ path, kind: "removed", before });
    return;
  }
  if (isPlainObject(before) && isPlainObject(after)) {
    const keys = [...new Set([...Object.keys(before), ...Object.keys(after)])].sort();
    for (const k of keys) walk(before[k], after[k], joinPath(path, k), out);
    return;
  }
  if (Array.isArray(before) && Array.isArray(after)) {
    if ([...before, ...after].every(isPrimitive)) {
      const added = after.filter((x) => !before.some((y) => y === x));
      const removed = before.filter((x) => !after.some((y) => y === x));
      out.push({ path: path || "(root)", kind: "changed", before, after, added, removed });
      return;
    }
    const idKey = identityKey(before, after);
    if (idKey) {
      const byId = (arr: unknown[]) => new Map((arr as Record<string, unknown>[]).map((o) => [String(o[idKey]), o]));
      const b = byId(before);
      const a = byId(after);
      const ids = [...new Set([...b.keys(), ...a.keys()])];
      for (const id of ids) walk(b.get(id), a.get(id), `${path}[${id}]`, out);
      return;
    }
    const n = Math.max(before.length, after.length);
    for (let i = 0; i < n; i++) walk(before[i], after[i], `${path}[${i}]`, out);
    return;
  }
  out.push({ path: path || "(root)", kind: "changed", before, after });
}

/** Every difference between `before` and `after`, in a stable order. */
export function diffJson(before: unknown, after: unknown): DiffEntry[] {
  const out: DiffEntry[] = [];
  walk(before, after, "", out);
  return out;
}

/** Compact, single-line rendering of a JSON value for diff cells. */
export function formatJsonValue(v: unknown, max = 160): string {
  if (v === undefined) return "—";
  const s = typeof v === "string" ? JSON.stringify(v) : JSON.stringify(v);
  if (s === undefined) return String(v);
  return s.length > max ? `${s.slice(0, max - 1)}…` : s;
}
