/**
 * Scenario comparison: metric vocabulary, Pareto frontier and the ECharts option for the
 * scatter. The scorecard table and the chart read the same options.
 *
 * M8 (`docs/contracts/126-m8-co2-robustness.md`): the optional "Extra CO₂e" metric, robustness
 * bands (p10 / p50 / p90 over sampled futures, drawn as whiskers on the y metric) and a
 * direction-aware Pareto recomputation for a user-chosen x axis.
 */
import type { CompareOption, ScenarioCompareProps } from "@keelson/contracts";
import { hexColor, type ThemeName } from "@keelson/tokens";
import { formatValue, type Unit } from "../format";
import { escapeHtml } from "../lib/html";

export type ScorecardMetric = NonNullable<ScenarioCompareProps["axes"]>["x"];

export const metricMeta: Record<ScorecardMetric, { label: string; unit: Unit; better: "lower" | "higher" }> = {
  fill_rate_bp: { label: "Fill rate", unit: "pct_bp", better: "higher" },
  lost_units: { label: "Lost units", unit: "units", better: "lower" },
  lost_margin_cents: { label: "Lost margin", unit: "eur_cents", better: "lower" },
  incremental_cost_cents: { label: "Extra cost", unit: "eur_cents", better: "lower" },
  net_benefit_cents: { label: "Net benefit", unit: "eur_cents", better: "higher" },
  shortage_days: { label: "Shortage days", unit: "days", better: "lower" },
  first_breach_shift_days: { label: "Stock-out delayed by", unit: "days", better: "higher" },
  tts_gain_days: { label: "Time-to-survive gain", unit: "days", better: "higher" },
  changes: { label: "Changes", unit: "count", better: "lower" },
  incremental_co2e_kg: { label: "Extra CO₂e", unit: "kg_co2e", better: "lower" },
};

export const DEFAULT_AXES = { x: "incremental_cost_cents", y: "lost_units" } as const satisfies Record<"x" | "y", ScorecardMetric>;

export function axesOf(props: ScenarioCompareProps): { x: ScorecardMetric; y: ScorecardMetric } {
  return { x: props.axes?.x ?? DEFAULT_AXES.x, y: props.axes?.y ?? DEFAULT_AXES.y };
}

/** The option's value of `m`; `NaN` when the scorecard does not carry it (M8: CO₂e is optional). */
export function metricValue(o: CompareOption, m: ScorecardMetric): number {
  const v = o.scorecard[m];
  return typeof v === "number" ? v : Number.NaN;
}

/** Every option carries `m` (an axis may only use a metric every option has). */
export function hasMetric(options: readonly CompareOption[], m: ScorecardMetric): boolean {
  return options.length > 0 && options.every((o) => Number.isFinite(metricValue(o, m)));
}

export function formatMetric(m: ScorecardMetric, value: number, compact = false): string {
  return formatValue(value, metricMeta[m].unit, { compact });
}

/** Scorecard table columns: the default set plus the plotted axes, so the table always carries every number the scatter shows. */
export const SCORECARD_COLUMNS: readonly ScorecardMetric[] = [
  "lost_units",
  "fill_rate_bp",
  "shortage_days",
  "incremental_cost_cents",
  "lost_margin_cents",
  "net_benefit_cents",
  "first_breach_shift_days",
  "tts_gain_days",
  "changes",
];

/** M8: the default columns, the plotted axes, and "Extra CO₂e" when any option carries it. */
export function scorecardColumns(props: ScenarioCompareProps): ScorecardMetric[] {
  const { x, y } = axesOf(props);
  const cols = [...SCORECARD_COLUMNS];
  const co2: ScorecardMetric[] = props.options.some((o) => Number.isFinite(metricValue(o, "incremental_co2e_kg")))
    ? ["incremental_co2e_kg"]
    : [];
  for (const m of [x, y, ...co2]) if (!cols.includes(m)) cols.push(m);
  return cols;
}

