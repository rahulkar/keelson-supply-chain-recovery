import type {
  BlockEnvelope,
  ImpactGraphData,
  NetworkMapData,
  ScenarioCompareProps,
  StockoutTimelineData,
  StockoutTimelineProps,
} from "@keelson/contracts";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { blockPropsSchemas } from "@keelson/contracts";
import type { ComponentType } from "react";
import type { z } from "zod";
import {
  BlockRenderer,
  blockRegistry,
  CANVAS_BLOCK_KEYS,
  mergeBlockData,
  registerBlock,
  type BlockComponentProps,
  type BlockDataLoader,
} from "../src";
import { axesOf, compareChartOption, paretoFrontier } from "../src/compare/model";
import { fixtureData, fixtureDataLoader, getFixture } from "../src/fixtures";
import { initialViewport, MIN_READABLE_ZOOM } from "../src/graph/GraphCanvas";
import { neighbour } from "../src/graph/layout";
import { buildGraphModel, GRAPH_LIMITS } from "../src/graph/model";
import { escapeHtml } from "../src/lib/html";
import { breachShift, breachShiftText, isoWeekMonday, weekLabel, weeksBetween } from "../src/lib/week";
import { configureNetworkMap, DEFAULT_NETWORK_MAP_CONFIG, networkMapConfig } from "../src/map/config";
import {
  ALL_OPTIONS,
  bboxToBounds,
  buildMapModel,
  CURRENT_ONLY,
  isOversized,
  linkColorRole,
  mapOptions,
  MAP_LIMITS,
} from "../src/map/model";
import { buildTimeline, NO_ACTION, summarize, timelineChartOption } from "../src/timeline/model";
import { expectNoA11yViolations } from "./a11y";

const fx = (name: string) => {
  const f = getFixture(name);
  if (!f) throw new Error(`missing fixture ${name}`);
  return { envelope: structuredClone(f.envelope), data: structuredClone(f.data) };
};

const mapData = () => structuredClone(fixtureData("blk_network_map")) as NetworkMapData;
const graphData = () => structuredClone(fixtureData("blk_impact_graph")) as ImpactGraphData;
const timelineData = () => structuredClone(fixtureData("blk_stockout_timeline")) as StockoutTimelineData;

async function renderReady(envelope: BlockEnvelope, extra: Partial<Parameters<typeof BlockRenderer>[0]> = {}) {
  const utils = render(<BlockRenderer envelope={envelope} onAction={() => {}} density="canvas" {...extra} />);
  await waitFor(() => expect(utils.container.querySelector(`[data-block-id="${envelope.id}"]`)).not.toBeNull(), { timeout: 10_000 });
  return utils;
}

const tableRowIds = (table: HTMLElement, attr: string) =>
  [...table.querySelectorAll(`tbody tr[${attr}]`)].map((tr) => tr.getAttribute(attr));

afterEach(() => {
  vi.restoreAllMocks();
  vi.doUnmock("../src/map/webgl");
  vi.doUnmock("../src/map/MapCanvas");
});

// ───────────────────────────── data_ref resolution ─────────────────────────────

