"use client";

import { chartPalette, hexColor } from "@keelson/tokens";
import { useEffect, useId, useRef } from "react";
import { formatValue, type FormatOptions, type Unit } from "../format";
import { useDocumentTheme } from "../lib/theme";

export type BarDatum = { label: string; value: number };

export type BarChartProps = {
  data: readonly BarDatum[];
  /** Names the chart (visible caption + accessible name of the data table). */
  caption: string;
  unit: Unit | (string & {});
  /** Passed to `formatValue` (e.g. `{ exactCents: true }` for small euro amounts). */
  formatOptions?: Omit<FormatOptions, "compact"> | undefined;
  /** Chart height in px (default 140, a "small" chart). */
  height?: number | undefined;
  className?: string | undefined;
};

/**
 * Small ECharts bar chart (SVG renderer, loaded on demand). The chart itself is decorative for
 * assistive tech: the same numbers are in a visually hidden table, so screen-reader and keyboard
 * users get every value; colours come from `@keelson/tokens` (light / dark).
 */
export function BarChart({ data, caption, unit, formatOptions, height = 140, className }: BarChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const tableId = `bar-${useId()}`;
  const key = JSON.stringify({ data, formatOptions: formatOptions ?? {} });
  const theme = useDocumentTheme();

  useEffect(() => {
    const el = host.current;
    if (!el || typeof window === "undefined") return;
    let disposed = false;
    let cleanup: (() => void) | undefined;
    void (async () => {
      try {
        const [core, charts, components, renderers] = await Promise.all([
          import("echarts/core"),
          import("echarts/charts"),
          import("echarts/components"),
          import("echarts/renderers"),
        ]);
        if (disposed) return;
        core.use([charts.BarChart, components.GridComponent, components.TooltipComponent, renderers.SVGRenderer]);
        const chart = core.init(el, undefined, { renderer: "svg" });
        const parsed = JSON.parse(key) as { data: BarDatum[]; formatOptions: FormatOptions };
        const points = parsed.data;
        const fmt = (v: number) => formatValue(v, unit, parsed.formatOptions);
        chart.setOption({
          animation: !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches,
          grid: { left: 8, right: 8, top: 12, bottom: 4, outerBoundsMode: "same", outerBoundsContain: "axisLabel" },
          tooltip: {
            trigger: "axis",
            axisPointer: { type: "shadow" },
            valueFormatter: (v: unknown) => (typeof v === "number" ? fmt(v) : String(v)),
          },
          xAxis: {
            type: "category",
            data: points.map((p) => p.label),
            axisLine: { lineStyle: { color: hexColor("border-strong", theme) } },
            axisTick: { show: false },
            axisLabel: { color: hexColor("fg-muted", theme), fontSize: 11 },
          },
          yAxis: {
            type: "value",
            splitNumber: 3,
            splitLine: { lineStyle: { color: hexColor("border", theme) } },
            axisLabel: { color: hexColor("fg-subtle", theme), fontSize: 11, formatter: (v: number) => formatValue(v, unit, { compact: true }) },
          },
          series: [
            {
              type: "bar",
              data: points.map((p) => p.value),
              barMaxWidth: 24,
              itemStyle: { color: chartPalette(theme)[0], borderRadius: [3, 3, 0, 0] },
            },
          ],
        });
        const ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => chart.resize()) : null;
        ro?.observe(el);
        cleanup = () => {
          ro?.disconnect();
          chart.dispose();
        };
      } catch {
        // No canvas/SVG support (tests, very old browsers): the data table still carries the numbers.
      }
    })();
    return () => {
      disposed = true;
      cleanup?.();
    };
  }, [key, unit, theme]);

  return (
    <figure className={className} aria-labelledby={`${tableId}-cap`}>
      <figcaption id={`${tableId}-cap`} className="mb-1 text-caption font-medium text-fg-muted">
        {caption}
      </figcaption>
      <div ref={host} aria-hidden="true" style={{ height }} className="w-full" data-chart="bar" data-chart-theme={theme} />
      <table id={tableId} className="sr-only">
        <caption>{caption}</caption>
        <thead>
          <tr>
            <th scope="col">Day</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {data.map((d) => (
            <tr key={d.label}>
              <th scope="row">{d.label}</th>
              <td>{formatValue(d.value, unit, formatOptions)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  );
}
