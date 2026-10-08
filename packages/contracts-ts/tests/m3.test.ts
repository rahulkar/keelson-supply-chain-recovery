import { describe, expect, it } from "vitest";
import {
  BLOCK_KEYS,
  CANVAS_ONLY_KINDS,
  blockDataSchemas,
  fromE6,
  knownBlockEnvelopeSchema,
  networkMapPropsSchema,
  parseBlockData,
  parseBlockProps,
  scenarioComparePropsSchema,
  stockoutTimelineDataSchema,
} from "../src";
import { legacyTuples } from "../scripts/gen-zod.mjs";

// Fixtures mirror packages/contracts-py/tests/examples.py (GOOD_PROPS / GOOD_DATA).
const scorecard = {
  fill_rate_bp: 9650,
  lost_units: 420,
  lost_margin_cents: 840_000,
  incremental_cost_cents: 183_000,
  cost_breakdown: { transfer: 63_000, expedite_premium: 120_000, nearshore_premium: 0 },
  net_benefit_cents: 1_517_000,
  shortage_days: 12,
  first_breach_shift_days: 7,
  tts_gain_days: 5,
  changes: 6,
};

const props: Record<string, unknown> = {
  "network_map@1": {
    title: "Inbound network",
    summary: { nodes: 3, at_risk_nodes: 1, lanes: 2 },
    bbox: [-10_000_000, 20_000_000, 95_000_000, 60_000_000],
    legend: [{ key: "at_risk", label: "At risk", kind: "node" }],
  },
  "impact_graph@1": { summary: { nodes: 4, disrupted: 1, impacted: 1 } },
  "stockout_timeline@1": {
    weeks: ["2026-W44", "2026-W45", "2026-W46"],
    options: [{ id: "opt-0", title: "Do nothing" }],
  },
  "scenario_compare@1": {
    options: [
      {
        id: "opt-B",
        title: "Rebalance + targeted sea-air",
        levers: ["reallocate_inventory", "expedite_mode_shift"],
        scorecard,
        recommended: true,
        guided_only: true,
        guided_reason: "Merxa connector cannot apply capacity_booking.create",
        pareto: true,
      },
    ],
  },
};

const mapData = {
  nodes: [
    { id: "port:BDCGP", kind: "port", label: "Chittagong", lat_e6: 22_330_000, lon_e6: 91_800_000, status: "disrupted" },
    { id: "dc:UK1", kind: "dc", label: "UK1", lat_e6: 52_486_000, lon_e6: -1_890_000, status: "at_risk", units_at_risk: 3200 },
  ],
  links: [
    {
      id: "BDCGP-UK1",
      kind: "inbound",
      from: "port:BDCGP",
      to: "dc:UK1",
      path: [
        [91_800_000, 22_330_000],
        [-1_890_000, 52_486_000],
      ],
      status: "impacted",
    },
  ],
  footprint: null,
};

const graphData = {
  nodes: [
    { id: "factory:F-BD-01", kind: "factory", label: "Gazipur", status: "impacted", units_at_risk: 9000 },
    { id: "dc:UK1", kind: "dc", label: "UK1", status: "at_risk", units_at_risk: 3200, first_breach_week: "2026-W45" },
  ],
  edges: [{ source: "factory:F-BD-01", target: "dc:UK1", kind: "ships" }],
};

const timelineRow = (i: number) => ({
  id: `row-${i}`,
  dc_code: "UK1",
  cluster_code: "UK1-COLD-A",
  style_code: "PUF-W-BLK",
  label: "UK1 cold A",
  cells: [{ week: "2026-W46", status: "short", shortage_units: 340 }],
  first_breach_week: "2026-W46",
  by_option: { "opt-0": "2026-W46", "opt-A": null },
});

const env = (key: string, over: Record<string, unknown> = {}) => {
  const [kind = "", v = "1"] = key.split("@");
  return {
    id: `blk_${kind}`,
    kind,
    v: Number(v),
    title: "x",
    placement: "canvas",
    provenance: { kind: "simulated", engine: "keelson-opt@0.2", inputs_hash: null, as_of: "2026-10-05" },
    data_ref: `/api/blocks/blk_${kind}/data`,
    props: props[key],
    ...over,
  };
};

