import { describe, expect, it, vi } from "vitest";
import { ApiClient, ApiError, blockDataPath, describeError } from "./client";

type Call = { url: string; init: RequestInit };

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

const ME = {
  user: { sub: "s1", username: "priya", name: "Priya Shah", email: null, roles: ["planner"], tenants: ["demo-retail"], active_tenant: "demo-retail" },
  csrf_token: "csrf-1",
  demo_mode: true,
  brand: { engine: "Keelson", vendor: "Merxa" },
};

function client(responses: Response[], opts: { csrf?: string | null; onUnauthorized?: () => void } = {}) {
  const calls: Call[] = [];
  const fetchImpl = vi.fn(async (url: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    const next = responses.shift();
    if (!next) throw new Error(`unexpected call ${String(url)}`);
    return next;
  });
  const c = new ApiClient({
    fetch: fetchImpl as unknown as typeof fetch,
    csrfToken: opts.csrf === undefined ? "csrf-0" : opts.csrf,
    onUnauthorized: opts.onUnauthorized ?? (() => {}),
  });
  return { c, calls };
}

const headers = (call: Call) => call.init.headers as Record<string, string>;

describe("ApiClient", () => {
  it("sends messages same-origin with the session cookie, CSRF token and Idempotency-Key", async () => {
    const { c, calls } = client([json(202, { run_id: "run-1" })]);
    await expect(c.postMessage("case 1", "Trims are late", "key-1")).resolves.toBe("run-1");
    expect(calls[0]!.url).toBe("/api/cases/case%201/messages");
    expect(calls[0]!.init).toMatchObject({ method: "POST", credentials: "include", cache: "no-store" });
    expect(headers(calls[0]!)).toMatchObject({
      "X-CSRF-Token": "csrf-0",
      "Idempotency-Key": "key-1",
      "Content-Type": "application/json",
    });
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ text: "Trims are late" });
  });

  it("sends case actions with the contract body", async () => {
    const { c, calls } = client([json(202, { run_id: "run-2" })]);
    await c.postAction("c1", { type: "apply", option_id: "opt-A", plan_version: 3 }, "key-2");
    expect(calls[0]!.url).toBe("/api/cases/c1/actions");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ type: "apply", option_id: "opt-A", plan_version: 3 });
    expect(headers(calls[0]!)["Idempotency-Key"]).toBe("key-2");
  });

  it("does not send a CSRF token or Idempotency-Key on reads", async () => {
    const { c, calls } = client([json(200, { items: [] })]);
    await c.listCases(20);
    expect(calls[0]!.url).toBe("/api/cases?limit=20");
    expect(headers(calls[0]!)["X-CSRF-Token"]).toBeUndefined();
    expect(headers(calls[0]!)["Idempotency-Key"]).toBeUndefined();
  });

  it("fetches the CSRF token from /api/me when it does not have one", async () => {
    const { c, calls } = client([json(200, ME), json(201, { id: "case-9" })], { csrf: null });
    await expect(c.createCase()).resolves.toBe("case-9");
    expect(calls.map((x) => x.url)).toEqual(["/api/me", "/api/cases"]);
    expect(headers(calls[1]!)["X-CSRF-Token"]).toBe("csrf-1");
  });

  it("refreshes the CSRF token once on 403 csrf_failed and retries with the same Idempotency-Key", async () => {
    const { c, calls } = client([
      json(403, { code: "csrf_failed", message: "missing or invalid X-CSRF-Token" }),
      json(200, ME),
      json(202, { run_id: "run-3" }),
    ]);
    await expect(c.postMessage("c1", "hi", "key-3")).resolves.toBe("run-3");
    expect(calls.map((x) => x.url)).toEqual(["/api/cases/c1/messages", "/api/me", "/api/cases/c1/messages"]);
    expect(headers(calls[2]!)).toMatchObject({ "X-CSRF-Token": "csrf-1", "Idempotency-Key": "key-3" });
  });

  it("hands 401 to onUnauthorized and rejects", async () => {
    const onUnauthorized = vi.fn();
    const { c } = client([json(401, { code: "unauthenticated", message: "sign in required" })], { onUnauthorized });
    await expect(c.getCase("c1")).rejects.toMatchObject({ status: 401, code: "unauthenticated" });
    expect(onUnauthorized).toHaveBeenCalledTimes(1);
  });

  it("exposes the error body (412 connect_required with connect_url)", async () => {
    const { c } = client([
      json(412, { code: "connect_required", message: "connect your Merxa account to approve", connect_url: "http://keelson.localhost/api/connectors/merxa/connect?return_to=%2Fcases%2Fc1" }),
    ]);
    const err = (await c.decide("approval-1", { decision: "approve", plan_version: 1, change_set_hash: "a".repeat(64) }).catch((e: unknown) => e)) as ApiError;
    expect(err).toBeInstanceOf(ApiError);
    expect(err.code).toBe("connect_required");
    expect(err.body["connect_url"]).toContain("/api/connectors/merxa/connect");
  });

  it("maps non-JSON failures and network errors", async () => {
    const { c } = client([new Response("<html>bad gateway</html>", { status: 502, statusText: "Bad Gateway" })]);
    await expect(c.listCases()).rejects.toMatchObject({ status: 502, code: "http_502" });
    const failing = new ApiClient({
      fetch: (async () => {
        throw new TypeError("Failed to fetch");
      }) as unknown as typeof fetch,
      csrfToken: "x",
    });
    const err = await failing.listCases().catch((e: unknown) => e);
    expect(err).toMatchObject({ code: "network_error", status: 0 });
    expect(describeError(err)).toMatch(/couldn't reach/i);
  });

  it("rejects responses that do not match the contract", async () => {
    const { c } = client([json(202, { nope: true })]);
    await expect(c.postMessage("c1", "x", "k")).rejects.toMatchObject({ code: "invalid_response" });
  });

  it("logout returns the IdP end-session URL and forgets the CSRF token", async () => {
    const { c, calls } = client([json(200, { ok: true, logout_url: "http://auth.localhost/logout" }), json(200, ME), json(202, { run_id: "r" })]);
    await expect(c.logout()).resolves.toBe("http://auth.localhost/logout");
    await c.postMessage("c1", "x", "k");
    expect(calls.map((x) => x.url)).toEqual(["/api/auth/logout", "/api/me", "/api/cases/c1/messages"]);
  });

  it("switches persona with POST /api/demo/login-as", async () => {
    const { c, calls } = client([json(200, { ok: true, ...ME })]);
    await c.loginAs("marco");
    expect(calls[0]!.url).toBe("/api/demo/login-as");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ username: "marco" });
    expect(headers(calls[0]!)["X-CSRF-Token"]).toBe("csrf-0");
  });

  it("reads the viewer's connector link (apply_ready, or derived from status + scopes)", async () => {
    const { c, calls } = client([
      json(200, { status: "active", ext_username: "marco", roles: ["approver"], scopes: ["changes:apply"], apply_ready: true }),
      json(200, { status: "active", ext_username: "marco", roles: [], scopes: ["openid"], apply_ready: false }),
      json(200, { status: "active", ext_username: "marco", roles: [], scopes: ["openid", "changes:apply"] }),
      json(200, { status: "revoked", ext_username: null, roles: [], scopes: [], apply_ready: true }),
    ]);
    expect(await c.connectorLink("merxa")).toEqual({ status: "active", applyReady: true, extUsername: "marco" });
    expect(await c.connectorLink("merxa")).toEqual({ status: "active", applyReady: false, extUsername: "marco" });
    expect(await c.connectorLink("merxa")).toEqual({ status: "active", applyReady: true, extUsername: "marco" });
    // an unknown status never counts as linked
    expect(await c.connectorLink("merxa")).toEqual({ status: "missing", applyReady: false, extUsername: null });
    expect(calls[0]!.url).toBe("/api/connectors/merxa/link");
    expect(calls[0]!.init.method).toBe("GET");
    expect((calls[0]!.init.headers as Record<string, string>)["X-CSRF-Token"]).toBeUndefined();
  });

  it("describes errors in plain sentences", () => {
    expect(describeError(new ApiError(409, "stale_plan", "the plan changed"))).toMatch(/out of date/);
    expect(describeError(new ApiError(403, "forbidden", "you do not have a role that allows deciding"))).toBe(
      "You do not have a role that allows deciding.",
    );
    expect(describeError(new Error("x"))).toBe("Something went wrong.");
  });
});

