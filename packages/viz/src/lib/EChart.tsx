"use client";

import { useEffect, useRef } from "react";
import { loadECharts } from "./echarts";

type Instance = { setOption: (o: Record<string, unknown>, notMerge?: boolean) => void; resize: () => void; dispose: () => void };

/**
 * ECharts host (SVG renderer, loaded on demand). Decorative for assistive tech (`aria-hidden`):
 * every chart in this package puts the same numbers in a table next to it. `option` must be
 * memoised by the caller; a new object re-applies it (not merged).
 */
export function EChart({
  option,
  height,
  kind,
  className,
}: {
  option: Record<string, unknown>;
  height: number;
  /** `data-chart` value (tests, e2e). */
  kind: string;
  className?: string | undefined;
}) {
  const host = useRef<HTMLDivElement>(null);
  const chart = useRef<Instance | null>(null);
  const latest = useRef(option);

  useEffect(() => {
    latest.current = option;
    try {
      chart.current?.setOption(option, true);
    } catch {
      // A chart that cannot draw (no text metrics, …) must not break the block; the table remains.
    }
  }, [option]);

  useEffect(() => {
    const el = host.current;
    if (!el || typeof window === "undefined") return;
    let disposed = false;
    let ro: ResizeObserver | null = null;
    void loadECharts()
      .then((core) => {
        if (disposed) return;
        const instance = core.init(el, undefined, { renderer: "svg" }) as unknown as Instance;
        chart.current = instance;
        instance.setOption(latest.current, true);
        ro = typeof ResizeObserver !== "undefined" ? new ResizeObserver(() => instance.resize()) : null;
        ro?.observe(el);
      })
      .catch(() => {
        // No SVG/canvas support (tests, very old browsers): the table still carries the numbers.
      });
    return () => {
      disposed = true;
      ro?.disconnect();
      chart.current?.dispose();
      chart.current = null;
    };
  }, []);

  useEffect(() => {
    try {
      chart.current?.resize();
    } catch {
      // see above
    }
  }, [height]);

  return <div ref={host} aria-hidden="true" data-chart={kind} style={{ height }} className={className ?? "w-full"} />;
}
