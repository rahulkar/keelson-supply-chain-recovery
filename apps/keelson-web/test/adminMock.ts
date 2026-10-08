/**
 * In-memory fake of the back-office API (`70-backoffice-m2.md` §1–§5) for tests, until the
 * engine side exists. Plug into `fakeApi([...adminRoutes(server)])`; mutate `server` to script.
 */
import { jsonResponse, type Route } from "./session";

export type AdminServer = ReturnType<typeof makeAdminServer>;

export function baseSpec() {
  return {
    tenant: "demo-retail",
    version: "v3",
    connector: "merxa",
    vendor_name: "Merxa",
    models: {
      intake: "fast-default",
      answer_investigator: "fast-default",
      answer_structurer: "fast-default",
      investigator: "fast-default",
      structurer: "fast-default",
      recommender: "reasoning-default",
    },
    prompts: { intake: "" },
    specialists: [{ slug: "inventory_demand", tools: ["get_inventory", "list_purchase_orders"], max_tool_calls: 15, connector: "merxa", display_name: null }],
    answer_tools: ["get_inventory"],
    answer_connector: "merxa",
    budgets: { max_llm_calls: 40, max_total_tokens: 600000 },
    policy: { version: "p-1", auto_max_lines: 10, auto_max_units: 500, approval_quorum: 1, auto_confirm_max_matched: 50, approver_role: "approver" },
    planning: { default_delay_days: 7 },
    history_messages: 8,
  };
}

