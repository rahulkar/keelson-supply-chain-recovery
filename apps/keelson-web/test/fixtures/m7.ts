/**
 * M7 back-office fixtures (`docs/contracts/110`), in the engine's answer shapes
 * (`services/engine/.../api/routes_admin_m7.py` and the core modules it calls). Every
 * contract-typed object is checked against the generated Zod schemas in `m7.test.tsx`.
 */

export const CR_SPEC = "0b9c1f9e-1c1a-4f8e-9a51-2b5b7a0c0001";
export const CR_OWN = "0b9c1f9e-1c1a-4f8e-9a51-2b5b7a0c0002";
export const CR_OLD = "0b9c1f9e-1c1a-4f8e-9a51-2b5b7a0c0003";
export const CR_USER = "0b9c1f9e-1c1a-4f8e-9a51-2b5b7a0c0004";

export const SUB_SAM = "6f0d3a2e-0000-4000-8000-00000000000a";
export const SUB_NORA = "6f0d3a2e-0000-4000-8000-00000000000b";
export const SUB_AVA = "6f0d3a2e-0000-4000-8000-00000000000c";

export const NOW = new Date("2026-10-08T10:00:00Z");

type Json = Record<string, unknown>;

/** An `AdminChangeRequest` (pending, asked by Nora two hours before `NOW`). */
export function changeRequest(over: Json = {}): Json {
  return {
    id: CR_SPEC,
    tenant: "demo-retail",
    kind: "agent_spec",
    target: "agent_spec",
    base_version: "v3",
    payload: { spec_patch: { policy: { auto_max_lines: 25 } } },
    diff: [{ op: "replace", path: "/policy/auto_max_lines", before: 10, after: 25 }],
    reasons: [{ code: "auto_cap_raised", field: "policy.auto_max_lines", reason: "raises the automatic-apply limit from 10 to 25 lines" }],
    requested_by: { sub: SUB_NORA, username: "nora", name: "Nora Berg" },
    requested_at: "2026-10-08T08:00:00Z",
    status: "pending",
    approver: null,
    decided_at: null,
    decision_reason: null,
    expires_at: "2026-10-11T08:00:00Z",
    ...over,
  };
}

export function changeRequests(): Json[] {
  return [
    changeRequest(),
    changeRequest({
      id: CR_OWN,
      kind: "tool_binding",
      target: "connector:merxa/tool:update_item",
      base_version: null,
      payload: { connector: "merxa", tool: "update_item", binding: "auto" },
      diff: [{ op: "replace", path: "/binding", before: "approval_gate", after: "auto" }],
      reasons: [{ code: "auto_binding_on_write", field: "connector:merxa/tool:update_item", reason: "lets agents call the write tool without approval" }],
      requested_by: { sub: SUB_SAM, username: "sam", name: "Sam Okafor" },
      requested_at: "2026-10-08T09:00:00Z",
      expires_at: "2026-10-11T09:00:00Z",
    }),
    changeRequest({
      id: CR_USER,
      kind: "user_roles",
      target: "user:lee",
      base_version: null,
      payload: { action: "create", user: { username: "lee", email: "lee@example.test", name: "Lee Park", roles: ["tenant-admin"] } },
      diff: [{ op: "add", path: "/users/lee", before: null, after: { roles: ["tenant-admin"] } }],
      reasons: [{ code: "admin_role_granted", field: "user:lee", reason: "grants tenant-admin" }],
      requested_at: "2026-10-05T07:00:00Z",
      expires_at: "2026-10-08T07:00:00Z",
    }),
    changeRequest({
      id: CR_OLD,
      kind: "kb_delete",
      target: "kb:playbooks",
      base_version: null,
      diff: [{ op: "remove", path: "/kbs/playbooks", before: { name: "Playbooks" }, after: null }],
      reasons: [{ code: "kb_deleted", field: "kb:playbooks", reason: "deletes a knowledge base" }],
      status: "rejected",
      approver: { sub: SUB_AVA, username: "ava", name: "Ava Lindqvist", role: "platform-admin" },
      decided_at: "2026-10-07T12:00:00Z",
      decision_reason: "Still used by the playbook analyst",
      requested_at: "2026-10-07T10:00:00Z",
      expires_at: "2026-10-10T10:00:00Z",
    }),
  ];
}

export const USER_ID = {
  sam: "7a6b1c00-0000-4000-8000-000000000001",
  nora: "7a6b1c00-0000-4000-8000-000000000002",
  ava: "7a6b1c00-0000-4000-8000-000000000003",
  pat: "7a6b1c00-0000-4000-8000-000000000004",
  dee: "7a6b1c00-0000-4000-8000-000000000005",
} as const;

