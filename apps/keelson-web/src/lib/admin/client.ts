/**
 * Typed browser client for the back-office API (`70-backoffice-m2.md` §1–§5), built on the app's
 * `ApiClient` (same origin, cookie session, CSRF on every mutation with one refresh on
 * `403 csrf_failed`, `401` → sign-in, `{code, message, error_id?}` errors as `ApiError`).
 */
import type { ApiClient } from "../api/client";
import {
  auditPageSchema,
  catalogSchema,
  connectorCreatedSchema,
  connectorDetailSchema,
  connectorEventListSchema,
  connectorListSchema,
  connectorTestSchema,
  connectorToolListSchema,
  connectorToolSchema,
  gatewayModelListSchema,
  kbDocumentListSchema,
  kbDocumentSchema,
  kbListSchema,
  knowledgeBaseSchema,
  reembedJobSchema,
  reembedListSchema,
  gatewayModelSchema,
  gatewayTenantSchema,
  okSchema,
  packListSchema,
  rotateKeySchema,
  specEnvelopeSchema,
  specSavedSchema,
  specVersionDetailSchema,
  specVersionListSchema,
  tenantListSchema,
  tenantCreatedSchema,
  usageReportSchema,
  usageSchema,
  type AgentCatalog,
  type AuditPageData,
  type AuthKind,
  type ConnectorDetailData,
  type ConnectorEvent,
  type ConnectorSummary,
  type ConnectorTest,
  type ConnectorTool,
  type GatewayModel,
  type GatewayTenant,
  type KbDocument,
  type KbList,
  type KnowledgeBase,
  type ReembedJob,
  type ReembedList,
  type Pack,
  type SpecEnvelope,
  type SpecVersionItem,
  type Tenant,
  type TenantCreated,
  type UsageReport,
  type UsageRow,
  type ToolBinding,
} from "./types";

import {
  policiesEnvelopeSchema,
  policiesSavedSchema,
  policyTestResultSchema,
  type PoliciesEnvelope,
  type PoliciesSaved,
  type PolicyRuleView,
  type PolicyTestResult,
} from "./policies";
import { mappingPlanSchema, type AdminUserCreate, type AdminUserPatch, type EvalsCompare, type EvalsRunCreate, type MappingDryRunRequest, type MappingPlan, type PolicyContext } from "@keelson/contracts";
import { z } from "zod";
import { ApiError } from "../api/client";
import { changeRequestAnswerSchema, changeRequestListSchema, guardedSchemaFor, pendingCountSchema, type ChangeRequest, type Guarded } from "./changeRequests";
import { activityPageSchema, isAuditCategory, type ActivityPage, type ActivityQuery } from "./audit";
import { evalsResponseSchema, type EvalList } from "./evals";
import {
  evalCaseResultSchema,
  evalCompareSchema,
  evalEventsSchema,
  evalFilesSchema,
  evalRunDetailSchema,
  evalRunListSchema,
  evalRunSchema,
  evalStatusSchema,
  evalTrendsSchema,
  folderImportSchema,
  importAnswerSchema,
  type EvalCaseResult,
  type EvalEvents,
  type EvalFiles,
  type EvalRun,
  type EvalRunDetail,
  type EvalRunList,
  type EvalStatusData,
  type EvalTrendsData,
  type FolderImport,
  type ImportAnswer,
} from "./evalRuns";
import { playgroundStatusSchema, type PlaygroundStatus } from "./playground";
import {
  capabilitiesSchema,
  dryRunResultSchema,
  mappingEnvelopeSchema,
  mappingSavedSchema,
  readPreviewSchema,
  snapshotHistorySchema,
  stagedSetSchema,
  syncQueuedSchema,
  type Capabilities,
  type DryRunResult,
  type MappingEnvelope,
  type MappingKind,
  type MappingSaved,
  type ReadPreview,
  type SnapshotHistory,
  type StagedSet,
  type SyncQueued,
} from "./mappings";
import {
  proposalListSchema,
  proposalSchema,
  proposerStatusSchema,
  type Proposal,
  type ProposalSummary,
  type ProposeBody,
  type ProposerStatus,
} from "./proposals";
import {
  alertRulesSchema,
  networkCodesSchema,
  regionBody,
  signalSourceListSchema,
  sourceSavedSchema,
  watchRegionListSchema,
  type AlertRule,
  type AlertRules,
  type NetworkCodes,
  type SignalSourceList,
  type SourceSaved,
  type SourceSettingPatch,
  type WatchRegion,
} from "./signals";
import { userAnswerSchema, userCreatedSchema, userListSchema, type User, type UserCreated } from "./users";

