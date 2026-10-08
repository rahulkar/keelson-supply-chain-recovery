import { STAGE_IDS } from "@keelson/contracts";
import { stageLabels, stageOrder } from "@keelson/tokens";
import { describe, expect, it } from "vitest";
import { formatDate, formatDateTime, formatDelta, formatValue, isAllowedHref, opLabel } from "../src";

describe("formatValue", () => {
  it.each([
    [1240000, "eur_cents", "€12,400"],
    // Whole euros, half away from zero — like the engine's narrative (format_eur_cents).
    [1240050, "eur_cents", "€12,401"],
    [1240049, "eur_cents", "€12,400"],
    [5109315, "eur_cents", "€51,093"],
    [99, "eur_cents", "€1"],
    [-1240050, "eur_cents", "−€12,401"],
    [0, "eur_cents", "€0"],
    [-250000, "eur_cents", "−€2,500"],
    [3200, "units", "3,200 units"],
    [1, "units", "1 unit"],
    [-1, "units", "−1 unit"],
    [3, "days", "3 days"],
    [1, "days", "1 day"],
    [1250, "pct_bp", "12.5%"],
    [9420, "pct_bp", "94.2%"],
    [5, "pct_bp", "0.05%"],
    [10000, "pct_bp", "100%"],
    [12, "count", "12"],
    [1234567, "count", "1,234,567"],
  ] as const)("%d %s → %s", (value, unit, expected) => {
    expect(formatValue(value, unit)).toBe(expected);
  });

  it("exactCents keeps non-zero cents", () => {
    expect(formatValue(1240050, "eur_cents", { exactCents: true })).toBe("€12,400.50");
    expect(formatValue(99, "eur_cents", { exactCents: true })).toBe("€0.99");
    expect(formatValue(1240000, "eur_cents", { exactCents: true })).toBe("€12,400");
  });

  it("supports compact notation", () => {
    expect(formatValue(124000000, "eur_cents", { compact: true })).toMatch(/^€1\.2[mM]$/);
    expect(formatValue(18400, "units", { compact: true })).toMatch(/^18\.4[kK] units$/);
  });

  it("handles unknown units and non-finite values defensively", () => {
    expect(formatValue(5, "pallets")).toBe("5 pallets");
    expect(formatValue(Number.NaN, "units")).toBe("—");
  });

  it("formatDelta signs values and names zero", () => {
    expect(formatDelta(120000, "eur_cents")).toBe("+€1,200");
    expect(formatDelta(-400, "units")).toBe("−400 units");
    expect(formatDelta(610, "pct_bp")).toBe("+6.1%");
    expect(formatDelta(0, "units")).toBe("No change");
  });
});

describe("dates, ops, links", () => {
  it("formats dates in UTC", () => {
    expect(formatDate("2026-10-05")).toBe("5 Oct 2026");
    expect(formatDateTime("2026-10-05T09:12:04Z")).toBe("5 Oct 2026, 09:12 UTC");
    expect(formatDate("not a date")).toBe("not a date");
  });

  it("humanises change-set operations", () => {
    expect(opLabel("transfer_order.create")).toBe("Create transfer order");
    expect(opLabel("allocation.set_qty")).toBe("Change allocation");
    expect(opLabel("widget.do_thing")).toBe("Do thing widget");
  });

  it("allowlists only merxa.localhost / keelson.localhost over http(s)", () => {
    expect(isAllowedHref("http://merxa.localhost/change-batches/CB-1")).toBe(true);
    expect(isAllowedHref("https://keelson.localhost/cases/1")).toBe(true);
    expect(isAllowedHref("http://merxa.localhost.evil.com/")).toBe(false);
    expect(isAllowedHref("http://user:pw@merxa.localhost/")).toBe(false);
    expect(isAllowedHref("javascript:alert(1)")).toBe(false);
    expect(isAllowedHref("/cases/1")).toBe(false);
    expect(isAllowedHref(null)).toBe(false);
  });

  it("never links to API / MCP endpoints on the allowlisted hosts (GET side effects)", () => {
    expect(isAllowedHref("http://keelson.localhost/api/connectors/merxa/connect?return_to=https://evil.example")).toBe(false);
    expect(isAllowedHref("http://keelson.localhost/api")).toBe(false);
    expect(isAllowedHref("http://keelson.localhost/cases/../api/me")).toBe(false); // dot segments resolved first
    expect(isAllowedHref("http://keelson.localhost/API/me")).toBe(false);
    expect(isAllowedHref("http://merxa.localhost/v1/auth/me")).toBe(false);
    expect(isAllowedHref("http://merxa.localhost/mcp")).toBe(false);
    expect(isAllowedHref("http://merxa.localhost/mcp-lite/x")).toBe(false);
    expect(isAllowedHref("http://merxa.localhost/.well-known/oauth-protected-resource")).toBe(false);
    // Pages stay linkable.
    expect(isAllowedHref("http://keelson.localhost/cases/019a0c4e")).toBe(true);
    expect(isAllowedHref("http://keelson.localhost/apiary")).toBe(true);
    expect(isAllowedHref("http://merxa.localhost/change-batches/CB-2026-0042")).toBe(true);
  });
});

describe("cross-package vocabularies", () => {
  it("tokens stage order/labels match the contracts stage enum", () => {
    expect([...stageOrder]).toEqual([...STAGE_IDS]);
    for (const s of STAGE_IDS) expect(stageLabels[s]).toBeTruthy();
  });
});
