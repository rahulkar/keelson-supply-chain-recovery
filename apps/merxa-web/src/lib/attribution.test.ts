import { describe, expect, it } from "vitest";

import {
  attributionText,
  changeSetCodeFromCreatedVia,
  isExternal,
  isRecentlyApplied,
  parseApprovedBy,
  recentlyApplied,
} from "./attribution";

const NOW = Date.parse("2026-10-06T12:00:00Z");
const minutesAgo = (m: number) => new Date(NOW - m * 60_000).toISOString();

describe("parseApprovedBy", () => {
  it("parses delegated and policy approvals", () => {
    expect(parseApprovedBy("user:marco")).toEqual({ kind: "user", username: "marco" });
    expect(parseApprovedBy("policy:P-12@3")).toEqual({ kind: "policy", rule: "P-12", version: "3" });
    expect(parseApprovedBy("policy:auto-rebalance")).toEqual({ kind: "policy", rule: "auto-rebalance", version: null });
    expect(parseApprovedBy("someone")).toEqual({ kind: "unknown", raw: "someone" });
    expect(parseApprovedBy(null)).toBeNull();
  });
});

describe("attributionText", () => {
  it("prefers the server-rendered text", () => {
    expect(attributionText({ status: "applied", attribution_text: "Changed by Marco Rossi via Keelson (APR-0001)" })).toBe(
      "Changed by Marco Rossi via Keelson (APR-0001)",
    );
  });

  it("renders delegated changes like merxa-api", () => {
    expect(
      attributionText({
        status: "applied",
        approved_by: "user:marco",
        via: "Keelson",
        applied_by_display: "Marco Rossi",
        approval_ref: "APR-982",
      }),
    ).toBe("Changed by Marco Rossi via Keelson (APR-982)");
  });

  it("renders policy changes like merxa-api", () => {
    expect(
      attributionText({ status: "applied", approved_by: "policy:P-12@3", via: "Keelson", initiated_by: "Priya Shah" }),
    ).toBe("Applied via Keelson auto-policy P-12@3, initiated by Priya Shah");
  });

  it("falls back to username / API channel and omits a missing approval", () => {
    expect(attributionText({ status: "applied", applied_by_username: "svc", via: null })).toBe("Changed by svc via API");
  });

  it("is empty for batches that were not applied", () => {
    expect(attributionText({ status: "validated", approved_by: "user:marco" })).toBeNull();
    expect(attributionText({ status: "conflict" })).toBeNull();
  });
});

describe("external changes", () => {
  it("classifies channels", () => {
    expect(isExternal({ via: "Keelson" })).toBe(true);
    expect(isExternal({ via: "API" })).toBe(true);
    expect(isExternal({ via: "Merxa UI" })).toBe(false);
    expect(isExternal({ via: "Demo director" })).toBe(false);
    expect(isExternal({ via: null })).toBe(false);
  });

  it("detects batches applied in the last 10 minutes", () => {
    expect(isRecentlyApplied({ status: "applied", applied_at: minutesAgo(3) }, NOW)).toBe(true);
    expect(isRecentlyApplied({ status: "applied", applied_at: minutesAgo(10) }, NOW)).toBe(true);
    expect(isRecentlyApplied({ status: "applied", applied_at: minutesAgo(11) }, NOW)).toBe(false);
    expect(isRecentlyApplied({ status: "applied", applied_at: null }, NOW)).toBe(false);
    expect(isRecentlyApplied({ status: "conflict", applied_at: minutesAgo(1) }, NOW)).toBe(false);
    // tolerate a slightly fast server clock
    expect(isRecentlyApplied({ status: "applied", applied_at: new Date(NOW + 30_000).toISOString() }, NOW)).toBe(true);
  });

  it("lists recent batches newest first", () => {
    const items = [
      { code: "CS-0001", status: "applied", applied_at: minutesAgo(30) },
      { code: "CS-0002", status: "applied", applied_at: minutesAgo(8) },
      { code: "CS-0003", status: "applied", applied_at: minutesAgo(2) },
      { code: "CS-0004", status: "validated", applied_at: null },
    ];
    expect(recentlyApplied(items, NOW).map((c) => c.code)).toEqual(["CS-0003", "CS-0002"]);
  });

  it("extracts the change batch from created_via", () => {
    expect(changeSetCodeFromCreatedVia("change_set:CS-0004")).toBe("CS-0004");
    expect(changeSetCodeFromCreatedVia("ui")).toBeNull();
    expect(changeSetCodeFromCreatedVia("change_set:")).toBeNull();
    expect(changeSetCodeFromCreatedVia(null)).toBeNull();
  });
});

describe("attributionText (M5 fallbacks)", () => {
  const base = { status: "applied", via: "Keelson", applied_by_display: "Marco Rossi", approval_ref: "APR-0007" };

  it("adds four-eyes co-approvers like merxa-api", () => {
    expect(attributionText({ ...base, co_approvers: [{ ref: "user:lena", name: "Lena Vogel" }] })).toBe(
      "Changed by Marco Rossi via Keelson (APR-0007), co-approved by Lena Vogel",
    );
    expect(
      attributionText({
        ...base,
        co_approvers: [
          { ref: "user:lena", name: null },
          { ref: "user:omar", name: "Omar Haddad" },
          { ref: "user:kim", name: "Kim Lee" },
        ],
      }),
    ).toBe("Changed by Marco Rossi via Keelson (APR-0007), co-approved by lena, Omar Haddad and Kim Lee");
    expect(attributionText({ ...base, co_approvers: null })).toBe("Changed by Marco Rossi via Keelson (APR-0007)");
  });

  it("words a compensating batch as a rollback", () => {
    expect(attributionText({ ...base, rollback_of: "0199-cs" })).toBe("Rolled back by Marco Rossi via Keelson (APR-0007)");
  });
});