const seg = (s: string) => encodeURIComponent(s);

/**
 * A path segment that can't escape its route: `encodeURIComponent` leaves "." alone and the URL
 * parser resolves "." / ".." (even as %2E) as dot segments, so those (and "") are refused.
 */
function safeSeg(s: string): string {
  if (s === "" || s === "." || s === ".." || s.length > 200) throw new ApiError(0, "invalid_request", "Invalid identifier.");
  return encodeURIComponent(s);
}

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
function uuidSeg(id: string): string {
  if (!UUID.test(id)) throw new ApiError(0, "invalid_request", "Invalid identifier.");
  return id;
}

/** `{}` / 204 → `null`. */
const nothing = z.object({}).loose().transform(() => null);

export type ConnectorAuthInput = {
  kind: AuthKind;
  header_name?: string | undefined;
  secret?: string | undefined;
  token_url?: string | undefined;
  client_id?: string | undefined;
  client_secret?: string | undefined;
  scopes?: string[] | undefined;
  resource?: string | undefined;
};

export type ConnectorCreateInput = {
  slug: string;
  name: string;
  mcp_url: string;
  trust_level: string;
  auth: ConnectorAuthInput;
};

export type ToolPatch = Partial<Pick<ConnectorTool, "access" | "content_trust" | "egress" | "idempotency" | "status">>;

export type GatewayModelInput = {
  provider_model: string;
  mode: string;
  params: Record<string, unknown>;
  grant_all_tenants?: boolean | undefined;
};

export type GatewayTenantPatch = { rpm_limit?: number | undefined; tpm_limit?: number | undefined; budget_eur?: number | undefined };

/** The engine's audit filters: `action` prefix, `actor` username (exact), `target` substring. */
export type AuditQuery = {
  limit?: number | undefined;
  /** Row id: only rows older than it (the previous page's `nextBefore`). */
  before?: number | null | undefined;
  action?: string | undefined;
  actor?: string | undefined;
  target?: string | undefined;
  /** `platform`: platform-wide rows (tenant creation, gateway models); platform-admin only. */
  scope?: "tenant" | "platform" | undefined;
};

/** Largest page `GET /api/admin/audit` serves. */
export const AUDIT_PAGE_MAX = 200;

export class AdminApi {
  constructor(private readonly api: ApiClient) {}

  // ── §1 agents ──
  getSpec(): Promise<SpecEnvelope> {
    return this.api.call("/api/admin/agents/spec", { schema: specEnvelopeSchema });
  }

  listSpecVersions(): Promise<SpecVersionItem[]> {
    return this.api.call("/api/admin/agents/spec/versions", { schema: specVersionListSchema });
  }

  getSpecVersion(version: string) {
    return this.api.call(`/api/admin/agents/spec/versions/${seg(version)}`, { schema: specVersionDetailSchema });
  }

  /**
   * New version from `base_version` + a JSON merge patch; `409` when someone saved meanwhile.
   * M7: a tenant admin's guarded change is answered `202` with a change request instead.
   */
  async saveSpec(baseVersion: string, specPatch: Record<string, unknown>): Promise<Guarded<{ version: string; unchanged?: boolean | undefined }>> {
    const out = await this.api.callByStatus(
      "/api/admin/agents/spec",
      { method: "PUT", body: { base_version: baseVersion, spec_patch: specPatch } },
      guardedSchemaFor(specSavedSchema),
    );
    return out.data;
  }

  async restoreSpec(version: string): Promise<Guarded<{ version: string }>> {
    const out = await this.api.callByStatus("/api/admin/agents/spec/restore", { method: "POST", body: { version } }, guardedSchemaFor(specSavedSchema));
    return out.data;
  }

  catalog(): Promise<AgentCatalog> {
    return this.api.call("/api/admin/agents/catalog", { schema: catalogSchema });
  }

  // ── §2 gateway ──
  models(): Promise<GatewayModel[]> {
    return this.api.call("/api/admin/gateway/models", { schema: gatewayModelListSchema });
  }

  upsertModel(alias: string, body: GatewayModelInput): Promise<GatewayModel | null> {
    return this.api
      .call(`/api/admin/gateway/models/${seg(alias)}`, { method: "PUT", body, schema: gatewayModelSchema.nullable().catch(null) })
      .then((m) => m ?? null);
  }