describe("BlockRenderer resolves data_ref before rendering", () => {
  it("shows a loading state, calls the loader with the envelope, then renders the validated payload", async () => {
    const { envelope } = fx("impact_graph");
    let resolve!: (v: unknown) => void;
    const loader = vi.fn<BlockDataLoader>(() => new Promise((r) => (resolve = r)));
    const { container } = render(<BlockRenderer envelope={envelope} onAction={() => {}} loadData={loader} density="canvas" />);
    expect(screen.getByRole("status", { name: "Loading How the delay spreads" })).toBeInTheDocument();
    expect(loader).toHaveBeenCalledTimes(1);
    expect(loader.mock.calls[0]![0].id).toBe("blk_impact_graph");
    expect(loader.mock.calls[0]![1].signal).toBeInstanceOf(AbortSignal);
    await act(async () => resolve(graphData()));
    // the visual's chunk loads lazily after the payload: allow for a busy (parallel) test run
    await waitFor(() => expect(container.querySelector('[data-block-kind="impact_graph"]')).not.toBeNull(), { timeout: 10_000 });
  });

  it("a failed load offers Try again, which loads again", async () => {
    const user = userEvent.setup();
    const { envelope } = fx("stockout_timeline");
    const loader = vi
      .fn<BlockDataLoader>()
      .mockRejectedValueOnce(new Error("HTTP 503"))
      .mockResolvedValueOnce(timelineData());
    const { container } = render(<BlockRenderer envelope={envelope} onAction={() => {}} loadData={loader} />);
    const card = await waitFor(() => {
      const el = container.querySelector('[data-block-fallback="data_unavailable"]');
      expect(el).not.toBeNull();
      return el as HTMLElement;
    });
    expect(card).toHaveTextContent("HTTP 503");
    await user.click(within(card).getByRole("button", { name: "Try again" }));
    await waitFor(() => expect(container.querySelector('[data-block-kind="stockout_timeline"]')).not.toBeNull(), { timeout: 10_000 });
    expect(loader).toHaveBeenCalledTimes(2);
  });

  it("an invalid payload falls back to RawDataCard (invalid_data) with the issues", async () => {
    const { envelope } = fx("network_map");
    const bad = mapData();
    (bad.nodes[0] as { lat_e6: number }).lat_e6 = 1.5; // not an integer
    const { container } = render(<BlockRenderer envelope={envelope} onAction={() => {}} data={bad} />);
    const card = container.querySelector('[data-block-fallback="invalid_data"]');
    expect(card).not.toBeNull();
    expect(card).toHaveTextContent(/nodes\.0\.lat_e6/);
  });

  it("over-cap payloads are rejected (impact graph > 300 nodes)", () => {
    const f = fx("invalid.impact_graph.data");
    const { container } = render(<BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} />);
    expect(container.querySelector('[data-block-fallback="invalid_data"]')).not.toBeNull();
  });

  it("invalid props are reported before any data is fetched", () => {
    const { envelope } = fx("network_map");
    (envelope.props as Record<string, unknown>)["nodes"] = []; // data in props is not allowed (.strict())
    const loader = vi.fn<BlockDataLoader>();
    const { container } = render(<BlockRenderer envelope={envelope} onAction={() => {}} loadData={loader} />);
    expect(container.querySelector('[data-block-fallback="invalid_props"]')).not.toBeNull();
    expect(loader).not.toHaveBeenCalled();
  });

  it("without a loader (or without data_ref) the block says its data is unavailable", () => {
    const { envelope } = fx("network_map");
    const a = render(<BlockRenderer envelope={envelope} onAction={() => {}} />);
    expect(a.container.querySelector('[data-block-fallback="data_unavailable"]')).toHaveTextContent("No data loader");
    a.unmount();
    const b = render(<BlockRenderer envelope={{ ...envelope, data_ref: null }} onAction={() => {}} loadData={vi.fn()} />);
    expect(b.container.querySelector('[data-block-fallback="data_unavailable"]')).toHaveTextContent("no data_ref");
  });

  it("re-fetches when dataKey changes and aborts the stale request", async () => {
    const { envelope } = fx("impact_graph");
    const signals: AbortSignal[] = [];
    const loader = vi.fn<BlockDataLoader>((_e, { signal }) => {
      signals.push(signal);
      return new Promise(() => {});
    });
    const { rerender, unmount } = render(<BlockRenderer envelope={envelope} onAction={() => {}} loadData={loader} dataKey={1} />);
    rerender(<BlockRenderer envelope={envelope} onAction={() => {}} loadData={loader} dataKey={1} />);
    expect(loader).toHaveBeenCalledTimes(1); // same key, new loader identity → no refetch
    rerender(<BlockRenderer envelope={envelope} onAction={() => {}} loadData={loader} dataKey={2} />);
    expect(loader).toHaveBeenCalledTimes(2);
    expect(signals[0]!.aborted).toBe(true);
    unmount();
    expect(signals[1]!.aborted).toBe(true);
  });

  it("the fixture loader serves fixture payloads by block id and rejects unknown ids", async () => {
    const load = fixtureDataLoader();
    const ctrl = new AbortController();
    await expect(load(fx("network_map").envelope, { signal: ctrl.signal })).resolves.toEqual(mapData());
    await expect(load({ ...fx("network_map").envelope, id: "nope" }, { signal: ctrl.signal })).rejects.toThrow("No data");
  });

  it("M1 kinds with a data_ref still get the payload merged over props (legacy)", async () => {
    const plan = structuredClone(getFixture("plan_diff")!.envelope);
    const rows = (plan.props as { rows: unknown[] }).rows;
    const envelope = { ...plan, data_ref: "/api/blocks/blk_plan/data", props: { ...(plan.props as object), rows: undefined } };
    const loader = vi.fn<BlockDataLoader>().mockResolvedValue({ rows });
    const { container } = render(<BlockRenderer envelope={envelope} onAction={() => {}} loadData={loader} density="canvas" />);
    await waitFor(() => expect(container.querySelector('[data-block-kind="plan_diff"]')).not.toBeNull());
    expect(mergeBlockData(envelope, [1]).props).toMatchObject({ data: [1] });
  });

  it("registers the four canvas visuals lazily and lets apps replace them (next/dynamic)", async () => {
    for (const key of CANVAS_BLOCK_KEYS) expect(blockRegistry[key]).toBeDefined();
    const original = blockRegistry["scenario_compare@1"]!;
    const schema = blockPropsSchemas["scenario_compare@1"] as unknown as z.ZodType<unknown>;
    const Replacement = (p: BlockComponentProps<unknown>) => <div data-replaced="" data-block-id={p.envelope.id} />;
    registerBlock("scenario_compare@1", { schema, component: Replacement });
    try {
      const { container } = render(<BlockRenderer envelope={fx("scenario_compare").envelope} onAction={() => {}} />);
      expect(container.querySelector("[data-replaced]")).not.toBeNull();
    } finally {
      registerBlock("scenario_compare@1", { schema, component: original as ComponentType<BlockComponentProps<unknown>> });
    }
  });
});

