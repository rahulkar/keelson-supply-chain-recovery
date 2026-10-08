import { describe, expect, it } from "vitest";

import {
  formatCents,
  formatDate,
  formatDateTime,
  formatDelta,
  formatInt,
  formatRelative,
  formatValue,
  humanize,
  isoWeek,
  opLabel,
  shortHash,
} from "./format";
import { statusTone } from "./status";

describe("numbers", () => {
  it("formats integer quantities with thousands separators", () => {
    expect(formatInt(12400)).toBe("12,400");
    expect(formatInt(0)).toBe("0");
    expect(formatInt(null)).toBe("—");
    expect(formatInt(undefined)).toBe("—");
  });

  it("formats signed deltas", () => {
    expect(formatDelta(5)).toBe("+5");
    expect(formatDelta(-1200)).toBe("-1,200");
    expect(formatDelta(0)).toBe("0");
  });

  it("formats EUR cents (integers only, never floats in contracts)", () => {
    expect(formatCents(1_240_000)).toBe("€12,400");
    expect(formatCents(1250)).toBe("€12.50");
    expect(formatCents(null)).toBe("—");
  });
});

describe("dates", () => {
  it("formats calendar dates without time-zone shift", () => {
    expect(formatDate("2026-10-29")).toBe("29 Oct 2026");
    expect(formatDate("2026-01-01")).toBe("01 Jan 2026");
    expect(formatDate(null)).toBe("—");
    expect(formatDate("not-a-date")).toBe("not-a-date");
  });

  it("formats RFC 3339 timestamps in UK time", () => {
    // 2026-10-06 is BST (UTC+1)
    expect(formatDateTime("2026-10-06T13:03:00Z")).toBe("06 Oct 2026, 14:03");
    // 2026-11-27 is GMT
    expect(formatDateTime("2026-11-27T09:30:00Z")).toBe("27 Nov 2026, 09:30");
  });

  it("computes ISO weeks per the demo calendar", () => {
    expect(isoWeek("2026-10-05")).toBe("2026-W41"); // demo "today"
    expect(isoWeek("2026-10-29")).toBe("2026-W44");
    expect(isoWeek("2026-11-02")).toBe("2026-W45"); // peak starts
    expect(isoWeek("2026-11-27")).toBe("2026-W48"); // Black Friday
    expect(isoWeek("2027-01-01")).toBe("2026-W53");
    expect(isoWeek("2024-12-30")).toBe("2025-W01");
    expect(isoWeek(undefined)).toBe("—");
  });

  it("formats relative times", () => {
    const now = Date.parse("2026-10-06T12:00:00Z");
    expect(formatRelative("2026-10-06T11:59:50Z", now)).toBe("just now");
    expect(formatRelative("2026-10-06T11:57:00Z", now)).toBe("3 min ago");
    expect(formatRelative("2026-10-06T09:00:00Z", now)).toBe("3 h ago");
    expect(formatRelative("2026-10-03T12:00:00Z", now)).toBe("3 d ago");
    expect(formatRelative("2026-10-06T12:00:30Z", now)).toBe("just now"); // small clock skew
  });
});

describe("labels", () => {
  it("humanizes codes and labels change-set ops", () => {
    expect(humanize("in_production")).toBe("In production");
    expect(humanize("rolled_back")).toBe("Rolled back");
    expect(opLabel("transfer_order.create")).toBe("Create transfer order");
    expect(opLabel("po_line.reschedule")).toBe("Reschedule PO line");
    expect(opLabel("frobnicate")).toBe("frobnicate");
  });

  it("shortens hashes and formats diff values", () => {
    const h = "a".repeat(10) + "b".repeat(48) + "c".repeat(6);
    expect(shortHash(h)).toBe(`${"a".repeat(10)}…${"c".repeat(6)}`);
    expect(shortHash("abc")).toBe("abc");
    expect(formatValue(1200)).toBe("1,200");
    expect(formatValue("2026-11-10")).toBe("10 Nov 2026");
    expect(formatValue(null)).toBe("—");
    expect(formatValue(true)).toBe("Yes");
    expect(formatValue({ a: 1 })).toBe('{"a":1}');
  });

  it("maps statuses to badge tones", () => {
    expect(statusTone("applied")).toBe("success");
    expect(statusTone("conflict")).toBe("warning");
    expect(statusTone("failed")).toBe("danger");
    expect(statusTone("new")).toBe("external");
    expect(statusTone("whatever")).toBe("neutral");
    expect(statusTone(null)).toBe("neutral");
  });
});
