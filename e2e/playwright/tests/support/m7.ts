import { randomUUID } from "node:crypto";
import { expect } from "@playwright/test";
import { keelsonApi, merxaApi, type KeelsonApi } from "./api";
import { MERXA } from "./env";

/**
 * M7 set-up helpers (docs/contracts/110): the foreign-ERP connector (Merxa `/mcp-lite`, static
 * API-key header) on northwind-test, a temporary tenant-admin role for nora (that tenant has no admin
 * persona; the web back-office never acts cross-tenant), and four-eyes change requests.
 * Everything goes through the real APIs; every helper has its undo.
 */

export const NW = "northwind-test";
export const LITE = "merxa-lite";
const KEY_PREFIX = "keelson-m7-pw-";
const READS = ["list_items", "get_item"];
const WRITES = ["update_item", "create_transfer_order"];

type Json = Record<string, unknown>;

async function call(api: KeelsonApi, method: string, path: string, data?: unknown, tenant?: string): Promise<{ status: number; body: Json }> {
  const url = `/api/admin${path}${tenant ? `${path.includes("?") ? "&" : "?"}tenant=${tenant}` : ""}`;
  const r = await api.ctx.fetch(url, {
    method,
    ...(data !== undefined ? { data } : {}),
    headers: method === "GET" ? {} : { "X-CSRF-Token": api.csrf },
    timeout: 120_000,
  });
  const text = await r.text();
  let body: Json = {};
  try {
    body = text ? (JSON.parse(text) as Json) : {};
  } catch {
    body = { raw: text.slice(0, 300) };
  }
  return { status: r.status(), body };
}

export async function admin(api: KeelsonApi, method: string, path: string, data?: unknown, tenant?: string, ...ok: number[]): Promise<Json> {
  const r = await call(api, method, path, data, tenant);
  const codes = ok.length ? ok : [200];
  expect(codes, `${method} ${path}${tenant ? ` (${tenant})` : ""}: HTTP ${r.status} ${JSON.stringify(r.body).slice(0, 300)}`).toContain(r.status);
  return r.body;
}

/** Mint a Merxa API key (read scopes + changes:apply); revokes the keys of earlier runs. */
async function mintKey(): Promise<string> {
  const mx = await merxaApi("merxa.admin");
  try {
    const r = await mx.post(`${MERXA}/v1/api-keys`, {
      data: { name: `${KEY_PREFIX}${Date.now()}`, scopes: ["catalog:read", "inventory:read", "po:read", "orders:read", "changes:apply"] },
      headers: { "Idempotency-Key": randomUUID() },
    });
    expect(r.status(), "mint Merxa API key").toBe(201);
    const body = (await r.json()) as { id: string; key: string };
    const listed = (await (await mx.get(`${MERXA}/v1/api-keys`)).json()) as { items?: { id: string; name: string; revoked_at?: string | null }[] };
    for (const k of listed.items ?? []) {
      if (k.name.startsWith(KEY_PREFIX) && !k.revoked_at && k.id !== body.id) await mx.post(`${MERXA}/v1/api-keys/${k.id}:revoke`);
    }
    return body.key;
  } finally {
    await mx.dispose();
  }
}