// ───────────────────────────── M8: bands, Pareto per axis ─────────────────────────────

export type Band = { p10: number; p50: number; p90: number };
export type BandedMetric = "fill_rate_bp" | "lost_units" | "lost_margin_cents" | "net_benefit_cents" | "shortage_days";
export const BANDED_METRICS: readonly BandedMetric[] = [
  "fill_rate_bp",
  "lost_units",
  "lost_margin_cents",
  "net_benefit_cents",
  "shortage_days",
];

export function isBanded(m: ScorecardMetric): m is BandedMetric {
  return (BANDED_METRICS as readonly string[]).includes(m);
}

/** The option's p10 / p50 / p90 of `m` over the sampled futures (`null` without robustness bands). */
export function bandOf(o: CompareOption, m: ScorecardMetric): Band | null {
  const bands = o.scorecard.bands;
  if (!bands || !isBanded(m)) return null;
  return bands[m];
}

/** Number of sampled futures behind the options' bands (`null` when none has bands). */
export function bandSamples(options: readonly CompareOption[]): number | null {
  const n = options.map((o) => o.scorecard.bands?.samples).filter((s): s is number => typeof s === "number");
  return n.length ? Math.min(...n) : null;
}

/** The pessimistic end of a band: p10 when higher is better, p90 when lower is better. */
export function pessimistic(m: ScorecardMetric, band: Band): number {
  return metricMeta[m].better === "higher" ? band.p10 : band.p90;
}

/** "likely 93.1%–98.4%" — the 10th–90th percentile range in the metric's unit. */
export function likelyText(m: ScorecardMetric, band: Band, compact = false): string {
  return `likely ${formatMetric(m, band.p10, compact)}–${formatMetric(m, band.p90, compact)}`;
}

/**
 * Non-dominated options for axes `x` and `y`, each in its own direction (lower or higher is
 * better). Recomputed client-side when the user switches the x axis (comparisons only, no KPI
 * arithmetic); options without a value on an axis are never on the frontier.
 */
export function paretoFlags(options: readonly CompareOption[], x: ScorecardMetric, y: ScorecardMetric): Record<string, boolean> {
  const sign = (m: ScorecardMetric) => (metricMeta[m].better === "lower" ? 1 : -1);
  const pts = options.map((o) => ({ id: o.id, x: sign(x) * metricValue(o, x), y: sign(y) * metricValue(o, y) }));
  const out: Record<string, boolean> = {};
  for (const p of pts) {
    if (!Number.isFinite(p.x) || !Number.isFinite(p.y)) {
      out[p.id] = false;
      continue;
    }
    out[p.id] = !pts.some(
      (q) => q.id !== p.id && Number.isFinite(q.x) && Number.isFinite(q.y) && q.x <= p.x && q.y <= p.y && (q.x < p.x || q.y < p.y),
    );
  }
  return out;
}

/** The block's props seen on another x axis (frontier flags recomputed); the server's when unchanged. */
export function withXAxis(props: ScenarioCompareProps, x: ScorecardMetric): ScenarioCompareProps {
  const axes = axesOf(props);
  if (x === axes.x) return props;
  const flags = paretoFlags(props.options, x, axes.y);
  return {
    ...props,
    axes: { ...axes, x },
    options: props.options.map((o) => ({ ...o, pareto: flags[o.id] ?? false })),
  };
}

/** The options flagged `pareto`, ordered along the x axis (the frontier line). */
export function paretoFrontier(options: readonly CompareOption[], x: ScorecardMetric, y: ScorecardMetric): CompareOption[] {
  return options
    .filter((o) => o.pareto)
    .sort((a, b) => metricValue(a, x) - metricValue(b, x) || metricValue(a, y) - metricValue(b, y) || a.id.localeCompare(b.id));
}

/** Short lever names for the table ("reallocate_inventory" → "Reallocate inventory"). */
export const leverLabels: Record<string, string> = {
  reallocate_inventory: "Rebalance stock",
  expedite_mode_shift: "Sea–air / air",
  resource_alt_supplier: "Near-shore supplier",
  prioritize_allocation: "Prioritise allocation",
  resequence_production: "Resequence production",
};

