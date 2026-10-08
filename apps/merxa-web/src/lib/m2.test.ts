import { describe, expect, it } from "vitest";

import { basisUnit, formatBp, formatMoney, formatRate, modeLabel, opLabel } from "./format";
import {
  addDays,
  capacityMatrix,
  daysBetween,
  deliveryState,
  displayWebhookUrl,
  isIsoDate,
  isIsoWeek,
  isOrderAtRisk,
  isoWeekMonday,
  laneWeekCapacity,
  limitTracks,
  maskedKey,
  parsePoLineRef,
  placeOrders,
  poLineHref,
  promotionRow,
  rateOn,
  redactSubscriptionUrls,
  scheduleWindow,
  sortPromotions,
  utilisation,
  webhookUrlProblem,
  weekOf,
  weekRange,
} from "./m2";
import { statusTone } from "./status";
import type { CapacityBooking, Lane, ProductionOrder, Promotion, RateCard } from "./types";

describe("ISO weeks and dates", () => {
  it("maps ISO weeks to their Monday, including 53-week years", () => {
    expect(isoWeekMonday("2026-W41")).toBe("2026-10-05");
    expect(isoWeekMonday("2026-W01")).toBe("2025-12-29");
    expect(isoWeekMonday("2026-W53")).toBe("2026-12-28"); // 2026 starts on a Thursday: 53 weeks
    expect(isoWeekMonday("2025-W53")).toBeNull();
    expect(isoWeekMonday("2026-W00")).toBeNull();
    expect(isoWeekMonday("2026-41")).toBeNull();
    expect(isIsoWeek("2026-W45")).toBe(true);
    expect(isIsoWeek("2026-W54")).toBe(false);
  });

  it("computes the ISO week of a date across year boundaries", () => {
    expect(weekOf("2026-10-05")).toBe("2026-W41");
    expect(weekOf("2026-10-11")).toBe("2026-W41");
    expect(weekOf("2027-01-01")).toBe("2026-W53");
    expect(weekOf("2024-12-30")).toBe("2025-W01");
    expect(weekOf("2026-02-30")).toBeNull();
  });

  it("lists consecutive weeks and inclusive day ranges", () => {
    expect(weekRange("2026-W52", 3)).toEqual(["2026-W52", "2026-W53", "2027-W01"]);
    expect(weekRange("bogus", 3)).toEqual([]);
    expect(daysBetween("2026-10-30", "2026-11-02")).toEqual(["2026-10-30", "2026-10-31", "2026-11-01", "2026-11-02"]);
    expect(daysBetween("2026-11-02", "2026-10-30")).toEqual([]);
    expect(daysBetween("2026-02-30", "2026-03-02")).toEqual([]);
    expect(addDays("2026-09-28", 7)).toBe("2026-10-05");
    expect(isIsoDate("2026-10-05")).toBe(true);
    expect(isIsoDate("2026-13-05")).toBe(false);
    expect(isIsoDate(undefined)).toBe(false);
  });
});

const rate = (lane: string, from: string, to: string, basis: RateCard["basis"], cents: number, cap: number): RateCard => ({
  lane_code: lane,
  valid_from: from,
  valid_to: to,
  basis,
  rate_cents: cents,
  currency: "EUR",
  weekly_capacity: cap,
  version: 1,
});
const booking = (lane: string, week: string, qty: number, status: CapacityBooking["status"], basis: CapacityBooking["basis"] = "per_unit"): CapacityBooking => ({
  code: `CB-${lane}-${week}-${qty}-${status}`,
  lane_code: lane,
  iso_week: week,
  qty_units: qty,
  basis,
  status,
  expires_at: null,
  created_via: "ui",
  version: 1,
});
const lane = (code: string, mode: Lane["mode"] = "sea_air"): Lane => ({
  code,
  origin: "BDCGP",
  dest: "UK1",
  mode,
  carrier_code: "EKSC",
  carrier_name: "Emirates SkyCargo",
  transit_days: 16,
  via: ["AEJEA"],
  alt_lane_code: null,
  version: 1,
});

