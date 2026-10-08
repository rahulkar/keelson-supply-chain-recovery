/**
 * Stockout timeline model: the heat strip (rows × weeks) and, for a chosen plan option, how
 * each row's first stock-out moves. The chart and "View as table" read the same model.
 */
import type { StockoutRow, StockoutTimelineData, StockoutTimelineProps } from "@keelson/contracts";
import { hexColor, type ThemeName } from "@keelson/tokens";
import { formatValue } from "../format";
import { escapeHtml } from "../lib/html";
import { cellMeta, type CellStatus } from "../lib/status";
import { breachShift, shortWeek, weekLabel, type BreachShift } from "../lib/week";

/** `none` = doing nothing (the baseline the cells show). */
export const NO_ACTION = "none";

export type TimelineCellView = { status: CellStatus; shortage: number } | null;

export type TimelineRowView = {
  row: StockoutRow;
  /** One entry per `props.weeks` (null = no cell for that week). */
  cells: TimelineCellView[];
  baseline: string | null;
  /** First breach under the selected option (equals `baseline` for `none`). */
  withOption: string | null;
  shift: BreachShift;
  totalShortage: number;
};

export type TimelineSummary = {
  rows: number;
  shortRows: number;
  firstBreach: string | null;
  /** Only counted when an option is selected. */
  avoided: number;
  later: number;
  earlier: number;
  /** Rows that had no stock-out without action but run short with the option (e.g. stock taken from them). */
  added: number;
  unchanged: number;
};

/** First breach under an option; an option the row has no entry for keeps the baseline. */
export function optionBreach(row: StockoutRow, optionId: string): string | null {
  const by = row.by_option ?? {};
  return Object.prototype.hasOwnProperty.call(by, optionId) ? (by[optionId] ?? null) : row.first_breach_week;
}

export function buildTimeline(props: StockoutTimelineProps, data: StockoutTimelineData, optionId: string): TimelineRowView[] {
  const index = new Map(props.weeks.map((w, i) => [w, i]));
  return data.rows.map((row) => {
    const cells: TimelineCellView[] = props.weeks.map(() => null);
    let totalShortage = 0;
    for (const c of row.cells) {
      const i = index.get(c.week);
      if (i === undefined) continue;
      cells[i] = { status: c.status, shortage: c.shortage_units };
      totalShortage += c.shortage_units;
    }
    const baseline = row.first_breach_week;
    const withOption = optionId === NO_ACTION ? baseline : optionBreach(row, optionId);
    return { row, cells, baseline, withOption, shift: breachShift(baseline, withOption), totalShortage };
  });
}

export function summarize(views: readonly TimelineRowView[], optionId: string): TimelineSummary {
  const breaches = views
    .map((v) => v.baseline)
    .filter((w): w is string => w !== null)
    .sort();
  const s: TimelineSummary = {
    rows: views.length,
    shortRows: views.filter((v) => v.baseline !== null).length,
    firstBreach: breaches[0] ?? null,
    avoided: 0,
    later: 0,
    earlier: 0,
    added: 0,
    unchanged: 0,
  };
  if (optionId === NO_ACTION) return s;
  for (const v of views) {
    if (v.shift.kind === "avoided") s.avoided += 1;
    else if (v.shift.kind === "later") s.later += 1;
    else if (v.shift.kind === "earlier") s.earlier += 1;
    else if (v.shift.kind === "new") s.added += 1;
    else if (v.shift.kind === "same") s.unchanged += 1;
  }
  return s;
}

export function cellText(cell: TimelineCellView): string {
  if (!cell) return "No data";
  return cell.status === "short" ? `Short ${formatValue(cell.shortage, "units")}` : cellMeta[cell.status].label;
}

export function rowLabel(row: StockoutRow): string {
  return row.label || `${row.style_code} · ${row.dc_code} / ${row.cluster_code}`;
}

const CELL_CODE: Record<CellStatus, number> = { ok: 0, tight: 1, short: 2 };
const CODE_CELL: CellStatus[] = ["ok", "tight", "short"];

export const ROW_HEIGHT = 26;

export function chartHeight(rows: number): number {
  return Math.max(120, rows * ROW_HEIGHT + 56);
}

/** Minimal slice of ECharts' custom-series API used here. */
export type CustomApi = {
  value: (dim: number) => number;
  coord: (point: [number, number]) => [number, number];
  size: (dataSize: [number, number]) => [number, number];
};

/**
 * ECharts option for the heat strip: one custom series of cells (status colour), markers for
 * the first stock-out without action (ring) and with the selected option (diamond), and a
 * dashed line between them that shows the breach shift (to the window's end when avoided).
 */