export function leverLabel(lever: string): string {
  if (Object.prototype.hasOwnProperty.call(leverLabels, lever)) return leverLabels[lever]!;
  const words = lever.replace(/[_-]+/g, " ").trim();
  return words ? words.charAt(0).toUpperCase() + words.slice(1) : lever;
}

/**
 * Short tag of an option for grouped labels: the letter of engine ids like `opt-B` / `opt_b`
 * ("B"), otherwise the title.
 */
export function optionTag(o: Pick<CompareOption, "id" | "title">): string {
  const m = /^opt[-_]([A-Za-z0-9]{1,2})$/.exec(o.id);
  return m ? m[1]!.toUpperCase() : o.title;
}

export type PointGroup = {
  /** Options drawn at exactly the same point, in props order. */
  members: CompareOption[];
  /** The member whose marker carries the label (the recommended one, else the first). */
  lead: CompareOption;
  /** "Rebalance" for one option; "B = D" (or "Title 1 = Title 2") for coincident ones. */
  label: string;
};

/**
 * Options that land on the same point (same x and y) are labelled once, "B = D", so their
 * labels never print on top of each other (M3 carry-over, `docs/contracts/90` §10).
 */
export function pointGroups(options: readonly CompareOption[], x: ScorecardMetric, y: ScorecardMetric): PointGroup[] {
  const byPoint = new Map<string, CompareOption[]>();
  for (const o of options) {
    const key = `${metricValue(o, x)}|${metricValue(o, y)}`;
    byPoint.set(key, [...(byPoint.get(key) ?? []), o]);
  }
  return [...byPoint.values()].map((members) => {
    const lead = members.find((o) => o.recommended) ?? members[0]!;
    const label = members.length === 1 ? lead.title : members.map(optionTag).join(" = ");
    return { members, lead, label };
  });
}

/**
 * ECharts option: every option as a point (recommended = large diamond, guided-only = hollow
 * ring, others = dot), labelled with its title (coincident options share one "B = D" label),
 * and the Pareto frontier as a line.
 */
