/**
 * Display formatting. Everything is deterministic (fixed locale + time zone) so server
 * and client renders agree.
 */

export const LOCALE = "en-GB";
/** Merxa Systems is a European ERP; timestamps are shown in UK time on server and client alike. */
export const DISPLAY_TIME_ZONE = "Europe/London";

const intFmt = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0 });
const signedIntFmt = new Intl.NumberFormat(LOCALE, { maximumFractionDigits: 0, signDisplay: "exceptZero" });
const eurFmt = new Intl.NumberFormat(LOCALE, { style: "currency", currency: "EUR", maximumFractionDigits: 2 });
const eurWholeFmt = new Intl.NumberFormat(LOCALE, { style: "currency", currency: "EUR", maximumFractionDigits: 0 });
const dateFmt = new Intl.DateTimeFormat(LOCALE, { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
const dateTimeFmt = new Intl.DateTimeFormat(LOCALE, {
  day: "2-digit",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: DISPLAY_TIME_ZONE,
});

const EMPTY = "—";

export function isNil(value: unknown): value is null | undefined {
  return value === null || value === undefined;
}

/** Integer quantity with thousands separators: 12400 → "12,400". */
export function formatInt(value: number | null | undefined): string {
  if (isNil(value) || Number.isNaN(value)) return EMPTY;
  return intFmt.format(value);
}

/** Signed delta: 5 → "+5", -3 → "-3", 0 → "0". */
export function formatDelta(value: number | null | undefined): string {
  if (isNil(value) || Number.isNaN(value)) return EMPTY;
  return signedIntFmt.format(value);
}

/** Integer EUR cents → "€12,400" (whole euros) or "€12.50". */
export function formatCents(cents: number | null | undefined): string {
  if (isNil(cents) || Number.isNaN(cents)) return EMPTY;
  return cents % 100 === 0 ? eurWholeFmt.format(cents / 100) : eurFmt.format(cents / 100);
}

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;

function parseIsoDate(value: string): Date | null {
  const m = ISO_DATE.exec(value);
  if (!m) return null;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return Number.isNaN(d.getTime()) ? null : d;
}

/** `2026-10-29` → "29 Oct 2026" (calendar date, no time-zone shift). */
export function formatDate(value: string | null | undefined): string {
  if (!value) return EMPTY;
  const d = parseIsoDate(value);
  return d ? dateFmt.format(d) : value;
}

/** RFC 3339 timestamp → "06 Oct 2026, 14:03" in UK time. */
export function formatDateTime(value: string | null | undefined): string {
  if (!value) return EMPTY;
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value : dateTimeFmt.format(d);
}

/** ISO-8601 week of a calendar date: `2026-10-29` → "2026-W44". */
export function isoWeek(value: string | null | undefined): string {
  if (!value) return EMPTY;
  const d = parseIsoDate(value);
  if (!d) return EMPTY;
  const day = d.getUTCDay() || 7; // Mon=1..Sun=7
  const thursday = new Date(d);
  thursday.setUTCDate(d.getUTCDate() + 4 - day);
  const year = thursday.getUTCFullYear();
  const yearStart = Date.UTC(year, 0, 1);
  const week = Math.ceil(((thursday.getTime() - yearStart) / 86_400_000 + 1) / 7);
  return `${year}-W${String(week).padStart(2, "0")}`;
}

/** "3 min ago", "just now", "2 h ago", "4 d ago" relative to `now` (ms epoch). */
export function formatRelative(value: string | null | undefined, now: number): string {
  if (!value) return EMPTY;
  const t = new Date(value).getTime();
  if (Number.isNaN(t)) return value;
  const diffS = Math.round((now - t) / 1000);
  if (diffS < 0) return "just now";
  if (diffS < 45) return "just now";
  const min = Math.round(diffS / 60);
  if (min < 60) return `${min} min ago`;
  const h = Math.round(min / 60);
  if (h < 24) return `${h} h ago`;
  const d = Math.round(h / 24);
  return `${d} d ago`;
}

/** Shortens a long hash for display: first 10 + last 6 hex chars. */
export function shortHash(value: string | null | undefined): string {
  if (!value) return EMPTY;
  return value.length <= 20 ? value : `${value.slice(0, 10)}…${value.slice(-6)}`;
}

/** `in_production` → "In production". */
export function humanize(value: string | null | undefined): string {
  if (!value) return EMPTY;
  const s = value.replace(/[_-]+/g, " ").trim();
  return s.charAt(0).toUpperCase() + s.slice(1);
}

const OP_LABELS: Record<string, string> = {
  "transfer_order.create": "Create transfer order",
  "allocation.set_qty": "Set allocation quantity",
  "po_line.reschedule": "Reschedule PO line",
  "po_line.change_qty": "Change PO line quantity",
  "po_line.cancel": "Cancel PO line",
  "po_line.split": "Split PO line",
  "po.create": "Create purchase order",
  "capacity_booking.create": "Book lane capacity",
  "production_order.resequence": "Resequence production line",
  "promotion.reschedule": "Reschedule promotion",
  "online_order.repromise": "Re-promise online order",
};

/** Basis points → percent: 2500 → "25%", 1250 → "12.5%". */
export function formatBp(bp: number | null | undefined): string {
  if (isNil(bp) || Number.isNaN(bp)) return EMPTY;
  const pct = bp / 100;
  return `${Number.isInteger(pct) ? pct : pct.toFixed(2).replace(/0+$/, "")}%`;
}

const BASIS_UNIT: Record<string, string> = { per_unit: "unit", per_kg: "kg", per_teu: "TEU" };

/** Unit of a rate-card basis: per_unit → "unit", per_teu → "TEU". */
export function basisUnit(basis: string | null | undefined): string {
  if (!basis) return "";
  return BASIS_UNIT[basis] ?? basis.replace(/^per_/, "");
}

/**
 * Integer minor units in an ISO 4217 currency (rate cards carry their own `currency`):
 * EUR (or no currency) → `formatCents`; others → "US$12.50"; a malformed code → "12.50 XXX".
 */
export function formatMoney(cents: number | null | undefined, currency?: string | null): string {
  if (isNil(cents) || Number.isNaN(cents)) return EMPTY;
  if (!currency || currency === "EUR") return formatCents(cents);
  try {
    return new Intl.NumberFormat(LOCALE, {
      style: "currency",
      currency,
      maximumFractionDigits: cents % 100 === 0 ? 0 : 2,
    }).format(cents / 100);
  } catch {
    return `${(cents / 100).toFixed(2)} ${currency}`;
  }
}

/** Rate in its basis: (145, "per_unit") → "€1.45 / unit"; (380000, "per_teu") → "€3,800 / TEU". */
export function formatRate(
  cents: number | null | undefined,
  basis: string | null | undefined,
  currency?: string | null,
): string {
  if (isNil(cents)) return EMPTY;
  const unit = basisUnit(basis);
  const money = formatMoney(cents, currency);
  return unit ? `${money} / ${unit}` : money;
}

const MODE_LABELS: Record<string, string> = { sea: "Sea", air: "Air", sea_air: "Sea-air", road: "Road" };

/** Transport mode label: sea_air → "Sea-air". */
export function modeLabel(mode: string | null | undefined): string {
  if (!mode) return EMPTY;
  return MODE_LABELS[mode] ?? humanize(mode);
}

/** Change-set operation code → business label (unknown ops are shown verbatim). */
export function opLabel(op: string): string {
  return OP_LABELS[op] ?? op;
}

/** Any scalar or structure → short display text for diff cells. */
export function formatValue(value: unknown): string {
  if (isNil(value)) return EMPTY;
  if (typeof value === "number") return Number.isInteger(value) ? formatInt(value) : String(value);
  if (typeof value === "boolean") return value ? "Yes" : "No";
  if (typeof value === "string") return ISO_DATE.test(value) ? formatDate(value) : value;
  return JSON.stringify(value);
}
