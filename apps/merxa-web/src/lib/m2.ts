/**
 * Pure helpers for the M2 pages (logistics capacity, production calendar strip, webhook
 * delivery states, API key scopes). No React, no I/O: unit-tested in m2.test.ts.
 */
import type {
  CapacityBooking,
  Lane,
  ProductionOrder,
  Promotion,
  RateCard,
  WebhookDelivery,
} from "./types";

// ───────────────────────── ISO dates and weeks ─────────────────────────

const ISO_DATE = /^(\d{4})-(\d{2})-(\d{2})$/;
const ISO_WEEK = /^(\d{4})-W(\d{2})$/;
const DAY_MS = 86_400_000;

function utcDate(value: string): Date | null {
  const m = ISO_DATE.exec(value);
  if (!m) return null;
  const d = new Date(Date.UTC(Number(m[1]), Number(m[2]) - 1, Number(m[3])));
  return Number.isNaN(d.getTime()) || d.toISOString().slice(0, 10) !== value ? null : d;
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

export function isIsoDate(value: string | null | undefined): value is string {
  return !!value && utcDate(value) !== null;
}

export function isIsoWeek(value: string | null | undefined): value is string {
  return !!value && isoWeekMonday(value) !== null;
}

/** `2026-W41` → "2026-10-05" (Monday of that ISO week); null for malformed / out-of-range weeks. */
export function isoWeekMonday(week: string): string | null {
  const m = ISO_WEEK.exec(week);
  if (!m) return null;
  const year = Number(m[1]);
  const w = Number(m[2]);
  if (w < 1 || w > 53) return null;
  const jan4 = new Date(Date.UTC(year, 0, 4));
  const monday1 = jan4.getTime() - ((jan4.getUTCDay() || 7) - 1) * DAY_MS;
  const monday = new Date(monday1 + (w - 1) * 7 * DAY_MS);
  // Week 53 exists only in long years: its Thursday must still be in `year`.
  if (new Date(monday.getTime() + 3 * DAY_MS).getUTCFullYear() !== year) return null;
  return isoDate(monday);
}

/** ISO week of an ISO date: "2026-10-05" → "2026-W41". */
export function weekOf(day: string): string | null {
  const d = utcDate(day);
  if (!d) return null;
  const dow = d.getUTCDay() || 7;
  const thursday = new Date(d.getTime() + (4 - dow) * DAY_MS);
  const year = thursday.getUTCFullYear();
  const week = Math.floor((thursday.getTime() - Date.UTC(year, 0, 1)) / DAY_MS / 7) + 1;
  return `${year}-W${String(week).padStart(2, "0")}`;
}

/** `n` consecutive ISO weeks starting at `start` (crosses year ends). */
export function weekRange(start: string, n: number): string[] {
  const monday = isoWeekMonday(start);
  if (!monday) return [];
  const base = (utcDate(monday) as Date).getTime();
  const out: string[] = [];
  for (let i = 0; i < n; i += 1) out.push(weekOf(isoDate(new Date(base + i * 7 * DAY_MS))) as string);
  return out;
}

/** Inclusive list of ISO dates from `from` to `to` (empty when malformed or reversed; capped at 400). */
export function daysBetween(from: string, to: string): string[] {
  const a = utcDate(from);
  const b = utcDate(to);
  if (!a || !b || b < a) return [];
  const out: string[] = [];
  for (let t = a.getTime(); t <= b.getTime() && out.length < 400; t += DAY_MS) out.push(isoDate(new Date(t)));
  return out;
}

/** `date + n days` as an ISO date (null when malformed). */
export function addDays(day: string, n: number): string | null {
  const d = utcDate(day);
  return d ? isoDate(new Date(d.getTime() + n * DAY_MS)) : null;
}

// ───────────────────────── lane capacity ─────────────────────────

/** Booking statuses that consume weekly lane capacity (mirrors merxa-api `ACTIVE_BOOKING`). */
export const ACTIVE_BOOKING_STATUSES: readonly string[] = ["held", "confirmed"];

export type LaneWeekCapacity = {
  week: string;
  /** Rate card valid on the Monday of `week`, or null (no capacity offered). */
  rate: RateCard | null;
  capacity: number | null;
  /** Units booked (per-unit lanes only; other bases are not booked in units). */
  booked: number;
  remaining: number | null;
};

/**
 * The rate card of `laneCode` valid on `day` (inclusive validity). A null bound is an
 * unbounded `daterange` side (merxa-api returns `lower()` / `upper() - 1`, both null then),
 * which `valid @> day` treats as open-ended.
 */
export function rateOn(rateCards: readonly RateCard[], laneCode: string, day: string): RateCard | null {
  return (
    rateCards.find(
      (r) => r.lane_code === laneCode && (r.valid_from == null || r.valid_from <= day) && (r.valid_to == null || day <= r.valid_to),
    ) ?? null
  );
}

/**
 * Weekly capacity of a lane, computed like merxa-api `remaining_capacity()`: the rate card
 * valid on the week's Monday gives the capacity; for per-unit lanes, held and confirmed
 * per-unit bookings of that week are deducted.
 */
export function laneWeekCapacity(
  laneCode: string,
  week: string,
  rateCards: readonly RateCard[],
  bookings: readonly CapacityBooking[],
): LaneWeekCapacity {
  const monday = isoWeekMonday(week);
  const rate = monday ? rateOn(rateCards, laneCode, monday) : null;
  const booked = bookings
    .filter(
      (b) =>
        b.lane_code === laneCode &&
        b.iso_week === week &&
        b.basis === "per_unit" &&
        ACTIVE_BOOKING_STATUSES.includes(b.status),
    )
    .reduce((s, b) => s + b.qty_units, 0);
  if (!rate) return { week, rate: null, capacity: null, booked, remaining: null };
  const remaining = rate.basis === "per_unit" ? rate.weekly_capacity - booked : rate.weekly_capacity;
  return { week, rate, capacity: rate.weekly_capacity, booked, remaining };
}

export type CapacityRow = { lane: Lane; weeks: LaneWeekCapacity[] };

/** Capacity matrix (lanes × weeks); lanes without any rate card in the window are dropped. */
export function capacityMatrix(
  lanes: readonly Lane[],
  weeks: readonly string[],
  rateCards: readonly RateCard[],
  bookings: readonly CapacityBooking[],
): CapacityRow[] {
  return lanes
    .map((lane) => ({ lane, weeks: weeks.map((w) => laneWeekCapacity(lane.code, w, rateCards, bookings)) }))
    .filter((row) => row.weeks.some((w) => w.rate !== null));
}

export type Utilisation = "ok" | "tight" | "full" | "none";

/** ≥ 100 % used → full, ≥ 80 % → tight. */
export function utilisation(c: Pick<LaneWeekCapacity, "capacity" | "remaining">): Utilisation {
  if (c.capacity === null || c.remaining === null) return "none";
  if (c.capacity <= 0 || c.remaining <= 0) return "full";
  return (c.capacity - c.remaining) / c.capacity >= 0.8 ? "tight" : "ok";
}

// ───────────────────────── production calendar strip ─────────────────────────

export type PlacedOrder = {
  order: ProductionOrder;
  /** 1-based grid column of the first visible day. */
  colStart: number;
  /** Exclusive end column (CSS grid `grid-column: start / end`). */
  colEnd: number;
  /** 1-based track (row) so overlapping orders never cover each other. */
  track: number;
  clippedStart: boolean;
  clippedEnd: boolean;
};

/**
 * Places production orders on a day strip `days` (inclusive ISO dates): clips them to the
 * window and packs overlapping orders onto separate tracks (greedy, by start then seq).
 */
export function placeOrders(orders: readonly ProductionOrder[], days: readonly string[]): PlacedOrder[] {
  if (days.length === 0) return [];
  const first = days[0] as string;
  const last = days[days.length - 1] as string;
  const index = new Map(days.map((d, i) => [d, i]));
  const visible = orders
    .filter((o) => o.planned_end >= first && o.planned_start <= last)
    .sort((a, b) => a.planned_start.localeCompare(b.planned_start) || a.seq - b.seq || a.code.localeCompare(b.code));
  const trackEnds: string[] = []; // last visible day per track
  const placed: PlacedOrder[] = [];
  for (const order of visible) {
    const start = order.planned_start < first ? first : order.planned_start;
    const end = order.planned_end > last ? last : order.planned_end;
    let track = trackEnds.findIndex((e) => e < start);
    if (track === -1) {
      trackEnds.push(end);
      track = trackEnds.length - 1;
    } else {
      trackEnds[track] = end;
    }
    placed.push({
      order,
      colStart: (index.get(start) ?? 0) + 1,
      colEnd: (index.get(end) ?? days.length - 1) + 2,
      track: track + 1,
      clippedStart: order.planned_start < first,
      clippedEnd: order.planned_end > last,
    });
  }
  return placed;
}

/**
 * Keeps orders on the first `maxTracks` tracks and counts, per day column (0-based), the
 * orders left out, so a day with dozens of short orders does not make the strip huge.
 */
export function limitTracks(
  placed: readonly PlacedOrder[],
  maxTracks: number,
  dayCount: number,
): { visible: PlacedOrder[]; hiddenPerDay: number[] } {
  const hiddenPerDay = Array.from({ length: dayCount }, () => 0);
  const visible: PlacedOrder[] = [];
  for (const p of placed) {
    if (p.track <= maxTracks) {
      visible.push(p);
      continue;
    }
    for (let col = p.colStart; col < p.colEnd; col += 1) {
      if (col - 1 < dayCount) hiddenPerDay[col - 1] = (hiddenPerDay[col - 1] ?? 0) + 1;
    }
  }
  return { visible, hiddenPerDay };
}

/** `PO-BD-0001#3` → { po: "PO-BD-0001", line: 3 } (null when not a PO line reference). */
export function parsePoLineRef(ref: string | null | undefined): { po: string; line: number } | null {
  if (!ref) return null;
  const m = /^([^#\s]+)#(\d+)$/.exec(ref);
  if (!m) return null;
  const line = Number(m[2]);
  return Number.isSafeInteger(line) && line > 0 ? { po: m[1] as string, line } : null;
}

/** In-app link to the PO record with the line highlighted (or null). */
export function poLineHref(ref: string | null | undefined): string | null {
  const p = parsePoLineRef(ref);
  return p ? `/purchase-orders/${encodeURIComponent(p.po)}?line=${p.line}` : null;
}

// ───────────────────────── API keys and webhooks ─────────────────────────

/** Scopes merxa-api lets an API key carry (`services/apikeys.py` ALLOWED_SCOPES). */
export const API_KEY_SCOPES: readonly { scope: string; label: string; write?: boolean }[] = [
  { scope: "catalog:read", label: "Catalogue (styles, SKUs, promotions)" },
  { scope: "inventory:read", label: "Inventory, forecast, allocations" },
  { scope: "po:read", label: "Purchase orders, logistics, production" },
  { scope: "orders:read", label: "Online orders" },
  { scope: "snapshot:read", label: "Canonical snapshots (scn/1.0)" },
  { scope: "changes:preview", label: "Prepare and validate change batches", write: true },
  { scope: "changes:apply", label: "Apply change batches / write via /mcp-lite", write: true },
];

/** Event types a subscription can ask for (`services/events.py` WEBHOOK_EVENTS). */
export const WEBHOOK_EVENT_TYPES: readonly string[] = [
  "change_set.applied",
  "change_set.rolled_back",
  "po_line.eta_changed",
  "supplier_message.received",
  "inventory.adjusted",
  "sailing.updated",
  // M5 (docs/contracts/95): manual changes in Merxa (Guide-mode read-back) and lane capacity
  "transfer_order.created",
  "allocation.updated",
  "po_line.updated",
  "capacity_booking.created",
  "capacity_booking.cancelled",
  "lane.capacity_changed",
];

/** Display form of a key that is never shown again: `mx_ab12cd34_••••`. */
export function maskedKey(prefix: string): string {
  return `mx_${prefix}_••••••••`;
}

/**
 * Display form of a subscription endpoint: a password in the URL's userinfo
 * (`https://user:secret@host/…`) is masked so it is not left on screen; the rest is kept
 * verbatim (it is rendered as text, never as a link).
 */
export function displayWebhookUrl(url: string): string {
  const m = /^([A-Za-z][A-Za-z0-9+.-]*:\/\/)([^/?#\\]*)(.*)$/s.exec(url);
  if (!m) return url;
  const [, scheme, authority = "", rest = ""] = m;
  const at = authority.lastIndexOf("@");
  if (at < 0) return url;
  const userinfo = authority.slice(0, at);
  const colon = userinfo.indexOf(":");
  if (colon < 0 || colon === userinfo.length - 1) return url;
  return `${scheme}${userinfo.slice(0, colon)}:••••••${authority.slice(at)}${rest}`;
}

/**
 * Subscriptions as handed to the client component: the endpoint is replaced by its display
 * form on the server, so a URL password never reaches the browser (props of a client
 * component are serialised into the page's RSC payload, not only the rendered cell).
 */
export function redactSubscriptionUrls<T extends { url: string }>(subs: readonly T[]): T[] {
  return subs.map((s) => ({ ...s, url: displayWebhookUrl(s.url) }));
}

/** Client-side pre-check of a subscription URL (merxa-api validates authoritatively). */
export function webhookUrlProblem(url: string): string | null {
  const value = url.trim();
  if (!value) return "Enter the endpoint URL.";
  if (value.length > 2048) return "The URL is longer than 2,048 characters.";
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    return "Enter an absolute http(s) URL.";
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") return "Only http and https URLs are supported.";
  if (!parsed.hostname) return "The URL needs a host name.";
  return null;
}

// ───────────────────────── online orders ─────────────────────────

/** Order statuses whose stock reservation can still fail the promise (merxa-api `at_risk=true` filter). */
export const OPEN_ORDER_STATUSES: readonly string[] = ["open", "picking"];

/**
 * At risk like merxa-api's `?at_risk=true` filter: some ordered units are not reserved
 * **and** the order is still open or picking. The per-item `at_risk` flag of the listing is
 * only `shortfall > 0`, which would also flag shipped, delivered and cancelled orders.
 */
export function isOrderAtRisk(o: { status: string; shortfall: number }): boolean {
  return o.shortfall > 0 && OPEN_ORDER_STATUSES.includes(o.status);
}

// ───────────────────────── promotions ─────────────────────────

export type PromotionDisplay = Promotion & { days: number | null; weeks: string };

/** Display row of a promotion; an unbounded period side reads "open-ended" and has no day count. */
export function promotionRow(p: Promotion): PromotionDisplay {
  const start = p.period_start ? utcDate(p.period_start) : null;
  const end = p.period_end ? utcDate(p.period_end) : null;
  const days = start && end ? Math.round((end.getTime() - start.getTime()) / DAY_MS) + 1 : null;
  const a = p.period_start ? (weekOf(p.period_start) ?? p.period_start) : "open-ended";
  const b = p.period_end ? (weekOf(p.period_end) ?? p.period_end) : "open-ended";
  return { ...p, days, weeks: a === b ? a : `${a} – ${b}` };
}

/** Promotions by start (an unbounded start first), then code. */
export function sortPromotions(rows: readonly Promotion[]): Promotion[] {
  return [...rows].sort((x, y) => (x.period_start ?? "").localeCompare(y.period_start ?? "") || x.code.localeCompare(y.code));
}

// ───────────────────────── production schedule window ─────────────────────────

/** Length of merxa-api's default schedule window (today − 7 … today + 42). */
export const SCHEDULE_DEFAULT_SPAN_DAYS = 49;
/** merxa-api rejects windows longer than this. */
export const SCHEDULE_MAX_SPAN_DAYS = 366;

/**
 * The `from` / `to` to request. merxa-api fills a missing side from the simulation clock,
 * so a lone `from` after today + 42 (or a lone `to` before today − 7) would be rejected
 * as reversed: a lone side is completed with the default span instead. A reversed pair is
 * swapped and an over-long one is cut to the API maximum.
 */
export function scheduleWindow(from: string | undefined, to: string | undefined): { from?: string; to?: string } {
  if (from && !to) return { from, to: addDays(from, SCHEDULE_DEFAULT_SPAN_DAYS) ?? undefined };
  if (to && !from) return { from: addDays(to, -SCHEDULE_DEFAULT_SPAN_DAYS) ?? undefined, to };
  if (from && to) {
    const [a, b] = from <= to ? [from, to] : [to, from];
    const maxEnd = addDays(a, SCHEDULE_MAX_SPAN_DAYS) as string;
    return { from: a, to: b > maxEnd ? maxEnd : b };
  }
  return {};
}

export type DeliveryState = "pending" | "retrying" | "delivered" | "failed";

/** A pending delivery that has already failed at least once is "retrying". */
export function deliveryState(d: Pick<WebhookDelivery, "status" | "attempt">): DeliveryState {
  if (d.status === "pending") return d.attempt > 0 ? "retrying" : "pending";
  return d.status;
}