export function compareChartOption(props: ScenarioCompareProps, theme: ThemeName, reducedMotion: boolean): Record<string, unknown> {
  const { x, y } = axesOf(props);
  const accent = hexColor("accent", theme);
  const fg = hexColor("fg", theme);
  const muted = hexColor("fg-muted", theme);
  const surface = hexColor("surface", theme);
  const groups = pointGroups(props.options, x, y);
  const groupOf = new Map<string, PointGroup>();
  for (const g of groups) for (const o of g.members) groupOf.set(o.id, g);
  const point = (o: CompareOption) => {
    const group = groupOf.get(o.id)!;
    const isLead = group.lead.id === o.id;
    const emphasis = group.members.some((m) => m.recommended);
    return {
      name: o.title,
      value: [metricValue(o, x), metricValue(o, y)],
      symbol: o.recommended ? "diamond" : "circle",
      symbolSize: o.recommended ? 18 : 12,
      // The lead marker (and its label) is drawn on top of the options it shares the point with.
      z: isLead ? 3 : 2,
      itemStyle: o.guided_only
        ? { color: surface, borderColor: muted, borderWidth: 2, borderType: "dashed" }
        : { color: o.recommended ? accent : hexColor("chart-2", theme), borderColor: surface, borderWidth: 1 },
      label: {
        show: isLead,
        position: "right",
        color: emphasis ? fg : muted,
        fontSize: 11,
        fontWeight: emphasis ? 600 : 400,
        // A function, not the title as a template string: "{b}" / "{c}" in a title must print as typed.
        formatter: () => group.label,
        width: 170,
        overflow: "truncate",
      },
    };
  };
  const samples = bandSamples(props.options);
  const tooltip = (o: CompareOption) => {
    const same = groupOf.get(o.id)!.members.filter((m) => m.id !== o.id);
    const band = bandOf(o, y);
    return [
      `<strong>${escapeHtml(o.title)}</strong>${o.recommended ? " · recommended" : ""}${o.guided_only ? " · guide only" : ""}`,
      `${metricMeta[x].label}: ${formatMetric(x, metricValue(o, x))}`,
      `${metricMeta[y].label}: ${formatMetric(y, metricValue(o, y))}`,
      ...(band ? [`${likelyText(y, band)} (10th–90th percentile of ${formatValue(samples ?? 0, "count")} sampled futures)`] : []),
      ...(same.length ? [`Same result as ${escapeHtml(same.map((m) => m.title).join(", "))}`] : []),
    ].join("<br/>");
  };
  const frontier = paretoFrontier(props.options, x, y);
  // M8: a vertical whisker from p10 to p90 of the y metric at each option's x (robustness bands)
  const whiskers = props.options
    .map((o) => ({ o, band: bandOf(o, y) }))
    .filter((w): w is { o: CompareOption; band: Band } => w.band !== null && Number.isFinite(metricValue(w.o, x)))
    .map(({ o, band }) => [metricValue(o, x), band.p10, band.p90]);
  const whiskerSeries = whiskers.length
    ? [
        {
          type: "custom",
          name: "Likely range (p10–p90)",
          data: whiskers,
          encode: { x: 0, y: [1, 2] },
          silent: true,
          z: 1,
          renderItem: (_params: unknown, api: { value: (i: number) => number; coord: (p: number[]) => number[] }) => {
            const [ax, ay] = api.coord([api.value(0), api.value(1)]) as [number, number];
            const [, by] = api.coord([api.value(0), api.value(2)]) as [number, number];
            const style = { stroke: muted, lineWidth: 1.5, opacity: 0.8 };
            return {
              type: "group",
              children: [
                { type: "line", shape: { x1: ax, y1: ay, x2: ax, y2: by }, style },
                { type: "line", shape: { x1: ax - 4, y1: ay, x2: ax + 4, y2: ay }, style },
                { type: "line", shape: { x1: ax - 4, y1: by, x2: ax + 4, y2: by }, style },
              ],
            };
          },
        },
      ]
    : [];
  return {
    animation: !reducedMotion,
    grid: { left: 12, right: 176, top: 28, bottom: 28, outerBoundsMode: "same", outerBoundsContain: "axisLabel" },
    tooltip: { trigger: "item", confine: true },
    xAxis: {
      type: "value",
      name: `${metricMeta[x].label} (${metricMeta[x].better} is better)`,
      nameLocation: "middle",
      nameGap: 28,
      nameTextStyle: { color: muted, fontSize: 11 },
      scale: true,
      axisLabel: { color: muted, fontSize: 11, formatter: (v: number) => formatMetric(x, v, true) },
      splitLine: { lineStyle: { color: hexColor("border", theme) } },
    },
    yAxis: {
      type: "value",
      name: `${metricMeta[y].label} (${metricMeta[y].better} is better)`,
      nameTextStyle: { color: muted, fontSize: 11, align: "left" },
      scale: true,
      axisLabel: { color: muted, fontSize: 11, formatter: (v: number) => formatMetric(y, v, true) },
      splitLine: { lineStyle: { color: hexColor("border", theme) } },
    },
    series: [
      {
        type: "line",
        name: "Pareto frontier",
        data: frontier.map((o) => [metricValue(o, x), metricValue(o, y)]),
        symbol: "none",
        lineStyle: { color: accent, width: 1.5, type: "dashed", opacity: 0.7 },
        silent: true,
        z: 1,
      },
      {
        type: "scatter",
        name: "Options",
        data: props.options.map(point),
        tooltip: { formatter: (p: { dataIndex: number }) => tooltip(props.options[p.dataIndex]!) },
        z: 2,
      },
      ...whiskerSeries,
    ],
  };
}
