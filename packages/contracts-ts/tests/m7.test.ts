import { describe, expect, it } from "vitest";
import {
  CHANGE_REQUEST_KINDS,
  CHANGE_REQUEST_STATUSES,
  CHANGE_REQUEST_TTL_MS,
  COMMIT_LEDGER_STATES,
  COMMIT_MODES,
  CRAWLED_ADVISORY_TEXT,
  GUARD_REASON_CODES,
  MAPPING_EXPRESSION_FUNCTIONS,
  PRODUCT_ROLES,
  SNAPSHOT_CONSISTENCIES,
  adminChangeRequestDecisionSchema,
  adminChangeRequestSchema,
  adminUserCreateSchema,
  adminUserCreatedSchema,
  adminUserPatchSchema,
  agentsCustomSpecialistSchema,
  agentsPlaygroundRequestSchema,
  agentsPlaygroundResultSchema,
  canApproveChangeRequest,
  changeRequestExpired,
  commitLedgerEntrySchema,
  connectorsCapabilitiesSchema,
  isProModel,
  mappingActionSchema,
  mappingDryRunResultSchema,
  mappingReadSchema,
  narrativePropsSchema,
  parseBlockProps,
  scnSnapshotLabelSchema,
  type BlockAdvisory,
} from "../src";

// Fixtures mirror packages/contracts-py/tests/test_m7.py.
const UUID_A = "0199c1a2-7b3c-7d4e-8f50-0123456789ab";
const KB_1 = "0199c1a2-7b3c-7d4e-8f50-00000000000b";
const T0 = "2026-10-08T09:00:00Z";
const EXPIRES = "2026-10-11T09:00:00Z";

const changeRequest = {
  id: UUID_A,
  tenant: "northwind-test",
  kind: "agent_spec",
  target: "agent_spec",
  payload: { base_version: "v3", spec_patch: { policy: { auto_max_lines: 20 } } },
  base_version: "v3",
  diff: [{ op: "replace", path: "/policy/auto_max_lines", before: 5, after: 20 }],
  reasons: [
    { code: "auto_cap_raised", field: "policy.auto_max_lines", reason: "raises the automatic-approval caps" },
  ],
  requested_by: { sub: "sub-tina", username: "tina", name: "Tina Admin" },
  requested_at: T0,
  status: "pending",
  approver: null,
  decided_at: null,
  decision_reason: null,
  expires_at: EXPIRES,
};

const src = (type: string) => ({
  tool: "list_items",
  args: { type },
  page_param: "page",
  page_size_param: "page_size",
  page_size: 200,
  items_path: "$.items",
  cursor_path: null,
  max_pages: 50,
});

const advisory: BlockAdvisory = {
  reason: "crawled_snapshot",
  text: CRAWLED_ADVISORY_TEXT,
  as_of_range: { from: "2026-10-08T09:00:00Z", to: "2026-10-08T09:02:00Z" },
};

describe("M7 vocabularies", () => {
  it("derives the enums from the generated schemas", () => {
    expect(PRODUCT_ROLES).toEqual(["planner", "approver", "compliance", "tenant-admin", "platform-admin", "demo-presenter"]);
    expect(CHANGE_REQUEST_STATUSES).toEqual(["pending", "approved", "rejected", "applied", "expired"]);
    expect(CHANGE_REQUEST_KINDS).toContain("connector_delete");
    expect(GUARD_REASON_CODES).toContain("tool_reclassified_read");
    expect(COMMIT_LEDGER_STATES).toEqual(["attempted", "succeeded", "in_doubt", "failed", "verified"]);
    expect(SNAPSHOT_CONSISTENCIES).toEqual(["consistent", "crawled"]);
    expect(COMMIT_MODES).toEqual(["change_set", "per_op", "guide"]);
    expect(Object.keys(MAPPING_EXPRESSION_FUNCTIONS).sort()).toEqual(
      ["coalesce", "date", "if_present", "int", "lookup", "str"],
    );
    expect(CRAWLED_ADVISORY_TEXT).toBe("Data gathered by paging, not a consistent snapshot");
  });
});