export function users(): Json[] {
  return [
    { id: USER_ID.sam, username: "sam", email: "sam@example.test", name: "Sam Okafor", enabled: true, roles: ["planner", "tenant-admin"], tenants: ["demo-retail"], created_at: "2026-10-01T00:00:00+00:00" },
    { id: USER_ID.nora, username: "nora", email: "nora@example.test", name: "Nora Berg", enabled: true, roles: ["approver"], tenants: ["demo-retail"], created_at: "2026-10-01T00:00:00+00:00" },
    { id: USER_ID.ava, username: "ava", email: null, name: "Ava Lindqvist", enabled: true, roles: ["platform-admin"], tenants: ["demo-retail", "northwind-test"], created_at: null },
    { id: USER_ID.pat, username: "pat", email: "pat@example.test", name: "Pat Shared", enabled: true, roles: ["planner"], tenants: ["demo-retail", "northwind-test"], created_at: null },
    { id: USER_ID.dee, username: "dee", email: "dee@example.test", name: "Dee Former", enabled: false, roles: ["planner"], tenants: ["demo-retail"], created_at: null },
  ];
}

/** `ConnectorCapabilities` of the foreign ERP (tier A, both mappings saved). */
export function capabilities(over: Json = {}): Json {
  return {
    connector: "merxa-lite",
    tiers: ["A"],
    sync: "crawl",
    commit: "per_op",
    consistency: "crawled",
    advisory: true,
    read_mapping_version: 2,
    action_mapping_version: 1,
    mapped_ops: ["transfer_order.create", "allocation.set_qty"],
    ...over,
  };
}

export const READ_MAPPING: Json = {
  kind: "read",
  entities: [
    {
      entity: "location",
      source: { tool: "list_items", args: { type: "location" }, page_param: "page", page_size: 100, items_path: "$.items", cursor_path: null, max_pages: 20 },
      fields: { code: "$.key", name: "$.fields.name", type: "$.fields.kind" },
    },
  ],
};

export const ACTION_MAPPING: Json = {
  kind: "action",
  ops: [
    {
      op: "transfer_order.create",
      tool: "create_transfer_order",
      args: { from_dc: "{{payload.from_dc}}", to_dc: "{{payload.to_dc}}", lines: "{{payload.lines}}" },
      idempotency: "best_effort",
      request_id_arg: null,
      readback: { tool: "list_items", args: { type: "transfer_order" }, items_path: "$.items", match: { from_dc: "{{payload.from_dc}}" } },
    },
  ],
};

/** `GET …/mappings/{kind}` row. */
export function mappingRow(kind: "read" | "action", version: number | null, over: Json = {}): Json {
  if (version === null) return { connector: "merxa-lite", kind, version: null, mapping: null, problems: [] };
  return {
    connector: "merxa-lite",
    kind,
    version,
    mapping: kind === "read" ? READ_MAPPING : ACTION_MAPPING,
    summary: kind === "read" ? "1 entity mappings: location" : "1 ops: transfer_order.create",
    created_by: "sam",
    created_at: "2026-10-07T09:00:00+00:00",
    problems: [],
    ...over,
  };
}

/** Tools of the foreign ERP (`GET …/tools`). */
export function liteTools(): Json[] {
  return [
    { name: "list_items", title: "List items", access: "read", content_trust: "trusted_data", egress: "internal", idempotency: "none", status: "approved", binding: "auto" },
    { name: "list_notes", title: "Notes", access: "read", content_trust: "untrusted_text", egress: "internal", idempotency: "none", status: "approved", binding: "approval_gate" },
    { name: "create_transfer_order", title: "Create a transfer order", access: "write", content_trust: "trusted_data", egress: "internal", idempotency: "best_effort", status: "approved", binding: "approval_gate" },
    { name: "update_item", title: "Update item", access: "write", content_trust: "trusted_data", egress: "internal", idempotency: "best_effort", status: "approved", binding: "approval_gate" },
  ];
}

export function liteConnector(over: Json = {}): Json {
  return {
    slug: "merxa-lite",
    name: "Northwind ERP (lite)",
    url: "http://merxa-api:8000/mcp-lite",
    auth_kind: "static_header",
    tier: ["A"],
    status: "active",
    trust_level: "third_party",
    tools: { approved: 4, needs_review: 0, disabled: 0 },
    last_test_at: "2026-10-07T09:00:00Z",
    last_test_ok: true,
    webhook: false,
    ...over,
  };
}

