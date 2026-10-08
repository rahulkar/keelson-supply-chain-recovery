/**
 * Response shapes of the back-office routes (`docs/contracts/70-backoffice-m2.md` §1–§5).
 *
 * Parsed with Zod at the boundary; objects are `loose` so additive server fields never break the
 * UI, and where the contract leaves the envelope open (a bare array vs `{items: […]}`) both are
 * accepted. Everything the UI does not need is passed through untouched.
 */
import { z } from "zod";

/** `[…]` or `{items: […]}` → `[…]`. */
export function listOf<T extends z.ZodType>(item: T) {
  return z.preprocess(
    (v) => (v && typeof v === "object" && !Array.isArray(v) && Array.isArray((v as { items?: unknown }).items) ? (v as { items: unknown[] }).items : v),
    z.array(item),
  );
}

const iso = z.string().nullable().optional();
/** Spec versions are `v<n>` strings (`"default"` before the first save); numbers tolerated. */
export const specVersionSchema = z.union([z.string(), z.number()]).transform(String);
export type SpecVersion = z.infer<typeof specVersionSchema>;

// ───────────────────────────── §1 agent spec ─────────────────────────────

/**
 * LLM roles with a model alias card (`runtime/spec.py` `ModelAliases`), in pipeline order. M4 adds
 * the planner and the critic; the guard, extractor and follow-up classifier keep their built-in
 * aliases on this page.
 */
export const MODEL_ROLES = [
  "intake",
  "answer_investigator",
  "answer_structurer",
  "planner",
  "investigator",
  "structurer",
  "critic",
  "recommender",
] as const;
export type ModelRole = (typeof MODEL_ROLES)[number];

/** Roles that accept a prompt addendum (70 §1; M4: planner and critic, `PromptAddenda`). */
export const PROMPT_ROLES = ["intake", "answer_investigator", "planner", "investigator", "structurer", "critic", "recommender"] as const;
export type PromptRole = (typeof PROMPT_ROLES)[number];

export const PROMPT_MAX_CHARS = 4000;

export const specialistSchema = z
  .object({
    slug: z.string(),
    tools: z.array(z.string()),
    max_tool_calls: z.number().int().optional(),
    connector: z.string().optional(),
    display_name: z.string().nullable().optional(),
  })
  .loose();
export type Specialist = z.infer<typeof specialistSchema>;

export const agentSpecSchema = z
  .object({
    tenant: z.string().optional(),
    version: z.string().optional(),
    connector: z.string().optional(),
    vendor_name: z.string().optional(),
    models: z.record(z.string(), z.string()).default({}),
    prompts: z.record(z.string(), z.string().nullable()).optional(),
    specialists: z.array(specialistSchema).default([]),
    answer_tools: z.array(z.string()).default([]),
    answer_connector: z.string().optional(),
    budgets: z.object({ max_llm_calls: z.number().optional(), max_total_tokens: z.number().optional() }).loose().default({}),
    policy: z
      .object({
        auto_max_lines: z.number().optional(),
        auto_max_units: z.number().optional(),
        approval_quorum: z.number().optional(),
        auto_confirm_max_matched: z.number().optional(),
        approver_role: z.string().optional(),
      })
      .loose()
      .default({}),
  })
  .loose();
export type AgentSpec = z.infer<typeof agentSpecSchema>;

export const specEnvelopeSchema = z
  .object({
    version: specVersionSchema,
    spec: agentSpecSchema,
    created_by: z.unknown().optional(),
    created_at: iso,
    active: z.boolean().optional(),
  })
  .loose();
export type SpecEnvelope = z.infer<typeof specEnvelopeSchema>;

/** `GET …/versions/{v}` returns the full spec — either enveloped or bare (both accepted). */
export const specVersionDetailSchema = z.unknown().transform((v, ctx) => {
  const env = specEnvelopeSchema.safeParse(v);
  if (env.success) return { version: env.data.version, spec: env.data.spec };
  const bare = agentSpecSchema.safeParse(v);
  if (bare.success) return { version: bare.data.version ?? null, spec: bare.data };
  ctx.addIssue({ code: "custom", message: "not an agent spec" });
  return z.NEVER;
});

export const specVersionItemSchema = z
  .object({
    version: specVersionSchema,
    created_by: z.unknown().optional(),
    created_at: iso,
    summary: z.string().nullable().optional(),
  })
  .loose();
