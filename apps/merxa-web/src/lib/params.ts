/** Search-param helpers for server pages (`searchParams` values may be string | string[]). */

export type SearchParams = Record<string, string | string[] | undefined>;

/** First non-blank value, trimmed. */
export function param(sp: SearchParams, key: string): string | undefined {
  const raw = sp[key];
  const s = Array.isArray(raw) ? raw[0] : raw;
  return s && s.trim() ? s.trim() : undefined;
}

/** Value restricted to an allow-list (anything else → undefined). */
export function oneOf<T extends string>(sp: SearchParams, key: string, allowed: readonly T[]): T | undefined {
  const v = param(sp, key);
  return allowed.find((a) => a === v);
}

/** "PUF-W-BLK-M, puf-w-blk-l" → ["PUF-W-BLK-M", "PUF-W-BLK-L"] (upper-cased, de-duplicated). */
export function parseCodeList(value: string | undefined): string[] {
  if (!value) return [];
  return [
    ...new Set(
      value
        .split(/[\s,;]+/)
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
    ),
  ];
}

export type SkuQty = { sku: string; qty: number };

/**
 * Operator-guide line list `PUF-W-BLK-M:120,PUF-W-BLK-L:80` → [{sku, qty}]. Entries with a
 * missing SKU or a non-positive / non-integer quantity are reported in `invalid`.
 */
export function parseSkuQtyList(value: string | undefined): { lines: SkuQty[]; invalid: string[] } {
  const lines: SkuQty[] = [];
  const invalid: string[] = [];
  if (!value) return { lines, invalid };
  for (const raw of value.split(/[,;\n]+/)) {
    const entry = raw.trim();
    if (!entry) continue;
    const m = /^([A-Za-z0-9][A-Za-z0-9._-]*)\s*[:×x*]\s*(\d+)$/.exec(entry);
    const qty = m ? Number(m[2]) : NaN;
    if (!m || !Number.isSafeInteger(qty) || qty <= 0) {
      invalid.push(entry);
      continue;
    }
    lines.push({ sku: (m[1] as string).toUpperCase(), qty });
  }
  return { lines, invalid };
}
