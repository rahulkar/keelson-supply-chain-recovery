import { describe, expect, it } from "vitest";
import { summariseUsage } from "@/components/admin/GatewayPage";
import { adminRoutes, makeAdminServer } from "../../../test/adminMock";
import { fakeApi, jsonResponse } from "../../../test/session";
import { ApiError } from "../api/client";
import { AdminApi } from "./client";
import { adminError, keyPrefix } from "./format";
import { auditListSchema, connectorTestSchema } from "./types";

function setup() {
  const server = makeAdminServer();
  const { api, calls } = fakeApi(adminRoutes(server));
  return { admin: new AdminApi(api), calls, server };
}

describe("AdminApi (70 §1–§5)", () => {
  it("reads the agent spec, versions, a version and the catalog", async () => {
    const { admin } = setup();
    const spec = await admin.getSpec();
    expect(spec.version).toBe("v3");
    expect(spec.spec.policy.auto_max_lines).toBe(10);
    expect((await admin.listSpecVersions()).map((v) => v.version)).toEqual(["v3", "v2"]);
    // enveloped and bare versions both parse
    expect((await admin.getSpecVersion("v3")).spec.models["recommender"]).toBe("reasoning-default");
    expect((await admin.getSpecVersion("v2")).spec.models["recommender"]).toBe("fast-default");
    const catalog = await admin.catalog();
    expect(catalog.aliases.map((a) => [a.alias, a.mode, a.capabilities?.tool_loop_signatures ?? null])).toEqual([
      ["fast-default", "chat", true],
      ["reasoning-default", null, null],
      ["gpt-responses", "responses", false],
      ["chat-no-tools", "chat", false],
    ]);
    // The engine nests the classification; the UI reads it from there.
    const tool = (name: string) => catalog.connectors[0]!.tools.find((t) => t.name === name)!;
    expect(tool("apply_change_set")).toMatchObject({ access: "write", content_trust: "trusted_data", status: "approved", bindable: false });
    // No classification at all is never read as a read tool with trusted data (fail closed).
    expect(tool("mystery_tool")).toMatchObject({ access: undefined, content_trust: undefined });
    expect(catalog.platform_max?.max_llm_calls).toBe(200);
  });

  it("saves with PUT {base_version, spec_patch} and the CSRF token; restore is a POST", async () => {
    const { admin, calls } = setup();
    expect(await admin.saveSpec("v3", { models: { recommender: "fast-default" } })).toMatchObject({ kind: "applied", value: { version: "v4", unchanged: false } });
    const put = calls.find((c) => c.method === "PUT")!;
    expect(put.url).toBe("/api/admin/agents/spec");
    expect(put.body).toEqual({ base_version: "v3", spec_patch: { models: { recommender: "fast-default" } } });
    expect(put.headers["X-CSRF-Token"]).toBe("csrf-test");
    expect(put.headers["Content-Type"]).toBe("application/json");
    await admin.restoreSpec("v2");
    expect(calls.at(-1)).toMatchObject({ url: "/api/admin/agents/spec/restore", method: "POST", body: { version: "v2" } });
  });

  it("surfaces a stale base version (409) and a guarded change (403) as ApiErrors with human text", async () => {
    const { admin, server } = setup();
    const stale = await admin.saveSpec("v1", {}).catch((e: unknown) => e);
    expect(stale).toBeInstanceOf(ApiError);
    expect((stale as ApiError).status).toBe(409);
    expect(adminError(stale)).toMatch(/Someone saved a newer version/);
    server.failures["PUT /api/admin/agents/spec"] = [403, { code: "guarded_change", message: "requires platform-admin" }];
    const guarded = await admin.saveSpec("v3", { policy: { auto_max_lines: 99 } }).catch((e: unknown) => e);
    expect(adminError(guarded)).toBe("Requires platform-admin.");
  });

  it("gateway: models ({items}), tenant, PATCH limits, rotate key, usage, PUT/DELETE a model", async () => {
    const { admin, calls } = setup();
    const models = await admin.models();
    expect(models.map((m) => [m.alias, m.health])).toEqual([
      ["fast-default", "healthy"],
      ["reasoning-default", "unhealthy"],
    ]);
    expect((await admin.gatewayTenant()).key_prefix).toBe("sk-abcd…");
    // The engine already appends "…" to key prefixes: shown once, never "……".
    expect(keyPrefix("sk-abcd…")).toBe("sk-abcd…");
    expect(keyPrefix("sk-new")).toBe("sk-new…");
    expect(keyPrefix(null)).toBe("—");
    await admin.patchGatewayTenant({ rpm_limit: 60 });
    expect(calls.at(-1)).toMatchObject({ method: "PATCH", url: "/api/admin/gateway/tenant", body: { rpm_limit: 60 } });
    expect(await admin.rotateKey()).toEqual({ key_prefix: "sk-new" });
    expect((await admin.usage(7)).length).toBe(3);
    expect(calls.at(-1)!.url).toBe("/api/admin/gateway/usage?days=7");
    await admin.upsertModel("anthropic/claude-fast", { provider_model: "anthropic/claude-x", mode: "chat", params: {}, grant_all_tenants: true });
    expect(calls.at(-1)).toMatchObject({ method: "PUT", url: "/api/admin/gateway/models/anthropic%2Fclaude-fast" });
    await admin.deleteModel("old-alias");
    expect(calls.at(-1)).toMatchObject({ method: "DELETE", url: "/api/admin/gateway/models/old-alias" });
    expect(calls.at(-1)!.headers["X-CSRF-Token"]).toBe("csrf-test");
  });

  it("usage accepts per-alias groupings too", async () => {
    const { api } = fakeApi([
      (url) =>
        url.startsWith("/api/admin/gateway/usage")
          ? jsonResponse(200, { aliases: { "fast-default": [{ date: "2026-10-05", spend_eur: 1, tokens_in: 1, tokens_out: 1, requests: 1 }] } })
          : undefined,
    ]);
    expect(await new AdminApi(api).usage()).toEqual([
      { date: "2026-10-05", spend_eur: 1, tokens_in: 1, tokens_out: 1, requests: 1, alias: "fast-default" },
    ]);
  });

  it("usageReport keeps the engine's zero-filled daily totals (quiet days still get a bar)", async () => {
    // Engine `gateway.usage`: {days, from, to, items (per alias, only days with traffic), daily (every day)}.
    const daily = ["2026-09-29", "2026-09-30", "2026-10-01", "2026-10-02", "2026-10-03", "2026-10-04", "2026-10-05"].map((date) => ({
      date,
      spend_eur: date === "2026-10-05" ? 2 : 0,
      tokens_in: 0,
      tokens_out: 0,
      requests: date === "2026-10-05" ? 3 : 0,
    }));
    const { api } = fakeApi([
      (url) =>
        url.startsWith("/api/admin/gateway/usage")
          ? jsonResponse(200, {
              days: 7,
              from: "2026-09-29",
              to: "2026-10-05",
              items: [{ date: "2026-10-05", alias: "fast-default", spend_eur: 2, tokens_in: 10, tokens_out: 5, requests: 3 }],
              daily,
            })
          : undefined,
    ]);
    const admin = new AdminApi(api);
    const report = await admin.usageReport(7);
    expect(report.rows).toHaveLength(1);
    expect(report.daily?.map((d) => d.date)).toEqual(daily.map((d) => d.date));
    const { days, aliases } = summariseUsage(report.rows, report.daily);
    expect(days).toHaveLength(7);
    expect(days.at(-1)).toEqual({ date: "2026-10-05", spend: 2 });
    expect(days[0]).toEqual({ date: "2026-09-29", spend: 0 });
    expect(aliases.map((a) => [a.alias, a.requests])).toEqual([["fast-default", 3]]);
    // Without `daily` (older shapes) the days come from the rows.
    expect((await admin.usage(7)).length).toBe(1);
    expect(summariseUsage(report.rows, null).days).toEqual([{ date: "2026-10-05", spend: 2 }]);
  });

  it("connectors: create, test, tools, PATCH a tool, activate, events, delete", async () => {
    const { admin, calls } = setup();
    const created = await admin.createConnector({
      slug: "erp-lite",
      name: "ERP Lite",
      mcp_url: "http://merxa.localhost/mcp-lite",
      trust_level: "third_party",
      auth: { kind: "static_header", header_name: "X-Merxa-Api-Key", secret: "s3cret" },
    });
    expect(created).toEqual({ slug: "erp-lite", status: "draft" });
    const test = await admin.testConnector("erp-lite");
    expect(test.ok).toBe(true);
    expect(test.steps.map((s) => s.step)).toContain("tier_detect");
    expect((await admin.connectorTools("erp-lite")).map((t) => t.status)).toEqual(["needs_review", "needs_review"]);
    const t = await admin.patchTool("erp-lite", "list_items", { status: "approved" });
    expect(t.kind === "applied" ? t.value?.status : null).toBe("approved");
    expect(calls.at(-1)).toMatchObject({ method: "PATCH", url: "/api/admin/connectors/erp-lite/tools/list_items", body: { status: "approved" } });
    await admin.activateConnector("erp-lite");
    expect(calls.at(-1)).toMatchObject({ method: "POST", url: "/api/admin/connectors/erp-lite/activate" });
    expect((await admin.connectorEvents("merxa", 5))[0]!.key).toBe("evt_1");
    expect(calls.at(-1)!.url).toBe("/api/admin/connectors/merxa/events?limit=5");
    await admin.deleteConnector("erp-lite"); // 204 without a body
    expect(calls.at(-1)).toMatchObject({ method: "DELETE", url: "/api/admin/connectors/erp-lite" });
  });

  it("tenants, packs and audit", async () => {
    const { admin, calls } = setup();
    expect((await admin.packs()).map((p) => p.name)).toEqual(["value-fashion-eu", "blank"]);
    expect((await admin.tenants()).length).toBe(2);
    await admin.createTenant({ slug: "acme", name: "Acme", pack: "blank", admin_usernames: ["sam"] });
    expect(calls.at(-1)).toMatchObject({ method: "POST", url: "/api/admin/tenants", body: { slug: "acme", pack: "blank", admin_usernames: ["sam"] } });
    const audit = await admin.audit({ action: "agent_spec.update", limit: 50 });
    expect(calls.at(-1)!.url).toBe("/api/admin/audit?limit=50&action=agent_spec.update");
    // The engine sends `actor_username` (no `actor`): that is who is shown, never "system".
    expect(audit.items.map((a) => a.actorName)).toEqual(["sam", "ava"]);
    expect(audit.items[0]!.when).toBe("2026-10-06T09:00:00Z");
    expect(audit.nextBefore).toBeNull();
  });

  it("audit: never asks for more than the engine's 200 rows, never sends tenant=, pages with before", async () => {
    const { admin, calls } = setup();
    // The engine rejects limit > 200 with 422; the page used to ask for 500.
    const page = await admin.audit({ limit: 500 });
    expect(calls.at(-1)!.url).toBe("/api/admin/audit?limit=200");
    expect(page.items).toHaveLength(2);
    // `tenant=` on /api/admin/* switches a platform admin into another tenant: not a filter.
    await admin.audit({ ...({ tenant: "northwind-test" } as object), scope: "platform", before: 3 });
    expect(calls.at(-1)!.url).toBe("/api/admin/audit?limit=200&before=3&scope=platform");
    expect(calls.every((c) => !c.url.includes("tenant="))).toBe(true);
    const older = await admin.audit({ limit: 1 });
    expect(older.items.map((i) => i.key)).toEqual(["2"]);
    expect(older.nextBefore).toBe(2);
    expect((await admin.audit({ limit: 1, before: older.nextBefore })).items.map((i) => i.key)).toEqual(["1"]);
  });

  it("audit rows: actor object, actor_username, actor_sub, else system", () => {
    const rows = auditListSchema.parse([
      { id: 1, action: "a", actor: { name: "Sam Okafor", username: "sam" } },
      { id: 2, action: "a", actor_username: "nora" },
      { id: 3, action: "a", actor_sub: "svc-123" },
      { id: 4, action: "a" },
    ]);
    expect(rows.map((r) => r.actorName)).toEqual(["Sam Okafor", "nora", "svc-123", "system"]);
  });

  it("connector detail carries the stored last test; unknown slugs are 404", async () => {
    const { admin, calls } = setup();
    const merxa = await admin.connector("merxa");
    expect(calls.at(-1)!.url).toBe("/api/admin/connectors/merxa");
    expect(connectorTestSchema.parse(merxa.last_test).steps.map((st) => st.step)).toEqual(["url_policy", "reach"]);
    expect(merxa.trust_level).toBe("first_party");
    await expect(admin.connector("nope")).rejects.toMatchObject({ status: 404 });
  });

  it("never sends a CSRF token on reads", async () => {
    const { admin, calls } = setup();
    await admin.connectors();
    expect(calls.at(-1)!.headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("an unexpected response shape is an error, not a crash", async () => {
    const { api } = fakeApi([(url) => (url === "/api/admin/connectors" ? jsonResponse(200, { nope: true }) : undefined)]);
    await expect(new AdminApi(api).connectors()).rejects.toMatchObject({ code: "invalid_response" });
  });
});