// ───────────────────────────── network map ─────────────────────────────

describe("NetworkMap model", () => {
  it("filters proposed lanes by option and keeps the current network", () => {
    const data = mapData();
    const all = buildMapModel(data, ALL_OPTIONS);
    const current = buildMapModel(data, CURRENT_ONLY);
    const rebalance = buildMapModel(data, "opt_rebalance");
    expect(all.links).toHaveLength(data.links.length);
    expect(current.links.every((l) => l.option_id === null)).toBe(true);
    expect(rebalance.links.filter((l) => l.option_id).map((l) => l.id).sort()).toEqual(["opt-a-de1-nl1", "opt-a-de1-uk1"]);
    expect(rebalance.links).toHaveLength(current.links.length + 2);
    // sea–air / near-shore are arcs, the rest paths
    expect(all.arcs.map((l) => l.kind).sort()).toEqual(["nearshore", "nearshore", "sea_air"]);
    expect(all.paths.length + all.arcs.length).toBe(all.links.length);
    // sites sorted most severe first
    expect(all.nodes[0]!.status).toBe("disrupted");
  });

  it("names options from the legend or humanises the id", () => {
    const data = mapData();
    expect(mapOptions(data, []).map((o) => o.label)).toEqual(["Rebalance", "Sea air", "Nearshore"]);
    expect(mapOptions(data, [{ key: "opt_sea_air", label: "Targeted sea–air", kind: "link" }])[1]!.label).toBe("Targeted sea–air");
  });

  it("converts e6 bboxes to bounds and pads a single point", () => {
    expect(bboxToBounds([-10_000_000, 5_000_000, 92_600_000, 54_000_000])).toEqual([
      [-10, 5],
      [92.6, 54],
    ]);
    expect(bboxToBounds([1_000_000, 50_000_000, 1_000_000, 50_000_000])).toEqual([
      [0.5, 49.5],
      [1.5, 50.5],
    ]);
  });

  it("colours affected current lanes by status and proposals by kind", () => {
    expect(linkColorRole({ kind: "inbound", status: "disrupted", option_id: null })).toBe("sev-critical");
    expect(linkColorRole({ kind: "inbound", status: "ok", option_id: null })).toBe("chart-2");
    expect(linkColorRole({ kind: "sea_air", status: "disrupted", option_id: "opt_b" })).toBe("chart-3");
  });

  it("flags payloads too large to draw", () => {
    const data = mapData();
    expect(isOversized(data)).toBe(false);
    expect(isOversized(fx("network_map.large").data as NetworkMapData)).toBe(true);
    const longPath = { ...data, links: [{ ...data.links[0]!, path: Array.from({ length: MAP_LIMITS.pathPoints + 1 }, () => [0, 0] as [number, number]) }] };
    expect(isOversized(longPath)).toBe(true);
  });

  it("map settings default to OpenFreeMap Positron / dark and the same-origin worker", () => {
    expect(DEFAULT_NETWORK_MAP_CONFIG.styleUrls.light).toBe("https://tiles.openfreemap.org/styles/positron");
    expect(DEFAULT_NETWORK_MAP_CONFIG.styleUrls.dark).toBe("https://tiles.openfreemap.org/styles/dark");
    expect(DEFAULT_NETWORK_MAP_CONFIG.workerUrl).toBe("/maplibre/maplibre-gl-worker.mjs");
    configureNetworkMap({ styleUrls: { light: "https://tiles.example/light", dark: "https://tiles.example/dark" } });
    expect(networkMapConfig().styleUrls.light).toBe("https://tiles.example/light");
    configureNetworkMap(DEFAULT_NETWORK_MAP_CONFIG);
  });
});

