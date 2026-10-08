/**
 * Regression tests for the M3 canvas review: stable props / data identity across re-renders,
 * keeping a payload on screen while a re-emitted block reloads, layout only on structural
 * change, timeline "new" stock-outs, scorecard ⇄ scatter equivalence for every axis, prototype
 * keys in legends / levers, antimeridian bboxes and base-map error classification.
 */
import { blockPropsSchemas, type BlockEnvelope, type ImpactGraphData, type ScenarioCompareProps, type StockoutTimelineProps } from "@keelson/contracts";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import type { ComponentType } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { z } from "zod";
import { BlockRenderer, blockRegistry, registerBlock, type BlockComponentProps, type BlockDataLoader } from "../src";
import { breachSummaryText } from "../src/blocks/StockoutTimeline";
import { compareChartOption, leverLabel, scorecardColumns, SCORECARD_COLUMNS } from "../src/compare/model";
import { fixtureData, getFixture } from "../src/fixtures";
import { buildGraphModel } from "../src/graph/model";
import { isBaseMapFailure } from "../src/map/MapCanvas";
import { bboxToBounds } from "../src/map/model";
import { buildTimeline, summarize } from "../src/timeline/model";
import { expectNoA11yViolations } from "./a11y";

vi.mock("../src/graph/layout", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/graph/layout")>();
  return { ...mod, layoutGraph: vi.fn(mod.layoutGraph) };
});

const fx = (name: string) => {
  const f = getFixture(name);
  if (!f) throw new Error(`missing fixture ${name}`);
  return { envelope: structuredClone(f.envelope), data: structuredClone(f.data) };
};

type Seen = { props: unknown; data: unknown };

/** Temporarily replaces a registered kind with a spy that records what it receives. */
function withSpyBlock(key: "network_map@1" | "scenario_compare@1", seen: Seen[]) {
  const original = blockRegistry[key]!;
  const schema = blockPropsSchemas[key] as unknown as z.ZodType<unknown>;
  const Spy = (p: BlockComponentProps<unknown>) => {
    seen.push({ props: p.props, data: p.data });
    return <div data-spy="" data-block-id={p.envelope.id} />;
  };
  registerBlock(key, { schema, component: Spy });
  return () => registerBlock(key, { schema, component: original as ComponentType<BlockComponentProps<unknown>> });
}

afterEach(() => {
  vi.clearAllMocks();
});

describe("BlockRenderer keeps block inputs stable", () => {
  it("re-rendering with the same envelope passes the same props and data objects (no re-validation churn)", () => {
    const seen: Seen[] = [];
    const restore = withSpyBlock("network_map@1", seen);
    try {
      const f = fx("network_map");
      const { rerender } = render(<BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} />);
      rerender(<BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} busy />);
      rerender(<BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} />);
      expect(seen.length).toBeGreaterThanOrEqual(3);
      for (const s of seen) {
        expect(s.props).toBe(seen[0]!.props);
        expect(s.data).toBe(seen[0]!.data);
      }
      // a new envelope object is validated again
      rerender(<BlockRenderer envelope={{ ...f.envelope }} onAction={() => {}} data={f.data} />);
      expect(seen.at(-1)!.props).not.toBe(seen[0]!.props);
    } finally {
      restore();
    }
  });

  it("M1-style kinds without data also keep their props object across re-renders", () => {
    const seen: Seen[] = [];
    const restore = withSpyBlock("scenario_compare@1", seen);
    try {
      const f = fx("scenario_compare");
      const { rerender } = render(<BlockRenderer envelope={f.envelope} onAction={() => {}} />);
      rerender(<BlockRenderer envelope={f.envelope} onAction={() => {}} busy />);
      expect(seen).toHaveLength(2);
      expect(seen[1]!.props).toBe(seen[0]!.props);
    } finally {
      restore();
    }
  });

  it("a re-emitted block keeps showing its payload while the new one loads, then switches", async () => {
    const seen: Seen[] = [];
    const restore = withSpyBlock("network_map@1", seen);
    try {
      const f = fx("network_map");
      const first = structuredClone(fixtureData("blk_network_map"));
      const second = structuredClone(fixtureData("blk_network_map")) as { nodes: { label: string }[] };
      second.nodes[0]!.label = "Renamed site";
      let resolveSecond!: (v: unknown) => void;
      const loader = vi
        .fn<BlockDataLoader>()
        .mockResolvedValueOnce(first)
        .mockImplementationOnce(() => new Promise((r) => (resolveSecond = r)));
      const { container, rerender } = render(<BlockRenderer envelope={f.envelope} onAction={() => {}} loadData={loader} dataKey={1} />);
      await waitFor(() => expect(container.querySelector("[data-spy]")).not.toBeNull());
      const spy = container.querySelector("[data-spy]");

      rerender(<BlockRenderer envelope={{ ...f.envelope }} onAction={() => {}} loadData={loader} dataKey={2} />);
      expect(loader).toHaveBeenCalledTimes(2);
      // no skeleton in between: the same element stays mounted with the previous payload
      expect(screen.queryByRole("status")).toBeNull();
      expect(container.querySelector("[data-spy]")).toBe(spy);
      expect((seen.at(-1)!.data as { nodes: { label: string }[] }).nodes[0]!.label).not.toBe("Renamed site");

      await act(async () => resolveSecond(second));
      await waitFor(() => expect((seen.at(-1)!.data as { nodes: { label: string }[] }).nodes[0]!.label).toBe("Renamed site"));
      expect(container.querySelector("[data-spy]")).toBe(spy);
    } finally {
      restore();
    }
  });

  it("a different block id never shows the previous block's payload", async () => {
    const f = fx("network_map");
    const loader = vi
      .fn<BlockDataLoader>()
      .mockResolvedValueOnce(structuredClone(fixtureData("blk_network_map")))
      .mockImplementationOnce(() => new Promise(() => {}));
    const { container, rerender } = render(<BlockRenderer envelope={f.envelope} onAction={() => {}} loadData={loader} />);
    await waitFor(() => expect(container.querySelector('[data-block-kind="network_map"]')).not.toBeNull(), { timeout: 10_000 });
    const other: BlockEnvelope = { ...f.envelope, id: "blk_other", data_ref: "/api/blocks/blk_other/data" };
    rerender(<BlockRenderer envelope={other} onAction={() => {}} loadData={loader} />);
    expect(screen.getByRole("status", { name: /^Loading/ })).toBeInTheDocument();
  });
});

