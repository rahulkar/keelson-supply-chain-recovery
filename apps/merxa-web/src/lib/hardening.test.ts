import { describe, expect, it } from "vitest";

import { externallyChangedEntities, trackLatestChange } from "./attribution";
import { csvSafeCell } from "./csv";
import { appPath, isForwardableCookieValue, safeHttpUrl } from "./http";

describe("appPath (operator-guide deep links)", () => {
  it("keeps the in-app path of Merxa links", () => {
    expect(appPath("http://merxa.localhost/transfer-orders/new?from=DE1&to=NL1&lines=PUF-W-BLK-M%3A120")).toBe(
      "/transfer-orders/new?from=DE1&to=NL1&lines=PUF-W-BLK-M%3A120",
    );
    expect(appPath("http://merxa.localhost/purchase-orders/PO-BD-0001?line=2&action=reschedule&eta=2026-11-05")).toBe(
      "/purchase-orders/PO-BD-0001?line=2&action=reschedule&eta=2026-11-05",
    );
    expect(appPath("/change-batches/CS-0001")).toBe("/change-batches/CS-0001");
  });

  it("never leaves the Merxa origin", () => {
    // Another host collapses to the same path on this origin.
    expect(appPath("https://evil.example/phish?x=1")).toBe("/phish?x=1");
  });

  it.each([
    ["javascript:alert(1)"],
    ["data:text/html,<script>alert(1)</script>"],
    ["vbscript:msgbox"],
    ["//evil.example/x"],
    [""],
    [null],
  ])("rejects %s", (link) => {
    const out = appPath(link as string | null);
    expect(out === null || (out.startsWith("/") && !out.startsWith("//"))).toBe(true);
    expect(out ?? "").not.toMatch(/^(javascript|data|vbscript):/i);
  });

  it("drops protocol-relative results", () => {
    expect(appPath("//evil.example/x")).toBe("/x");
    expect(appPath("http://merxa.localhost//evil.example/")).toBeNull();
  });
});

describe("safeHttpUrl (identity-provider logout)", () => {
  it("accepts http(s) only", () => {
    expect(safeHttpUrl("http://auth.localhost/realms/merxa/protocol/openid-connect/logout?id_token_hint=x")).toBe(
      "http://auth.localhost/realms/merxa/protocol/openid-connect/logout?id_token_hint=x",
    );
    expect(safeHttpUrl("javascript:alert(1)")).toBeNull();
    expect(safeHttpUrl("/relative")).toBeNull();
    expect(safeHttpUrl(null)).toBeNull();
  });
});

describe("isForwardableCookieValue", () => {
  it("accepts URL-safe session tokens", () => {
    expect(isForwardableCookieValue("Zx8_-a9QkLm0pP1oiu2y3t4r5e6w7q8a9s0d1f2g3h4")).toBe(true);
  });

  it.each([["a;b"], ["a b"], ["a\r\nX-Injected: 1"], ['a"b'], ["a,b"], ["a\\b"], [""], [undefined], ["x".repeat(5000)]])(
    "rejects %j",
    (value) => {
      expect(isForwardableCookieValue(value as string | undefined)).toBe(false);
    },
  );
});

describe("csvSafeCell (CSV injection)", () => {
  it.each([
    ["=HYPERLINK(\"http://evil\",\"x\")", "'=HYPERLINK(\"http://evil\",\"x\")"],
    ["+cmd|' /C calc'!A0", "'+cmd|' /C calc'!A0"],
    ["-2+3", "'-2+3"],
    ["@SUM(A1:A2)", "'@SUM(A1:A2)"],
    ["\t=1", "'\t=1"],
    ["Rebalance UK1 peak", "Rebalance UK1 peak"],
  ])("neutralises text %j", (raw, expected) => {
    expect(csvSafeCell(raw, raw)).toBe(expected);
  });

  it("leaves numbers (including negative quantities) untouched", () => {
    expect(csvSafeCell(-40, "-40")).toBe("-40");
    expect(csvSafeCell(12400, "12,400")).toBe("12,400");
  });

  it("checks the formatted text and handles empty values", () => {
    expect(csvSafeCell("2026-10-29", "29 Oct 2026")).toBe("29 Oct 2026");
    expect(csvSafeCell(null, null)).toBe("");
    expect(csvSafeCell(undefined, undefined)).toBe("");
  });
});

describe("latest change per entity", () => {
  const row = (id: number, entity_ref: string, via: string | null) => ({ id, entity_ref, via });

  it("keeps the newest audit row per entity across pages", () => {
    const latest = new Map<string, ReturnType<typeof row>>();
    trackLatestChange(latest, [row(1, "dc/UK1/A", "Keelson"), row(2, "dc/NL1/A", "Demo director")]);
    trackLatestChange(latest, [row(5, "dc/UK1/A", "Demo director"), row(4, "dc/NL1/A", "Keelson"), row(3, "dc/NL1/A", "Merxa UI")]);
    expect(latest.get("dc/UK1/A")?.id).toBe(5);
    expect(latest.get("dc/NL1/A")?.id).toBe(4);
  });

  it("only reports entities whose latest change was external", () => {
    const latest = trackLatestChange(new Map(), [
      row(1, "dc/UK1/A", "Keelson"),
      row(2, "dc/UK1/A", "Demo director"), // a later internal change clears the highlight
      row(3, "dc/NL1/A", "Keelson"),
      row(4, "dc/DE1/A", "API"),
    ]);
    expect([...externallyChangedEntities(latest).keys()].sort()).toEqual(["dc/DE1/A", "dc/NL1/A"]);
  });
});