describe("NetworkMap block", () => {
  it("without WebGL shows the network as tables and says why", async () => {
    const f = fx("network_map");
    const { container } = await renderReady(f.envelope, { data: f.data });
    expect(await screen.findByText(/WebGL is unavailable/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "View as table" })).not.toBeInTheDocument();
    const sites = screen.getByRole("table", { name: /^Sites/ });
    expect(within(sites).getAllByRole("row")).toHaveLength(1 + mapData().nodes.length);
    expect(container).toHaveTextContent("16 sites · 10 at risk · 17 lanes");
    // legend from props; option entries name the option chips instead of drawing a swatch
    const legend = screen.getByRole("list", { name: "Legend" });
    expect(legend).toHaveTextContent("Sea–air (proposed)");
    expect(legend).not.toHaveTextContent("Rebalance between DCs");
    expect(screen.getByRole("group", { name: "Plan option lanes" })).toHaveTextContent("Rebalance between DCs (2)");
  });

  it("option chips switch the lanes listed (table = map selection)", async () => {
    const user = userEvent.setup();
    const f = fx("network_map");
    await renderReady(f.envelope, { data: f.data });
    const group = screen.getByRole("group", { name: "Plan option lanes" });
    const lanes = () => screen.getByRole("table", { name: /^Lanes/ });
    expect(tableRowIds(lanes(), "data-link-id")).toHaveLength(17);
    await user.click(within(group).getByRole("button", { name: "Current network" }));
    expect(within(group).getByRole("button", { name: "Current network" })).toHaveAttribute("aria-pressed", "true");
    expect(tableRowIds(lanes(), "data-link-id")).toEqual(buildMapModel(mapData(), CURRENT_ONLY).links.map((l) => l.id));
    await user.click(within(group).getByRole("button", { name: "Rebalance between DCs (2)" }));
    expect(tableRowIds(lanes(), "data-link-id")).toEqual(buildMapModel(mapData(), "opt_rebalance").links.map((l) => l.id));
    expect(lanes()).toHaveTextContent("DE1 Bremen DC → UK1 Midlands DC");
  });

  it("with WebGL draws the map from the same model the table lists (View as table equivalence)", async () => {
    vi.resetModules();
    vi.doMock("../src/map/webgl", () => ({ supportsWebGL2: () => true }));
    const seen: { nodes: string[]; links: string[]; style?: string }[] = [];
    vi.doMock("../src/map/MapCanvas", () => ({
      default: (p: { model: ReturnType<typeof buildMapModel>; theme: string }) => {
        seen.push({ nodes: p.model.nodes.map((n) => n.id), links: p.model.links.map((l) => l.id), style: p.theme });
        return <div data-testid="map-canvas" />;
      },
    }));
    const viz = await import("../src");
    const fixtures = await import("../src/fixtures");
    const f = fixtures.getFixture("network_map")!;
    const user = userEvent.setup();
    render(<viz.BlockRenderer envelope={f.envelope} onAction={() => {}} data={f.data} density="canvas" />);
    expect(await screen.findByTestId("map-canvas")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /^Map of 16 sites and 17 lanes/ })).toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: "Near-shore top-up + resequence (2)" }));
    const drawn = seen.at(-1)!;
    await user.click(screen.getByRole("button", { name: "View as table" }));
    expect(tableRowIds(screen.getByRole("table", { name: /^Lanes/ }), "data-link-id")).toEqual(drawn.links);
    const siteRows = within(screen.getByRole("table", { name: /^Sites/ })).getAllByRole("row").slice(1);
    expect(siteRows).toHaveLength(drawn.nodes.length);
    await user.click(screen.getByRole("button", { name: "View as map" }));
    expect(await screen.findByTestId("map-canvas")).toBeInTheDocument();
  });

  it("an oversized network is listed in pages, never drawn", async () => {
    const user = userEvent.setup();
    const f = fx("network_map.large");
    await renderReady(f.envelope, { data: f.data });
    expect(screen.getByText(/too large to draw/)).toBeInTheDocument();
    const sites = screen.getByRole("table", { name: /^Sites \(5,200\)/ });
    expect(within(sites).getAllByRole("row")).toHaveLength(101);
    await user.click(screen.getByRole("button", { name: "Show 100 more (of 5,200)" }));
    expect(within(sites).getAllByRole("row")).toHaveLength(201);
  });

  it("an empty network says so", async () => {
    const f = fx("network_map.empty");
    await renderReady(f.envelope, { data: f.data });
    expect(screen.getByText("No sites or lanes to show yet.")).toBeInTheDocument();
  });

  it("has no axe violations (tables)", async () => {
    const f = fx("network_map");
    const { container } = await renderReady(f.envelope, { data: f.data });
    await expectNoA11yViolations(container);
  });
});