export type SpecVersionItem = z.infer<typeof specVersionItemSchema>;
export const specVersionListSchema = listOf(specVersionItemSchema);

/**
 * A saved version (`PUT` / `restore`): at least its version id. `unchanged: true` when the patch
 * changed nothing after the server normalised it (no new version was written).
 */
export const specSavedSchema = z.object({ version: specVersionSchema, unchanged: z.boolean().optional() }).loose();

/** Model capability badges (70 §2). */
const capabilitiesSchema = z
  .object({
    structured_output: z.boolean().optional(),
    reasoning_stream: z.boolean().optional(),
    tool_loop_signatures: z.boolean().optional(),
  })
  .loose();

export const TOOL_ACCESS = ["read", "write", "destructive"] as const;
export const CONTENT_TRUST = ["trusted_data", "untrusted_text"] as const;
export const EGRESS = ["internal", "external"] as const;
export const IDEMPOTENCY = ["enforced", "best_effort", "none"] as const;
export const TOOL_STATUS = ["needs_review", "approved", "disabled"] as const;
export type ToolAccess = (typeof TOOL_ACCESS)[number];
export type ContentTrust = (typeof CONTENT_TRUST)[number];
export type Egress = (typeof EGRESS)[number];
export type Idempotency = (typeof IDEMPOTENCY)[number];
export type ToolStatus = (typeof TOOL_STATUS)[number];

export const classificationSchema = z
  .object({
    access: z.string().optional(),
    content_trust: z.string().optional(),
    egress: z.string().optional(),
    idempotency: z.string().optional(),
  })
  .loose();
export type Classification = z.infer<typeof classificationSchema>;

/**
 * A catalog tool. The engine nests the classification (`classification: {access, …}`) and adds
 * `bindable` / `reason` (it also knows whether the connector is active). An unclassified tool is
 * never assumed to be a read tool (fail closed); a missing `status` means approved, because the
 * catalog lists approved tools (70 §1).
 */
export const catalogToolSchema = z
  .object({
    name: z.string(),
    title: z.string().nullable().optional(),
    description: z.string().nullable().optional(),
    access: z.string().optional(),
    content_trust: z.string().optional(),
    egress: z.string().optional(),
    idempotency: z.string().optional(),
    status: z.string().optional(),
    classification: classificationSchema.nullable().optional(),
    bindable: z.boolean().nullable().optional(),
    reason: z.string().nullable().optional(),
  })
  .loose()
  .transform((t) => ({
    ...t,
    access: t.access ?? t.classification?.access,
    content_trust: t.content_trust ?? t.classification?.content_trust,
    status: t.status ?? "approved",
  }));
export type CatalogTool = z.infer<typeof catalogToolSchema>;

export const catalogSchema = z
  .object({
    roles: listOf(
      z
        .object({
          role: z.string(),
          display_name: z.string().nullable().optional(),
          alias: z.string().nullable().optional(),
          description: z.string().nullable().optional(),
          /** The `prompts` key of this role's addendum (`null`: the role takes none). */
          prompt_role: z.string().nullable().optional(),
          /** The saved addendum (as in the active version). */
          prompt: z.string().nullable().optional(),
          uses_tools: z.boolean().optional(),
        })
        .loose(),
    ).default([]),
    aliases: z
      .array(
        z.union([
          z.string(),
          z.object({ alias: z.string(), mode: z.string().nullable().optional(), capabilities: capabilitiesSchema.nullable().optional() }).loose(),
        ]),
      )
      .default([])
      .transform((xs) =>
        xs.map((a) =>
          typeof a === "string"
            ? { alias: a, mode: null, capabilities: null }
            : { alias: a.alias, mode: a.mode ?? null, capabilities: a.capabilities ?? null },
        ),
      ),
    connectors: listOf(
      z
        .object({
          slug: z.string(),
          name: z.string().nullable().optional(),
          tools: z.array(catalogToolSchema).default([]),
        })
        .loose(),
    ).default([]),
    /** `false` when the AI gateway could not be asked for the tenant's aliases (`aliases` is then empty). */
    gateway_available: z.boolean().optional(),
    /** Platform maxima for `budgets` (the server rejects anything above). */
    platform_max: z
      .object({ max_llm_calls: z.number().nullable().optional(), max_total_tokens: z.number().nullable().optional() })
      .loose()
      .nullable()
      .optional(),
  })
  .loose();