describe("M3 block props", () => {
  it.each(Object.keys(props))("%s parses and is a known key", (key) => {
    expect(BLOCK_KEYS).toContain(key);
    const result = parseBlockProps(env(key));
    expect(result.ok, JSON.stringify(result)).toBe(true);
    expect(knownBlockEnvelopeSchema.safeParse(env(key)).success).toBe(true);
  });

  it("scenario_compare fills default axes", () => {
    const parsed = scenarioComparePropsSchema.parse(props["scenario_compare@1"]);
    expect(parsed.options[0]?.scorecard.cost_breakdown.expedite_premium).toBe(120_000);
  });

  it.each([
    ["float bbox", { bbox: [-10.5, 20_000_000, 95_000_000, 60_000_000] }],
    ["bbox of 3", { bbox: [-10_000_000, 20_000_000, 95_000_000] }],
    ["bbox of 5", { bbox: [-10_000_000, 20_000_000, 95_000_000, 60_000_000, 1] }],
    ["latitude out of range", { bbox: [-10_000_000, -91_000_000, 95_000_000, 60_000_000] }],
    ["bad legend kind", { legend: [{ key: "a", label: "A", kind: "colour" }] }],
  ])("network_map props reject %s", (_name, mutation) => {
    const bad = { ...(props["network_map@1"] as object), ...mutation };
    expect(networkMapPropsSchema.safeParse(bad).success).toBe(false);
  });

  it("scenario_compare rejects float money and unknown axes", () => {
    const base = props["scenario_compare@1"] as { options: Record<string, unknown>[] };
    const floatMoney = { options: [{ ...base.options[0], scorecard: { ...scorecard, lost_margin_cents: 1.5 } }] };
    expect(scenarioComparePropsSchema.safeParse(floatMoney).success).toBe(false);
    expect(scenarioComparePropsSchema.safeParse({ ...base, axes: { x: "vibes", y: "lost_units" } }).success).toBe(false);
  });

  it("lists the M3 visuals as canvas-only", () => {
    expect(CANVAS_ONLY_KINDS).toEqual(
      expect.arrayContaining(["network_map", "impact_graph", "stockout_timeline", "scenario_compare"]),
    );
  });
});

describe("M3 data_ref payloads", () => {
  it("parses map, graph and timeline data", () => {
    expect(parseBlockData({ kind: "network_map", v: 1 }, mapData)).toMatchObject({ ok: true });
    expect(parseBlockData({ kind: "impact_graph", v: 1 }, graphData)).toMatchObject({ ok: true });
    expect(parseBlockData({ kind: "stockout_timeline", v: 1 }, { rows: [timelineRow(1)] })).toMatchObject({
      ok: true,
    });
    expect(Object.keys(blockDataSchemas).sort()).toEqual(["impact_graph@1", "network_map@1", "stockout_timeline@1"]);
  });

  it("keeps the wire name `from` and integer [lon_e6, lat_e6] tuples", () => {
    const result = parseBlockData({ kind: "network_map", v: 1 }, mapData);
    expect(result.ok).toBe(true);
    if (result.ok && result.key === "network_map@1") {
      const link = (result.data as { links: { from: string; path: [number, number][] }[] }).links[0];
      expect(link?.from).toBe("port:BDCGP");
      expect(link?.path[0]).toEqual([91_800_000, 22_330_000]);
      expect(fromE6(link?.path[0]?.[1] ?? 0)).toBeCloseTo(22.33, 6);
    }
  });

  it.each([
    ["float coordinate", { ...mapData, nodes: [{ ...mapData.nodes[0], lat_e6: 22.33 }] }],
    ["path point of 3", { ...mapData, links: [{ ...mapData.links[0], path: [[1, 2, 3], [1, 2]] }] }],
    ["single-point path", { ...mapData, links: [{ ...mapData.links[0], path: [[1, 2]] }] }],
    ["unknown link kind", { ...mapData, links: [{ ...mapData.links[0], kind: "teleport" }] }],
  ])("network_map data rejects %s", (_name, data) => {
    expect(parseBlockData({ kind: "network_map", v: 1 }, data)).toMatchObject({ ok: false, reason: "invalid_data" });
  });

  it("caps impact graphs at 300 nodes and timelines at 40 rows", () => {
    const node = (i: number) => ({ id: `n${i}`, kind: "dc", label: "x", status: "ok", units_at_risk: 0 });
    const ok = { nodes: Array.from({ length: 300 }, (_, i) => node(i)), edges: [] };
    const tooMany = { nodes: Array.from({ length: 301 }, (_, i) => node(i)), edges: [] };
    expect(parseBlockData({ kind: "impact_graph", v: 1 }, ok).ok).toBe(true);
    expect(parseBlockData({ kind: "impact_graph", v: 1 }, tooMany).ok).toBe(false);
    const rows = (n: number) => ({ rows: Array.from({ length: n }, (_, i) => timelineRow(i)) });
    expect(stockoutTimelineDataSchema.safeParse(rows(40)).success).toBe(true);
    expect(stockoutTimelineDataSchema.safeParse(rows(41)).success).toBe(false);
  });

  it("reports kinds without a data payload", () => {
    expect(parseBlockData({ kind: "kpi_strip", v: 1 }, {})).toMatchObject({ ok: false, reason: "no_data_schema" });
  });
});

describe("gen-zod tuple rewrite", () => {
  it("turns closed prefixItems into draft-07 tuples and leaves other nodes alone", () => {
    const schema = {
      type: "array",
      items: { type: "array", prefixItems: [{ type: "integer" }, { type: "integer" }], minItems: 2, maxItems: 2 },
    };
    expect(legacyTuples(schema)).toEqual({
      type: "array",
      items: { type: "array", items: [{ type: "integer" }, { type: "integer" }], minItems: 2, maxItems: 2 },
    });
    expect(legacyTuples({ type: "string" })).toEqual({ type: "string" });
  });

  it("refuses open tuples it cannot express", () => {
    expect(() => legacyTuples({ type: "array", prefixItems: [{ type: "integer" }], items: { type: "string" } })).toThrow(
      /prefixItems/,
    );
  });
});