// ───────────────────────────── impact graph ─────────────────────────────

describe("ImpactGraph model + keyboard order", () => {
  it("filters by status and keeps only edges between visible nodes", () => {
    const data = graphData();
    const all = buildGraphModel(data, "all");
    expect(all.nodes).toHaveLength(data.nodes.length);
    expect(all.edges).toHaveLength(data.edges!.length);
    const atRisk = buildGraphModel(data, "at_risk");
    expect(atRisk.nodes.every((n) => n.status === "at_risk")).toBe(true);
    const ids = new Set(atRisk.nodes.map((n) => n.id));
    expect(atRisk.edges.every((e) => ids.has(e.source) && ids.has(e.target))).toBe(true);
    expect(all.downstream.get("dc:UK1")).toEqual(["cl:UK-N", "cl:UK-S"]);
    expect(all.upstream.get("fac:BD-KNIT")).toEqual(["cmp:ZIP-5"]);
  });

  it("drops dangling edges, self loops and duplicates", () => {
    const data = graphData();
    data.edges!.push({ source: "dc:UK1", target: "nope", kind: "stocks" }, { source: "dc:UK1", target: "dc:UK1", kind: "stocks" }, data.edges![0]!);
    expect(buildGraphModel(data, "all").edges).toHaveLength(graphData().edges!.length);
  });

  it("opens wide graphs at a readable zoom from the upstream (left) edge, small ones fitted", () => {
    const small = new Map([
      ["a", { x: 0, y: 0 }],
      ["b", { x: 280, y: 0 }],
    ]);
    expect(initialViewport(small, 700, 480)).toEqual({ fit: true });
    const wide = new Map(Array.from({ length: 9 }, (_, i) => [`n${i}`, { x: i * 280, y: (i % 3) * 92 }] as const));
    const view = initialViewport(wide, 700, 480);
    expect(view).toMatchObject({ fit: false, zoom: MIN_READABLE_ZOOM, x: 24 });
    expect(initialViewport(new Map(), 700, 480)).toEqual({ fit: true });
  });

  it("places a wide graph's opening view on the upstream nodes, not the empty top corner", () => {
    // Regression (M3 integration, full profile): the upstream column sat ~1,400 px below the
    // graph's top (the cluster column on the far right started at y = 12), so the view opened
    // on empty canvas. Nodes inside the first visible columns must be on screen.
    const positions = new Map<string, { x: number; y: number }>([
      ["po:1", { x: 12, y: 1400 }],
      ["po:2", { x: 12, y: 1500 }],
      ["factory:F", { x: 292, y: 1450 }],
      ["cluster:A", { x: 6864, y: 12 }],
      ["cluster:B", { x: 6864, y: 4656 }],
    ]);
    const height = 480;
    const view = initialViewport(positions, 606, height);
    expect(view.fit).toBe(false);
    if (view.fit) return;
    for (const id of ["po:1", "po:2", "factory:F"]) {
      const p = positions.get(id)!;
      const screenY = p.y * view.zoom + view.y;
      const screenX = p.x * view.zoom + view.x;
      expect(screenY, id).toBeGreaterThanOrEqual(0);
      expect(screenY + 72 * view.zoom, id).toBeLessThanOrEqual(height);
      expect(screenX, id).toBeGreaterThanOrEqual(0);
    }
  });

  it("arrow keys follow edges and columns", () => {
    const positions = new Map([
      ["a", { x: 0, y: 0 }],
      ["b", { x: 100, y: 0 }],
      ["c", { x: 100, y: 50 }],
      ["d", { x: 0, y: 50 }],
    ]);
    const down = new Map([["a", ["c", "b"]]]);
    const up = new Map([
      ["b", ["a"]],
      ["c", ["a"]],
    ]);
    expect(neighbour("a", "ArrowRight", positions, down, up)).toBe("b"); // topmost first
    expect(neighbour("c", "ArrowLeft", positions, down, up)).toBe("a");
    expect(neighbour("b", "ArrowDown", positions, down, up)).toBe("c");
    expect(neighbour("c", "ArrowUp", positions, down, up)).toBe("b");
    expect(neighbour("d", "ArrowRight", positions, down, up)).toBeNull();
  });
});

