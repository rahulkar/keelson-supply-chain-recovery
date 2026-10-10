import { blockEnvelopeSchema, type BlockEnvelope, type ScenarioCompareProps } from "@keelson/contracts";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { BlockRenderer, KpiTiles } from "../src";
import { ScorecardTable } from "../src/blocks/ScenarioCompare";
import {
  bandOf,
  compareChartOption,
  hasMetric,
  likelyText,
  metricMeta,
  paretoFlags,
  pessimistic,
  scorecardColumns,
  withXAxis,
} from "../src/compare/model";
import { rangeChartOption, robustnessRows, samplesSentence } from "../src/compare/robustness";
import { blockFixtures, getFixture } from "../src/fixtures";
import { expectNoA11yViolations } from "./a11y";

const fixture = (name: string): BlockEnvelope => {
  const f = getFixture(name);
  if (!f) throw new Error(`missing fixture ${name}`);
  return structuredClone(f.envelope);
};
const robust = () => fixture("scenario_compare.robust").props as ScenarioCompareProps;

async function renderReady(envelope: BlockEnvelope) {
  const utils = render(<BlockRenderer envelope={envelope} onAction={() => {}} density="canvas" />);
  await waitFor(() => expect(utils.container.querySelector(`[data-block-id="${envelope.id}"]`)).not.toBeNull(), { timeout: 10_000 });
  return utils;
}

const row = (table: HTMLElement, id: string) => table.querySelector(`tr[data-option-id="${id}"]`) as HTMLElement;

describe("M8 fixtures", () => {
  it("are appended after the M7 ones and validate against the envelope contract", () => {
    const names = blockFixtures.map((f) => f.name);
    const m7 = names.indexOf("scenario_compare.advisory");
    const at = names.indexOf("scenario_compare.robust");  // after the CP-SAT fixture of 125 (m8-cpsat.test)
    expect(at).toBeGreaterThan(m7);
    expect(names.slice(at)).toEqual(["scenario_compare.robust", "kpi_strip.robust"]);
    for (const n of ["scenario_compare.robust", "kpi_strip.robust"]) {
      expect(blockEnvelopeSchema.safeParse(fixture(n)).success, n).toBe(true);
    }
  });
});

