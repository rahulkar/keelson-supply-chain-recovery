/** ISO week helpers (`2026-W45`) for the stockout timeline. */

const ISO_WEEK = /^(\d{4})-W(\d{2})$/;

/** Monday of an ISO week (UTC). `null` for malformed input. */
export function isoWeekMonday(week: string): Date | null {
  const m = ISO_WEEK.exec(week);
  if (!m) return null;
  const year = Number(m[1]);
  const w = Number(m[2]);
  // 4 January is always in week 1.
  const jan4 = new Date(Date.UTC(year, 0, 4));
  const day = jan4.getUTCDay() || 7;
  const monday = new Date(jan4);
  monday.setUTCDate(jan4.getUTCDate() - day + 1 + (w - 1) * 7);
  return monday;
}

/** Whole weeks from `a` to `b` (positive = `b` later). `null` if either is not an ISO week. */
export function weeksBetween(a: string, b: string): number | null {
  const da = isoWeekMonday(a);
  const db = isoWeekMonday(b);
  if (!da || !db) return null;
  return Math.round((db.getTime() - da.getTime()) / (7 * 86_400_000));
}

/** "W45" (short axis label). */
export function shortWeek(week: string): string {
  const m = ISO_WEEK.exec(week);
  return m ? `W${m[2]}` : week;
}

/** "Week 45 (2 Nov)" — readable label with the Monday's date. */
export function weekLabel(week: string): string {
  const m = ISO_WEEK.exec(week);
  const monday = isoWeekMonday(week);
  if (!m || !monday) return week;
  const date = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", timeZone: "UTC" }).format(monday);
  return `Week ${Number(m[2])} (${date})`;
}

/**
 * How the first breach moves under an option compared with doing nothing:
 * - `avoided`: breach without action, none with the option;
 * - `later` / `earlier` with `weeks`;
 * - `same`; `new` (no breach before, one with the option); `none` (no breach either way).
 */
export type BreachShift =
  | { kind: "avoided" }
  | { kind: "later"; weeks: number }
  | { kind: "earlier"; weeks: number }
  | { kind: "same" }
  | { kind: "new" }
  | { kind: "none" };

export function breachShift(baseline: string | null, withOption: string | null): BreachShift {
  if (baseline === null && withOption === null) return { kind: "none" };
  if (baseline !== null && withOption === null) return { kind: "avoided" };
  if (baseline === null) return { kind: "new" };
  const w = weeksBetween(baseline, withOption as string);
  if (w === null || w === 0) return { kind: "same" };
  return w > 0 ? { kind: "later", weeks: w } : { kind: "earlier", weeks: -w };
}

export function breachShiftText(shift: BreachShift): string {
  switch (shift.kind) {
    case "avoided":
      return "Stock-out avoided";
    case "later":
      return `${shift.weeks} ${shift.weeks === 1 ? "week" : "weeks"} later`;
    case "earlier":
      return `${shift.weeks} ${shift.weeks === 1 ? "week" : "weeks"} earlier`;
    case "same":
      return "No change";
    case "new":
      return "New stock-out";
    case "none":
      return "No stock-out";
  }
}