/** Register (or re-register) merxa-lite on northwind-test, classify, activate, install the pack mappings. */
export async function setUpForeignErp(): Promise<void> {
  const ava = await keelsonApi("ava");
  try {
    const list = (await admin(ava, "GET", "/connectors", undefined, NW)) as { items?: { slug: string; status: string }[] };
    const existing = (list.items ?? []).find((c) => c.slug === LITE);
    const spec = (await admin(ava, "GET", "/agents/spec", undefined, NW)) as { version: string; spec: Json };
    const specialists = (spec.spec["specialists"] as { connector?: string; tools?: string[] }[] | undefined) ?? [];
    const bound = spec.spec["connector"] === LITE || spec.spec["answer_connector"] === LITE || specialists.some((s) => s.connector === LITE && (s.tools ?? []).length > 0);
    const key = await mintKey();
    if (existing && bound) {
      // the live foreign-ERP run plans on it: keep it and rotate its API key in place
      await admin(ava, "PUT", `/connectors/${LITE}/secret`, { secret: key }, NW);
    } else {
      if (existing) await admin(ava, "DELETE", `/connectors/${LITE}`, undefined, NW, 204);
      await admin(ava, "POST", "/connectors", {
        slug: LITE, name: "Merxa (mcp-lite, API key)", mcp_url: `${MERXA}/mcp-lite`, trust_level: "third_party",
        auth: { kind: "static_header", header_name: "X-Merxa-Api-Key", secret: key },
      }, NW, 201);
    }
    const test = (await admin(ava, "POST", `/connectors/${LITE}/test`, undefined, NW)) as { ok: boolean; tier: string[] };
    expect(test.ok && test.tier.join() === "A", "the lite connector tests as tier A").toBe(true);
    for (const name of READS) await admin(ava, "PATCH", `/connectors/${LITE}/tools/${name}`, { status: "approved", access: "read", content_trust: "trusted_data" }, NW);
    for (const name of WRITES) {
      await admin(ava, "PATCH", `/connectors/${LITE}/tools/${name}`, { status: "approved", access: "write", content_trust: "trusted_data", idempotency: "best_effort" }, NW);
    }
    const now = (await admin(ava, "GET", "/connectors", undefined, NW)) as { items?: { slug: string; status: string }[] };
    if ((now.items ?? []).find((c) => c.slug === LITE)?.status !== "active") await admin(ava, "POST", `/connectors/${LITE}/activate`, undefined, NW);
    await admin(ava, "POST", `/connectors/${LITE}/mappings/from-pack`, { pack: "foreign-erp-lite" }, NW);
  } finally {
    await ava.ctx.dispose();
  }
}

/** Disable merxa-lite again (no hourly crawls with a key the next Merxa reset revokes). */
export async function tearDownForeignErp(): Promise<void> {
  const ava = await keelsonApi("ava");
  try {
    await call(ava, "POST", `/connectors/${LITE}/disable`, undefined, NW);
  } finally {
    await ava.ctx.dispose();
  }
}

/** Set nora's product roles through the Users API (ava, platform admin, cross-tenant). */
export async function setNoraRoles(roles: string[]): Promise<void> {
  const ava = await keelsonApi("ava");
  try {
    const users = (await admin(ava, "GET", "/users", undefined, NW)) as { items: { id: string; username: string }[] };
    const nora = users.items.find((u) => u.username === "nora");
    expect(nora, "nora in northwind-test").toBeTruthy();
    await admin(ava, "PATCH", `/users/${nora!.id}`, { roles }, NW, 200);
  } finally {
    await ava.ctx.dispose();
  }
}

export type SpecEnvelope = { version: string; spec: Json };

export async function spec(username: string): Promise<SpecEnvelope> {
  const api = await keelsonApi(username);
  try {
    return (await admin(api, "GET", "/agents/spec")) as SpecEnvelope;
  } finally {
    await api.ctx.dispose();
  }
}

/** sam raises the auto-apply line cap: a pending four-eyes change request (202). */
export async function raiseAutoCap(by: number): Promise<{ id: string; lines: number }> {
  const sam = await keelsonApi("sam");
  try {
    const cur = (await admin(sam, "GET", "/agents/spec")) as SpecEnvelope;
    const lines = Number((cur.spec["policy"] as Json)["auto_max_lines"]);
    const cr = await admin(sam, "PUT", "/agents/spec", { base_version: cur.version, spec_patch: { policy: { auto_max_lines: lines + by } } }, undefined, 202);
    return { id: String(cr["id"]), lines };
  } finally {
    await sam.ctx.dispose();
  }
}

/** Make `version` the active demo-retail spec again (no-op when it already is). */
export async function restoreSpec(version: string): Promise<void> {
  const ava = await keelsonApi("ava");
  try {
    const cur = (await admin(ava, "GET", "/agents/spec")) as SpecEnvelope;
    if (cur.version !== version) await admin(ava, "POST", "/agents/spec/restore", { version }, undefined, 201);
  } finally {
    await ava.ctx.dispose();
  }
}

/** Reject every pending demo-retail change request (ava), e.g. after a failed run. */
export async function rejectPending(reason: string): Promise<void> {
  const ava = await keelsonApi("ava");
  try {
    const q = (await admin(ava, "GET", "/change-requests?status=pending")) as { items: { id: string }[] };
    for (const cr of q.items) await call(ava, "POST", `/change-requests/${cr.id}/reject`, { reason });
  } finally {
    await ava.ctx.dispose();
  }
}