describe("compare model (M8)", () => {
  it("bands, pessimistic ends and likely texts", () => {
    const [a] = robust().options;
    expect(bandOf(a!, "fill_rate_bp")).toEqual({ p10: 9126, p50: 9467, p90: 9716 });
    expect(bandOf(a!, "incremental_cost_cents")).toBeNull();
    expect(pessimistic("fill_rate_bp", bandOf(a!, "fill_rate_bp")!)).toBe(9126);
    expect(pessimistic("lost_units", bandOf(a!, "lost_units")!)).toBe(24271);
    expect(likelyText("fill_rate_bp", bandOf(a!, "fill_rate_bp")!)).toBe("likely 91.26%–97.16%");
    expect(metricMeta.incremental_co2e_kg).toEqual({ label: "Extra CO₂e", unit: "kg_co2e", better: "lower" });
  });

  it("recomputes the Pareto frontier for the CO₂e axis, direction-aware", () => {
    const p = robust();
    expect(hasMetric(p.options, "incremental_co2e_kg")).toBe(true);
    expect(paretoFlags(p.options, "incremental_co2e_kg", "lost_units")).toEqual({
      "opt-A": false,
      "opt-B": true,
      "opt-C": true,
      "do-nothing": false,
    });
    // higher-is-better metrics flip: the best fill rate at the lowest cost wins
    const flags = paretoFlags(p.options, "incremental_cost_cents", "fill_rate_bp");
    expect(flags["opt-B"]).toBe(true);
    expect(flags["do-nothing"]).toBe(true);
    const co2 = withXAxis(p, "incremental_co2e_kg");
    expect(co2.axes).toEqual({ x: "incremental_co2e_kg", y: "lost_units" });
    expect(co2.options.filter((o) => o.pareto).map((o) => o.id)).toEqual(["opt-B", "opt-C"]);
    expect(withXAxis(p, "incremental_cost_cents")).toBe(p);
    expect(scorecardColumns(p)).toContain("incremental_co2e_kg");
    const plain = fixture("scenario_compare").props as ScenarioCompareProps;
    expect(scorecardColumns(plain)).not.toContain("incremental_co2e_kg");
    expect(hasMetric(plain.options, "incremental_co2e_kg")).toBe(false);
  });

  it("draws p10–p90 whiskers on the y metric and puts the range in the tooltip", () => {
    const opt = compareChartOption(robust(), "light", true);
    expect(opt.animation).toBe(false);
    const series = opt.series as { type: string; data: unknown[]; tooltip?: { formatter: (x: unknown) => string } }[];
    expect(series.map((s) => s.type)).toEqual(["line", "scatter", "custom"]);
    expect(series[2]!.data).toEqual([
      [428430, 7881, 24271],
      [2228482, 2439, 17497],
      [6874047, 4501, 18991],
      [0, 11974, 32358],
    ]);
    const tip = series[1]!.tooltip!.formatter({ dataIndex: 0 });
    expect(tip).toContain("likely 7,881 units–24,271 units (10th–90th percentile of 64 sampled futures)");
    const plain = compareChartOption(fixture("scenario_compare").props as ScenarioCompareProps, "light", false);
    expect((plain.series as unknown[]).length).toBe(2);
  });

  it("range chart and its rows come straight from the bands", () => {
    const p = robust();
    const rows = robustnessRows(p.options, "lost_margin_cents");
    expect(rows.map((r) => [r.id, r.band.p50, r.nominal, r.winRateBp])).toEqual([
      ["opt-A", 18553550, 11947130, 1406],
      ["opt-B", 8095400, 0, 6875],
      ["opt-C", 10458450, 3144140, 1719],
      ["do-nothing", 30471800, 29585280, 0],
    ]);
    const opt = rangeChartOption(rows, "lost_margin_cents", "dark", true);
    expect(opt.animation).toBe(false);
    const [ranges, planned] = opt.series as { data: unknown[] }[];
    expect(ranges!.data[1]).toEqual([1, 3374500, 8095400, 34126550]);
    expect(planned!.data).toHaveLength(4);
    expect(samplesSentence(p)).toBe(
      "64 sampled futures, varying how long the disruption lasts, how severe it is, transit times, demand",
    );
    expect(robustnessRows((fixture("scenario_compare").props as ScenarioCompareProps).options, "fill_rate_bp")).toEqual([]);
  });
});