export type AgentCatalog = z.infer<typeof catalogSchema>;

// ───────────────────────────── §2 AI gateway ─────────────────────────────

export const gatewayModelSchema = z
  .object({
    alias: z.string(),
    mode: z.string().nullable().optional(),
    capabilities: capabilitiesSchema.optional(),
    health: z
      .union([z.string(), z.object({ status: z.string().optional() }).loose()])
      .nullable()
      .optional()
      .transform((h) => (h == null ? null : typeof h === "string" ? h : (h.status ?? null))),
    provider_model: z.string().nullable().optional(),
    params: z.record(z.string(), z.unknown()).nullable().optional(),
  })
  .loose();
export type GatewayModel = z.infer<typeof gatewayModelSchema>;
export const gatewayModelListSchema = listOf(gatewayModelSchema);

export const gatewayTenantSchema = z
  .object({
    team_alias: z.string().nullable().optional(),
    budget_eur: z.number().nullable().optional(),
    spend_eur: z.number().nullable().optional(),
    budget_duration: z.string().nullable().optional(),
    rpm_limit: z.number().nullable().optional(),
    tpm_limit: z.number().nullable().optional(),
    models: z.array(z.string()).default([]),
    key_prefix: z.string().nullable().optional(),
  })
  .loose();
export type GatewayTenant = z.infer<typeof gatewayTenantSchema>;

export const usageRowSchema = z
  .object({
    date: z.string(),
    alias: z.string().nullable().optional(),
    spend_eur: z.number().default(0),
    tokens_in: z.number().default(0),
    tokens_out: z.number().default(0),
    requests: z.number().default(0),
  })
  .loose();
export type UsageRow = z.infer<typeof usageRowSchema>;
/**
 * Daily usage per alias. Accepted envelopes: flat rows (`[{date, alias, …}]` / `{items}`), or
 * per-alias groups (`{aliases: {<alias>: [{date, …}]}}` / `[{alias, days: […]}]`) — flattened.
 */
export const usageSchema = z.unknown().transform((v, ctx): UsageRow[] => {
  const flat = listOf(usageRowSchema).safeParse(v);
  if (flat.success) return flat.data;
  const byAlias = z.object({ aliases: z.record(z.string(), z.array(usageRowSchema)) }).loose().safeParse(v);
  if (byAlias.success) return Object.entries(byAlias.data.aliases).flatMap(([alias, rows]) => rows.map((r) => ({ ...r, alias })));
  const groups = listOf(z.object({ alias: z.string(), days: z.array(usageRowSchema) }).loose()).safeParse(v);
  if (groups.success) return groups.data.flatMap((g) => g.days.map((r) => ({ ...r, alias: g.alias })));
  ctx.addIssue({ code: "custom", message: "unexpected usage shape" });
  return z.NEVER;
});

/** A whole day's totals over every alias (engine `daily`: one row per day, zero-filled). */
export type DailyUsage = { date: string; spend_eur: number; tokens_in: number; tokens_out: number; requests: number };

/**
 * `GET /api/admin/gateway/usage`: the per-alias rows plus, when the engine sends it, `daily` —
 * every day of the window including days without traffic (so a 7-day chart has 7 bars).
 */
export const usageReportSchema = z.unknown().transform((v, ctx): { rows: UsageRow[]; daily: DailyUsage[] | null } => {
  const rows = usageSchema.safeParse(v);
  if (!rows.success) {
    ctx.addIssue({ code: "custom", message: "unexpected usage shape" });
    return z.NEVER;
  }
  const rawDaily = v && typeof v === "object" && !Array.isArray(v) ? (v as { daily?: unknown }).daily : undefined;
  const daily = z.array(usageRowSchema).safeParse(rawDaily);
  return {
    rows: rows.data,
    daily: daily.success
      ? daily.data.map((d) => ({ date: d.date, spend_eur: d.spend_eur, tokens_in: d.tokens_in, tokens_out: d.tokens_out, requests: d.requests }))
      : null,
  };
});
export type UsageReport = z.infer<typeof usageReportSchema>;