export function makeAdminServer() {
  return {
    spec: { version: "v3", spec: baseSpec(), created_by: { name: "Sam Okafor" }, created_at: "2026-10-05T08:00:00Z", active: true } as Record<string, unknown> & {
      version: string;
      spec: ReturnType<typeof baseSpec>;
    },
    versions: [
      { version: "v3", created_by: { name: "Sam Okafor" }, created_at: "2026-10-05T08:00:00Z", summary: "Raised token budget" },
      { version: "v2", created_by: "ava", created_at: "2026-10-04T08:00:00Z", summary: null },
    ],
    oldSpecs: { v2: { ...baseSpec(), version: "v2", models: { ...baseSpec().models, recommender: "fast-default" } } } as Record<string, unknown>,
    // Engine shape (`agent_specs.catalog`): approved tools with a nested classification, `bindable` and `reason`.
    catalog: {
      version: "v3",
      roles: [{ role: "intake", display_name: "Intake", alias: "fast-default", prompt_role: "intake", uses_tools: false }],
      aliases: [
        { alias: "fast-default", mode: "chat", capabilities: { structured_output: true, reasoning_stream: true, tool_loop_signatures: true } },
        "reasoning-default",
        { alias: "gpt-responses", mode: "responses", capabilities: { structured_output: true, reasoning_stream: true, tool_loop_signatures: false } },
        { alias: "chat-no-tools", mode: "chat", capabilities: { structured_output: true, reasoning_stream: false, tool_loop_signatures: false } },
      ],
      connectors: [
        {
          slug: "merxa",
          name: "Merxa",
          status: "active",
          tools: [
            { name: "get_inventory", title: "Inventory positions", classification: { access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none" }, bindable: true, reason: null },
            { name: "list_purchase_orders", title: "Open POs", classification: { access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none" }, bindable: true, reason: null },
            { name: "get_demand_forecast", title: "Forecast", classification: { access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none" }, bindable: true, reason: null },
            { name: "list_supplier_messages", title: "Supplier messages", classification: { access: "read", content_trust: "untrusted_text", egress: "internal", idempotency: "none" }, bindable: false, reason: "returns untrusted free text" },
            { name: "apply_change_set", title: "Apply", classification: { access: "write", content_trust: "trusted_data", egress: "internal", idempotency: "enforced" }, bindable: false, reason: "write tool (only deterministic steps may call it)" },
            { name: "list_sailings", title: "Sailings", access: "read", content_trust: "trusted_data", status: "needs_review" },
            { name: "mystery_tool", title: "No classification sent" },
          ],
        },
        {
          slug: "erp-lite",
          name: "ERP Lite",
          status: "tested",
          tools: [
            { name: "list_items", title: "List items", classification: { access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none" }, bindable: false, reason: "connector is tested" },
          ],
        },
      ],
      platform_max: { max_llm_calls: 200, max_total_tokens: 2000000 },
      guarded_fields: ["policy.auto_max_lines", "policy.auto_max_units", "policy.approver_role", "policy.approval_quorum", "connector"],
    },
    models: [
      { alias: "fast-default", mode: "chat", capabilities: { structured_output: true, reasoning_stream: true, tool_loop_signatures: true }, health: "healthy", provider_model: "vertex_ai/gemini-2.5-flash", params: { temperature: 0.2 } },
      { alias: "reasoning-default", mode: "responses", capabilities: { structured_output: true, reasoning_stream: true, tool_loop_signatures: false }, health: { status: "unhealthy" } },
    ],
    gatewayTenant: { team_alias: "tenant-demo-retail", budget_eur: 50, spend_eur: 42.5, budget_duration: "30d", rpm_limit: 120, tpm_limit: 400000, models: ["fast-default", "reasoning-default"], key_prefix: "sk-abcd…" },
    usage: [
      { date: "2026-10-05", alias: "fast-default", spend_eur: 1.25, tokens_in: 1000, tokens_out: 200, requests: 10 },
      { date: "2026-10-05", alias: "reasoning-default", spend_eur: 0.75, tokens_in: 500, tokens_out: 100, requests: 2 },
      { date: "2026-10-04", alias: "fast-default", spend_eur: 0.5, tokens_in: 300, tokens_out: 50, requests: 3 },
    ],
    connectors: [
      { slug: "merxa", name: "Merxa Retail Suite", url: "http://merxa.localhost/mcp", auth_kind: "oauth_platform_merxa", tier: ["A", "B", "W"], status: "active", trust_level: "first_party", tools: { approved: 15, needs_review: 1, disabled: 0 }, last_test_at: "2026-10-05T07:00:00Z", last_test_ok: true, webhook: true },
    ] as Record<string, unknown>[],
    test: {
      ok: true,
      tier: ["A"],
      steps: [
        { step: "url_policy", ok: true, ms: 1 },
        { step: "reach", ok: true, ms: 12 },
        { step: "discover", ok: true, ms: 30, detail: "fallback: initialize" },
        { step: "auth", ok: true, ms: 5 },
        { step: "tools_list", ok: true, ms: 40, detail: "4 tools" },
        { step: "tier_detect", ok: true, ms: 1 },
        { step: "classify", ok: true, ms: 2 },
      ],
      tools: [
        { name: "list_items", title: "List items", suggested: { access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none" }, annotations: { readOnlyHint: true }, definition_hash: "h1" },
        { name: "update_item", title: "Update item", suggested: { access: "write", content_trust: "trusted_data", egress: "internal", idempotency: "best_effort" }, annotations: {}, definition_hash: "h2" },
      ],
    } as Record<string, unknown>,
    tools: [
      { name: "list_items", title: "List items", access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none", status: "needs_review", suggested: { access: "read" } },
      { name: "update_item", title: "Update item", access: "write", content_trust: "trusted_data", egress: "internal", idempotency: "best_effort", status: "needs_review", suggested: { access: "write" } },
    ] as Record<string, unknown>[],
    events: [{ event_id: "evt_1", type: "po.updated", received_at: "2026-10-06T08:00:00Z", processed_at: null }],
    tenants: [
      { slug: "demo-retail", name: "Demo Retail", pack: "value-fashion-eu", created_at: "2026-10-01T00:00:00Z" },
      { slug: "northwind-test", name: "Isolation test tenant", pack: "blank" },
    ],
    packs: [
      { name: "value-fashion-eu", description: "Merxa connector + lanes", connectors: [{}] },
      { name: "blank", description: "No connectors; answer path only", connectors: [] },
    ],
    /** Stored `last_test` per connector (only the detail route returns it, like the engine). */
    lastTests: {
      merxa: {
        ok: true,
        tier: ["A", "B", "W"],
        steps: [
          { step: "url_policy", ok: true, ms: 1, detail: "merxa.localhost (dev host)" },
          { step: "reach", ok: true, ms: 8, detail: "HTTP 200" },
        ],
        at: 1759651200,
      },
    } as Record<string, unknown>,
    // Engine shape (`core/audit.list_audit`): actor_username / actor_sub columns and `at`;
    // platform rows (tenant null) only with `scope=platform`.
    audit: [
      { id: 3, at: "2026-10-06T10:00:00Z", actor_sub: "u-ava", actor_username: "ava", tenant: null, action: "gateway.model.upsert", target: "model:fast-default", before: null, after: { mode: "chat" }, trace_id: null },
      { id: 2, at: "2026-10-06T09:00:00Z", actor_sub: "u-sam", actor_username: "sam", tenant: "demo-retail", action: "agent_spec.update", target: "agent_spec:v3", before: { a: 1 }, after: { a: 2 }, trace_id: null },
      { id: 1, at: "2026-10-06T08:00:00Z", actor_sub: "u-ava", actor_username: "ava", tenant: "demo-retail", action: "console.handoff", target: "console:gateway", before: null, after: null, trace_id: null },
    ] as Record<string, unknown>[],
    /** M7 activity timeline rows (engine `core/audit.list_activity` shape), newest first. */
    activity: [] as Record<string, unknown>[],
    // Knowledge base (docs/contracts/90 §7; engine shapes from `core/kb.py`).
    kbs: [
      { id: "kb-1", name: "Playbooks & policies", description: "Seeded demo playbooks", created_by: "bootstrap", created_at: "2026-10-06T08:00:00+00:00", documents: 2, ready_documents: 1, chunks: 14 },
    ] as Record<string, unknown>[],
    kbProfile: { id: 1, alias: "embed-default", provider_model: "vertex_ai/text-embedding-005", dims: 768, version: 1, active: true, status: "active", storage: "vector(768)" } as Record<string, unknown> | null,
    kbDocs: {
      "kb-1": [
        { id: "doc-1", kb_id: "kb-1", title: "Freight & expedite policy", filename: "freight-expedite-policy.md", mime: "text/markdown", bytes: 6144, sha256: "a".repeat(64), status: "ready", error: null, chunks: 9, tokens: 4100, created_by: "bootstrap", created_at: "2026-10-06T08:00:00+00:00", updated_at: "2026-10-06T08:00:01+00:00" },
        { id: "doc-2", kb_id: "kb-1", title: "DC-to-DC transfer SOP", filename: "dc-transfer-sop.pdf", mime: "application/pdf", bytes: 2_400_000, sha256: "b".repeat(64), status: "failed", error: "embedding_unavailable", chunks: 5, tokens: 2600, created_by: "sam", created_at: "2026-10-06T09:00:00+00:00", updated_at: "2026-10-06T09:00:05+00:00" },
      ],
    } as Record<string, Record<string, unknown>[]>,
    kbJobs: [] as Record<string, unknown>[],
    /** Usernames Keycloak doesn't know (`POST /tenants` reports them as `not_found`). */
    unknownUsers: ["ghost"] as string[],
    /** Override a route's answer: `failures["PUT /api/admin/agents/spec"] = [409, {...}]`. */
    failures: {} as Record<string, [number, Record<string, unknown>]>,
  };
}

const body = (init: RequestInit): Record<string, unknown> => (init.body ? (JSON.parse(String(init.body)) as Record<string, unknown>) : {});

export function adminRoutes(s: AdminServer): Route[] {
  return [
    (url, init) => {
      const method = init.method ?? "GET";
      const path = url.split("?")[0]!;
      const fail = s.failures[`${method} ${path}`];
      if (fail) return jsonResponse(fail[0], fail[1]);
      if (!path.startsWith("/api/admin/")) return undefined;
      const p = path.slice("/api/admin".length);
      const m = (re: RegExp) => re.exec(p);
      if (method === "GET" && p === "/agents/spec") return jsonResponse(200, s.spec);
      if (method === "GET" && p === "/agents/spec/versions") return jsonResponse(200, s.versions);
      let r = m(/^\/agents\/spec\/versions\/([^/]+)$/);
      if (method === "GET" && r) {
        const v = decodeURIComponent(r[1]!);
        if (v === s.spec.version) return jsonResponse(200, s.spec);
        return s.oldSpecs[v] ? jsonResponse(200, s.oldSpecs[v]) : jsonResponse(404, { code: "not_found", message: "no such version" });
      }
      if (method === "PUT" && p === "/agents/spec") {
        const b = body(init);
        if (b["base_version"] !== s.spec.version) {
          return jsonResponse(409, { code: "version_conflict", message: "the spec changed since you loaded it", latest_version: s.spec.version });
        }
        const n = Number(s.spec.version.slice(1)) + 1;
        s.spec = { ...s.spec, version: `v${n}` };
        return jsonResponse(201, { ...s.spec, unchanged: false });
      }
      if (method === "POST" && p === "/agents/spec/restore") {
        const n = Number(s.spec.version.slice(1)) + 1;
        s.spec = { ...s.spec, version: `v${n}` };
        return jsonResponse(200, { version: `v${n}` });
      }
      if (method === "GET" && p === "/agents/catalog") return jsonResponse(200, s.catalog);
      if (method === "GET" && p === "/gateway/models") return jsonResponse(200, { items: s.models });
      r = m(/^\/gateway\/models\/(.+)$/);
      if (r && method === "PUT") return jsonResponse(200, { alias: decodeURIComponent(r[1]!), ...body(init) });
      if (r && method === "DELETE") return new Response(null, { status: 204 });
      if (method === "GET" && p === "/gateway/tenant") return jsonResponse(200, s.gatewayTenant);
      if (method === "PATCH" && p === "/gateway/tenant") {
        s.gatewayTenant = { ...s.gatewayTenant, ...body(init) };
        return jsonResponse(200, s.gatewayTenant);
      }
      if (method === "POST" && p === "/gateway/tenant/rotate-key") return jsonResponse(200, { key_prefix: "sk-new" });
      if (method === "GET" && p === "/gateway/usage") return jsonResponse(200, s.usage);
      if (method === "GET" && p === "/connectors") return jsonResponse(200, s.connectors);
      if (method === "POST" && p === "/connectors") {
        const b = body(init);
        s.connectors = [...s.connectors, { slug: b["slug"], name: b["name"], url: b["mcp_url"], auth_kind: (b["auth"] as { kind: string }).kind, tier: [], status: "draft", tools: { approved: 0, needs_review: 0, disabled: 0 } }];
        return jsonResponse(201, { slug: b["slug"], status: "draft" });
      }
      r = m(/^\/connectors\/([^/]+)\/test$/);
      if (r && method === "POST") return jsonResponse(200, s.test);
      r = m(/^\/connectors\/([^/]+)\/tools$/);
      if (r && method === "GET") return jsonResponse(200, s.tools);
      r = m(/^\/connectors\/([^/]+)\/tools\/([^/]+)$/);
      if (r && method === "PATCH") {
        const name = decodeURIComponent(r[2]!);
        s.tools = s.tools.map((t) => (t["name"] === name ? { ...t, ...body(init) } : t));
        return jsonResponse(200, s.tools.find((t) => t["name"] === name));
      }
      r = m(/^\/connectors\/([^/]+)\/(activate|disable)$/);
      if (r && method === "POST") {
        s.connectors = s.connectors.map((c) => (c["slug"] === r![1] ? { ...c, status: r![2] === "activate" ? "active" : "disabled" } : c));
        return jsonResponse(200, { ok: true });
      }
      r = m(/^\/connectors\/([^/]+)\/events$/);
      if (r && method === "GET") return jsonResponse(200, s.events);
      r = m(/^\/connectors\/([^/]+)$/);
      if (r && method === "DELETE") return new Response(null, { status: 204 });
      if (r && method === "GET") {
        const slug = decodeURIComponent(r[1]!);
        const c = s.connectors.find((x) => x["slug"] === slug);
        if (!c) return jsonResponse(404, { code: "not_found", message: "connector not found" });
        return jsonResponse(200, { ...c, last_test: s.lastTests[slug] ?? null, webhook_path: c["webhook"] ? `/api/webhooks/demo-retail/${slug}` : null });
      }
      if (method === "GET" && p === "/tenants") return jsonResponse(200, s.tenants);
      if (method === "POST" && p === "/tenants") {
        const b = body(init);
        // Engine shape (`tenants.create_tenant`): 201 + created:true, or 200 + created:false when the
        // slug exists with the same pack; per-username Keycloak result (`added` / `already` / `not_found` / `failed`).
        const existing = s.tenants.find((t) => t["slug"] === b["slug"]);
        if (existing && existing["pack"] !== b["pack"]) return jsonResponse(409, { code: "tenant_exists", message: "tenant already exists with another pack" });
        if (!existing) s.tenants = [...s.tenants, { slug: String(b["slug"]), name: String(b["name"]), pack: String(b["pack"]), created_at: "2026-10-06T10:00:00Z" }];
        const admins = ((b["admin_usernames"] as string[] | undefined) ?? []).map((username) => ({
          username,
          status: s.unknownUsers.includes(username) ? "not_found" : existing ? "already" : "added",
        }));
        const t = s.tenants.find((x) => x["slug"] === b["slug"])!;
        return jsonResponse(existing ? 200 : 201, { ...t, created: !existing, admins, spec_version: "v1", connectors: [] });
      }
      if (method === "GET" && p === "/packs") return jsonResponse(200, s.packs);
      // ── knowledge base (engine routes_admin_kb.py) ──
      if (method === "GET" && p === "/kb") return jsonResponse(200, { items: s.kbs, profile: s.kbProfile });
      if (method === "POST" && p === "/kb") {
        const b = body(init);
        const name = String(b["name"] ?? "").trim();
        if (s.kbs.some((k) => String(k["name"]).toLowerCase() === name.toLowerCase())) return jsonResponse(409, { code: "name_taken", message: "a knowledge base with this name exists" });
        const kb = { id: `kb-${s.kbs.length + 1}`, name, description: b["description"] ?? "", created_by: "sam", created_at: "2026-10-07T10:00:00+00:00", documents: 0, ready_documents: 0, chunks: 0 };
        s.kbs = [...s.kbs, kb];
        s.kbDocs[kb.id] = [];
        return jsonResponse(201, kb);
      }
      if (method === "GET" && p === "/kb/reembed") return jsonResponse(200, { items: s.kbJobs, profiles: s.kbProfile ? [s.kbProfile] : [] });
      if (method === "POST" && p === "/kb/reembed") {
        if (s.kbJobs.some((j) => j["status"] === "running")) return jsonResponse(409, { code: "reembed_running", message: "a re-embed job is already running" });
        const b = body(init);
        const job = { id: `job-${s.kbJobs.length + 1}`, tenant: b["scope"] === "platform" ? null : "demo-retail", scope: b["scope"] ?? "tenant", from_profile_id: 1, to_profile_id: null, status: "running", chunks_total: 14, chunks_done: 3, error: null, requested_by: "sam", created_at: "2026-10-07T10:00:00+00:00", updated_at: "2026-10-07T10:00:01+00:00", finished_at: null };
        s.kbJobs = [job, ...s.kbJobs];
        return jsonResponse(202, job);
      }
      r = m(/^\/kb\/([^/]+)\/documents$/);
      if (r && method === "GET") {
        const docs = s.kbDocs[decodeURIComponent(r[1]!)];
        return docs ? jsonResponse(200, { items: docs }) : jsonResponse(404, { code: "not_found", message: "knowledge base not found" });
      }
      if (r && method === "POST") {
        const kbId = decodeURIComponent(r[1]!);
        const form = init.body;
        if (!(form instanceof FormData)) return jsonResponse(415, { code: "unsupported_media_type", message: "upload the file as multipart/form-data (field 'file')" });
        const file = form.get("file");
        if (!(file instanceof Blob)) return jsonResponse(422, { code: "file_required", message: "the multipart field 'file' is required" });
        const filename = (file as File).name ?? "document";
        if (file.size > 5 * 1024 * 1024) return jsonResponse(413, { code: "file_too_large", message: "files may be at most 5 MB" });
        if (!/\.(md|txt|pdf)$/i.test(filename)) return jsonResponse(415, { code: "unsupported_type", message: "only .md, .txt and .pdf files are accepted" });
        const docs = s.kbDocs[kbId] ?? [];
        const same = docs.find((d) => d["filename"] === filename && d["bytes"] === file.size);
        if (same) return jsonResponse(200, { ...same, duplicate: true });
        const title = typeof form.get("title") === "string" && String(form.get("title")).trim() ? String(form.get("title")).trim() : filename.replace(/\.[^.]+$/, "");
        const doc = { id: `doc-${docs.length + 10}`, kb_id: kbId, title, filename, mime: file.type || "text/markdown", bytes: file.size, sha256: "c".repeat(64), status: "ready", error: null, chunks: 3, tokens: 900, created_by: "sam", created_at: "2026-10-07T10:00:00+00:00", updated_at: "2026-10-07T10:00:01+00:00" };
        s.kbDocs[kbId] = [doc, ...docs];
        return jsonResponse(201, { ...doc, duplicate: false });
      }
      r = m(/^\/kb\/([^/]+)\/documents\/([^/]+)$/);
      if (r && method === "DELETE") {
        const kbId = decodeURIComponent(r[1]!);
        const docId = decodeURIComponent(r[2]!);
        const docs = s.kbDocs[kbId] ?? [];
        if (!docs.some((d) => d["id"] === docId)) return jsonResponse(404, { code: "not_found", message: "document not found" });
        s.kbDocs[kbId] = docs.filter((d) => d["id"] !== docId);
        return new Response(null, { status: 204 });
      }
      if (method === "GET" && p === "/audit/activity") {
        // Like the engine: categories (repeated `category=`), exact actor, case id, action prefix, since / until,
        // opaque cursor `<at>|<key>` (rows strictly older).
        const q = new URL(url, "http://localhost").searchParams;
        const limit = Number(q.get("limit") ?? 50);
        if (!Number.isInteger(limit) || limit < 1 || limit > 200) return jsonResponse(422, { code: "validation_error", message: "limit must be at most 200" });
        if (q.has("tenant")) return jsonResponse(400, { code: "unexpected_tenant", message: "unexpected tenant parameter" });
        const cats = q.getAll("category");
        const before = q.get("before");
        const since = q.get("since");
        const until = q.get("until");
        const rows = s.activity.filter((r) => {
          const at = String(r["at"]);
          if (cats.length && !cats.includes(String(r["category"]))) return false;
          if (q.get("actor") && r["actor"] !== q.get("actor")) return false;
          if (q.get("case_id") && r["case_id"] !== q.get("case_id")) return false;
          if (q.get("action") && !String(r["action"]).startsWith(q.get("action")!)) return false;
          if (since && Date.parse(at) < Date.parse(since)) return false;
          if (until && Date.parse(at) >= Date.parse(until)) return false;
          if (before && `${at}|${String(r["key"])}` >= before) return false;
          return true;
        });
        const items = rows.slice(0, limit);
        const last = items.at(-1);
        return jsonResponse(200, { items, next_before: rows.length > limit && last ? `${String(last["at"])}|${String(last["key"])}` : null, categories: cats });
      }
      if (method === "GET" && p === "/audit") {
        // Like the engine: limit 1..200 (else 422); `before` pages by id; `scope=platform` = rows without a tenant.
        const q = new URL(url, "http://localhost").searchParams;
        const limit = Number(q.get("limit") ?? 50);
        if (!Number.isInteger(limit) || limit < 1 || limit > 200) return jsonResponse(422, { code: "validation_error", message: "limit must be at most 200" });
        // `?tenant=` would switch a platform admin into another tenant; the audit page must never send it.
        if (q.has("tenant")) return jsonResponse(400, { code: "unexpected_tenant", message: "unexpected tenant parameter" });
        const platform = q.get("scope") === "platform";
        const before = q.get("before");
        const rows = s.audit.filter((row) => (platform ? row["tenant"] === null : row["tenant"] !== null) && (before === null || Number(row["id"]) < Number(before)));
        const items = rows.slice(0, limit);
        return jsonResponse(200, { items, next_before: rows.length > limit ? items.at(-1)!["id"] : null });
      }
      return undefined;
    },
  ];
}