describe("four-eyes change requests", () => {
  it("parses a pending request and applies the UI rules", () => {
    const cr = adminChangeRequestSchema.parse(changeRequest);
    expect(Date.parse(cr.expires_at) - Date.parse(cr.requested_at)).toBe(CHANGE_REQUEST_TTL_MS);
    const now = new Date("2026-10-08T10:00:00Z");
    expect(canApproveChangeRequest(cr, "sub-omar", now)).toBe(true);
    expect(canApproveChangeRequest(cr, "sub-tina", now)).toBe(false); // self-approval
    expect(changeRequestExpired(cr, new Date(EXPIRES))).toBe(true);
    expect(canApproveChangeRequest(cr, "sub-omar", new Date(EXPIRES))).toBe(false);
    expect(adminChangeRequestDecisionSchema.parse({})).toEqual({ reason: null });
  });

  it("rejects unknown kinds, reason codes and bad pointers", () => {
    expect(adminChangeRequestSchema.safeParse({ ...changeRequest, kind: "delete_everything" }).success).toBe(false);
    expect(
      adminChangeRequestSchema.safeParse({
        ...changeRequest,
        reasons: [{ code: "because", field: "x", reason: "y" }],
      }).success,
    ).toBe(false);
    expect(
      adminChangeRequestSchema.safeParse({ ...changeRequest, diff: [{ op: "replace", path: "a/b", before: 1, after: 2 }] })
        .success,
    ).toBe(false);
    expect(adminChangeRequestSchema.safeParse({ ...changeRequest, extra: 1 }).success).toBe(false);
  });
});

describe("users", () => {
  it("creates users with product roles only and never carries a password", () => {
    const body = { username: "nora", email: "nora@northwind.test", name: "Nora Planner", roles: ["planner"] };
    expect(adminUserCreateSchema.parse(body).roles).toEqual(["planner"]);
    expect(adminUserCreateSchema.safeParse({ ...body, roles: ["merxa-admin"] }).success).toBe(false);
    expect(adminUserCreateSchema.safeParse({ ...body, roles: [] }).success).toBe(false);
    expect(adminUserCreateSchema.safeParse({ ...body, password: "x" }).success).toBe(false);
    const created = {
      user: {
        id: UUID_A, username: "nora", email: null, name: "Nora", enabled: true, roles: ["planner"],
        tenants: ["northwind-test"], created_at: null,
      },
      setup: {
        required_action: "UPDATE_PASSWORD",
        link: "https://auth.localhost/realms/keelson/login-actions/action-token?key=abc",
        link_expires_at: T0,
        instructions: "Open the link once to set a password.",
      },
    };
    expect(adminUserCreatedSchema.parse(created).setup.required_action).toBe("UPDATE_PASSWORD");
    for (const link of ["javascript:alert(1)", "https://evil@auth.localhost/x", "//auth.localhost/x"]) {
      expect(adminUserCreatedSchema.safeParse({ ...created, setup: { ...created.setup, link } }).success).toBe(false);
    }
    expect(adminUserPatchSchema.parse({ enabled: false })).toEqual({ roles: null, enabled: false });
  });
});

describe("custom specialists + playground", () => {
  const spec = {
    slug: "supplier_risk",
    display_name: "Supplier risk analyst",
    description: "Checks open purchase orders and supplier delivery history in the foreign ERP.",
    role_prompt: "You review supplier reliability.\nCite every number.",
    model_alias: "fast-default",
    tools: [{ connector: "merxa-lite", tool: "list_items" }],
    kb_ids: [KB_1],
  };

  it("applies defaults and pattern checks", () => {
    const parsed = agentsCustomSpecialistSchema.parse(spec);
    expect([parsed.max_tool_calls, parsed.max_turns, parsed.enabled]).toEqual([8, 6, true]);
    expect(agentsCustomSpecialistSchema.safeParse({ ...spec, slug: "Supplier-Risk" }).success).toBe(false);
    expect(agentsCustomSpecialistSchema.safeParse({ ...spec, role_prompt: "x".repeat(4001) }).success).toBe(false);
    expect(agentsCustomSpecialistSchema.safeParse({ ...spec, max_tool_calls: 16 }).success).toBe(false);
    expect(agentsCustomSpecialistSchema.safeParse({ ...spec, model_alias: "gemini/flash" }).success).toBe(false);
  });

  it("flags Pro models", () => {
    expect(isProModel("gemini-2.5-pro")).toBe(true);
    expect(isProModel("vertex_ai/gemini-pro")).toBe(true);
    expect(isProModel("gemini-2.5-flash")).toBe(false);
    expect(isProModel("prod-fast")).toBe(false);
  });

  it("parses playground requests and results", () => {
    expect(agentsPlaygroundRequestSchema.parse({ specialist: "supplier_risk", question: "Late POs?" }).specialist).toBe(
      "supplier_risk",
    );
    expect(agentsPlaygroundRequestSchema.parse({ specialist: spec, question: "Late POs?" }).question).toBe("Late POs?");
    const result = agentsPlaygroundResultSchema.parse({
      playground_id: UUID_A,
      specialist: "supplier_risk",
      display_name: "Supplier risk analyst",
      status: "completed",
      findings: { task_id: "t1", status: "ok", claims: [], affected: [], gaps: [], scenario_updates: [] },
      tool_calls: [
        { connector: "merxa-lite", tool: "list_items", ok: true, duration_ms: 41, quarantined: false,
          evidence_id: "ev_abc", error: null },
      ],
      usage: { llm_calls: 2, tool_calls: 1, tokens_in: 900, tokens_out: 120 },
      error: null,
      started_at: T0,
      duration_ms: 1200,
    });
    expect(result.committed).toBe(false);
  });
});