describe("ScenarioCompare with bands, CO₂e and profiles", () => {
  it("scorecard table: likely ranges under banded values, the CO₂e column, ranks", () => {
    const p = robust();
    render(<ScorecardTable options={p.options} metrics={scorecardColumns(p)} rankBy={{ profileId: "net-benefit", label: "Net benefit" }} />);
    const table = screen.getByRole("table", { name: "Scorecard by option" });
    const a = row(table, "opt-A");
    expect(a).toHaveTextContent("likely 91.26%–97.16%");
    expect(a).toHaveTextContent("10th–90th percentile of 64 sampled futures");
    expect(within(table).getByRole("columnheader", { name: "Extra CO₂e" })).toBeInTheDocument();
    expect(row(table, "opt-B")).toHaveTextContent("22,932 kg CO₂e");
    expect(row(table, "opt-C")).toHaveTextContent("−2,235 kg CO₂e");
    expect(within(table).getByRole("columnheader", { name: "Rank (Net benefit)" })).toBeInTheDocument();
    expect(within(row(table, "opt-B")).getByText("Top under Net benefit")).toBeInTheDocument();
    expect(within(row(table, "opt-A")).queryByText(/Top under/)).toBeNull();
  });

  it("switches the x axis to CO₂e (frontier recomputed) and ranks by another profile", async () => {
    const user = userEvent.setup();
    const { container } = await renderReady(fixture("scenario_compare.robust"));
    const summary = () => screen.getByText(/options compared on/);
    expect(summary()).toHaveTextContent("compared on extra cost and lost units");
    const axis = screen.getByRole("group", { name: "Compare against" });
    const table = () => screen.getByRole("table", { name: "Scorecard by option" });
    const bestTradeOffs = () => [...table().querySelectorAll("tbody tr")].filter((tr) => tr.textContent?.includes("Best trade-off")).map((tr) => tr.getAttribute("data-option-id"));
    expect(bestTradeOffs()).toEqual(["opt-A", "opt-B", "do-nothing"]);
    await user.click(within(axis).getByRole("button", { name: "Extra CO₂e" }));
    expect(within(axis).getByRole("button", { name: "Extra CO₂e" })).toHaveAttribute("aria-pressed", "true");
    expect(summary()).toHaveTextContent("compared on extra co₂e and lost units");
    expect(bestTradeOffs()).toEqual(["opt-B", "opt-C"]);

    const rank = screen.getByRole("group", { name: "Rank by" });
    expect(within(rank).getByRole("button", { name: "Net benefit" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText(/Ranking only: the recommendation follows the workspace default \(Net benefit\)/)).toBeInTheDocument();
    await user.click(within(rank).getByRole("button", { name: "Cost first" }));
    expect(within(table()).getByRole("columnheader", { name: "Rank (Cost first)" })).toBeInTheDocument();
    expect(within(row(table(), "opt-B")).getByText("Top under Cost first")).toBeInTheDocument();
    expect(container.querySelector("[data-emissions-note]")).toHaveTextContent(
      "transport only (well-to-wheel), factors from GLEC Framework v3 default intensities (indicative figures)",
    );
  });

  it("disables the CO₂e axis with a reason when an option has no CO₂e", async () => {
    const env = fixture("scenario_compare.robust");
    const props = env.props as ScenarioCompareProps;
    props.options[3]!.scorecard = { ...props.options[3]!.scorecard, incremental_co2e_kg: null, co2e_breakdown: null };
    props.emissions = null;
    const { container } = await renderReady(env);
    const chip = within(screen.getByRole("group", { name: "Compare against" })).getByRole("button", { name: "Extra CO₂e" });
    expect(chip).toBeDisabled();
    expect(chip).toHaveAttribute("title", "No CO₂e for Do nothing");
    expect(container.querySelector("[data-co2-disabled]")).toHaveTextContent("it is missing for Do nothing");
    expect(container.querySelector("[data-emissions-note]")).toBeNull();
  });

  it("flags carbon-priced profiles when an option has no CO₂e (review fix: it would count as 0 kg)", async () => {
    const env = fixture("scenario_compare.robust");
    const props = env.props as ScenarioCompareProps;
    props.options[1]!.scorecard = { ...props.options[1]!.scorecard, incremental_co2e_kg: null, co2e_breakdown: null };
    const { container } = await renderReady(env);
    const rank = screen.getByRole("group", { name: "Rank by" });
    const low = within(rank).getByRole("button", { name: "Low carbon" });
    expect(low).toBeDisabled();
    expect(low).toHaveAttribute("title", "Prices CO₂e, which some options don't have");
    expect(within(rank).getByRole("button", { name: "Cost first" })).toBeEnabled();
    expect(within(rank).getByRole("button", { name: "Net benefit" })).toHaveAttribute("aria-pressed", "true");
    expect(container.querySelector("[data-co2-unranked]")).toHaveTextContent(
      `CO₂e is missing for ${props.options[1]!.title}: a carbon price counts it as 0 kg`,
    );
    await expectNoA11yViolations(container);
  });

  it("does not flag the profiles when every option carries CO₂e", async () => {
    const { container } = await renderReady(fixture("scenario_compare.robust"));
    expect(within(screen.getByRole("group", { name: "Rank by" })).getByRole("button", { name: "Low carbon" })).toBeEnabled();
    expect(container.querySelector("[data-co2-unranked]")).toBeNull();
  });

  it("“How sure are we?” — the range chart turns into a p10 / p50 / p90 table", async () => {
    const user = userEvent.setup();
    await renderReady(fixture("scenario_compare.robust"));
    const panel = screen.getByRole("region", { name: "How sure are we?" });
    expect(within(panel).getByText(/re-run in 64 sampled futures, varying how long the disruption lasts/)).toBeInTheDocument();
    expect(panel.querySelector('[data-chart="robustness-range"]')).not.toBeNull();
    await user.click(within(panel).getByRole("button", { name: "View as table" }));
    const table = within(panel).getByRole("table", { name: "Likely range of lost margin" });
    const headers = within(table).getAllByRole("columnheader").map((h) => h.textContent);
    expect(headers).toEqual(["Option", "10th percentile", "Median", "90th percentile", "Planned", "Chance of a stock-out",
      "Best in % of futures"]);
    expect(within(table).getByRole("rowheader", { name: "Rebalance + targeted sea-air" }).closest("tr")).toHaveTextContent(
      "€33,745€80,954€341,266€0100%68.75%",
    );
    await user.click(within(screen.getByRole("group", { name: "Measure" })).getByRole("button", { name: "Fill rate" }));
    expect(within(panel).getByRole("table", { name: "Likely range of fill rate" })).toHaveTextContent("93.71%");
    await user.click(within(panel).getByRole("button", { name: "View as chart" }));
    expect(panel.querySelector('[data-chart="robustness-range"]')).not.toBeNull();
  });

  it("keeps the M7 view for options without M8 data", async () => {
    await renderReady(fixture("scenario_compare"));
    expect(screen.queryByRole("group", { name: "Compare against" })).toBeNull();
    expect(screen.queryByRole("group", { name: "Rank by" })).toBeNull();
    expect(screen.queryByRole("region", { name: "How sure are we?" })).toBeNull();
    expect(document.querySelector("[data-band]")).toBeNull();
  });

  it("has no axe violations (chart and table views)", async () => {
    const user = userEvent.setup();
    const { container } = await renderReady(fixture("scenario_compare.robust"));
    await expectNoA11yViolations(container);
    await user.click(within(screen.getByRole("region", { name: "How sure are we?" })).getByRole("button", { name: "View as table" }));
    await expectNoA11yViolations(container);
  });
});

describe("KPI tiles with likely ranges (M8)", () => {
  it("show the band line and the CO₂e tile", async () => {
    const env = fixture("kpi_strip.robust");
    const { container } = render(<BlockRenderer envelope={env} onAction={() => {}} />);
    const tile = (key: string) => container.querySelector(`[data-kpi="${key}"]`) as HTMLElement;
    expect(tile("units_at_risk")).toHaveTextContent("Likely 11,974 units–32,358 units");
    expect(tile("units_at_risk")).toHaveTextContent("(10th–90th percentile of 64 sampled futures)");
    expect(tile("fill_rate_bp")).toHaveTextContent("Likely 93.71%–99.12%");
    expect(tile("incremental_co2e_kg")).toHaveTextContent("22,932 kg CO₂e");
    expect(tile("incremental_cost_cents").querySelector("[data-kpi-band]")).toBeNull();
    await expectNoA11yViolations(container);
  });

  it("tiles without bands render as before", () => {
    const { container } = render(
      <KpiTiles items={[{ key: "units_at_risk", label: "Units at risk", value: 5, unit: "units", delta: null, prov: "simulated", preliminary: false, band: null }]} />,
    );
    expect(container.querySelector("[data-kpi-band]")).toBeNull();
  });
});