export const rotateKeySchema =z.object({ key_prefix: z.string().nullable().optional() }).loose();

// ───────────────────────────── §3 connectors ─────────────────────────────

export const AUTH_KINDS = ["none", "static_header", "oauth_client_credentials", "oauth_platform_merxa"] as const;
export type AuthKind = (typeof AUTH_KINDS)[number];
export const CONNECTOR_STATUS = ["draft", "tested", "active", "disabled"] as const;

export const connectorSummarySchema = z
  .object({
    slug: z.string(),
    name: z.string(),
    url: z.string().nullable().optional(),
    auth_kind: z.string().nullable().optional(),
    tier: z.array(z.string()).default([]),
    status: z.string(),
    tools: z
      .object({ approved: z.number().default(0), needs_review: z.number().default(0), disabled: z.number().default(0) })
      .loose()
      .default({ approved: 0, needs_review: 0, disabled: 0 }),
    last_test_at: iso,
    last_test: z.unknown().optional(),
  })
  .loose();
export type ConnectorSummary = z.infer<typeof connectorSummarySchema>;
export const connectorListSchema = listOf(connectorSummarySchema);

/** `GET /api/admin/connectors/{slug}`: the summary plus the stored `last_test`, trust level and webhook path. */
export const connectorDetailSchema = connectorSummarySchema
  .extend({
    trust_level: z.string().nullable().optional(),
    webhook_path: z.string().nullable().optional(),
  })
  .loose();
export type ConnectorDetailData = z.infer<typeof connectorDetailSchema>;

export const connectorCreatedSchema = z.object({ slug: z.string(), status: z.string().optional() }).loose();

export const TEST_STEPS = ["url_policy", "reach", "discover", "auth", "tools_list", "tier_detect", "classify"] as const;
export type TestStepId = (typeof TEST_STEPS)[number];

export const testStepSchema = z
  .object({
    step: z.string(),
    ok: z.boolean(),
    ms: z.number().nullable().optional(),
    detail: z.unknown().optional(),
  })
  .loose();
export type TestStep = z.infer<typeof testStepSchema>;

export const discoveredToolSchema = z
  .object({
    name: z.string(),
    title: z.string().nullable().optional(),
    description: z.string().nullable().optional(),
    suggested: classificationSchema.default({}),
    annotations: z.record(z.string(), z.unknown()).nullable().optional(),
    definition_hash: z.string().nullable().optional(),
  })
  .loose();
export type DiscoveredTool = z.infer<typeof discoveredToolSchema>;

export const connectorTestSchema = z
  .object({
    ok: z.boolean(),
    tier: z.array(z.string()).default([]),
    steps: z.array(testStepSchema).default([]),
    tools: z.array(discoveredToolSchema).default([]),
  })
  .loose();
export type ConnectorTest = z.infer<typeof connectorTestSchema>;

export const connectorToolSchema = z
  .object({
    name: z.string(),
    title: z.string().nullable().optional(),
    description: z.string().nullable().optional(),
    access: z.string().default("read"),
    content_trust: z.string().default("trusted_data"),
    egress: z.string().default("internal"),
    idempotency: z.string().default("none"),
    status: z.string().default("needs_review"),
    suggested: classificationSchema.nullable().optional(),
    annotations: z.record(z.string(), z.unknown()).nullable().optional(),
    drift: z.boolean().optional(),
    /** Agent tool binding (plan §10): `auto` | `approval_gate` | `deny`; `auto` on a write tool is guarded (M7). */
    binding: z.string().nullable().optional(),
  })
  .loose();
export type ConnectorTool = z.infer<typeof connectorToolSchema>;

export const TOOL_BINDINGS = ["auto", "approval_gate", "deny"] as const;
export type ToolBinding = (typeof TOOL_BINDINGS)[number];
export const connectorToolListSchema = listOf(connectorToolSchema);

export const connectorEventSchema = z
  .object({
    event_id: z.string().optional(),
    id: z.union([z.string(), z.number()]).optional(),
    type: z.string().nullable().optional(),
    received_at: iso,
    processed_at: iso,
  })
  .loose()
  .transform((e) => ({ ...e, key: String(e.event_id ?? e.id ?? `${e.type}-${e.received_at}`) }));
export type ConnectorEvent = z.infer<typeof connectorEventSchema>;
export const connectorEventListSchema = listOf(connectorEventSchema);