  async deleteModel(alias: string): Promise<void> {
    await this.api.call(`/api/admin/gateway/models/${seg(alias)}`, { method: "DELETE", schema: okSchema });
  }

  gatewayTenant(): Promise<GatewayTenant> {
    return this.api.call("/api/admin/gateway/tenant", { schema: gatewayTenantSchema });
  }

  patchGatewayTenant(patch: GatewayTenantPatch): Promise<GatewayTenant | null> {
    return this.api.call("/api/admin/gateway/tenant", { method: "PATCH", body: patch, schema: gatewayTenantSchema.nullable().catch(null) });
  }

  rotateKey(): Promise<{ key_prefix?: string | null | undefined }> {
    return this.api.call("/api/admin/gateway/tenant/rotate-key", { method: "POST", schema: rotateKeySchema.catch({}) });
  }

  usage(days = 7): Promise<UsageRow[]> {
    const n = Math.min(90, Math.max(1, Math.floor(days)));
    return this.api.call(`/api/admin/gateway/usage?days=${n}`, { schema: usageSchema });
  }

  /** Per-alias rows plus the zero-filled daily totals (`daily`) when the engine sends them. */
  usageReport(days = 7): Promise<UsageReport> {
    const n = Math.min(90, Math.max(1, Math.floor(days)));
    return this.api.call(`/api/admin/gateway/usage?days=${n}`, { schema: usageReportSchema });
  }

  // ── §3 connectors ──
  connectors(): Promise<ConnectorSummary[]> {
    return this.api.call("/api/admin/connectors", { schema: connectorListSchema });
  }

  /** One connector with its stored last test (`last_test`), trust level and webhook path. */
  connector(slug: string): Promise<ConnectorDetailData> {
    return this.api.call(`/api/admin/connectors/${seg(slug)}`, { schema: connectorDetailSchema });
  }

  createConnector(body: ConnectorCreateInput): Promise<{ slug: string; status?: string | undefined }> {
    return this.api.call("/api/admin/connectors", { method: "POST", body, schema: connectorCreatedSchema });
  }

  testConnector(slug: string): Promise<ConnectorTest> {
    return this.api.call(`/api/admin/connectors/${seg(slug)}/test`, { method: "POST", schema: connectorTestSchema });
  }

  connectorTools(slug: string): Promise<ConnectorTool[]> {
    return this.api.call(`/api/admin/connectors/${seg(slug)}/tools`, { schema: connectorToolListSchema });
  }

  /** Reclassifying a write tool as read (or untrusted text as trusted) is guarded (M7: `202` change request). */
  async patchTool(slug: string, name: string, patch: ToolPatch): Promise<Guarded<ConnectorTool | null>> {
    const out = await this.api.callByStatus(
      `/api/admin/connectors/${seg(slug)}/tools/${seg(name)}`,
      { method: "PATCH", body: patch },
      guardedSchemaFor(connectorToolSchema.nullable().catch(null)),
    );
    return out.data;
  }

  /** Enabling an external-egress connector is guarded (M7: `202` change request). */
  async activateConnector(slug: string): Promise<Guarded<null>> {
    const out = await this.api.callByStatus(`/api/admin/connectors/${seg(slug)}/activate`, { method: "POST" }, guardedSchemaFor(okSchema.transform(() => null)));
    return out.data;
  }

  async disableConnector(slug: string): Promise<void> {
    await this.api.call(`/api/admin/connectors/${seg(slug)}/disable`, { method: "POST", schema: okSchema });
  }

  /** Deleting a connector is guarded (M7: `202` change request). */
  async deleteConnector(slug: string): Promise<Guarded<null>> {
    const out = await this.api.callByStatus(`/api/admin/connectors/${seg(slug)}`, { method: "DELETE" }, guardedSchemaFor(okSchema.transform(() => null)));
    return out.data;
  }

  connectorEvents(slug: string, limit = 20): Promise<ConnectorEvent[]> {
    const n = Math.min(200, Math.max(1, Math.floor(limit)));
    return this.api.call(`/api/admin/connectors/${seg(slug)}/events?limit=${n}`, { schema: connectorEventListSchema });
  }

  // ── §4 tenants (platform-admin) ──
  packs(): Promise<Pack[]> {
    return this.api.call("/api/admin/packs", { schema: packListSchema });
  }

  tenants(): Promise<Tenant[]> {
    return this.api.call("/api/admin/tenants", { schema: tenantListSchema });
  }