describe("ImpactGraph block", () => {
  it("draws every node with its status badge and moves focus with the arrow keys", async () => {
    const f = fx("impact_graph");
    const { container } = await renderReady(f.envelope, { data: f.data });
    const canvas = await waitFor(
      () => {
        const nodes = container.querySelectorAll(".react-flow__node");
        expect(nodes).toHaveLength(graphData().nodes.length);
        return container.querySelector("[data-graph-canvas]") as HTMLElement;
      },
      { timeout: 10_000 },
    );
    const supplier = canvas.querySelector<HTMLElement>('.react-flow__node[data-id="sup:MEGHNA"]')!;
    expect(supplier).toHaveAttribute("aria-label", "Supplier Meghna Trims, Disrupted, 18,400 units on delayed receipts");
    expect(within(supplier).getByText("Disrupted")).toBeInTheDocument();
    supplier.focus();
    fireEvent.focus(supplier);
    expect(screen.getByText(/Supplier Meghna Trims, Disrupted.*feeds #5 coil zips/)).toBeInTheDocument();
    fireEvent.keyDown(supplier, { key: "ArrowRight" });
    expect(document.activeElement).toBe(canvas.querySelector('.react-flow__node[data-id="cmp:ZIP-5"]'));
    fireEvent.keyDown(document.activeElement!, { key: "ArrowLeft" });
    expect(document.activeElement).toBe(supplier);
    fireEvent.keyDown(supplier, { key: "ArrowLeft" });
    expect(screen.getByText("Meghna Trims has nothing upstream.")).toBeInTheDocument();
  });

  it("View as table lists exactly the nodes drawn, following the status filter", async () => {
    const user = userEvent.setup();
    const f = fx("impact_graph");
    const { container } = await renderReady(f.envelope, { data: f.data });
    await user.click(within(screen.getByRole("group", { name: "Show nodes" })).getByRole("button", { name: "At risk (6)" }));
    await waitFor(() => expect(container.querySelectorAll(".react-flow__node")).toHaveLength(6), { timeout: 10_000 });
    const drawn = [...container.querySelectorAll(".react-flow__node")].map((n) => n.getAttribute("data-id")).sort();
    await user.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: "Impact graph nodes (6)" });
    expect([...tableRowIds(table, "data-node-id")].sort()).toEqual(drawn);
    expect(within(table).getAllByRole("row")[1]).toHaveTextContent("UK1 Midlands DC");
    expect(table).toHaveTextContent("Week 44 (26 Oct)");
  });

  it("a graph with too many links is shown as a table", async () => {
    const f = fx("impact_graph");
    const data = graphData();
    const ids = data.nodes.map((n) => n.id);
    data.edges = Array.from({ length: GRAPH_LIMITS.edges + 1 }, (_, i) => ({ source: ids[i % ids.length]!, target: ids[(i + 1) % ids.length]!, kind: "ships" as const }));
    await renderReady(f.envelope, { data });
    expect(screen.getByText(/too large to draw/)).toBeInTheDocument();
    expect(screen.getByRole("table", { name: /Impact graph nodes/ })).toBeInTheDocument();
  });

  it("has no axe violations (graph and table)", async () => {
    const user = userEvent.setup();
    const f = fx("impact_graph");
    const { container } = await renderReady(f.envelope, { data: f.data });
    await waitFor(() => expect(container.querySelectorAll(".react-flow__node").length).toBeGreaterThan(0), { timeout: 10_000 });
    await expectNoA11yViolations(container);
    await user.click(screen.getByRole("button", { name: "View as table" }));
    await expectNoA11yViolations(container);
  });
});

// ───────────────────────────── stockout timeline ─────────────────────────────

describe("ISO weeks", () => {
  it("computes Mondays and week distances across years", () => {
    expect(isoWeekMonday("2026-W01")!.toISOString().slice(0, 10)).toBe("2025-12-29");
    expect(isoWeekMonday("2026-W45")!.toISOString().slice(0, 10)).toBe("2026-11-02");
    expect(weeksBetween("2026-W53", "2027-W01")).toBe(1);
    expect(weeksBetween("2026-W46", "2026-W44")).toBe(-2);
    expect(weekLabel("2026-W45")).toBe("Week 45 (2 Nov)");
  });

  it("describes breach shifts", () => {
    expect(breachShiftText(breachShift("2026-W44", null))).toBe("Stock-out avoided");
    expect(breachShiftText(breachShift("2026-W44", "2026-W46"))).toBe("2 weeks later");
    expect(breachShiftText(breachShift("2026-W46", "2026-W45"))).toBe("1 week earlier");
    expect(breachShiftText(breachShift(null, "2026-W45"))).toBe("New stock-out");
    expect(breachShiftText(breachShift(null, null))).toBe("No stock-out");
    expect(breachShiftText(breachShift("2026-W44", "2026-W44"))).toBe("No change");
  });
});