const RATES: RateCard[] = [
  rate("CGP-UK1-SA", "2026-07-01", "2026-10-31", "per_unit", 145, 20_000),
  rate("CGP-UK1-SA", "2026-11-01", "2027-02-28", "per_unit", 185, 18_000),
  rate("CGP-FXT-SEA", "2026-07-01", "2027-02-28", "per_teu", 380_000, 400),
];

describe("lane capacity (mirrors merxa-api remaining_capacity)", () => {
  it("picks the rate card valid on the Monday of the week (standard → peak season)", () => {
    expect(rateOn(RATES, "CGP-UK1-SA", "2026-10-31")?.rate_cents).toBe(145);
    expect(rateOn(RATES, "CGP-UK1-SA", "2026-11-01")?.rate_cents).toBe(185);
    // W44 starts Mon 26 Oct (standard), W45 starts Mon 2 Nov (peak).
    expect(laneWeekCapacity("CGP-UK1-SA", "2026-W44", RATES, []).rate?.rate_cents).toBe(145);
    expect(laneWeekCapacity("CGP-UK1-SA", "2026-W45", RATES, []).capacity).toBe(18_000);
  });

  it("deducts held and confirmed per-unit bookings of the same lane and week only", () => {
    const bookings = [
      booking("CGP-UK1-SA", "2026-W44", 5_000, "confirmed"),
      booking("CGP-UK1-SA", "2026-W44", 1_000, "held"),
      booking("CGP-UK1-SA", "2026-W44", 9_999, "released"),
      booking("CGP-UK1-SA", "2026-W45", 700, "confirmed"),
      booking("CGP-NL1-SA", "2026-W44", 4_000, "confirmed"),
      booking("CGP-UK1-SA", "2026-W44", 3, "confirmed", "per_teu"),
    ];
    const c = laneWeekCapacity("CGP-UK1-SA", "2026-W44", RATES, bookings);
    expect(c).toMatchObject({ capacity: 20_000, booked: 6_000, remaining: 14_000 });
  });

  it("does not deduct unit bookings from TEU-based lanes and has no capacity without a rate card", () => {
    const teu = laneWeekCapacity("CGP-FXT-SEA", "2026-W44", RATES, [booking("CGP-FXT-SEA", "2026-W44", 50, "confirmed")]);
    expect(teu).toMatchObject({ capacity: 400, remaining: 400 });
    const none = laneWeekCapacity("CGP-UK1-SA", "2027-W20", RATES, []);
    expect(none).toMatchObject({ rate: null, capacity: null, remaining: null });
    expect(utilisation(none)).toBe("none");
  });

  it("builds a matrix and drops lanes that offer no capacity in the window", () => {
    const m = capacityMatrix([lane("CGP-UK1-SA"), lane("DE1-NL1-RD", "road")], ["2026-W44", "2026-W45"], RATES, []);
    expect(m.map((r) => r.lane.code)).toEqual(["CGP-UK1-SA"]);
    expect(m[0]?.weeks.map((w) => w.capacity)).toEqual([20_000, 18_000]);
  });

  it("classifies utilisation (≥80 % tight, exhausted full)", () => {
    expect(utilisation({ capacity: 100, remaining: 50 })).toBe("ok");
    expect(utilisation({ capacity: 100, remaining: 20 })).toBe("tight");
    expect(utilisation({ capacity: 100, remaining: 0 })).toBe("full");
    expect(utilisation({ capacity: 100, remaining: -40 })).toBe("full");
    expect(utilisation({ capacity: 0, remaining: 0 })).toBe("full");
  });
});

const order = (code: string, start: string, end: string, seq = 1, status: ProductionOrder["status"] = "planned"): ProductionOrder => ({
  code,
  po_line_ref: `PO-BD-0001#${seq}`,
  line_code: "FL-BD01-A",
  seq,
  planned_start: start,
  planned_end: end,
  status,
  hold_reason: null,
  version: 1,
});

