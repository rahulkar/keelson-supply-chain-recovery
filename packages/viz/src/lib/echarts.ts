/**
 * ECharts 6, loaded on demand (modular imports, SVG renderer) so no page pays for it until a
 * chart is shown. Registers every module the Keelson charts use, once.
 */
export type EChartsCore = typeof import("echarts/core");

let loading: Promise<EChartsCore> | null = null;

export function loadECharts(): Promise<EChartsCore> {
  loading ??= (async () => {
    const [core, charts, components, renderers] = await Promise.all([
      import("echarts/core"),
      import("echarts/charts"),
      import("echarts/components"),
      import("echarts/renderers"),
    ]);
    core.use([
      charts.BarChart,
      charts.ScatterChart,
      charts.LineChart,
      charts.CustomChart,
      components.GridComponent,
      components.TooltipComponent,
      renderers.SVGRenderer,
    ]);
    return core;
  })().catch((err: unknown) => {
    loading = null;
    throw err;
  });
  return loading;
}