  createTenant(body: { slug: string; name: string; pack: string; admin_usernames: string[] }): Promise<TenantCreated | null> {
    return this.api.call("/api/admin/tenants", { method: "POST", body, schema: tenantCreatedSchema.nullable().catch(null) });
  }

  // ── knowledge base (docs/contracts/90 §7) ──
  /** The tenant's knowledge bases and the active embedding profile. */
  kbs(): Promise<KbList> {
    return this.api.call("/api/admin/kb", { schema: kbListSchema });
  }

  /** `409 name_taken` when a knowledge base with this name exists. */
  createKb(body: { name: string; description?: string | undefined }): Promise<KnowledgeBase> {
    return this.api.call("/api/admin/kb", { method: "POST", body, schema: knowledgeBaseSchema });
  }

  kbDocuments(kbId: string): Promise<KbDocument[]> {
    return this.api.call(`/api/admin/kb/${seg(kbId)}/documents`, { schema: kbDocumentListSchema });
  }

  /**
   * Multipart upload (`file`, optional `title`). Answers the stored document; `duplicate: true`
   * when the same bytes were already there. 413 / 415 / 422 / 502 / 503 as `ApiError`.
   */
  uploadKbDocument(kbId: string, file: Blob & { name?: string }, title?: string | undefined): Promise<KbDocument> {
    const form = new FormData();
    form.append("file", file, file.name ?? "document");
    if (title?.trim()) form.append("title", title.trim());
    return this.api.call(`/api/admin/kb/${seg(kbId)}/documents`, { method: "POST", form, schema: kbDocumentSchema });
  }

  async deleteKbDocument(kbId: string, docId: string): Promise<void> {
    await this.api.call(`/api/admin/kb/${seg(kbId)}/documents/${seg(docId)}`, { method: "DELETE", schema: okSchema });
  }

  reembedJobs(): Promise<ReembedList> {
    return this.api.call("/api/admin/kb/reembed", { schema: reembedListSchema });
  }

  /**
   * Start a re-embed: `tenant` re-embeds this workspace's chunks that have no vector yet;
   * `platform` (platform admins) builds a new embedding profile (`alias`, `dims`) and swaps it in.
   * `409 reembed_running` while one runs.
   */
  reembed(body: { scope: "tenant" } | { scope: "platform"; alias?: string | undefined; dims?: number | undefined }): Promise<ReembedJob> {
    return this.api.call("/api/admin/kb/reembed", { method: "POST", body, schema: reembedJobSchema });
  }

  // ── approval policies (M5, docs/contracts/95 §1) ──
  /** The active rule set (or the defaults), the spec version it belongs to and what the server can do. */
  policies(): Promise<PoliciesEnvelope> {
    return this.api.call("/api/admin/policies", { schema: policiesEnvelopeSchema });
  }

  /**
   * Store a new rule set as a new agent spec version (`409 version_conflict` when someone saved
   * meanwhile, `403 guarded_change` for a tenant admin's guarded edit, `422 policy_invalid` with
   * per-rule `errors` when a condition does not compile). The policy version is bumped by the
   * server when the rules change.
   */
  async savePolicies(baseVersion: string, rules: readonly PolicyRuleView[]): Promise<Guarded<PoliciesSaved>> {
    const out = await this.api.callByStatus("/api/admin/policies", { method: "PUT", body: { base_version: baseVersion, rules } }, guardedSchemaFor(policiesSavedSchema));
    return out.data;
  }

  /** Evaluate the active rules, or a draft (`rules`), against a sample context. */
  testPolicy(context: PolicyContext, rules?: readonly PolicyRuleView[] | undefined): Promise<PolicyTestResult> {
    return this.api.call("/api/admin/policies/test", {
      method: "POST",
      body: rules ? { context, rules } : { context },
      schema: policyTestResultSchema,
    });
  }

  // ── audit (route not in 70; see README) ──
  /**
   * One page of `admin_audit`, newest first. Never sends `tenant=`: on `/api/admin/*` that query
   * parameter switches a platform admin into another tenant (an audited cross-tenant request).
   */
  audit(q: AuditQuery = {}): Promise<AuditPageData> {
    const params = new URLSearchParams();
    params.set("limit", String(Math.min(AUDIT_PAGE_MAX, Math.max(1, Math.floor(q.limit ?? AUDIT_PAGE_MAX)))));
    if (typeof q.before === "number" && Number.isInteger(q.before) && q.before > 0) params.set("before", String(q.before));
    if (q.action) params.set("action", q.action);
    if (q.actor) params.set("actor", q.actor);
    if (q.target) params.set("target", q.target);
    if (q.scope === "platform") params.set("scope", "platform");
    return this.api.call(`/api/admin/audit?${params.toString()}`, { schema: auditPageSchema });
  }

