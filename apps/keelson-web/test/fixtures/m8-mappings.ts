/**
 * M8 mapping compiler + proposals fixtures (engine shapes of `routes_admin_mappings.py`, validated
 * against the generated contracts in `m8-mappings.test.tsx`).
 */

type Json = Record<string, unknown>;

export const PROPOSAL_ID = "0199b2c4-1a2b-7c3d-8e4f-000000000001";
export const OLDER_PROPOSAL_ID = "0199b2c4-1a2b-7c3d-8e4f-000000000002";
export const CR_MAPPING = "0199b2c4-1a2b-7c3d-8e4f-0000000000c1";
export const XSS = '<img src=x onerror="alert(1)">';

const SRC_STORE = {
  tool: "list_items", args: { type: "store" }, page_param: "page", page_size_param: "page_size", page_size: 200,
  items_path: "$.items", cursor_path: null, max_pages: 10, first_page: 1,
};
const SRC_ALLOC = { ...SRC_STORE, args: { type: "allocation" } };
const SRC_STYLE = { ...SRC_STORE, args: { type: "style" } };

function field(column: string, expression: string, confidence_bp: number, origin = "heuristic", rationale = `name matches ${column}`): Json {
  return { column, expression, confidence_bp, origin, rationale, alternatives: [] };
}

/** A rule-based proposal (the AI was unavailable): a complete store mapping, an allocation mapping missing `qty`, one generic op. */
export function proposal(over: Json = {}): Json {
  return {
    id: PROPOSAL_ID,
    connector: "merxa-lite",
    status: "ready",
    proposer: "heuristic",
    model_alias: null,
    created_by: "sam",
    created_at: "2026-10-08T09:30:00Z",
    inputs_hash: "a".repeat(64),
    base_versions: { read: null, action: null },
    profiles: [
      { ref: "S1", source: SRC_STORE, variant: "store", items_sampled: 3, total_hint: 3, paths: [
        { path: "$.code", shape: "code", date_format: null, types: [{ type: "str", count: 3 }], null_count: 0, distinct_count: 3, unique: true, max_len: 8, examples: ["S-DE-001"] },
      ] },
      { ref: "S2", source: SRC_ALLOC, variant: "allocation", items_sampled: 2, total_hint: 2, paths: [] },
    ],
    read: {
      entities: [
        {
          entity: "location", source_ref: "S1", source: SRC_STORE, group_by: null, dedupe: false,
          fields: [
            field("source_id", "'store:' + $.code", 7000, "heuristic", "'store:' + code: the kind-prefixed code"),
            field("kind", "'store'", 9000, "both", "the store records of list_items(store)"),
            field("code", "$.code", 8000),
            { ...field("name", "$.name", 6000, "llm", XSS), alternatives: [{ expression: "upper($.name)", origin: "heuristic", confidence_bp: 3000 }] },
            field("src_version", "$.version", 8000),
          ],
          missing_required: [], confidence_bp: 7600, origin: "both", rationale: "list_items(store) holds store records",
        },
        {
          entity: "allocation", source_ref: "S2", source: SRC_ALLOC, group_by: null, dedupe: false,
          fields: [
            field("source_id", "$.code", 7000),
            field("code", "$.code", 8000),
            field("sku_code", "$.sku_code", 8000),
            field("dc_code", "$.dc_code", 8000),
            field("ship_week", "$.ship_week", 8000),
            field("status", "$.status", 8000),
            field("src_version", "$.version", 8000),
            field("store_code", "field('style', $.store_code, '$.x')", 3000),
          ],
          missing_required: ["qty"], confidence_bp: 2000, origin: "heuristic", rationale: "list_items(allocation) holds allocation records",
        },
      ],
      lookups: [
        { name: "style", table: { kind: "record", source: SRC_STYLE, key: "$.code" }, source_ref: null, confidence_bp: 7000, origin: "heuristic", rationale: "keyed by code" },
      ],
    },
    action: {
      ops: [
        {
          mapping: {
            op: "allocation.set_qty", tool: "update_item",
            args: { type: "allocation", key: "{{target_ref}}", fields_json: { $json: { qty: "{{payload.qty}}" } }, expected_version: "{{expected_version}}" },
            idempotency: "best_effort", request_id_arg: "request_id",
            readback: { tool: "get_item", args: { type: "allocation", key: "{{target_ref}}" }, items_path: "$.item", match: { "$.qty": "{{payload.qty}}" } },
          },
          confidence_bp: 3000, origin: "heuristic", rationale: "generic update of the allocation record", generic: true,
        },
      ],
    },
    unmapped_entities: ["sailing"],
    unmapped_ops: ["po.create"],
    notes: [
      { code: "llm_unavailable", severity: "warning", where: null, message: "no AI model is configured for this tenant: rule-based suggestions only" },
      { code: "generic_update_tool", severity: "warning", where: "allocation.set_qty", message: "allocation.set_qty uses the generic update_item" },
      { code: "sample_value_withheld", severity: "info", where: null, message: "free-text values are summarised as their shape" },
    ],
    accepted: null,
    llm_calls: 0,
    ...over,
  };
}