describe("tier-A mappings", () => {
  it("parses a read mapping with lookups", () => {
    const mapping = mappingReadSchema.parse({
      lookups: {
        po_supplier: { kind: "source", source: src("purchase_order"), key: "$.code", value: "$.supplier_code" },
        currency: { kind: "static", values: { default: "EUR" } },
      },
      entities: [
        {
          entity: "location",
          source: src("store"),
          dedupe: true,
          fields: { source_id: "'cluster:' + $.cluster_code", kind: "'cluster'", code: "$.cluster_code", src_version: "0" },
        },
      ],
    });
    expect(mapping.kind).toBe("read");
    const first = mapping.entities[0]!;
    expect(first.source.first_page).toBe(1);
    expect(mappingReadSchema.safeParse({ entities: [{ ...first, entity: "sku" }] }).success).toBe(false);
  });

  it("parses an action mapping and a dry-run result", () => {
    const action = mappingActionSchema.parse({
      ops: [
        {
          op: "allocation.set_qty",
          tool: "update_item",
          args: { type: "allocation", key: "{{target_ref}}", fields_json: { $json: { qty: "{{payload.qty}}" } },
                  expected_version: "{{expected_version}}" },
          idempotency: "enforced",
          request_id_arg: "request_id",
          readback: { tool: "get_item", args: { type: "allocation", key: "{{target_ref}}" }, items_path: "$.item",
                      match: { "$.qty": "{{payload.qty}}" } },
        },
      ],
    });
    expect(action.ops[0]!.idempotency).toBe("enforced");
    const dry = mappingDryRunResultSchema.parse({
      op: "po.create", mapped: false, call: null, idempotency: null, request_id_arg: null, readback: null,
      guided_reason: "No action mapping for po.create: a guided step is generated.",
    });
    expect(dry.executed).toBe(false);
    expect(mappingDryRunResultSchema.safeParse({ ...dry, executed: true }).success).toBe(false);
  });
});

describe("snapshot consistency, advisory blocks, ledger, capabilities", () => {
  it("labels crawled snapshots", () => {
    const label = scnSnapshotLabelSchema.parse({ consistency: "crawled", as_of_range: advisory.as_of_range });
    expect(label.as_of_range?.from).toBe("2026-10-08T09:00:00Z");
    expect(scnSnapshotLabelSchema.safeParse({ consistency: "partial", as_of_range: null }).success).toBe(false);
  });

  it("carries the advisory label on plan blocks (absent = null, older blocks unchanged)", () => {
    expect(parseBlockProps({ kind: "narrative", v: 1, props: { markdown: "Plan", advisory } }).ok).toBe(true);
    expect(narrativePropsSchema.parse({ markdown: "Plan", advisory }).advisory?.text).toBe(CRAWLED_ADVISORY_TEXT);
    expect(narrativePropsSchema.parse({ markdown: "Plan" }).advisory).toBeNull();
    const bad = parseBlockProps({ kind: "narrative", v: 1, props: { markdown: "Plan", advisory: { ...advisory, reason: "x" } } });
    expect(bad.ok).toBe(false);
  });

  it("parses ledger entries and capability summaries", () => {
    const entry = commitLedgerEntrySchema.parse({
      change_set_id: "cs-1", line_seq: 1, op: "allocation.set_qty", connector: "merxa-lite", tool: "update_item",
      idempotency: "enforced", state: "in_doubt", attempts: 1, request_id: "rid-1", updated_at: T0,
    });
    expect(entry.readback).toBeNull();
    expect(commitLedgerEntrySchema.safeParse({ ...entry, state: "unknown" }).success).toBe(false);
    const caps = connectorsCapabilitiesSchema.parse({
      connector: "merxa-lite", tiers: ["A"], sync: "crawl", consistency: "crawled", commit: "per_op",
      read_mapping_version: 2, action_mapping_version: 1, mapped_ops: ["allocation.set_qty", "transfer_order.create"],
      advisory: true,
    });
    expect(caps.advisory).toBe(true);
  });
});