  // ───────────────────────────── M7 (docs/contracts/110) ─────────────────────────────

  // §1 four-eyes: pending changes
  /** `pending` (default) or every request the engine still lists (`all`: no status filter). */
  changeRequests(status: "pending" | "all" = "pending"): Promise<ChangeRequest[]> {
    const q = status === "pending" ? "?status=pending&limit=200" : "?limit=200";
    return this.api.call(`/api/admin/change-requests${q}`, { schema: changeRequestListSchema });
  }

  /** Requests waiting for a second admin (the engine's `pending` count; the list when it sends none). */
  pendingChangeCount(): Promise<number> {
    return this.api.call("/api/admin/change-requests?status=pending&limit=200", { schema: pendingCountSchema });
  }

  changeRequest(id: string): Promise<ChangeRequest> {
    return this.api.call(`/api/admin/change-requests/${uuidSeg(id)}`, { schema: changeRequestAnswerSchema });
  }

  /**
   * Approve (applies the change atomically, re-validated; `409 stale_base` when the base version
   * moved) or reject, with an optional reason. Self-approval is refused by the server (`403`).
   */
  decideChangeRequest(id: string, decision: "approve" | "reject", reason: string | null): Promise<ChangeRequest | null> {
    return this.api.call(`/api/admin/change-requests/${uuidSeg(id)}/${decision}`, {
      method: "POST",
      body: { reason },
      schema: z.union([changeRequestAnswerSchema, nothing]),
    });
  }

  // §2 users
  users(): Promise<User[]> {
    return this.api.call("/api/admin/users", { schema: userListSchema });
  }

  /** Creates the user in Keycloak (temporary password + UPDATE_PASSWORD); a tenant admin creating an admin gets a change request. */
  async createUser(body: AdminUserCreate): Promise<Guarded<UserCreated>> {
    return (await this.api.callByStatus("/api/admin/users", { method: "POST", body }, guardedSchemaFor(userCreatedSchema))).data;
  }

  async patchUser(id: string, patch: AdminUserPatch): Promise<Guarded<User | null>> {
    return (await this.api.callByStatus(`/api/admin/users/${uuidSeg(id)}`, { method: "PATCH", body: patch }, guardedSchemaFor(userAnswerSchema))).data;
  }

  /** `DELETE` disables the account (it is never removed). */
  disableUser(id: string): Promise<User | null> {
    return this.api.call(`/api/admin/users/${uuidSeg(id)}`, { method: "DELETE", schema: userAnswerSchema });
  }

  // §2 tenants (platform-admin)
  /** Suspend (blocks sign-ins and API calls of the workspace) or reactivate it. */
  async setTenantSuspended(slug: string, suspended: boolean): Promise<void> {
    await this.api.call(`/api/admin/tenants/${safeSeg(slug)}/${suspended ? "suspend" : "reactivate"}`, { method: "POST", schema: okSchema });
  }

  // guarded connector settings (110 §1)
  /** Connector trust level; adding `first_party` is guarded (change request `connector_trust`). */
  async setConnectorTrust(slug: string, trustLevel: "first_party" | "third_party"): Promise<Guarded<null>> {
    return (await this.api.callByStatus(`/api/admin/connectors/${safeSeg(slug)}`, { method: "PATCH", body: { trust_level: trustLevel } }, guardedSchemaFor(nothing))).data;
  }

  /** Agent tool binding; `auto` on a write / destructive tool is guarded (change request `tool_binding`). */
  async setToolBinding(slug: string, tool: string, binding: ToolBinding): Promise<Guarded<ConnectorTool | null>> {
    return (
      await this.api.callByStatus(
        `/api/admin/connectors/${safeSeg(slug)}/tools/${safeSeg(tool)}/binding`,
        { method: "PUT", body: { binding } },
        guardedSchemaFor(z.union([connectorToolSchema, nothing])),
      )
    ).data;
  }

  /** Deletes a knowledge base with its documents (guarded: change request `kb_delete`); `409 kb_in_use` while an agent uses it. */
  async deleteKb(kbId: string): Promise<Guarded<null>> {
    return (await this.api.callByStatus(`/api/admin/kb/${safeSeg(kbId)}`, { method: "DELETE" }, guardedSchemaFor(nothing))).data;
  }