// ───────────────────────────── §4 tenants & packs ─────────────────────────────

export const packSchema = z
  .object({
    name: z.string(),
    description: z.string().nullable().optional(),
    connectors: z.array(z.unknown()).optional(),
    budgets: z.record(z.string(), z.unknown()).nullable().optional(),
  })
  .loose();
export type Pack = z.infer<typeof packSchema>;
export const packListSchema = listOf(packSchema);

export const tenantSchema = z
  .object({
    slug: z.string(),
    name: z.string(),
    pack: z.string().nullable().optional(),
    created_at: iso,
    /** M7: a suspended tenant's users can't sign in and its API calls are refused. */
    suspended: z.boolean().optional(),
    status: z.string().nullable().optional(),
    suspended_at: iso,
  })
  .loose();
export type Tenant = z.infer<typeof tenantSchema>;

/** `suspended: true` or `status: "suspended"` (either shape). */
export function isSuspended(t: Pick<Tenant, "suspended" | "status">): boolean {
  return t.suspended === true || t.status === "suspended";
}
export const tenantListSchema = listOf(tenantSchema);

/**
 * `POST /api/admin/tenants` result (engine `tenants.create_tenant`): `created: false` when the
 * tenant already existed with the same pack (re-provisioned); per admin username whether the
 * Keycloak `tenant` attribute was `added` / `already` there, or `not_found` / `failed`.
 */
export const tenantCreatedSchema = tenantSchema
  .extend({
    created: z.boolean().optional(),
    admins: z
      .array(z.object({ username: z.string(), status: z.string() }).loose())
      .optional()
      .catch(undefined),
  })
  .loose();
export type TenantCreated = z.infer<typeof tenantCreatedSchema>;

/** Usernames that did not get access to the new tenant (unknown user or Keycloak failure). */
export function adminsWithoutAccess(result: Pick<TenantCreated, "admins"> | null): { username: string; status: string }[] {
  return (result?.admins ?? []).filter((a) => a.status !== "added" && a.status !== "already");
}

// ───────────────────────────── audit ─────────────────────────────

export const auditEntrySchema = z
  .object({
    id: z.union([z.string(), z.number()]).optional(),
    created_at: iso,
    at: iso,
    actor: z.unknown().optional(),
    tenant: z.string().nullable().optional(),
    /** The engine's row columns (`admin_audit.actor_username` / `actor_sub`). */
    actor_username: z.string().nullable().optional(),
    actor_sub: z.string().nullable().optional(),
    action: z.string(),
    target: z.string().nullable().optional(),
    before: z.unknown().optional(),
    after: z.unknown().optional(),
  })
  .loose()
  .transform((e) => {
    const actor = e.actor;
    const fromActor =
      typeof actor === "string"
        ? actor
        : actor && typeof actor === "object"
          ? String((actor as Record<string, unknown>).name || (actor as Record<string, unknown>).username || (actor as Record<string, unknown>).sub || "")
          : "";
    const actorName = fromActor || e.actor_username || e.actor_sub || "system";
    const when = e.created_at ?? e.at ?? null;
    return { ...e, when, actorName, key: String(e.id ?? `${when}-${e.action}-${e.target}`) };
  });
export type AuditEntry = z.infer<typeof auditEntrySchema>;
export const auditListSchema = listOf(auditEntrySchema);

/** `GET /api/admin/audit`: newest first; `next_before` (a row id) pages to older rows. */
export const auditPageSchema = z.unknown().transform((v, ctx) => {
  const items = auditListSchema.safeParse(v);
  if (!items.success) {
    ctx.addIssue({ code: "custom", message: "unexpected audit shape" });
    return z.NEVER;
  }
  const nb = v && typeof v === "object" && !Array.isArray(v) ? (v as { next_before?: unknown }).next_before : undefined;
  return { items: items.data, nextBefore: typeof nb === "number" ? nb : null };
});
export type AuditPageData = z.infer<typeof auditPageSchema>;

export const okSchema = z.unknown();

// ───────────────────────────── knowledge base (docs/contracts/90 §7) ─────────────────────────────

