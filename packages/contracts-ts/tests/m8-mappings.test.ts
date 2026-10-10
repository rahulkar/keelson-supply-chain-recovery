import { describe, expect, it } from "vitest";
import {
  CONFIDENCE_BP,
  MAPPING_AGGREGATE_FUNCTIONS,
  MAPPING_EXPRESSION_FUNCTIONS,
  PREVIEW_SOURCES,
  PROPOSAL_NOTE_CODES,
  PROPOSAL_STATUSES,
  PROPOSE_MODES,
  PROPOSERS,
  confidencePercent,
  confidenceTone,
  mappingPlanSchema,
  mappingPreviewRequestSchema,
  mappingPreviewSchema,
  mappingProposalLlmSchema,
  mappingProposalSchema,
  mappingProposeRequestSchema,
  mappingReadSchema,
} from "../src";

const SRC = {
  tool: "list_items", args: { type: "store" }, page_param: "page", page_size_param: "page_size", page_size: 200,
  items_path: "$.items", cursor_path: null, max_pages: 50,
};

const proposal = {
  id: "0199a0b2-0000-7000-8000-000000000001", connector: "merxa-lite", status: "ready", proposer: "llm+heuristic",
  model_alias: "fast-default", created_by: "nadia", created_at: "2026-10-08T10:00:00Z", inputs_hash: "a".repeat(64),
  base_versions: { read: null, action: null },
  profiles: [{ ref: "S1", source: SRC, variant: "store", items_sampled: 3, total_hint: 3, paths: [
    { path: "$.code", shape: "code", types: [{ type: "str", count: 3 }], null_count: 0, distinct_count: 3, unique: true,
      max_len: 8, examples: ["S-DE-001"] },
  ] }],
  read: { entities: [{
    entity: "location", source_ref: "S1", source: SRC, group_by: ["$.cluster_code"], fields: [
      { column: "code", expression: "$.cluster_code", confidence_bp: 3000, origin: "heuristic", rationale: "r" },
      { column: "parent_code", expression: "first($.home_dc_code)", confidence_bp: 3000, origin: "both", rationale: "r",
        alternatives: [{ expression: "max($.home_dc_code)", origin: "llm", confidence_bp: 2000 }] },
    ], missing_required: ["source_id"], confidence_bp: 2000, origin: "both", rationale: "derived clusters",
  }], lookups: [] },
  action: null, unmapped_entities: ["sailing"], unmapped_ops: [], accepted: null, llm_calls: 1,
  notes: [{ code: "generic_update_tool", severity: "warning", where: "allocation.set_qty", message: "check" }],
};

describe("M8 mapping compiler + proposals", () => {
  it("mirrors the expression and aggregate tables of contracts-py", () => {
    expect(MAPPING_EXPRESSION_FUNCTIONS.field).toEqual([3, 3]);
    expect(MAPPING_EXPRESSION_FUNCTIONS.cents).toEqual([1, 1]);
    expect(Object.keys(MAPPING_AGGREGATE_FUNCTIONS).sort()).toEqual(["count", "first", "max", "min", "sum"]);
    expect(MAPPING_AGGREGATE_FUNCTIONS.count).toEqual([0, 0]);
    expect(PREVIEW_SOURCES).toEqual(["live", "staged"]);
    expect(PROPOSAL_STATUSES).toEqual(["ready", "accepted", "discarded"]);
    expect(PROPOSERS).toEqual(["heuristic", "llm+heuristic"]);
    expect(PROPOSE_MODES).toEqual(["auto", "heuristic"]);
    expect(PROPOSAL_NOTE_CODES).toContain("injection_suspected");
    expect(CONFIDENCE_BP).toEqual({ high: 8500, medium: 6000, low: 3000 });
  });

  it("shows confidence as text and tone, never colour alone", () => {
    expect([confidenceTone(9500), confidenceTone(7500), confidenceTone(6000), confidenceTone(2999)])
      .toEqual(["high", "high", "medium", "low"]);
    expect([confidencePercent(8599), confidencePercent(0), confidencePercent(12_000)]).toEqual(["85%", "0%", "100%"]);
  });

  it("parses proposals, plans, previews and requests", () => {
    const p = mappingProposalSchema.parse(proposal);
    expect(p.read?.entities[0]?.fields[1]?.alternatives?.[0]?.origin).toBe("llm");
    expect(() => mappingProposalSchema.parse({ ...proposal, status: "active" })).toThrow();
    expect(mappingProposeRequestSchema.parse({ kinds: ["read"] })).toMatchObject({ proposer: "auto", sample: true });
    expect(() => mappingProposeRequestSchema.parse({ kinds: [] })).toThrow();
    expect(mappingPreviewRequestSchema.parse({})).toMatchObject({ source: "live", max_pages: 1, sample: 5 });
    expect(() => mappingPreviewRequestSchema.parse({ max_pages: 4 })).toThrow();
    const preview = mappingPreviewSchema.parse({
      ok: false, error: null, source: "staged",
      staged: { snapshot_id: "s1", status: "failed", fetched_from: null, fetched_to: null },
      entities: [{ entity: "item", where: "entities[0] (item)", rows: 1, sample: [], error: "mapping_failed: x",
        columns: [{ column: "code", null_bp: 0, distinct: 1, failures: 2, first_failure: "item 3: x" }] }],
      checks: [{ code: "fk", label: "po_line.sku_code → item(sku)", entity: "po_line", severity: "error", violations: 1,
        examples: ["X"] }],
    });
    expect(preview.engine).toBe("duckdb");
    const plan = mappingPlanSchema.parse({
      mapping_hash: "b".repeat(64), sources: [{ key: "0123456789abcdef", source: SRC, paths: ["$.code"], used_by: ["x"] }],
      lookups: [], entities: [{ where: "entities[0] (location)", entity: "location", source_key: "0123456789abcdef",
        dedupe: false, group_by: null, joins: [], columns: [{ column: "code", expression: "$.code", aggregate: null,
          target_type: "text", required: true, sql: "SELECT 1" }], sql: "SELECT 1" }],
      checks: [], warnings: [],
    });
    expect(plan.engine).toBe("duckdb");
    expect(mappingProposalLlmSchema.parse({ entities: [], lookups: [], ops: [], injection_suspected: true })
      .injection_suspected).toBe(true);
  });

  it("accepts M8 read mappings (record lookups, group_by)", () => {
    const read = mappingReadSchema.parse({
      entities: [{ entity: "location", source: SRC, group_by: ["$.cluster_code"], fields: { code: "$.cluster_code" } }],
      lookups: { style: { kind: "record", source: SRC, key: "$.code" } },
    });
    expect(read.lookups?.style?.kind).toBe("record");
  });
});