describe("StockoutTimeline", () => {
  const props = () => getFixture("stockout_timeline")!.envelope.props as StockoutTimelineProps;

  it("models cells per week and the shift under an option", () => {
    const views = buildTimeline(props(), timelineData(), "opt_rebalance");
    const first = views[0]!;
    expect(first.cells).toHaveLength(9);
    expect(first.baseline).toBe("2026-W45");
    expect(first.withOption).toBe("2026-W46");
    expect(first.shift).toEqual({ kind: "later", weeks: 1 });
    // a row without an entry for the option keeps its baseline
    expect(views.at(-1)!.withOption).toBeNull();
    // rebalancing takes stock from DE1, which then runs short itself (a "new" stock-out, not an earlier one)
    expect(summarize(views, "opt_rebalance")).toMatchObject({ shortRows: 6, avoided: 2, later: 2, unchanged: 2, earlier: 0, added: 1, firstBreach: "2026-W45" });
    expect(views[6]!.shift).toEqual({ kind: "new" });
    expect(summarize(buildTimeline(props(), timelineData(), NO_ACTION), NO_ACTION)).toMatchObject({ avoided: 0, later: 0 });
  });

  it("chart option: cells, baseline markers and option markers / shift lines only when an option is chosen", () => {
    const views = buildTimeline(props(), timelineData(), "opt_hybrid");
    const none = timelineChartOption(props().weeks, buildTimeline(props(), timelineData(), NO_ACTION), null, "light", false);
    expect((none.series as unknown[]).length).toBe(2);
    const opt = timelineChartOption(props().weeks, views, "Hybrid", "dark", true);
    const series = opt.series as { name: string; data: unknown[] }[];
    expect(series.map((s) => s.name)).toEqual(["Stock", "First stock-out without action", "Breach shift", "First stock-out with Hybrid"]);
    expect(series[0]!.data).toHaveLength(8 * 9);
    expect(series[2]!.data).toHaveLength(6); // every breach avoided → a line to the window's end
    expect((series[2]!.data as number[][]).every((d) => d[1] === 8 && d[3] === 1)).toBe(true);
    const later = timelineChartOption(props().weeks, buildTimeline(props(), timelineData(), "opt_sea_air"), "x", "light", false);
    expect((later.series as { data: number[][] }[])[2]!.data[0]).toEqual([3, 5, 0, 0]); // W45 → W47, row 0, inside the window
    // ECharts 6 ignores grid.containLabel without the legacy module: labels are kept in via outerBounds
    expect(JSON.stringify(opt)).not.toContain("containLabel");
    expect(opt.grid).toMatchObject({ outerBoundsMode: "same", outerBoundsContain: "axisLabel" });
    expect(series[3]!.data).toHaveLength(0);
    expect(opt.animation).toBe(false);
  });

  it("escapes row labels in tooltips", () => {
    const data = timelineData();
    data.rows[0]!.label = '<img src=x onerror="alert(1)">';
    const opt = timelineChartOption(props().weeks, buildTimeline(props(), data, NO_ACTION), null, "light", false);
    const cells = (opt.series as { tooltip: { formatter: (p: unknown) => string } }[])[0]!;
    const html = cells.tooltip.formatter({ value: [3, 0, 2, 300] });
    expect(html).toContain("&lt;img src=x onerror=&quot;alert(1)&quot;&gt;");
    expect(html).not.toContain("<img");
    expect(escapeHtml(`a&b'`)).toBe("a&amp;b&#39;");
  });

  it("the option switcher shows the breach shift in the table", async () => {
    const user = userEvent.setup();
    const f = fx("stockout_timeline");
    const { container } = await renderReady(f.envelope, { data: f.data });
    expect(container).toHaveTextContent("6 of 8 rows run short, the first in Week 45 (2 Nov) without action.");
    expect(container.querySelector('[data-chart="stockout-timeline"]')).toHaveAttribute("aria-hidden", "true");
    const group = screen.getByRole("group", { name: "Compare with option" });
    expect(within(group).getByRole("button", { name: "No action" })).toHaveAttribute("aria-pressed", "true");
    await user.click(within(group).getByRole("button", { name: "Rebalance + targeted sea–air" }));
    expect(screen.getByText(/With Rebalance \+ targeted sea–air: 4 stock-outs avoided, 1 later, 0 earlier, 1 unchanged/)).toBeInTheDocument();
    // the table (visually hidden while the chart shows) carries the same rows
    await user.click(screen.getByRole("button", { name: "View as table" }));
    const table = screen.getByRole("table", { name: /Stock by week, and the first stock-out with Rebalance \+ targeted sea–air/ });
    expect(tableRowIds(table, "data-row-id")).toEqual(timelineData().rows.map((r) => r.id));
    const firstRow = within(table).getAllByRole("row")[1]!;
    expect(firstRow).toHaveTextContent("Short 300 units");
    expect(firstRow).toHaveTextContent("2 weeks later");
    expect(within(table).getAllByRole("row")[2]).toHaveTextContent("Stock-out avoided");
    expect(container.querySelector('[data-chart="stockout-timeline"]')).toBeNull();
    await user.click(screen.getByRole("button", { name: "View as chart" }));
    expect(container.querySelector('[data-chart="stockout-timeline"]')).not.toBeNull();
  });

  it("empty rows render a calm message", async () => {
    const f = fx("stockout_timeline");
    await renderReady(f.envelope, { data: { rows: [] } });
    expect(screen.getByText("No rows to show yet.")).toBeInTheDocument();
  });

  it("has no axe violations", async () => {
    const f = fx("stockout_timeline");
    const { container } = await renderReady(f.envelope, { data: f.data });
    await expectNoA11yViolations(container);
  });
});