describe("production calendar strip placement", () => {
  const days = daysBetween("2026-10-01", "2026-10-10");

  it("places orders on 1-based grid columns with an exclusive end", () => {
    const [p] = placeOrders([order("MO-1", "2026-10-03", "2026-10-05")], days);
    expect(p).toMatchObject({ colStart: 3, colEnd: 6, track: 1, clippedStart: false, clippedEnd: false });
  });

  it("clips orders to the window and drops orders outside it", () => {
    const placed = placeOrders(
      [
        order("MO-A", "2026-09-25", "2026-10-02", 1),
        order("MO-B", "2026-10-09", "2026-10-20", 2),
        order("MO-C", "2026-09-01", "2026-09-30", 3),
        order("MO-D", "2026-10-11", "2026-10-12", 4),
      ],
      days,
    );
    expect(placed.map((p) => p.order.code)).toEqual(["MO-A", "MO-B"]);
    expect(placed[0]).toMatchObject({ colStart: 1, colEnd: 3, clippedStart: true, clippedEnd: false });
    expect(placed[1]).toMatchObject({ colStart: 9, colEnd: 11, clippedStart: false, clippedEnd: true });
  });

  it("packs overlapping orders onto separate tracks and reuses freed tracks", () => {
    const placed = placeOrders(
      [
        order("MO-1", "2026-10-01", "2026-10-04", 1),
        order("MO-2", "2026-10-03", "2026-10-06", 2), // overlaps MO-1
        order("MO-3", "2026-10-05", "2026-10-07", 3), // MO-1 ended: track 1 again
        order("MO-4", "2026-10-04", "2026-10-04", 4), // overlaps both running orders
      ],
      days,
    );
    const track = Object.fromEntries(placed.map((p) => [p.order.code, p.track]));
    expect(track).toEqual({ "MO-1": 1, "MO-2": 2, "MO-4": 3, "MO-3": 1 });
  });

  it("returns nothing for an empty window", () => {
    expect(placeOrders([order("MO-1", "2026-10-01", "2026-10-04")], [])).toEqual([]);
  });
});

describe("PO line references", () => {
  it("parses <po>#<line> and builds an encoded in-app link", () => {
    expect(parsePoLineRef("PO-BD-0001#3")).toEqual({ po: "PO-BD-0001", line: 3 });
    expect(parsePoLineRef("PO-BD-0001")).toBeNull();
    expect(parsePoLineRef("PO#0")).toBeNull();
    expect(parsePoLineRef("a b#1")).toBeNull();
    expect(poLineHref("PO-BD-0001#3")).toBe("/purchase-orders/PO-BD-0001?line=3");
    expect(poLineHref("PO/../x?y#2")).toBe("/purchase-orders/PO%2F..%2Fx%3Fy?line=2");
    expect(poLineHref(null)).toBeNull();
  });
});

describe("API keys and webhooks", () => {
  it("never shows more of a key than its public prefix", () => {
    expect(maskedKey("ab12cd34")).toBe("mx_ab12cd34_••••••••");
  });

  it("pre-checks webhook URLs (http(s) only, bounded length)", () => {
    expect(webhookUrlProblem("http://keelson.localhost/api/webhooks/demo-retail/merxa")).toBeNull();
    expect(webhookUrlProblem("  https://example.org/hook ")).toBeNull();
    expect(webhookUrlProblem("")).toMatch(/Enter/);
    expect(webhookUrlProblem("javascript:alert(1)")).toMatch(/http/);
    expect(webhookUrlProblem("ftp://example.org/")).toMatch(/http/);
    expect(webhookUrlProblem("/relative/path")).toMatch(/absolute/);
    expect(webhookUrlProblem(`https://example.org/${"a".repeat(2050)}`)).toMatch(/2,048/);
  });

  it("derives a retrying state for pending deliveries that already failed", () => {
    expect(deliveryState({ status: "pending", attempt: 0 })).toBe("pending");
    expect(deliveryState({ status: "pending", attempt: 2 })).toBe("retrying");
    expect(deliveryState({ status: "delivered", attempt: 1 })).toBe("delivered");
    expect(deliveryState({ status: "failed", attempt: 6 })).toBe("failed");
  });
});

