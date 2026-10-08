import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  BLOCK_KEYS,
  generated,
  knownBlockEnvelopeSchema,
  parseBlockProps,
  toBlockEnvelope,
} from "../src";
import { generate, sameText } from "../scripts/gen-zod.mjs";

const pkgRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

const kpiProps = {
  items: [
    { key: "units_at_risk", label: "Units at risk", value: 3200, unit: "units", delta: -400, prov: "simulated", preliminary: true },
    { key: "revenue_at_risk", label: "Revenue at risk", value: 1240000, unit: "eur_cents", prov: "inference", preliminary: false },
  ],
};

const envelope = (props: unknown, over: Record<string, unknown> = {}) => ({
  id: "blk_a1",
  kind: "kpi_strip",
  v: 1,
  title: "Exposure",
  placement: "inline",
  provenance: { kind: "simulated", engine: "keelson-opt@0.1", inputs_hash: null, as_of: "2026-10-05" },
  data_ref: null,
  props,
  ...over,
});

describe("generated Zod", () => {
  it("is up to date with schemas/*.schema.json", () => {
    const files = generate();
    const index = JSON.parse(fs.readFileSync(path.join(pkgRoot, "schemas/index.json"), "utf8"));
    expect(files.size).toBe(index.schemas.length + 1);
    for (const [rel, contents] of files) {
      const committed = fs.readFileSync(path.join(pkgRoot, "src/generated", rel), "utf8");
      expect(committed.replace(/\r\n/g, "\n"), `${rel} is stale — run the generator`).toBe(contents);
    }
  });

  it("treats CRLF checkouts of the generated files as up to date", () => {
    expect(sameText("x\r\ny\r\n", "x\ny\n")).toBe(true);
    expect(sameText("x\n", "y\n")).toBe(false);
    expect(sameText(null, "x\n")).toBe(false);
  });

  it("exports one schema per index entry", () => {
    const names = Object.keys(generated).filter((k) => k.endsWith("Schema"));
    expect(names).toHaveLength(85); // M6: +alert_origin props, +llm.news-extraction, +10 signals.*; M7: +16 back-office schemas; M8: +10 evals.*
  });
});

describe("block props", () => {
  it("covers the 8 M1, 4 M3, 1 M5 and 1 M6 block kinds", () => {
    expect(BLOCK_KEYS.sort()).toEqual(
      [
        "approach_card@1",
        "approval_card@1",
        "commit_tracker@1",
        "evidence_pack@1",
        "kpi_strip@1",
        "narrative@1",
        "plan_diff@1",
        "recommended_option@1",
        "network_map@1",
        "impact_graph@1",
        "stockout_timeline@1",
        "scenario_compare@1",
        "remediation_checklist@1",
        "alert_origin@1",
      ].sort(),
    );
  });

  it("parses valid props and fills nullable defaults", () => {
    const result = parseBlockProps(envelope(kpiProps));
    expect(result.ok).toBe(true);
    if (result.ok && result.key === "kpi_strip@1") {
      const items = (result.props as { items: { delta: number | null }[] }).items;
      expect(items[1]?.delta).toBeNull();
    }
  });

  it.each([
    ["float KPI value", { items: [{ ...kpiProps.items[0], value: 12.5 }] }],
    ["unknown unit", { items: [{ ...kpiProps.items[0], unit: "kg" }] }],
    ["empty items", { items: [] }],
    ["unknown key", { ...kpiProps, extra: 1 }],
    ["missing preliminary", { items: [{ key: "a", label: "A", value: 1, unit: "units", prov: "fact" }] }],
  ])("rejects %s", (_name, props) => {
    const result = parseBlockProps(envelope(props));
    expect(result.ok).toBe(false);
    if (!result.ok) {
      expect(result.reason).toBe("invalid_props");
      expect(result.issues.length).toBeGreaterThan(0);
    }
  });

  it("reports unknown kinds", () => {
    const result = parseBlockProps(envelope({}, { kind: "agent_trace_graph" }));
    expect(result).toMatchObject({ ok: false, reason: "unknown_kind", key: "agent_trace_graph@1" });
  });
});

describe("envelopes", () => {
  it("toBlockEnvelope is lenient about props/provenance but needs id/kind/v", () => {
    expect(toBlockEnvelope(envelope({ anything: true }, { provenance: "bogus" }))).toMatchObject({
      id: "blk_a1",
      provenance: null,
    });
    expect(toBlockEnvelope({ title: "x" })).toBeNull();
    expect(toBlockEnvelope(envelope(kpiProps, { placement: "sidebar" }))?.placement).toBe("inline");
  });

  it("knownBlockEnvelopeSchema is strict", () => {
    expect(knownBlockEnvelopeSchema.safeParse(envelope(kpiProps)).success).toBe(true);
    expect(knownBlockEnvelopeSchema.safeParse(envelope(kpiProps, { kind: "mystery" })).success).toBe(false);
    expect(knownBlockEnvelopeSchema.safeParse(envelope(kpiProps, { id: "kpi1" })).success).toBe(false);
  });
});
