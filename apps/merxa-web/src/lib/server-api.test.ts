// @vitest-environment node
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const cookieJar = new Map<string, string>();
vi.mock("next/headers", () => ({
  cookies: async () => ({ get: (name: string) => (cookieJar.has(name) ? { name, value: cookieJar.get(name) } : undefined) }),
  headers: async () => new Headers({ "x-merxa-path": "/inventory?dc=UK1" }),
}));
const redirect = vi.fn((url: string) => {
  throw new Error(`NEXT_REDIRECT ${url}`);
});
vi.mock("next/navigation", () => ({
  redirect: (url: string) => redirect(url),
  notFound: () => {
    throw new Error("NEXT_NOT_FOUND");
  },
}));

const { apiGet, apiGetLatest, apiWalk, getSimWeek } = await import("./server-api");

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

/** Fake ascending, keyset-paginated listing of `total` rows with ids 1..total. */
function pagedListing(total: number) {
  return vi.fn(async (input: string | URL) => {
    const url = new URL(String(input));
    const limit = Number(url.searchParams.get("limit"));
    const after = Number(url.searchParams.get("cursor") ?? 0);
    const items = Array.from({ length: Math.max(0, Math.min(limit, total - after)) }, (_, i) => ({ id: after + i + 1 }));
    const last = items.at(-1)?.id ?? total;
    return json({ items, next_cursor: last < total ? String(last) : null });
  });
}

describe("server-side merxa-api client", () => {
  beforeEach(() => {
    cookieJar.clear();
    process.env.MERXA_API_INTERNAL_URL = "http://merxa-api:8100";
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    redirect.mockClear();
  });

  it("forwards only a well-formed merxa_session cookie", async () => {
    const fetchMock = vi.fn(async () => json({ ok: true }));
    vi.stubGlobal("fetch", fetchMock);
    cookieJar.set("merxa_session", "tok_ABC-123");
    cookieJar.set("other", "secret");
    await apiGet("/v1/dcs");
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://merxa-api:8100/v1/dcs",
      expect.objectContaining({ headers: { accept: "application/json", cookie: "merxa_session=tok_ABC-123" } }),
    );

    cookieJar.set("merxa_session", "tok; injected=1");
    await apiGet("/v1/dcs");
    expect(fetchMock).toHaveBeenLastCalledWith(
      "http://merxa-api:8100/v1/dcs",
      expect.objectContaining({ headers: { accept: "application/json" } }),
    );
  });

  it("redirects to /login with the current path on 401", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ status: 401, code: "unauthorized" }, 401)));
    await expect(apiGet("/v1/auth/me")).rejects.toThrow("NEXT_REDIRECT /login?next=%2Finventory%3Fdc%3DUK1");
  });

  it("walks every page of a listing", async () => {
    const fetchMock = pagedListing(2500);
    vi.stubGlobal("fetch", fetchMock);
    const seen: number[] = [];
    const result = await apiWalk<{ id: number }>("/v1/audit-log", { entity_type: "po_line" }, (page) =>
      seen.push(...page.map((r) => r.id)),
    );
    expect(result).toEqual({ rows: 2500, complete: true });
    expect(seen).toHaveLength(2500);
    expect(seen.at(-1)).toBe(2500);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("entity_type=po_line");
  });

  it("keeps the newest rows of an oldest-first listing (audit log)", async () => {
    vi.stubGlobal("fetch", pagedListing(12_345));
    const result = await apiGetLatest<{ id: number }>("/v1/audit-log", {}, { keep: 5000 });
    expect(result.items).toHaveLength(5000);
    expect(result.items[0]?.id).toBe(7346);
    expect(result.items.at(-1)?.id).toBe(12_345);
    expect(result).toMatchObject({ omittedOlder: true, complete: true });
  });

  it("reports small listings as complete and not truncated", async () => {
    vi.stubGlobal("fetch", pagedListing(3));
    const result = await apiGetLatest<{ id: number }>("/v1/audit-log", {}, { keep: 5000 });
    expect(result).toEqual({ items: [{ id: 1 }, { id: 2 }, { id: 3 }], omittedOlder: false, complete: true });
  });

  it("stops at the row cap and says so", async () => {
    vi.stubGlobal("fetch", pagedListing(10_000));
    const result = await apiWalk("/v1/audit-log", {}, () => {}, { maxRows: 3000 });
    expect(result).toEqual({ rows: 3000, complete: false });
  });
});

describe("getSimWeek (simulation clock via /v1/lane-options)", () => {
  afterEach(() => vi.unstubAllGlobals());

  it("reads the week merxa-api echoes for a lane-options query without a week", async () => {
    const fetchMock = vi.fn(async () => json({ origin: "-", dest: "-", week: "2026-W41", options: [] }));
    vi.stubGlobal("fetch", fetchMock);
    await expect(getSimWeek()).resolves.toBe("2026-W41");
    const url = new URL(String((fetchMock.mock.calls[0] as unknown as [string])[0]));
    expect(url.pathname).toBe("/v1/lane-options");
    expect(url.searchParams.get("origin")).toBe("-");
    expect(url.searchParams.has("week")).toBe(false);
  });

  it("returns null when the clock cannot be read (no po:read, malformed or unreachable)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ status: 403, code: "insufficient_scope" }, 403)));
    await expect(getSimWeek()).resolves.toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => json({ week: "next week" })));
    await expect(getSimWeek()).resolves.toBeNull();
    vi.stubGlobal("fetch", vi.fn(async () => Promise.reject(new TypeError("fetch failed"))));
    await expect(getSimWeek()).resolves.toBeNull();
  });
});