/** `GET …/snapshots`: a ready crawled snapshot plus an older failed attempt. */
export function snapshots(over: Json = {}): Json {
  const current = {
    id: "snap-2",
    connector: "merxa-lite",
    status: "ready",
    source_snapshot_id: "crawl:snap-2",
    as_of: "2026-10-08T09:19:00+00:00",
    watermark: null,
    reason: "admin:sam",
    row_counts: { location: 42, sku: 1200, inventory_position: 9800 },
    rows: 11042,
    bytes: 0,
    duration_ms: 420000,
    error: null,
    created_at: "2026-10-08T09:12:00+00:00",
    ready_at: "2026-10-08T09:19:00+00:00",
    pins: 1,
    consistency: "crawled",
    as_of_range: { from: "2026-10-08T09:12:00+00:00", to: "2026-10-08T09:19:00+00:00" },
    crawl: { pages: 113, rows_seen: 11042, truncated: ["inventory_position"], lookups: {} },
  };
  return {
    connector: "merxa-lite",
    current,
    state: { stale: false },
    items: [current, { ...current, id: "snap-1", status: "failed", error: "tool_error: list_items failed on page 3", created_at: "2026-10-07T09:00:00+00:00", ready_at: null, crawl: null, as_of_range: null }],
    ...over,
  };
}

export function readPreview(): Json {
  return {
    ok: false,
    error: null,
    entities: [
      { entity: "location", where: "entities[0]", rows: 2, sample: [{ code: "DC-1", name: "Rotterdam DC", type: "dc" }, { code: "ST-9", name: "Store 9", type: "store" }], error: null },
      { entity: "sku", where: "entities[1]", rows: 0, sample: [], error: "expression_error: int() of 'n/a' at $.fields.size" },
    ],
    pages: 2,
    rows_seen: 3,
    truncated: ["location"],
    lookups: {},
  };
}

export function dryRun(): Json {
  return {
    op: "transfer_order.create",
    mapped: true,
    executed: false,
    call: { tool: "create_transfer_order", args: { from_dc: "DC-1", to_dc: "DC-2", lines: [{ sku: "SKU-1", qty: 10 }] } },
    idempotency: "best_effort",
    request_id_arg: null,
    guided_reason: null,
    readback: { call: { tool: "list_items", args: { type: "transfer_order" } }, executed: true, ok: true, items_found: 3, matched: false, detail: null },
  };
}

export function signalSources(): Json {
  return {
    items: [
      { id: "gdacs", title: "GDACS disaster alerts", licence: "CC BY 4.0", cadence_s: 900, last_polled_at: "2026-10-08T09:45:00Z", next_poll_at: null, last_status: "ok", last_error: null, consecutive_failures: 0, items_total: 312, enabled: true },
      { id: "usgs", title: null, licence: "Public domain (USGS)", cadence_s: 300, last_polled_at: "2026-10-08T09:55:00Z", next_poll_at: null, last_status: "error", last_error: "HTTP 503", consecutive_failures: 3, items_total: 40, enabled: false },
    ],
    hub_error: null,
  };
}

export function watchRegions(): Json {
  return {
    items: [
      { id: "cat-lai", name: "Cat Lai port area", kind: "radius", node: { type: "port", code: "VNSGN" }, radius_km: 150, polygon: null, enabled: true },
      {
        id: "bay",
        name: "Bay of Bengal",
        kind: "polygon",
        polygon: { type: "Polygon", coordinates: [[[80, 10], [95, 10], [95, 22], [80, 22], [80, 10]]] },
        node: null,
        radius_km: null,
        enabled: true,
      },
    ],
  };
}

export function alertRules(): Json {
  const rule = { id: "severe", action: "alert_and_draft_case", hysteresis_bp: 500, when: { kinds: ["natural_hazard"], node_types: [], min_severity_bp: 7000, min_confidence_bp: 6000, min_exposed_nodes: 1 } };
  return { custom: false, rules: [rule], defaults: [rule] };
}

export function activity(): Json[] {
  return [
    { key: "admin:9", category: "admin", at: "2026-10-08T09:30:00+00:00", actor: "sam", action: "connector.mapping.update", target: "connector:merxa-lite/mapping:read", case_id: null, detail: { before: { version: 1 }, after: { version: 2 }, trace_id: null } },
    { key: "approval:5", category: "approvals", at: "2026-10-08T09:20:00+00:00", actor: "nora", action: "approval.approve", target: "interrupt:i-1", case_id: "019a0c4e-6f1e-7c8a-9d55-0b1c2d3e4f50", detail: { role: "approver", decision: "approve", reason: "Looks right", plan_version: "p2" } },
    { key: "run:r-1", category: "runs", at: "2026-10-08T09:00:00+00:00", actor: "priya", action: "run.chat.succeeded", target: "case:019a0c4e-6f1e-7c8a-9d55-0b1c2d3e4f50", case_id: "019a0c4e-6f1e-7c8a-9d55-0b1c2d3e4f50", detail: { kind: "chat", status: "succeeded", agent_spec_version: "v3" } },
    { key: "change_set:c-1", category: "change_sets", at: "2026-10-07T18:00:00+00:00", actor: null, action: "change_set.applied", target: "change_set:c-1", case_id: "019a0c4e-6f1e-7c8a-9d55-0b1c2d3e4f50", detail: { connector: "merxa", lines: 4, ext_code: "CS-104" } },
    { key: "alert_case:a-1", category: "signals", at: "2026-10-07T08:00:00+00:00", actor: "system", action: "signal.alert_case_auto", target: "alert:a-1", case_id: "019a0c4e-6f1e-7c8a-9d55-0b1c2d3e4f51", detail: { auto: true } },
  ];
}

