/**
 * "How sure are we?" — the robustness view of `scenario_compare@1` (M8,
 * `docs/contracts/126-m8-co2-robustness.md` §5): per option the p10 / p50 / p90 of a metric over
 * the same sampled futures, the planned (nominal) value, the chance of a stock-out and how often the
 * option is the best one. Every number comes from the props (computed by the engine and the
 * optimizer); this module only arranges them for the range chart and its table.
 */
import type { CompareOption, ScenarioCompareProps } from "@keelson/contracts";
import { hexColor, type ThemeName } from "@keelson/tokens";
import { formatValue } from "../format";
import { escapeHtml } from "../lib/html";
import { bandOf, formatMetric, metricMeta, metricValue, type Band, type BandedMetric } from "./model";

/** Metrics the panel can show (chips, in this order). */
export const ROBUSTNESS_METRICS: readonly BandedMetric[] = ["lost_margin_cents", "fill_rate_bp", "net_benefit_cents"];

export const SAMPLED_LABELS: Record<string, string> = {
  disruption_duration: "how long the disruption lasts",
  disruption_severity: "how severe it is",
  lead_time: "transit times",
  demand: "demand",
};

export type RobustnessRow = {
  id: string;
  title: string;
  recommended: boolean;
  band: Band;
  nominal: number;
  stockoutChanceBp: number;
  winRateBp: number | null;
};

/** Options that carry bands, with the band of `metric` (props order). */
export function robustnessRows(options: readonly CompareOption[], metric: BandedMetric): RobustnessRow[] {
  const rows: RobustnessRow[] = [];
  for (const o of options) {
    const band = bandOf(o, metric);
    const bands = o.scorecard.bands;
    if (!band || !bands) continue;
    rows.push({
      id: o.id,
      title: o.title,
      recommended: o.recommended,
      band,
      nominal: metricValue(o, metric),
      stockoutChanceBp: bands.stockout_chance_bp,
      winRateBp: o.win_rate_bp ?? null,
    });
  }
  return rows;
}

export function hasBands(props: ScenarioCompareProps): boolean {
  return props.options.some((o) => o.scorecard.bands != null);
}

/** "the 10th–90th percentile of 64 sampled futures (how long the disruption lasts, transit times, demand)". */
export function samplesSentence(props: ScenarioCompareProps): string | null {
  const u = props.uncertainty;
  if (!u) return null;
  const what = u.sampled.map((s) => SAMPLED_LABELS[s] ?? s.replace(/_/g, " "));
  return `${formatValue(u.samples, "count")} sampled futures, varying ${what.join(", ")}`;
}

/**
 * Range chart: one row per option (category axis, props order top-down), a bar from p10 to p90, a
 * tick at the median and a diamond at the planned value.
 */
export function rangeChartOption(
  rows: readonly RobustnessRow[],
  metric: BandedMetric,
  theme: ThemeName,
  reducedMotion: boolean,
): Record<string, unknown> {
  const accent = hexColor("accent", theme);
  const muted = hexColor("fg-muted", theme);
  const fg = hexColor("fg", theme);
  const bar = hexColor("chart-2", theme);
  const categories = rows.map((r) => r.title);
  return {
    animation: !reducedMotion,
    grid: { left: 12, right: 24, top: 12, bottom: 28, outerBoundsMode: "same", outerBoundsContain: "axisLabel" },
    tooltip: {
      trigger: "item",
      confine: true,
      formatter: (p: { dataIndex: number }) => {
        const r = rows[p.dataIndex];
        if (!r) return "";
        return [
          `<strong>${escapeHtml(r.title)}</strong>`,
          `10th percentile: ${formatMetric(metric, r.band.p10)}`,
          `Median: ${formatMetric(metric, r.band.p50)}`,
          `90th percentile: ${formatMetric(metric, r.band.p90)}`,
          `Planned: ${formatMetric(metric, r.nominal)}`,
        ].join("<br/>");
      },
    },
    xAxis: {
      type: "value",
      scale: true,
      name: `${metricMeta[metric].label} (${metricMeta[metric].better} is better)`,
      nameLocation: "middle",
      nameGap: 26,
      nameTextStyle: { color: muted, fontSize: 11 },
      axisLabel: { color: muted, fontSize: 11, formatter: (v: number) => formatMetric(metric, v, true) },
      splitLine: { lineStyle: { color: hexColor("border", theme) } },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: categories,
      axisLabel: { color: fg, fontSize: 11, width: 150, overflow: "truncate" },
    },
    series: [
      {
        type: "custom",
        name: "Likely range",
        data: rows.map((r, i) => [i, r.band.p10, r.band.p50, r.band.p90]),
        encode: { y: 0, x: [1, 2, 3] },
        renderItem: (_params: unknown, api: { value: (i: number) => number; coord: (p: number[]) => number[]; size: (p: number[]) => number[] }) => {
          const row = api.value(0);
          const [x10, cy] = api.coord([api.value(1), row]) as [number, number];
          const [x50] = api.coord([api.value(2), row]) as [number, number];
          const [x90] = api.coord([api.value(3), row]) as [number, number];
          const h = Math.max(6, Math.min(14, (api.size([0, 1])[1] ?? 20) * 0.4));
          const recommended = rows[row]?.recommended ?? false;
          return {
            type: "group",
            children: [
              {
                type: "rect",
                shape: { x: x10, y: cy - h / 2, width: Math.max(1, x90 - x10), height: h, r: 3 },
                style: { fill: recommended ? accent : bar, opacity: 0.35 },
              },
              { type: "line", shape: { x1: x50, y1: cy - h / 2 - 2, x2: x50, y2: cy + h / 2 + 2 }, style: { stroke: fg, lineWidth: 2 } },
            ],
          };
        },
      },
      {
        type: "scatter",
        name: "Planned",
        symbol: "diamond",
        symbolSize: 10,
        data: rows.map((r, i) => ({ value: [r.nominal, i], itemStyle: { color: r.recommended ? accent : muted } })),
        encode: { x: 0, y: 1 },
        z: 3,
      },
    ],
  };
}
