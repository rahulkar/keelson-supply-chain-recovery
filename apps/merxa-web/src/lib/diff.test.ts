import { describe, expect, it } from "vitest";

import { diffItem, numericDelta, summarizeChange } from "./diff";
import { formatValue } from "./format";

describe("diffItem", () => {
  it("diffs allocation.set_qty before/after", () => {
    const before = { qty: 40, status: "planned", sku: "PUF-W-BLK-M", dc_code: "UK1", store_code: "GB-LDN-01", ship_week: "2026-W45", version: 3 };
    const after = { ...before, qty: 25, version: 4 };
    const rows = diffItem(before, after);
    expect(rows.find((r) => r.path === "qty")).toMatchObject({ before: 40, after: 25, changed: true, label: "qty" });
    expect(rows.find((r) => r.path === "sku")).toMatchObject({ changed: false });
    expect(diffItem(before, after, { onlyChanged: true }).map((r) => r.path)).toEqual(["qty", "version"]);
  });

  it("keys transfer-order lines by SKU and keeps the new header", () => {
    const before = {
      transfer_order: null,
      from_dc: "DE1",
      to_dc: "NL1",
      lines: [{ sku: "PUF-W-BLK-M", from_atp: 900, to_in_transit: 0 }],
    };
    const after = {
      transfer_order: { from_dc: "DE1", to_dc: "NL1", ship_date: "2026-10-12", eta_date: "2026-10-14", status: "planned" },
      from_dc: "DE1",
      to_dc: "NL1",
      lines: [{ sku: "PUF-W-BLK-M", qty: 300, from_atp: 600, to_in_transit: 300 }],
    };
    const rows = diffItem(before, after);
    const atp = rows.find((r) => r.path === "lines[PUF-W-BLK-M].from_atp");
    expect(atp).toMatchObject({ before: 900, after: 600, changed: true, label: "PUF-W-BLK-M · from-DC ATP" });
    expect(numericDelta(atp!)).toBe(-300);
    const qty = rows.find((r) => r.path === "lines[PUF-W-BLK-M].qty");
    expect(qty).toMatchObject({ before: null, after: 300, changed: true });
    expect(numericDelta(qty!)).toBe(300);
    expect(rows.find((r) => r.path === "transfer_order.eta_date")).toMatchObject({
      before: null,
      after: "2026-10-14",
      changed: true,
      label: "transfer order ETA",
    });
    // `transfer_order: null` before → the header fields only exist after
    expect(rows.find((r) => r.path === "transfer_order")).toBeUndefined();
    expect(rows.find((r) => r.path === "from_dc")).toMatchObject({ changed: false });
  });

  it("diffs a PO line reschedule", () => {
    const rows = diffItem(
      { eta_dc: "2026-10-29", sku: "PUF-W-BLK-M", qty_open: 1200, status: "in_production", version: 1 },
      { eta_dc: "2026-11-05", sku: "PUF-W-BLK-M", qty_open: 1200, status: "in_production", version: 2 },
      { onlyChanged: true },
    );
    expect(rows.map((r) => [r.label, r.before, r.after])).toEqual([
      ["ETA DC", "2026-10-29", "2026-11-05"],
      ["version", 1, 2],
    ]);
    expect(numericDelta(rows[0]!)).toBeNull();
  });

  it("handles items that were never validated", () => {
    expect(diffItem(null, null)).toEqual([]);
  });
});

describe("summarizeChange", () => {
  it("summarizes audit rows and skips versions", () => {
    expect(summarizeChange({ reserved: 120, atp: 300 }, { reserved: 420, atp: 0 }, formatValue)).toBe(
      "reserved 120 → 420; atp 300 → 0",
    );
    expect(summarizeChange({ eta_dc: "2026-10-29", version: 1 }, { eta_dc: "2026-11-05", version: 2 }, formatValue)).toBe(
      "ETA DC 29 Oct 2026 → 05 Nov 2026",
    );
    expect(summarizeChange(null, null)).toBe("");
  });

  it("caps long summaries", () => {
    const before = Object.fromEntries(Array.from({ length: 8 }, (_, i) => [`f${i}`, i]));
    const after = Object.fromEntries(Array.from({ length: 8 }, (_, i) => [`f${i}`, i + 1]));
    expect(summarizeChange(before, after, String, 3)).toBe("f0 0 → 1; f1 1 → 2; f2 2 → 3; +5 more");
  });
});