/** `GET /api/admin/evals`: the engine's summary of `evals/results/*.json` (counts, not check lists). */
export function evals(): Json {
  return {
    directory_available: true,
    items: [
      {
        run: "20261008T080000Z",
        file: "20261008T080000Z.json",
        error: null,
        schema: "keelson-evals/1",
        started_at: "2026-10-08T08:00:00Z",
        finished_at: "2026-10-08T08:20:00Z",
        summary: { cases: 2, passed: 1, failed: 1, checks: 20, checks_failed: 2, numeric_grounding_mean: 0.95 },
        cases: [
          { id: "trim-delay", title: "Trim delay", tags: ["act1"], status: "pass", duration_s: 61.2, checks: 12, checks_failed: 0, scores: { numeric_grounding: 1, entity_recall: 0.9 } },
          { id: "port-strike", title: "Port strike", tags: ["act2"], status: "fail", duration_s: 80, checks: 8, checks_failed: 2, scores: { numeric_grounding: 0.9 } },
        ],
      },
      { run: "20261007T080000Z", file: "20261007T080000Z.json", error: "unreadable: JSONDecodeError" },
    ],
  };
}

/** A custom specialist as saved in the spec. */
export const SUPPLIER_RISK: Json = {
  slug: "supplier_risk",
  display_name: "Supplier risk analyst",
  description: "Checks supplier delays and signals for the affected styles.",
  role_prompt: "Look for late purchase orders and recent supplier signals.",
  model_alias: "fast-default",
  tools: [
    { connector: "merxa", tool: "list_purchase_orders" },
    { connector: "merxa", tool: "list_supplier_messages" },
  ],
  kb_ids: [],
  max_tool_calls: 8,
  max_turns: 6,
  enabled: true,
};

export const PLAYGROUND_ID = "5c3e2a10-0000-4000-8000-000000000001";

export function playgroundResult(over: Json = {}): Json {
  return {
    playground_id: PLAYGROUND_ID,
    specialist: "supplier_risk",
    display_name: "Supplier risk analyst",
    status: "completed",
    committed: false,
    started_at: "2026-10-08T10:00:00Z",
    duration_ms: 8200,
    tool_calls: [{ connector: "merxa", tool: "list_supplier_messages", ok: true, quarantined: true, duration_ms: 300, evidence_id: null, error: null }],
    usage: { llm_calls: 4, tokens_in: 10000, tokens_out: 2400, tool_calls: 3 },
    findings: {
      task_id: "pg-1",
      status: "ok",
      claims: [{ kind: "fact", text: "Two trim POs are 9 days late.", confidence: "high", evidence_id: null, json_pointer: null, calc_id: null }],
      affected: [],
      gaps: ["No supplier messages after 1 Oct."],
      scenario_updates: [],
    },
    error: null,
    ...over,
  };
}

/** AG-UI frames the playground streams (Translator shapes). */
export function playgroundFrames(): Json[] {
  const sc = (seq: number, label: string) => ({ sc: { tenantId: "demo-retail", caseId: `playground-${PLAYGROUND_ID}`, runId: `pg-${PLAYGROUND_ID}`, seq, stage: "checking_systems", prov: "fact", label, agent: "supplier_risk", evidenceRefs: [] } });
  return [
    { type: "SUBAGENT_STARTED", subagentRunId: "pg:supplier_risk", name: "supplier_risk", description: "Supplier risk analyst", timestamp: 1, metadata: sc(1, "Supplier risk analyst started") },
    { type: "TOOL_CALL_START", toolCallId: "fc-1", toolCallName: "list_purchase_orders", subagentRunId: "pg:supplier_risk", timestamp: 2, metadata: sc(2, "Checking open purchase orders in Merxa") },
    { type: "TOOL_CALL_RESULT", messageId: "fc-1:result", toolCallId: "fc-1", content: "Read 12 purchase orders", role: "tool", subagentRunId: "pg:supplier_risk", timestamp: 3, metadata: sc(3, "Read 12 purchase orders from Merxa") },
    { type: "CUSTOM", name: "sc.playground.result", value: playgroundResult(), timestamp: 4, metadata: sc(4, "Playground finished") },
  ];
}