describe("ImpactGraph layout runs only when the structure changes", () => {
  it("a structurally identical graph (new object, new statuses) is not laid out again", async () => {
    const { layoutGraph } = await import("../src/graph/layout");
    const { default: GraphCanvas } = await import("../src/graph/GraphCanvas");
    const data = structuredClone(fixtureData("blk_impact_graph")) as ImpactGraphData;
    const model = buildGraphModel(data, "all");
    const { container, rerender } = render(<GraphCanvas model={model} theme="light" reducedMotion height={300} />);
    await waitFor(() => expect(container.querySelectorAll(".react-flow__node")).toHaveLength(data.nodes.length), { timeout: 10_000 });
    expect(layoutGraph).toHaveBeenCalledTimes(1);

    const sameShape = buildGraphModel(structuredClone(data), "all");
    expect(sameShape).not.toBe(model);
    rerender(<GraphCanvas model={sameShape} theme="dark" reducedMotion height={300} />);
    await act(async () => {});
    expect(layoutGraph).toHaveBeenCalledTimes(1);

    const fewer = buildGraphModel(data, "disrupted");
    rerender(<GraphCanvas model={fewer} theme="dark" reducedMotion height={300} />);
    await waitFor(() => expect(container.querySelectorAll(".react-flow__node")).toHaveLength(fewer.nodes.length), { timeout: 10_000 });
    expect(layoutGraph).toHaveBeenCalledTimes(2);
  });
});

describe("StockoutTimeline: new stock-outs are not 'earlier'", () => {
  const props = () => getFixture("stockout_timeline")!.envelope.props as StockoutTimelineProps;
  const data = () => structuredClone(fixtureData("blk_stockout_timeline")) as Parameters<typeof buildTimeline>[1];

  it("counts a row that only runs short with the option as new", () => {
    const s = summarize(buildTimeline(props(), data(), "opt_rebalance"), "opt_rebalance");
    expect(s).toMatchObject({ earlier: 0, added: 1 });
    expect(breachSummaryText("Rebalance", s)).toBe("With Rebalance: 2 stock-outs avoided, 2 later, 0 earlier, 2 unchanged, 1 new.");
    const none = summarize(buildTimeline(props(), data(), "opt_hybrid"), "opt_hybrid");
    expect(breachSummaryText("Hybrid", none)).not.toContain("new");
  });

  it("the block says so after switching options", async () => {
    const { default: userEvent } = await import("@testing-library/user-event");
    const user = userEvent.setup();
    const f = fx("stockout_timeline");
    render(<BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} density="canvas" />);
    const group = await screen.findByRole("group", { name: "Compare with option" }, { timeout: 10_000 });
    const rebalance = within(group)
      .getAllByRole("button")
      .find((b) => /^Rebalance between DCs/.test(b.textContent ?? ""));
    expect(rebalance).toBeDefined();
    await user.click(rebalance!);
    expect(screen.getByText(/0 earlier, 2 unchanged, 1 new\.$/)).toBeInTheDocument();
  });
});