  // §3 agents playground
  /** Whether a playground turn can run here (`available: false` when the engine only replays recorded answers or it is switched off). */
  playgroundStatus(): Promise<PlaygroundStatus> {
    return this.api.call("/api/admin/agents/playground", { schema: playgroundStatusSchema });
  }

  /** Streams the turn's AG-UI frames to `onFrame`; a non-streaming server's JSON result is returned instead. */
  playground(
    body: { specialist: unknown; question: string },
    onFrame: (frame: unknown) => void,
    signal?: AbortSignal | undefined,
  ): Promise<{ streamed: boolean; json: unknown }> {
    return this.api.postStream("/api/admin/agents/playground", body, (f) => onFrame(f), { signal });
  }

  // §4 tier-A mappings, capabilities, crawl
  /** The saved mapping; `version: null` / `mapping: null` when the connector has none yet. */
  async mapping(slug: string, kind: MappingKind): Promise<MappingEnvelope | null> {
    const env = await this.api.call(`/api/admin/connectors/${safeSeg(slug)}/mappings/${kind}`, { schema: mappingEnvelopeSchema });
    return env.mapping === null || env.version === null ? null : env;
  }

  /**
   * New version (`base_version` = the version edited, `null` for the first). `409
   * version_conflict` when it moved; `422 mapping_invalid` with `errors` (contract) / `problems`
   * (approved-tool checks). An identical mapping answers `unchanged: true`. M8: `proposal_id` =
   * the proposal the draft was opened from; an action mapping from a proposal is guarded (a tenant
   * admin gets `202` + change request `connector_mapping`).
   */
  async saveMapping(
    slug: string,
    kind: MappingKind,
    body: { base_version: number | null; mapping: Record<string, unknown>; proposal_id?: string | undefined },
  ): Promise<Guarded<MappingSaved>> {
    const out = await this.api.callByStatus(`/api/admin/connectors/${safeSeg(slug)}/mappings/${kind}`, { method: "PUT", body }, guardedSchemaFor(mappingSavedSchema));
    return out.data;
  }

