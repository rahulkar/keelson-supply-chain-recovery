import { randomUUID } from "node:crypto";
import { expect, request, type APIRequestContext } from "@playwright/test";
import { KEELSON, MERXA } from "./env";

/**
 * Out-of-band API helpers (own cookie jars, never the browser's): demo resets and reading
 * state back. Every call goes through Caddy like the browser does.
 */
export type KeelsonApi = { ctx: APIRequestContext; csrf: string; username: string };

export async function keelsonApi(username: string): Promise<KeelsonApi> {
  const ctx = await request.newContext({ baseURL: KEELSON, extraHTTPHeaders: { Origin: KEELSON } });
  const r = await ctx.post("/api/demo/login-as", { data: { username } });
  expect(r.status(), `Keelson login-as ${username}`).toBe(200);
  const me = await ctx.get("/api/me");
  expect(me.status(), "GET /api/me").toBe(200);
  const body = (await me.json()) as { csrf_token: string };
  return { ctx, csrf: body.csrf_token, username };
}

export async function merxaApi(username: string): Promise<APIRequestContext> {
  const ctx = await request.newContext({ baseURL: MERXA, extraHTTPHeaders: { Origin: MERXA } });
  const r = await ctx.post("/v1/auth/demo/login-as", { data: { username } });
  expect(r.status(), `Merxa login-as ${username}`).toBe(200);
  return ctx;
}

/**
 * Re-seed Merxa from the golden seed (demo director, `merxa.admin`), then bring Keelson's
 * canonical store up to date (M3: cases are framed from the synced `scn` snapshot, and a Merxa
 * scenario reset is not a webhook event — the tier-C poll would only notice it within 5 min).
 */
export async function resetMerxa(): Promise<void> {
  const ctx = await merxaApi("merxa.admin");
  try {
    const r = await ctx.post("/v1/admin/scenario:reset", {
      headers: { "Idempotency-Key": randomUUID() },
      timeout: 120_000,
    });
    expect(r.status(), `Merxa scenario reset: ${await r.text()}`).toBe(200);
  } finally {
    await ctx.dispose();
  }
  await resyncKeelson();
}

type Snapshot = { id: string; status: string; error?: string | null };
type Snapshots = { current: Snapshot | null; items: Snapshot[] };

/**
 * Tenant admin `sam` queues a canonical snapshot sync of the Merxa connector and waits until a
 * snapshot whose sync started after the request is current.
 */
export async function resyncKeelson(timeout = 240_000): Promise<string> {
  const sam = await keelsonApi("sam");
  try {
    const read = async (): Promise<Snapshots> => {
      const r = await sam.ctx.get("/api/admin/connectors/merxa/snapshots?limit=10");
      expect(r.status(), "GET snapshots").toBe(200);
      return (await r.json()) as Snapshots;
    };
    const before = (await read()).current?.id ?? "";
    // A sync already running when Merxa was reset read the old data: only a sync that STARTED
    // after this moment counts (snapshot ids are UUIDv7: their first 48 bits are the start in ms).
    const requested = Date.now();
    const startedAfter = (id: string) => id > before && parseInt(id.replace(/-/g, "").slice(0, 12), 16) >= requested;
    const queued = await sam.ctx.post("/api/admin/connectors/merxa/sync", { headers: { "X-CSRF-Token": sam.csrf } });
    expect(queued.status(), `queue an scn sync: ${await queued.text()}`).toBe(202);
    const deadline = Date.now() + timeout;
    for (;;) {
      const body = await read();
      if (body.current && body.current.status === "ready" && startedAfter(body.current.id)) return body.current.id;
      const failed = body.items.find((i) => i.status === "failed" && startedAfter(i.id));
      if (failed) throw new Error(`scn sync failed: ${failed.error ?? "unknown error"}`);
      if (Date.now() > deadline) throw new Error("timed out waiting for the scn sync after the Merxa reset");
      await new Promise((r) => setTimeout(r, 2_000));
    }
  } finally {
    await sam.ctx.dispose();
  }
}

/** Drop `username`'s delegated Merxa link in Keelson, so approving needs Connect Merxa again. */
export async function disconnectMerxa(username: string): Promise<void> {
  const api = await keelsonApi(username);
  try {
    const r = await api.ctx.delete("/api/connectors/merxa/link", { headers: { "X-CSRF-Token": api.csrf } });
    expect(r.status(), `disconnect Merxa for ${username}`).toBe(200);
    const link = (await (await api.ctx.get("/api/connectors/merxa/link")).json()) as { apply_ready: boolean };
    expect(link.apply_ready, "apply_ready after disconnect").toBe(false);
  } finally {
    await api.ctx.dispose();
  }
}

export type ChangeSetDetail = {
  code: string;
  status: string;
  attribution_text?: string | null;
  approval_ref?: string | null;
  items: { seq: number; op: string; status: string }[];
};

export async function merxaChangeSet(code: string): Promise<ChangeSetDetail> {
  const ctx = await merxaApi("merxa.admin");
  try {
    const r = await ctx.get(`/v1/change-sets/${encodeURIComponent(code)}`);
    expect(r.status(), `GET change set ${code}`).toBe(200);
    return (await r.json()) as ChangeSetDetail;
  } finally {
    await ctx.dispose();
  }
}