describe("M2 formatting and tones", () => {
  it("formats basis points, rates and modes", () => {
    expect(formatBp(2500)).toBe("25%");
    expect(formatBp(1250)).toBe("12.5%");
    expect(formatBp(null)).toBe("—");
    expect(formatRate(145, "per_unit")).toBe("€1.45 / unit");
    expect(formatRate(380_000, "per_teu")).toBe("€3,800 / TEU");
    expect(formatRate(null, "per_unit")).toBe("—");
    expect(basisUnit("per_kg")).toBe("kg");
    expect(modeLabel("sea_air")).toBe("Sea-air");
    expect(modeLabel("road")).toBe("Road");
  });

  it("labels the M2 change-set operations", () => {
    expect(opLabel("capacity_booking.create")).toBe("Book lane capacity");
    expect(opLabel("production_order.resequence")).toBe("Resequence production line");
    expect(opLabel("promotion.reschedule")).toBe("Reschedule promotion");
    expect(opLabel("online_order.repromise")).toBe("Re-promise online order");
  });

  it("gives the new statuses meaningful tones", () => {
    expect(statusTone("scheduled")).toBe("info");
    expect(statusTone("arrived")).toBe("success");
    expect(statusTone("in_transit")).toBe("progress");
    expect(statusTone("held")).toBe("warning");
    expect(statusTone("running")).toBe("progress");
    expect(statusTone("retrying")).toBe("warning");
    expect(statusTone("revoked")).toBe("neutral");
    expect(statusTone("failed")).toBe("danger");
  });
});

describe("calendar strip track limit", () => {
  it("keeps the first tracks and counts hidden orders per day", () => {
    const days = daysBetween("2026-10-01", "2026-10-05");
    const many = Array.from({ length: 5 }, (_, i) => order(`MO-${i}`, "2026-10-02", i === 4 ? "2026-10-03" : "2026-10-02", i + 1));
    const { visible, hiddenPerDay } = limitTracks(placeOrders(many, days), 3, days.length);
    expect(visible.map((p) => p.order.code)).toEqual(["MO-0", "MO-1", "MO-2"]);
    expect(hiddenPerDay).toEqual([0, 2, 1, 0, 0]);
  });

  it("hides nothing when everything fits", () => {
    const days = daysBetween("2026-10-01", "2026-10-03");
    const { visible, hiddenPerDay } = limitTracks(placeOrders([order("MO-1", "2026-10-01", "2026-10-03")], days), 3, days.length);
    expect(visible).toHaveLength(1);
    expect(hiddenPerDay).toEqual([0, 0, 0]);
  });
});

describe("review regressions", () => {
  it("treats a null rate-card bound as an unbounded daterange side (like `valid @> day`)", () => {
    const open: RateCard[] = [
      { lane_code: "X", valid_from: "2026-01-01", valid_to: null, basis: "per_unit", rate_cents: 100, currency: "EUR", weekly_capacity: 50, version: 1 },
      { lane_code: "Y", valid_from: null, valid_to: "2026-12-31", basis: "per_teu", rate_cents: 9, currency: "EUR", weekly_capacity: 7, version: 1 },
    ];
    expect(rateOn(open, "X", "2030-06-01")?.rate_cents).toBe(100);
    expect(rateOn(open, "X", "2025-12-31")).toBeNull();
    expect(rateOn(open, "Y", "1999-01-01")?.rate_cents).toBe(9);
    expect(rateOn(open, "Y", "2027-01-01")).toBeNull();
    expect(laneWeekCapacity("X", "2029-W10", open, []).capacity).toBe(50);
  });

  it("flags online orders at risk only while open or picking (merxa-api ?at_risk=true)", () => {
    expect(isOrderAtRisk({ status: "open", shortfall: 1 })).toBe(true);
    expect(isOrderAtRisk({ status: "picking", shortfall: 2 })).toBe(true);
    expect(isOrderAtRisk({ status: "open", shortfall: 0 })).toBe(false);
    for (const status of ["shipped", "delivered", "cancelled"]) expect(isOrderAtRisk({ status, shortfall: 3 })).toBe(false);
  });

  it("completes, orders and caps the production schedule window so merxa-api accepts it", () => {
    expect(scheduleWindow(undefined, undefined)).toEqual({});
    // A lone side gets the default 49-day span instead of the API's clock-based other side.
    expect(scheduleWindow("2027-03-01", undefined)).toEqual({ from: "2027-03-01", to: "2027-04-19" });
    expect(scheduleWindow(undefined, "2026-01-31")).toEqual({ from: "2025-12-13", to: "2026-01-31" });
    expect(scheduleWindow("2026-11-30", "2026-11-01")).toEqual({ from: "2026-11-01", to: "2026-11-30" });
    expect(scheduleWindow("2026-01-01", "2030-01-01")).toEqual({ from: "2026-01-01", to: "2027-01-02" });
    const w = scheduleWindow("2026-01-01", "2030-01-01");
    expect(daysBetween(w.from as string, w.to as string).length - 1).toBe(366);
  });
});

