import { describe, expect, it } from "vitest";

import { ApiError, buildQuery, decodeParam, errorMessage, loginPath, readProblem, safeNext } from "./http";
import { oneOf, param, parseCodeList, parseSkuQtyList } from "./params";

describe("buildQuery", () => {
  it("drops empty values and repeats array keys", () => {
    expect(buildQuery({ a: 1, b: ["x", "y"], c: undefined, d: "", e: null, f: true })).toBe("?a=1&b=x&b=y&f=true");
    expect(buildQuery({})).toBe("");
    expect(buildQuery(undefined)).toBe("");
    expect(buildQuery({ q: "a b&c" })).toBe("?q=a+b%26c");
  });
});

describe("safeNext", () => {
  it("accepts same-site paths", () => {
    expect(safeNext("/purchase-orders/PO-BD-0001?line=3")).toBe("/purchase-orders/PO-BD-0001?line=3");
  });

  it.each([
    ["https://evil.example/"],
    ["//evil.example"],
    ["/\\evil.example"],
    ["/\tevil"],
    ["relative/path"],
    ["/login?next=/x"],
    [""],
    [null],
  ])("rejects %s", (value) => {
    expect(safeNext(value as string | null)).toBe("/change-batches");
  });

  it("builds login paths", () => {
    expect(loginPath("/inventory?dc=UK1")).toBe("/login?next=%2Finventory%3Fdc%3DUK1");
    expect(loginPath("//evil")).toBe("/login");
    expect(loginPath(null)).toBe("/login");
  });
});

describe("problems", () => {
  it("parses RFC 9457 problem documents", async () => {
    const res = new Response(JSON.stringify({ title: "Insufficient scope", status: 403, code: "insufficient_scope", detail: "requires scope po:read" }), {
      status: 403,
      headers: { "content-type": "application/problem+json" },
    });
    const err = new ApiError(403, await readProblem(res));
    expect(err.code).toBe("insufficient_scope");
    expect(errorMessage(err)).toBe("requires scope po:read");
  });

  it("tolerates FastAPI validation errors and plain-text bodies", async () => {
    const v = await readProblem(new Response(JSON.stringify({ detail: [{ loc: ["body"], msg: "x" }] }), { status: 422 }));
    expect(v.code).toBe("invalid_request");
    const t = await readProblem(new Response("upstream down", { status: 502, statusText: "Bad Gateway" }));
    expect(t).toMatchObject({ status: 502, title: "Bad Gateway", detail: "upstream down" });
    expect(new ApiError(502, t).code).toBe("http_502");
  });

  it("decodes route params safely", () => {
    expect(decodeParam("CS-0001")).toBe("CS-0001");
    expect(decodeParam("PO%2DBD")).toBe("PO-BD");
    expect(decodeParam("%E0%A4%A")).toBe("%E0%A4%A");
  });
});

describe("search params", () => {
  it("reads and restricts params", () => {
    const sp = { a: " x ", b: ["y", "z"], c: "  ", s: "applied" };
    expect(param(sp, "a")).toBe("x");
    expect(param(sp, "b")).toBe("y");
    expect(param(sp, "c")).toBeUndefined();
    expect(oneOf(sp, "s", ["draft", "applied"] as const)).toBe("applied");
    expect(oneOf({ s: "evil" }, "s", ["draft"] as const)).toBeUndefined();
  });

  it("parses SKU lists", () => {
    expect(parseCodeList("puf-w-blk-m, PUF-W-BLK-L;PUF-W-BLK-M")).toEqual(["PUF-W-BLK-M", "PUF-W-BLK-L"]);
    expect(parseCodeList(undefined)).toEqual([]);
  });

  it("parses operator-guide line lists", () => {
    expect(parseSkuQtyList("PUF-W-BLK-M:120,PUF-W-BLK-XS:80")).toEqual({
      lines: [
        { sku: "PUF-W-BLK-M", qty: 120 },
        { sku: "PUF-W-BLK-XS", qty: 80 },
      ],
      invalid: [],
    });
    expect(parseSkuQtyList("PUF-W-BLK-M:0, :5, PUF-W-BLK-L:abc, PUF-W-BLK-L × 7")).toEqual({
      lines: [{ sku: "PUF-W-BLK-L", qty: 7 }],
      invalid: ["PUF-W-BLK-M:0", ":5", "PUF-W-BLK-L:abc"],
    });
  });
});