describe("ScenarioCompare: the table carries every plotted number", () => {
  const props = () => structuredClone(getFixture("scenario_compare")!.envelope.props) as ScenarioCompareProps;

  it("score columns always include both axes (e.g. shortage days)", () => {
    const p = props();
    p.axes = { x: "shortage_days", y: "fill_rate_bp" };
    const cols = scorecardColumns(p);
    expect(cols).toContain("shortage_days");
    expect(cols).toContain("fill_rate_bp");
    expect(new Set(cols).size).toBe(cols.length);
    expect(SCORECARD_COLUMNS).toContain("shortage_days");
  });

  it("renders a Shortage days column when the scatter plots it", async () => {
    const f = fx("scenario_compare");
    (f.envelope.props as ScenarioCompareProps).axes = { x: "shortage_days", y: "lost_units" };
    render(<BlockRenderer envelope={f.envelope} onAction={() => {}} density="canvas" />);
    const table = await screen.findByRole("table", { name: "Scorecard by option" }, { timeout: 10_000 });
    expect(within(table).getByRole("columnheader", { name: "Shortage days" })).toBeInTheDocument();
    const row = table.querySelector('tr[data-option-id="opt_none"]') as HTMLElement;
    expect(row).toHaveTextContent("212");
    await expectNoA11yViolations(table);
  });

  it("point labels print titles verbatim (no ECharts template substitution)", () => {
    const p = props();
    p.options[0]!.title = "Cost {c} vs {b}";
    const opt = compareChartOption(p, "light", false);
    const points = (opt.series as { data: { label: { formatter: unknown } }[] }[])[1]!;
    const formatter = points.data[0]!.label.formatter;
    expect(typeof formatter).toBe("function");
    expect((formatter as () => string)()).toBe("Cost {c} vs {b}");
  });

  it("lever names that collide with Object.prototype are humanised, not looked up", () => {
    expect(leverLabel("constructor")).toBe("Constructor");
    expect(leverLabel("to_string")).toBe("To string");
    expect(leverLabel("reallocate_inventory")).toBe("Rebalance stock");
  });
});

describe("NetworkMap robustness", () => {
  it("a legend key such as 'constructor' gets the neutral swatch colour (own keys only, no Object.prototype hit)", async () => {
    const f = fx("network_map");
    const p = f.envelope.props as { legend: { key: string; label: string; kind: string }[] };
    p.legend.push({ key: "constructor", label: "Odd key", kind: "node" }, { key: "toString", label: "Another", kind: "link" });
    const { container } = render(<BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} density="canvas" />);
    const legend = await screen.findByRole("list", { name: "Legend" }, { timeout: 10_000 });
    for (const label of ["Odd key", "Another"]) {
      const item = within(legend).getByText(label).closest("li")!;
      // the neutral swatch colour (an own-key lookup), not `undefined` from Object.prototype
      expect((item.querySelector("span[aria-hidden]") as HTMLElement).style.backgroundColor).not.toBe("");
    }
    expect(container.querySelector("[data-block-fallback]")).toBeNull();
  });

  it("fits bboxes across the antimeridian and swaps an inverted south / north", () => {
    // Los Angeles (−118°) … Shanghai (121°) the short way: west 121, east −118
    const [[w, s], [e, n]] = bboxToBounds([121_000_000, 22_000_000, -118_000_000, 40_000_000]);
    expect(w).toBe(121);
    expect(e).toBe(242);
    expect([s, n]).toEqual([22, 40]);
    const [[, s2], [, n2]] = bboxToBounds([0, 50_000_000, 10_000_000, 40_000_000]);
    expect([s2, n2]).toEqual([40, 50]);
    // the ordinary case is unchanged
    expect(bboxToBounds([-4_000_000, 48_000_000, 20_000_000, 55_000_000])).toEqual([
      [-4, 48],
      [20, 55],
    ]);
  });

  it("only base-map failures (not one failed tile, not after load) switch to the plain background", () => {
    expect(isBaseMapFailure({ error: new Error("style 503") }, false)).toBe(true);
    expect(isBaseMapFailure({ error: new Error("tile 500"), tile: {} }, false)).toBe(false);
    expect(isBaseMapFailure({ error: new Error("style 503") }, true)).toBe(false);
  });
});