describe("review regressions (M2)", () => {
  it("formats rates in the rate card's own currency, not always in euros", () => {
    expect(formatRate(145, "per_unit", "EUR")).toBe("€1.45 / unit");
    expect(formatRate(145, "per_unit", null)).toBe("€1.45 / unit");
    expect(formatRate(380_000, "per_teu", "USD")).toBe("US$3,800 / TEU");
    expect(formatRate(1250, "per_kg", "GBP")).toBe("£12.50 / kg");
    expect(formatMoney(1250, "??")).toBe("12.50 ??");
    expect(formatMoney(null, "USD")).toBe("—");
  });

  const promo = (code: string, start: string | null, end: string | null): Promotion => ({
    code,
    name: code,
    markets: [],
    grades: [],
    style_codes: [],
    period_start: start,
    period_end: end,
    discount_bp: 2000,
    uplift_bp: 3000,
    version: 1,
  });

  it("renders promotions with an unbounded period instead of crashing", () => {
    expect(promotionRow(promo("BF", "2026-11-23", "2026-11-29"))).toMatchObject({ days: 7, weeks: "2026-W48" });
    expect(promotionRow(promo("XM", "2026-12-21", "2027-01-03"))).toMatchObject({ days: 14, weeks: "2026-W52 – 2026-W53" });
    expect(promotionRow(promo("EVER", "2026-11-01", null))).toMatchObject({ days: null, weeks: "2026-W44 – open-ended" });
    expect(promotionRow(promo("OLD", null, "2026-10-04"))).toMatchObject({ days: null, weeks: "open-ended – 2026-W40" });
    // The page sorted with `period_start.localeCompare`, which threw on a null start.
    expect(sortPromotions([promo("B", "2026-11-23", "2026-11-29"), promo("A", null, "2026-10-04"), promo("C", "2026-11-23", null)]).map((p) => p.code)).toEqual([
      "A",
      "B",
      "C",
    ]);
  });

  it("masks a password in a webhook endpoint URL and keeps everything else verbatim", () => {
    expect(displayWebhookUrl("https://svc:s3cr3t@hooks.example.org/merxa?x=1")).toBe("https://svc:••••••@hooks.example.org/merxa?x=1");
    expect(displayWebhookUrl("https://a:b:c@h@host.example/p")).toBe("https://a:••••••@host.example/p");
    expect(displayWebhookUrl("https://svc@hooks.example.org/merxa")).toBe("https://svc@hooks.example.org/merxa");
    expect(displayWebhookUrl("http://keelson.localhost/api/webhooks/demo-retail/merxa")).toBe(
      "http://keelson.localhost/api/webhooks/demo-retail/merxa",
    );
    // An "@" after the authority (path, query) is not userinfo.
    expect(displayWebhookUrl("https://host.example/p?u=a:b@c")).toBe("https://host.example/p?u=a:b@c");
    expect(displayWebhookUrl("javascript:alert(1)")).toBe("javascript:alert(1)");
    // Idempotent: the page masks on the server and the component masks again.
    expect(displayWebhookUrl(displayWebhookUrl("https://svc:s3cr3t@h.example/"))).toBe("https://svc:••••••@h.example/");
  });

  it("redacts subscription URLs before they are handed to the client component (RSC payload)", () => {
    const subs = [
      { id: "a", url: "https://svc:hunter2@hooks.example.org/in", version: 1 },
      { id: "b", url: "http://keelson.localhost/api/webhooks/demo-retail/merxa", version: 2 },
    ];
    const out = redactSubscriptionUrls(subs);
    expect(JSON.stringify(out)).not.toContain("hunter2");
    expect(out.map((s) => s.url)).toEqual(["https://svc:••••••@hooks.example.org/in", "http://keelson.localhost/api/webhooks/demo-retail/merxa"]);
    expect(out[1]).toEqual(subs[1]);
    expect(subs[0]?.url).toContain("hunter2"); // input not mutated
  });
});