/** The embedding profile chunks are indexed with (`GET /api/admin/kb` `profile`). */
export const embeddingProfileSchema = z
  .object({
    id: z.number(),
    alias: z.string(),
    provider_model: z.string().nullable().optional(),
    dims: z.number(),
    version: z.number().optional(),
    active: z.boolean().optional(),
    /** `active` | `building` | `retired` | `failed`. */
    status: z.string().optional(),
    /** e.g. `vector(768)` / `halfvec(3072)`. */
    storage: z.string().nullable().optional(),
  })
  .loose();
export type EmbeddingProfile = z.infer<typeof embeddingProfileSchema>;

export const knowledgeBaseSchema = z
  .object({
    id: z.string(),
    name: z.string(),
    description: z.string().nullable().optional(),
    created_by: z.string().nullable().optional(),
    created_at: iso,
    documents: z.number().default(0),
    ready_documents: z.number().default(0),
    chunks: z.number().default(0),
  })
  .loose();
export type KnowledgeBase = z.infer<typeof knowledgeBaseSchema>;

/** `GET /api/admin/kb`: `{items, profile}` (a bare list is tolerated; the profile is then unknown). */
export const kbListSchema = z.preprocess(
  (v) => (Array.isArray(v) ? { items: v, profile: null } : v),
  z.object({ items: z.array(knowledgeBaseSchema), profile: embeddingProfileSchema.nullable().catch(null).default(null) }).loose(),
);
export type KbList = z.infer<typeof kbListSchema>;

/** Document status: `processing` (chunking / embedding), `ready` (searchable), `failed` (`error` says why). */
export const KB_DOCUMENT_STATUSES = ["processing", "ready", "failed"] as const;

export const kbDocumentSchema = z
  .object({
    id: z.string(),
    kb_id: z.string().optional(),
    title: z.string(),
    filename: z.string().nullable().optional(),
    mime: z.string().nullable().optional(),
    bytes: z.number().default(0),
    sha256: z.string().nullable().optional(),
    status: z.string(),
    error: z.string().nullable().optional(),
    chunks: z.number().default(0),
    tokens: z.number().default(0),
    created_by: z.string().nullable().optional(),
    created_at: iso,
    updated_at: iso,
    /** Upload answer only: the same bytes were already in this knowledge base. */
    duplicate: z.boolean().optional(),
  })
  .loose();
export type KbDocument = z.infer<typeof kbDocumentSchema>;
export const kbDocumentListSchema = listOf(kbDocumentSchema);

/** A re-embed job: `tenant` scope repairs missing vectors; `platform` builds a new profile (shadow table + swap). */
export const reembedJobSchema = z
  .object({
    id: z.string(),
    tenant: z.string().nullable().optional(),
    scope: z.string(),
    from_profile_id: z.number().nullable().optional(),
    to_profile_id: z.number().nullable().optional(),
    /** `running` | `succeeded` | `failed`. */
    status: z.string(),
    chunks_total: z.number().default(0),
    chunks_done: z.number().default(0),
    error: z.string().nullable().optional(),
    requested_by: z.string().nullable().optional(),
    created_at: iso,
    updated_at: iso,
    finished_at: iso,
  })
  .loose();
export type ReembedJob = z.infer<typeof reembedJobSchema>;

export const reembedListSchema = z.preprocess(
  (v) => (Array.isArray(v) ? { items: v, profiles: [] } : v),
  z.object({ items: z.array(reembedJobSchema), profiles: z.array(embeddingProfileSchema).default([]) }).loose(),
);
export type ReembedList = z.infer<typeof reembedListSchema>;

/** Upload rules the engine enforces (`kb_max_upload_bytes`, accepted types). */
export const KB_UPLOAD = { maxBytes: 5 * 1024 * 1024, extensions: [".md", ".txt", ".pdf"] as const, accept: ".md,.txt,.pdf,text/markdown,text/plain,application/pdf" };

/** Why a file can't be uploaded (checked before sending; the engine checks again). */
export function kbFileProblem(file: { name: string; size: number }): string | null {
  const name = file.name.toLowerCase();
  if (!KB_UPLOAD.extensions.some((ext) => name.endsWith(ext))) return `${file.name}: only Markdown (.md), text (.txt) and PDF files can be added.`;
  if (file.size > KB_UPLOAD.maxBytes) return `${file.name}: files can be at most 5 MB.`;
  if (file.size === 0) return `${file.name}: the file is empty.`;
  return null;
}