// ───────────────────────────── scenario compare ─────────────────────────────

describe("ScenarioCompare", () => {
  const props = () => structuredClone(getFixture("scenario_compare")!.envelope.props) as ScenarioCompareProps;

  it("plots every option, the Pareto frontier ordered by x, the recommended one as a diamond", () => {
    const p = props();
    expect(axesOf(p)).toEqual({ x: "incremental_cost_cents", y: "lost_units" });
    expect(axesOf({ options: p.options, advisory: null, profiles: [], uncertainty: null, emissions: null })).toEqual({
      x: "incremental_cost_cents",
      y: "lost_units",
    });
    expect(paretoFrontier(p.options, "incremental_cost_cents", "lost_units").map((o) => o.id)).toEqual([
      "opt_none",
      "opt_rebalance",
      "opt_sea_air",
      "opt_hybrid",
    ]);
    const opt = compareChartOption(p, "light", false);
    const [frontier, points] = opt.series as { data: { symbol?: string; itemStyle?: { borderType?: string } }[] }[];
    expect(frontier!.data).toHaveLength(4);
    expect(points!.data).toHaveLength(5);
    expect(points!.data[1]!.symbol).toBe("diamond");
    expect(points!.data[3]!.itemStyle!.borderType).toBe("dashed"); // guided only
    expect(opt.grid).toMatchObject({ outerBoundsMode: "same", outerBoundsContain: "axisLabel" });
  });

  it("escapes option titles in tooltips", () => {
    const p = props();
    p.options[0]!.title = "<b>bold</b>";
    const opt = compareChartOption(p, "light", false);
    const points = (opt.series as { tooltip: { formatter: (x: unknown) => string } }[])[1]!;
    expect(points.tooltip.formatter({ dataIndex: 0 })).toContain("&lt;b&gt;bold&lt;/b&gt;");
  });

  it("scorecard table: recommended and guide-only markers with reasons", async () => {
    const f = fx("scenario_compare");
    const { container } = await renderReady(f.envelope);
    const table = screen.getByRole("table", { name: "Scorecard by option" });
    expect(tableRowIds(table, "data-option-id")).toEqual(props().options.map((o) => o.id));
    const rec = table.querySelector('tr[data-option-id="opt_rebalance"]') as HTMLElement;
    expect(rec).toHaveAttribute("data-recommended", "true");
    expect(within(rec).getByText("Recommended")).toBeInTheDocument();
    expect(rec).toHaveTextContent("€12,400");
    expect(rec).toHaveTextContent("8,900 units");
    const guided = table.querySelector('tr[data-option-id="opt_nearshore"]') as HTMLElement;
    expect(within(guided).getByText("Guide only")).toBeInTheDocument();
    expect(guided).toHaveTextContent("a guided checklist is offered instead");
    expect(guided).toHaveTextContent("Near-shore supplier + Resequence production");
    expect(container).toHaveTextContent("recommended: Rebalance between DCs");
    expect(container).toHaveTextContent("2 can only be guided");
    expect(container.querySelector('[data-chart="scenario-compare"]')).toHaveAttribute("aria-hidden", "true");
  });

  it("has no axe violations", async () => {
    const f = fx("scenario_compare");
    const { container } = await renderReady(f.envelope);
    await expectNoA11yViolations(container);
  });
});