export function timelineChartOption(
  weeks: readonly string[],
  views: readonly TimelineRowView[],
  optionTitle: string | null,
  theme: ThemeName,
  reducedMotion: boolean,
): Record<string, unknown> {
  const color = (s: CellStatus) => hexColor(cellMeta[s].color, theme);
  const weekIndex = new Map(weeks.map((w, i) => [w, i]));
  const labels = views.map((v) => rowLabel(v.row));
  const safe = labels.map(escapeHtml);
  const safeOption = optionTitle === null ? "" : escapeHtml(optionTitle);
  const cells: [number, number, number, number][] = [];
  views.forEach((v, r) =>
    v.cells.forEach((c, w) => {
      if (c) cells.push([w, r, CELL_CODE[c.status], c.shortage]);
    }),
  );
  const baseline: [number, number][] = [];
  const option: [number, number][] = [];
  const shifts: [number, number, number, number][] = [];
  views.forEach((v, r) => {
    const b = v.baseline !== null ? weekIndex.get(v.baseline) : undefined;
    if (b !== undefined) baseline.push([b, r]);
    if (optionTitle === null) return;
    const o = v.withOption !== null ? weekIndex.get(v.withOption) : undefined;
    if (o !== undefined) option.push([o, r]);
    if (b !== undefined && v.shift.kind !== "same" && v.shift.kind !== "none") {
      // [from, to, row, runs to the window's end (avoided / beyond the window)]
      shifts.push(o !== undefined ? [b, o, r, 0] : [b, weeks.length - 1, r, 1]);
    }
  });
  const fg = hexColor("fg", theme);
  const accent = hexColor("accent", theme);
  const series: Record<string, unknown>[] = [
    {
      type: "custom",
      name: "Stock",
      data: cells,
      encode: { x: 0, y: 1 },
      renderItem: (_params: unknown, api: CustomApi) => {
        const center = api.coord([api.value(0), api.value(1)]);
        const size = api.size([1, 1]);
        const status = CODE_CELL[api.value(2)] ?? "ok";
        return {
          type: "rect",
          shape: { x: center[0] - size[0] / 2 + 1, y: center[1] - size[1] / 2 + 2, width: size[0] - 2, height: size[1] - 4, r: 3 },
          style: { fill: color(status) },
        };
      },
      tooltip: {
        formatter: (p: { value: [number, number, number, number] }) => {
          const [w, r, code, shortage] = p.value;
          const status = CODE_CELL[code] ?? "ok";
          const what = status === "short" ? `short ${formatValue(shortage, "units")}` : cellMeta[status].label.toLowerCase();
          return `${safe[r] ?? ""}<br/>${weekLabel(weeks[w] ?? "")}: ${what}`;
        },
      },
      z: 1,
    },
    {
      type: "scatter",
      name: "First stock-out without action",
      data: baseline,
      symbol: "circle",
      symbolSize: 9,
      itemStyle: { color: "transparent", borderColor: fg, borderWidth: 2 },
      tooltip: {
        formatter: (p: { value: [number, number] }) =>
          `${safe[p.value[1]] ?? ""}<br/>First stock-out without action: ${weekLabel(weeks[p.value[0]] ?? "")}`,
      },
      z: 3,
    },
  ];
  if (optionTitle !== null) {
    series.push(
      {
        type: "custom",
        name: "Breach shift",
        data: shifts,
        renderItem: (_params: unknown, api: CustomApi) => {
          const a = api.coord([api.value(0), api.value(2)]);
          const b = api.coord([api.value(1), api.value(2)]);
          // Avoided: the line runs to the right edge of the last week (inside the grid).
          const x2 = api.value(3) === 1 ? b[0] + api.size([1, 1])[0] / 2 - 2 : b[0];
          return { type: "line", shape: { x1: a[0], y1: a[1], x2, y2: b[1] }, style: { stroke: accent, lineWidth: 2, lineDash: [4, 3] } };
        },
        silent: true,
        z: 2,
      },
      {
        type: "scatter",
        name: `First stock-out with ${optionTitle}`,
        data: option,
        symbol: "diamond",
        symbolSize: 14,
        itemStyle: { color: accent, opacity: 1, borderColor: hexColor("surface", theme), borderWidth: 1.5 },
        tooltip: {
          formatter: (p: { value: [number, number] }) =>
            `${safe[p.value[1]] ?? ""}<br/>First stock-out with ${safeOption}: ${weekLabel(weeks[p.value[0]] ?? "")}`,
        },
        z: 4,
      },
    );
  }
  return {
    animation: !reducedMotion,
    grid: { left: 8, right: 16, top: 8, bottom: 8, outerBoundsMode: "same", outerBoundsContain: "axisLabel" },
    tooltip: { trigger: "item", confine: true },
    xAxis: {
      type: "category",
      position: "top",
      data: weeks.map(shortWeek),
      axisTick: { show: false },
      axisLine: { show: false },
      axisLabel: { color: hexColor("fg-muted", theme), fontSize: 11 },
      splitLine: { show: false },
    },
    yAxis: {
      type: "category",
      inverse: true,
      data: labels,
      axisTick: { show: false },
      axisLine: { show: false },
      axisLabel: { color: hexColor("fg", theme), fontSize: 11, width: 180, overflow: "truncate" },
    },
    series,
  };
}