  /** M8 preview through the DuckDB compiler: `live` (≤ `max_pages` per source) or `staged` (the last crawl, no vendor call). */
  previewReadMapping(
    slug: string,
    body: { source: "live" | "staged"; mapping?: Record<string, unknown> | undefined; max_pages?: number | undefined; sample?: number | undefined },
  ): Promise<ReadPreview> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/mappings/read/preview`, { method: "POST", body, schema: readPreviewSchema });
  }

  /** M8 explain: what the compiler makes of a draft (or the saved) read mapping — no vendor call. */
  compileReadMapping(slug: string, mapping?: Record<string, unknown> | undefined): Promise<MappingPlan> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/mappings/read/compile`, { method: "POST", body: mapping ? { mapping } : {}, schema: mappingPlanSchema });
  }

  /** M8: the last crawl's staged raw pages (`null` when there are none: the staged preview is unavailable). */
  async stagedSet(slug: string): Promise<StagedSet | null> {
    try {
      return await this.api.call(`/api/admin/connectors/${safeSeg(slug)}/staged`, { schema: stagedSetSchema });
    } catch (err) {
      if (err instanceof ApiError && err.status === 404) return null;
      throw err;
    }
  }

  /** M8: whether proposals run here and whether the AI step is available. */
  proposerStatus(): Promise<ProposerStatus> {
    return this.api.call("/api/admin/mappings/proposer", { schema: proposerStatusSchema });
  }

  /** M8: sample (when `sample`) and propose read / action drafts; stored for review, never activated. */
  proposeMappings(slug: string, body: ProposeBody): Promise<Proposal> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/mappings/proposals`, { method: "POST", body, schema: proposalSchema });
  }

  mappingProposals(slug: string): Promise<ProposalSummary[]> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/mappings/proposals`, { schema: proposalListSchema });
  }

  mappingProposal(slug: string, id: string): Promise<Proposal> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/mappings/proposals/${uuidSeg(id)}`, { schema: proposalSchema });
  }

  discardProposal(slug: string, id: string): Promise<Proposal> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/mappings/proposals/${uuidSeg(id)}/discard`, { method: "POST", schema: proposalSchema });
  }

  /** Crawls at most `max_pages` page(s) per source with a draft (or the saved) read mapping and maps them — nothing is stored. */
  testReadMapping(slug: string, body: { mapping?: Record<string, unknown> | undefined; max_pages?: number | undefined }): Promise<ReadPreview> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/mappings/read/test`, { method: "POST", body, schema: readPreviewSchema });
  }

  /** Renders the vendor call for a sample action without executing it (and runs the read-back query). */
  dryRun(slug: string, body: MappingDryRunRequest): Promise<DryRunResult> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/mappings/action/dry-run`, { method: "POST", body, schema: dryRunResultSchema });
  }

  capabilities(slug: string): Promise<Capabilities> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/capabilities`, { schema: capabilitiesSchema });
  }

  /** Canonical snapshot history (crawled or exported) of a connector: current snapshot, trigger state, latest attempts. */
  snapshots(slug: string, limit = 10): Promise<SnapshotHistory> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/snapshots?limit=${Math.min(200, Math.max(1, Math.floor(limit)))}`, { schema: snapshotHistorySchema });
  }

  /** Queues a sync now (a crawl for a tier-A connector with a read mapping); `409` when the connector can't be synced. */
  syncNow(slug: string): Promise<SyncQueued> {
    return this.api.call(`/api/admin/connectors/${safeSeg(slug)}/sync`, { method: "POST", schema: syncQueuedSchema });
  }

  // §5 signals admin
  signalSources(): Promise<SignalSourceList> {
    return this.api.call("/api/admin/signals/sources", { schema: signalSourceListSchema });
  }

  /** The tenant's subscription to one source: `enabled` and / or `cadence_s` (`null` = the source's default). */
  setSignalSource(id: string, patch: SourceSettingPatch): Promise<SourceSaved | null> {
    const body: Record<string, unknown> = {};
    if (patch.enabled !== undefined) body["enabled"] = patch.enabled;
    if (patch.cadence_s !== undefined) body["cadence_s"] = patch.cadence_s;
    return this.api.call(`/api/admin/signals/sources/${safeSeg(id)}`, { method: "PUT", body, schema: sourceSavedSchema.nullable().catch(null) });
  }

  /** Site codes of the tenant network by type (for radius watch regions). */
  signalNetwork(): Promise<NetworkCodes> {
    return this.api.call("/api/admin/signals/network", { schema: networkCodesSchema });
  }

  watchRegions(): Promise<WatchRegion[]> {
    return this.api.call("/api/admin/signals/watch-regions", { schema: watchRegionListSchema });
  }

  /** Replaces the list; answers what was stored and whether the hub took it (`hubSynced: false` = pushed again later). */
  async saveWatchRegions(regions: readonly WatchRegion[]): Promise<{ regions: WatchRegion[] | null; hubSynced: boolean | null; hubError: string | null }> {
    const out = await this.api.call("/api/admin/signals/watch-regions", {
      method: "PUT",
      body: { regions: regions.map(regionBody) },
      schema: z.unknown(),
    });
    const parsed = watchRegionListSchema.safeParse(out);
    const o = out && typeof out === "object" && !Array.isArray(out) ? (out as Record<string, unknown>) : {};
    return {
      regions: parsed.success && out !== null ? parsed.data : null,
      hubSynced: typeof o["hub_synced"] === "boolean" ? o["hub_synced"] : null,
      hubError: typeof o["hub_error"] === "string" ? o["hub_error"] : null,
    };
  }

  alertRules(): Promise<AlertRules> {
    return this.api.call("/api/admin/signals/alert-rules", { schema: alertRulesSchema });
  }

  /** Stores the rules (`[]` = back to the defaults) and pushes them to the hub. */
  saveAlertRules(rules: readonly AlertRule[]): Promise<AlertRules | null> {
    return this.api.call("/api/admin/signals/alert-rules", { method: "PUT", body: { rules }, schema: alertRulesSchema.nullable().catch(null) });
  }

  // §6 audit activity & evals
  /**
   * One newest-first timeline across runs, approvals, change sets, admin changes and signals of
   * the active tenant (`next_before` is an opaque cursor). Never sends `tenant=`.
   */
  activity(q: ActivityQuery = {}): Promise<ActivityPage> {
    const params = new URLSearchParams();
    params.set("limit", String(Math.min(AUDIT_PAGE_MAX, Math.max(1, Math.floor(q.limit ?? 100)))));
    for (const c of q.categories ?? []) if (isAuditCategory(c)) params.append("category", c);
    if (q.before) params.set("before", q.before.slice(0, 300));
    if (q.actor) params.set("actor", q.actor.slice(0, 200));
    if (q.case_id) params.set("case_id", q.case_id.slice(0, 64));
    if (q.action) params.set("action", q.action.slice(0, 100));
    if (q.since) params.set("since", q.since);
    if (q.until) params.set("until", q.until);
    return this.api.call(`/api/admin/audit/activity?${params.toString()}`, { schema: activityPageSchema });
  }

  /** Latest eval runs (platform admins; read from the mounted `evals/results`). */
  evals(): Promise<EvalList> {
    return this.api.call("/api/admin/evals?limit=10", { schema: evalsResponseSchema });
  }

  // M8 Evals UI (docs/contracts/128 §4): platform admins only.
  evalsStatus(): Promise<EvalStatusData> {
    return this.api.call("/api/admin/evals/status", { schema: evalStatusSchema });
  }

  evalRuns(q: { limit?: number; before?: string | null; mode?: string | null; source?: string | null } = {}): Promise<EvalRunList> {
    const params = new URLSearchParams({ limit: String(Math.min(100, Math.max(1, Math.floor(q.limit ?? 20)))) });
    if (q.before) params.set("before", uuidSeg(q.before));
    if (q.mode) params.set("mode", q.mode);
    if (q.source) params.set("source", q.source);
    return this.api.call(`/api/admin/evals/runs?${params.toString()}`, { schema: evalRunListSchema });
  }

  /** `409 evals_unavailable {reason}` / `eval_running`, `422 unknown_cases` / `confirm_required` / `budget_*`. */
  createEvalRun(body: EvalsRunCreate): Promise<EvalRun> {
    return this.api.call("/api/admin/evals/runs", { method: "POST", body, schema: evalRunSchema });
  }

  evalRun(runId: string): Promise<EvalRunDetail> {
    return this.api.call(`/api/admin/evals/runs/${uuidSeg(runId)}`, { schema: evalRunDetailSchema });
  }

  evalRunEvents(runId: string, after = 0): Promise<EvalEvents> {
    return this.api.call(`/api/admin/evals/runs/${uuidSeg(runId)}/events?after=${Math.max(0, Math.floor(after))}`, { schema: evalEventsSchema });
  }

  evalCase(runId: string, caseId: string): Promise<EvalCaseResult> {
    return this.api.call(`/api/admin/evals/runs/${uuidSeg(runId)}/cases/${safeSeg(caseId)}`, { schema: evalCaseResultSchema });
  }

  cancelEvalRun(runId: string): Promise<EvalRun> {
    return this.api.call(`/api/admin/evals/runs/${uuidSeg(runId)}/cancel`, { method: "POST", schema: evalRunSchema });
  }

  /** Same-origin download link of a finished run (`keelson-evals/2`, re-imports as the run). */
  evalExportHref(runId: string): string {
    return `/api/admin/evals/runs/${uuidSeg(runId)}/export`;
  }

  /** A parsed results document (`keelson-evals/1|2`); `200 duplicate: true` when it was imported before. */
  importEvals(doc: Record<string, unknown>, fileName?: string | undefined): Promise<ImportAnswer> {
    const q = fileName ? `?file_name=${encodeURIComponent(fileName.slice(0, 120))}` : "";
    return this.api.call(`/api/admin/evals/import${q}`, { method: "POST", body: doc, schema: importAnswerSchema });
  }

  /** Every new file of the mounted results folder, or only `file` (a `<UTC stamp>.json` name). */
  importEvalsFolder(file?: string | undefined): Promise<FolderImport> {
    const q = file ? `?file=${encodeURIComponent(file.slice(0, 40))}` : "";
    return this.api.call(`/api/admin/evals/import/folder${q}`, { method: "POST", schema: folderImportSchema });
  }

  evalFiles(): Promise<EvalFiles> {
    return this.api.call("/api/admin/evals/files", { schema: evalFilesSchema });
  }

  compareEvalRuns(base: string, head: string): Promise<EvalsCompare> {
    return this.api.call(`/api/admin/evals/compare?base=${uuidSeg(base)}&head=${uuidSeg(head)}`, { schema: evalCompareSchema });
  }

  evalTrends(mode: "all" | "replay" | "live" = "all", limit = 30): Promise<EvalTrendsData> {
    const params = new URLSearchParams({ limit: String(Math.min(100, Math.max(2, Math.floor(limit)))) });
    if (mode !== "all") params.set("mode", mode);
    return this.api.call(`/api/admin/evals/trends?${params.toString()}`, { schema: evalTrendsSchema });
  }
}
