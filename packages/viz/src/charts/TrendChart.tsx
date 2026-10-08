"use client";

import { chartPalette, hexColor } from "@keelson/tokens";
import { useEffect, useId, useRef } from "react";
import { useDocumentTheme } from "../lib/theme";

/** One point of a trend; `null` = not measured in that run (a gap in the line). */
export type TrendPoint = { label: string; value: number | null };

export type TrendChartProps = {
  data: readonly TrendPoint[];
  /** Names the chart (visible caption + accessible name of the data table). */
  caption: string;
  /** Formats a value for the axis, the tooltip and the accessible table (default: the number). */
  format?: ((value: number) => string) | undefined;
  /** A target drawn as a dashed line and listed in the table ("Target ≥ 80 %"). */
  threshold?: { value: number; label: string } | undefined;
  /** Header of the first table column (default "Run"). */
  labelHeader?: string | undefined;
  /** Chart height in px (default 120, a small multiple). */
  height?: number | undefined;
  className?: string | undefined;
};

const plain = (value: number) => new Intl.NumberFormat("en-GB", { maximumFractionDigits: 2 }).format(value);

/**
 * Small ECharts line chart for a metric over runs (SVG renderer, loaded on demand), with an
 * optional target line. Like {@link BarChart} the drawing is decorative for assistive tech: every
 * point (and the target) is in a visually hidden table; colours come from `@keelson/tokens`.
 */
export function TrendChart({ data, caption, format, threshold, labelHeader = "Run", height = 120, className }: TrendChartProps) {
  const host = useRef<HTMLDivElement>(null);
  const tableId = `trend-${useId()}`;
  const fmt = format ?? plain;
  const fmtRef = useRef(fmt);
  useEffect(() => {
    fmtRef.current = fmt;
  });
  const key = JSON.stringify({ data, threshold: threshold ?? null });
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
        core.use([charts.LineChart, components.GridComponent, components.TooltipComponent, components.MarkLineComponent, renderers.SVGRenderer]);
        const chart = core.init(el, undefined, { renderer: "svg" });
        const parsed = JSON.parse(key) as { data: TrendPoint[]; threshold: { value: number; label: string } | null };
        const show = (v: unknown) => (typeof v === "number" ? fmtRef.current(v) : "—");
        const palette = chartPalette(theme);
        chart.setOption({
          animation: !window.matchMedia?.("(prefers-reduced-motion: reduce)").matches,
          grid: { left: 8, right: 12, top: 12, bottom: 4, outerBoundsMode: "same", outerBoundsContain: "axisLabel" },
          tooltip: { trigger: "axis", valueFormatter: show },
          xAxis: {
            type: "category",
            data: parsed.data.map((p) => p.label),
            boundaryGap: false,
            axisLine: { lineStyle: { color: hexColor("border-strong", theme) } },
            axisTick: { show: false },
            axisLabel: { color: hexColor("fg-muted", theme), fontSize: 10, hideOverlap: true },
          },
          yAxis: {
            type: "value",
            splitNumber: 2,
            scale: true,
            splitLine: { lineStyle: { color: hexColor("border", theme) } },
            axisLabel: { color: hexColor("fg-subtle", theme), fontSize: 10, formatter: (v: number) => fmtRef.current(v) },
          },
          series: [
            {
              type: "line",
              data: parsed.data.map((p) => p.value),
              connectNulls: false,
              symbolSize: 5,
              lineStyle: { color: palette[0], width: 2 },
              itemStyle: { color: palette[0] },
              markLine: parsed.threshold
                ? {
                    silent: true,
                    symbol: "none",
                    label: { show: false },
                    lineStyle: { color: hexColor("warning", theme), type: "dashed", width: 1 },
                    data: [{ yAxis: parsed.threshold.value }],
                  }
                : undefined,
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
  }, [key, theme]);

  return (
    <figure className={className} aria-labelledby={`${tableId}-cap`}>
      <figcaption id={`${tableId}-cap`} className="mb-1 text-caption font-medium text-fg-muted">
        {caption}
      </figcaption>
      <div ref={host} aria-hidden="true" style={{ height }} className="w-full" data-chart="trend" data-chart-theme={theme} />
      <table id={tableId} className="sr-only">
        <caption>{caption}</caption>
        <thead>
          <tr>
            <th scope="col">{labelHeader}</th>
            <th scope="col">Value</th>
          </tr>
        </thead>
        <tbody>
          {data.map((d, i) => (
            <tr key={`${d.label}-${i}`}>
              <th scope="row">{d.label}</th>
              <td>{d.value === null ? "Not measured" : fmt(d.value)}</td>
            </tr>
          ))}
        </tbody>
        {threshold ? (
          <tfoot>
            <tr>
              <th scope="row">Target</th>
              <td>{threshold.label}</td>
            </tr>
          </tfoot>
        ) : null}
      </table>
    </figure>
  );
}
