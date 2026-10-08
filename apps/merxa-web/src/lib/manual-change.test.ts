import { describe, expect, it } from "vitest";

import { ApiError, appPath } from "./http";
import {
  idempotencyKeyFor,
  isRetryable,
  parseQty,
  plannedVersion,
  saveErrorMessage,
  seg,
  transferOrderBody,
  transferProblem,
  versionNotice,
} from "./manual-change";
import { parseSkuQtyList } from "./params";

describe("manual-change helpers (M5 Guide-mode forms)", () => {
  it("accepts whole non-negative quantities only", () => {
    expect(parseQty("12")).toBe(12);
    expect(parseQty(" 0 ")).toBe(0);
    expect(parseQty("0", { min: 1 })).toBeNull();
    for (const bad of ["", "-1", "1.5", "1e3", "12abc", "0x10", "99999999999"]) expect(parseQty(bad)).toBeNull();
  });

  it("reads the planned version of a guide link and warns when the record moved on", () => {
    expect(plannedVersion("3")).toBe(3);
    expect(plannedVersion(undefined)).toBeNull();
    expect(plannedVersion("0")).toBeNull();
    expect(plannedVersion("3; drop")).toBeNull();
    expect(versionNotice(null, 4)).toBeNull();
    expect(versionNotice(4, 4)).toBeNull();
    expect(versionNotice(3, 5)).toMatch(/planned against version 3; the record is now at version 5/);
  });

  it("builds the transfer-order request from a guide line list", () => {
    const { lines, invalid } = parseSkuQtyList("PUF-W-BLK-S:7,puf-w-blk-m:5");
    expect(invalid).toEqual([]);
    expect(transferOrderBody("DE1", "NL1", "2026-10-14", lines)).toEqual({
      from_dc: "DE1",
      to_dc: "NL1",
      ship_date: "2026-10-14",
      lines: [
        { sku: "PUF-W-BLK-S", qty: 7 },
        { sku: "PUF-W-BLK-M", qty: 5 },
      ],
    });
    expect(transferProblem("DE1", "NL1", "2026-10-14", lines, [])).toBeNull();
    expect(transferProblem(undefined, "NL1", "2026-10-14", lines, [])).toMatch(/From and To/);
    expect(transferProblem("DE1", "DE1", "2026-10-14", lines, [])).toMatch(/must differ/);
    expect(transferProblem("DE1", "NL1", undefined, lines, [])).toMatch(/ship date/);
    expect(transferProblem("DE1", "NL1", "2026-10-14", lines, ["x"])).toMatch(/unreadable/);
    expect(transferProblem("DE1", "NL1", "2026-10-14", [], [])).toMatch(/at least one line/);
    const dup = parseSkuQtyList("A-1:1,a-1:2").lines;
    expect(transferProblem("DE1", "NL1", "2026-10-14", dup, [])).toMatch(/only once/);
  });

  it("explains failed saves with the next step", () => {
    expect(saveErrorMessage(new ApiError(412, { code: "precondition_failed" }))).toMatch(/changed by someone else/);
    expect(saveErrorMessage(new ApiError(0, { code: "network_error", detail: "offline" }))).toMatch(/Saving again is safe/);
    expect(saveErrorMessage(new ApiError(409, { code: "idempotency_in_flight" }))).toMatch(/still being processed/);
    expect(saveErrorMessage(new ApiError(422, { code: "validation_failed", detail: "insufficient ATP" }))).toBe(
      "insufficient ATP",
    );
    expect(saveErrorMessage(new ApiError(403, { detail: "requires role merch-planner" }))).toMatch(/^Not allowed/);
  });

  it("reuses the key only for an ambiguous retry of the identical request", () => {
    expect(isRetryable(new ApiError(0, { code: "network_error" }))).toBe(true);
    expect(isRetryable(new ApiError(422, { code: "validation_failed" }))).toBe(false);
    expect(isRetryable(new ApiError(412, { code: "precondition_failed" }))).toBe(false);
    const pending = { path: "/v1/transfer-orders", body: JSON.stringify({ a: 1 }), key: "k-1" };
    expect(idempotencyKeyFor(pending, "/v1/transfer-orders", { a: 1 })).toBe("k-1");
    expect(idempotencyKeyFor(pending, "/v1/transfer-orders", { a: 2 })).not.toBe("k-1");
  });

  it("encodes codes taken from links as one path segment", () => {
    expect(seg("AL/1?x#y")).toBe("AL%2F1%3Fx%23y");
  });

  it("keeps merxa-api guide deep links in the app (encoded codes stay one segment)", () => {
    expect(appPath("http://merxa.localhost/allocations/AL%2F1%3Fx%3D1%23y?action=set-qty&qty=9&version=3")).toBe(
      "/allocations/AL%2F1%3Fx%3D1%23y?action=set-qty&qty=9&version=3",
    );
    expect(appPath("http://merxa.localhost/capacity-bookings/new?lane=CGP-UK1-SA&week=2026-W44&qty=120")).toBe(
      "/capacity-bookings/new?lane=CGP-UK1-SA&week=2026-W44&qty=120",
    );
    expect(appPath("http://merxa.localhost/capacity-bookings/CB-0001?action=cancel&version=1")).toBe(
      "/capacity-bookings/CB-0001?action=cancel&version=1",
    );
  });
});
