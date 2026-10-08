/**
 * Unit-aware formatting for every number a block shows (`30-engine-api-events.md`: units are
 * explicit; money is integer cents). Locale is fixed (en-GB, EUR) so server and client render
 * identically and numbers match the narrative composed by the engine.
 */
import type { ValueUnit } from "@keelson/contracts";

export type Unit = ValueUnit;

export type FormatOptions = {
  /** Compact notation for large numbers ("€1.2M", "12.5K units"). */
  compact?: boolean | undefined;
  /** Always show a sign (deltas). */
  signed?: boolean | undefined;
  /** `eur_cents`: show non-zero cents ("€12,400.50") instead of whole euros. */
  exactCents?: boolean | undefined;
};

const LOCALE = "en-GB";
const MINUS = "−";

function withSign(text: string, value: number, signed: boolean | undefined): string {
  if (value < 0) return text.replace(/^-/, MINUS);
  if (signed && value > 0) return `+${text}`;
  return text;
}

function plural(n: number, one: string, many: string): string {
  return Math.abs(n) === 1 ? one : many;
}

function number(value: number, opts: FormatOptions, fractionDigits = 0): string {
  return new Intl.NumberFormat(LOCALE, {
    notation: opts.compact ? "compact" : "standard",
    maximumFractionDigits: opts.compact ? 1 : fractionDigits,
    minimumFractionDigits: 0,
  }).format(value);
}

/**
 * Format an integer value in its unit:
 * `eur_cents` → "€12,400" (whole euros, half away from zero, as in the engine's narratives;
 * `exactCents` keeps non-zero cents) · `units` → "3,200 units" ·
 * `days` → "3 days" · `pct_bp` (basis points) → "12.5%" · `count` → "12" ·
 * `kg_co2e` → "26,811 kg CO₂e".
 */
export function formatValue(value: number, unit: Unit | (string & {}), opts: FormatOptions = {}): string {
  if (!Number.isFinite(value)) return "—";
  switch (unit) {
    case "eur_cents": {
      const euros = value / 100;
      // Whole euros, rounded half away from zero, exactly like the narrative the engine composes
      // (`keelson_contracts.common.format_eur_cents`), so a tile never reads "€51,093.15" next to
      // "€51,093" in the text. `exactCents` shows cents when they are non-zero.
      const hasCents = !!opts.exactCents && !opts.compact && value % 100 !== 0;
      const text = new Intl.NumberFormat(LOCALE, {
        style: "currency",
        currency: "EUR",
        notation: opts.compact ? "compact" : "standard",
        minimumFractionDigits: hasCents ? 2 : 0,
        maximumFractionDigits: hasCents ? 2 : opts.compact ? 1 : 0,
      }).format(euros);
      return withSign(text, value, opts.signed);
    }
    case "units":
      return withSign(`${number(value, opts)} ${plural(value, "unit", "units")}`, value, opts.signed);
    case "days":
      return withSign(`${number(value, opts)} ${plural(value, "day", "days")}`, value, opts.signed);
    case "pct_bp":
      return withSign(`${number(value / 100, { ...opts, compact: false }, 2)}%`, value, opts.signed);
    case "count":
      return withSign(number(value, opts), value, opts.signed);
    case "kg_co2e":
      // M8: kilograms of CO₂-equivalent, as the engine writes them ("26,811 kg CO₂e")
      return withSign(`${number(value, opts)} kg CO₂e`, value, opts.signed);
    default:
      return withSign(`${number(value, opts)} ${unit}`.trim(), value, opts.signed);
  }
}

/** Signed change ("+€1,200", "−400 units"); zero → "No change". */
export function formatDelta(value: number, unit: Unit | (string & {}), opts: Omit<FormatOptions, "signed"> = {}): string {
  if (value === 0) return "No change";
  return formatValue(value, unit, { ...opts, signed: true });
}

const dateFmt = new Intl.DateTimeFormat(LOCALE, { day: "numeric", month: "short", year: "numeric", timeZone: "UTC" });
const dateTimeFmt = new Intl.DateTimeFormat(LOCALE, {
  day: "numeric",
  month: "short",
  year: "numeric",
  hour: "2-digit",
  minute: "2-digit",
  hour12: false,
  timeZone: "UTC",
});

/** "2026-10-05" → "5 Oct 2026". Invalid input is returned unchanged. */
export function formatDate(iso: string): string {
  const d = new Date(/^\d{4}-\d{2}-\d{2}$/.test(iso) ? `${iso}T00:00:00Z` : iso);
  return Number.isNaN(d.getTime()) ? iso : dateFmt.format(d);
}

/** RFC 3339 → "5 Oct 2026, 09:12 UTC" (UTC keeps SSR and client identical). */
export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : `${dateTimeFmt.format(d)} UTC`;
}

const OP_LABELS: Record<string, string> = {
  "transfer_order.create": "Create transfer order",
  "allocation.set_qty": "Change allocation",
  "po_line.reschedule": "Reschedule PO line",
  "po_line.change_qty": "Change PO line quantity",
  "po_line.cancel": "Cancel PO line",
  "po_line.split": "Split PO line",
  "po.create": "Create purchase order",
  "capacity_booking.create": "Book capacity",
  "production_order.resequence": "Resequence production",
  "promotion.reschedule": "Reschedule promotion",
  "online_order.repromise": "Re-promise online order",
};

/** Plain-language label for a change-set operation ("transfer_order.create" → "Create transfer order"). */
export function opLabel(op: string): string {
  const known = OP_LABELS[op];
  if (known) return known;
  const [entity = op, verb = ""] = op.split(".");
  const words = `${verb.replace(/_/g, " ")} ${entity.replace(/_/g, " ")}`.trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}