describe("blockData (data_ref payloads)", () => {
  it("GETs /api/blocks/{id}/data (id encoded) and passes the abort signal", async () => {
    const seen: { url: string; signal: AbortSignal | null | undefined }[] = [];
    const api = new ApiClient({
      fetch: (async (input: RequestInfo | URL, init: RequestInit = {}) => {
        seen.push({ url: String(input), signal: init.signal });
        return new Response(JSON.stringify({ nodes: [] }), { status: 200, headers: { "Content-Type": "application/json" } });
      }) as typeof fetch,
      csrfToken: "x",
      onUnauthorized: () => {},
    });
    const ctrl = new AbortController();
    await expect(api.blockData("blk/1", { signal: ctrl.signal })).resolves.toEqual({ nodes: [] });
    expect(seen[0]!.url).toBe("/api/blocks/blk%2F1/data");
    expect(seen[0]!.signal).toBe(ctrl.signal);
    expect(blockDataPath("a b")).toBe("/api/blocks/a%20b/data");
  });

  it("refuses dot-segment ids, which the URL parser would resolve outside /api/blocks/", async () => {
    // new URL("/api/blocks/../data", origin) is /api/data; "%2E%2E" is a dot segment too.
    expect(new URL("/api/blocks/../data", "http://x").pathname).toBe("/api/data");
    expect(() => blockDataPath("..")).toThrow(/Invalid block id/);
    expect(() => blockDataPath(".")).toThrow(/Invalid block id/);
    expect(() => blockDataPath("")).toThrow(/Invalid block id/);
    expect(blockDataPath("...")).toBe("/api/blocks/.../data");
    expect(blockDataPath("%2E%2E")).toBe("/api/blocks/%252E%252E/data");
    const fetchSpy = vi.fn();
    const api = new ApiClient({ fetch: fetchSpy as unknown as typeof fetch, csrfToken: "x", onUnauthorized: () => {} });
    await expect(api.blockData("..")).rejects.toThrow(/Invalid block id/);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("an aborted request rejects with the abort error, not a network_error", async () => {
    const api = new ApiClient({
      fetch: ((_i: RequestInfo | URL, init: RequestInit = {}) =>
        new Promise((_r, reject) => init.signal?.addEventListener("abort", () => reject(new DOMException("Aborted", "AbortError"))))) as typeof fetch,
      csrfToken: "x",
      onUnauthorized: () => {},
    });
    const ctrl = new AbortController();
    const p = api.blockData("b", { signal: ctrl.signal });
    ctrl.abort();
    await expect(p).rejects.toMatchObject({ name: "AbortError" });
  });
});