export function proposalSummary(over: Json = {}): Json {
  return {
    id: OLDER_PROPOSAL_ID, connector: "merxa-lite", status: "discarded", proposer: "llm+heuristic", model_alias: "fast-default",
    kinds: ["read"], created_by: "sam", created_at: "2026-10-07T09:00:00+00:00", entities: 4, complete_entities: 3, lookups: 1, ops: 0,
    notes: 2, accepted: null, ...over,
  };
}

export function proposerStatus(over: Json = {}): Json {
  return {
    mode: "auto", enabled: true, alias: "fast-default",
    llm: { available: false, reason: "no_key", message: "No AI model is configured for this tenant: rule-based suggestions only." },
    ...over,
  };
}

export function plan(): Json {
  const key = "0123456789abcdef";
  return {
    mapping_hash: "b".repeat(64),
    engine: "duckdb",
    sources: [
      { key, source: SRC_STORE, paths: ["$.code", "$.name", "$.cluster_code"], used_by: ["entities[0] (location)", "entities[1] (location)"] },
      { key: "fedcba9876543210", source: SRC_STYLE, paths: ["$.code", "$.family"], used_by: ["lookups.style"] },
    ],
    lookups: [{ name: "style", kind: "record", source_key: "fedcba9876543210", entries: null, key: "$.code", value: null, paths: ["$.family"] }],
    entities: [
      {
        where: "entities[0] (location)", entity: "location", source_key: key, dedupe: false, group_by: null, joins: [],
        columns: [{ column: "code", expression: "$.code", aggregate: null, target_type: "text", required: true, sql: "src.v0" }],
        sql: "WITH s0 AS (SELECT ord, t0, v0 FROM src_0123456789abcdef) SELECT 1",
      },
      {
        where: "entities[1] (location)", entity: "location", source_key: key, dedupe: false, group_by: ["$.cluster_code"],
        joins: [{ table: "style", kind: "record", calls: 1 }],
        columns: [{ column: "parent_code", expression: "first($.home_dc_code)", aggregate: "first", target_type: "text", required: false, sql: "arg_min" }],
        sql: "SELECT arg_min(v1, ord) FROM ev GROUP BY k0",
      },
    ],
    checks: [
      { code: "duplicate_source_id", label: "location: unique source_id", entity: "location", severity: "error" },
      { code: "fk", label: "location.parent_code → location", entity: "location", severity: "warning" },
    ],
    warnings: [],
  };
}

export function stagedSet(over: Json = {}): Json {
  return {
    connector: "merxa-lite", snapshot_id: "snap-2", status: "complete", mapping_version: 2, mapping_hash: "c".repeat(64),
    sources: [{ key: "0123456789abcdef", tool: "list_items", pages: 1, items: 3, truncated: false }],
    pages: 9, items: 25, bytes: 9000, fetched_from: "2026-10-08T09:12:00+00:00", fetched_to: "2026-10-08T09:19:00+00:00", ...over,
  };
}

/** A staged preview with column statistics and a failing FK check (examples). */
export function stagedPreview(): Json {
  return {
    ok: false, error: null, engine: "duckdb", source: "staged",
    staged: { snapshot_id: "snap-2", status: "complete", fetched_from: "2026-10-08T09:12:00+00:00", fetched_to: "2026-10-08T09:19:00+00:00" },
    capped: false, pages: 9, rows_seen: 25, truncated: [], lookups: {},
    entities: [
      { entity: "location", where: "entities[0] (location)", rows: 3, failed: 1, skipped: 0, sample: [{ code: "S-DE-001" }], error: null, columns: [
        { column: "code", null_bp: 0, distinct: 3, failures: 0, first_failure: null },
        { column: "src_version", null_bp: 3333, distinct: 2, failures: 1, first_failure: "item 2: int(): not an integer: 'x'" },
      ] },
    ],
    checks: [{ code: "fk", label: "inventory_position.location → location", entity: "inventory_position", severity: "error", violations: 2, examples: ["dc:DE1", "dc:NL1"] }],
  };
}

export function mappingChangeRequest(over: Json = {}): Json {
  return {
    id: CR_MAPPING, tenant: "demo-retail", kind: "connector_mapping", target: "connector:merxa-lite/mapping:action",
    payload: { connector: "merxa-lite", kind: "action", proposal_id: PROPOSAL_ID }, base_version: "1",
    diff: [{ op: "replace", path: "/ops", before: [], after: [] }],
    reasons: [{ code: "ai_proposed_action_mapping", field: "connector:merxa-lite/mapping:action", reason: "an action mapping proposed by the mapping proposer" }],
    requested_by: { sub: "6f0d3a2e-0000-4000-8000-00000000000a", username: "sam", name: "Sam Okafor" },
    requested_at: "2026-10-08T10:00:00Z", status: "pending", approver: null, decided_at: null, decision_reason: null,
    expires_at: "2026-10-11T10:00:00Z", ...over,
  };
}
