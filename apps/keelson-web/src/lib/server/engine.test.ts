import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/headers", () => ({
  cookies: async () => ({
    get: (name: string) => (name === "keelson_session" ? { name, value: "sess-1" } : undefined),
  }),
}));

import { getCase, getMe, listCases } from "./engine";

const fetchMock = vi.fn<typeof fetch>();

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => vi.unstubAllGlobals());

describe("server-side engine reads", () => {
  it("never sends an id that would turn into another engine route", async () => {
    for (const id of [".", "..", "", "a/b", "../me", "x".repeat(65), "%2e%2e"]) {
      expect(await getCase(id)).toEqual({ kind: "not_found" });
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards only the session cookie, clamps the limit and never follows redirects", async () => {
    fetchMock.mockResolvedValue(json(200, { items: [] }));
    expect(await listCases(500)).toEqual({ kind: "ok", data: [] });
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("http://engine-api:8000/api/cases?limit=200");
    expect(init?.headers).toEqual({ Accept: "application/json", Cookie: "keelson_session=sess-1" });
    expect(init?.redirect).toBe("manual");
    expect(init?.cache).toBe("no-store");
  });

  it("treats a redirect as unavailable (the cookie is not replayed elsewhere)", async () => {
    fetchMock.mockResolvedValue(new Response(null, { status: 307, headers: { Location: "/api/cases" } }));
    expect((await getCase("0199a0c4-redirect")).kind).toBe("unavailable");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("maps auth and lookup failures", async () => {
    fetchMock.mockResolvedValueOnce(json(401, { code: "unauthenticated" }));
    expect(await getMe()).toEqual({ kind: "unauthenticated" });
    fetchMock.mockResolvedValueOnce(json(403, { code: "forbidden" }));
    expect(await getCase("0199a0c4-forbidden")).toEqual({ kind: "forbidden" });
    fetchMock.mockResolvedValueOnce(json(404, { code: "not_found" }));
    expect(await getCase("0199a0c4-missing")).toEqual({ kind: "not_found" });
    fetchMock.mockResolvedValueOnce(json(200, { unexpected: true }));
    expect((await getCase("0199a0c4-bad-shape")).kind).toBe("unavailable");
    fetchMock.mockRejectedValueOnce(new TypeError("fetch failed"));
    expect((await getCase("0199a0c4-down")).kind).toBe("unavailable");
  });
});
