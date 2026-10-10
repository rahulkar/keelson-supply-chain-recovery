"use client";

import type { CompareOption, ScenarioCompareProps } from "@keelson/contracts";
import { Badge, Table, usePrefersReducedMotion, type TableColumn } from "@keelson/ui";
import { ChartScatter, Hand, Star } from "lucide-react";
import { useMemo } from "react";
import { formatValue } from "../format";
import { EChart } from "../lib/EChart";
import { useDocumentTheme } from "../lib/theme";
import {
  axesOf,
  compareChartOption,
  formatMetric,
  leverLabel,
  metricMeta,
  optionTag,
  SCORECARD_COLUMNS,
  scorecardColumns,
  type ScorecardMetric,
} from "../compare/model";
import { integerNote } from "../compare/integer";
import type { BlockComponentProps } from "../types";
import { AdvisoryBadge, AdvisoryNote } from "./Advisory";
import { BlockFrame } from "./BlockFrame";

function costBreakdown(o: CompareOption): string | null {
  const b = o.scorecard.cost_breakdown;
  const parts = [
    b.transfer ? `transfers ${formatValue(b.transfer, "eur_cents")}` : null,
    b.expedite_premium ? `expedite ${formatValue(b.expedite_premium, "eur_cents")}` : null,
    b.nearshore_premium ? `near-shore ${formatValue(b.nearshore_premium, "eur_cents")}` : null,
  ].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

/** Scorecard of every option (the same numbers the scatter plots). */
export function ScorecardTable({
  options,
  metrics = SCORECARD_COLUMNS,
}: {
  options: readonly CompareOption[];
  /** Score columns (default {@link SCORECARD_COLUMNS}; the block adds its plotted axes). */
  metrics?: readonly ScorecardMetric[] | undefined;
}) {
  const columns: TableColumn<CompareOption>[] = [
    {
      id: "option",
      header: "Option",
      rowHeader: true,
      className: "min-w-56",
      cell: (o) => (
        <span className="flex flex-col gap-1">
          <span className="flex flex-wrap items-center gap-1.5">
            {optionTag(o) !== o.title ? (
              <span className="inline-flex min-w-5 justify-center rounded-sm bg-surface-3 px-1 font-mono text-caption text-fg-muted" title={`Option ${optionTag(o)}`}>
                {optionTag(o)}
              </span>
            ) : null}
            <span>{o.title}</span>
            {o.recommended ? (
              <Badge size="sm" tone="accent" variant="solid" icon={<Star />}>
                Recommended
              </Badge>
            ) : null}
            {o.guided_only ? (
              <Badge size="sm" tone="warning" icon={<Hand />}>
                Guide only
              </Badge>
            ) : null}
            {o.pareto ? (
              <Badge size="sm" tone="neutral" variant="outline" title="No other option is better on both compared measures">
                Best trade-off
              </Badge>
            ) : null}
          </span>
          {o.guided_only && o.guided_reason ? <span className="text-caption font-normal text-fg-muted">{o.guided_reason}</span> : null}
          <span className="text-caption font-normal text-fg-subtle">
            {o.levers.length ? o.levers.map(leverLabel).join(" + ") : "No changes"}
          </span>
          {o.integer ? (
            <span className="text-caption font-normal text-fg-subtle" data-integer-method={o.integer.method}>
              {integerNote(o.integer)}
            </span>
          ) : null}
        </span>
      ),
    },
    ...metrics.map<TableColumn<CompareOption>>((m) => ({
      id: m,
      header: metricMeta[m].label,
      align: "end",
      nowrap: m !== "incremental_cost_cents",
      cell: (o) =>
        m === "incremental_cost_cents" ? (
          <span className="flex flex-col items-end">
            <span className="whitespace-nowrap">{formatMetric(m, o.scorecard[m])}</span>
            {costBreakdown(o) ? <span className="text-caption text-fg-subtle">{costBreakdown(o)}</span> : null}
          </span>
        ) : (
          formatMetric(m, o.scorecard[m])
        ),
    })),
  ];
  return (
    <Table
      caption="Scorecard by option"
      columns={columns}
      rows={options}
      rowKey={(o) => o.id}
      rowTone={(o) => (o.recommended ? "accent" : undefined)}
      rowProps={(o) => ({ "data-option-id": o.id, "data-recommended": o.recommended ? "true" : undefined })}
    />
  );
}

/**
 * `scenario_compare@1` (canvas) — every candidate plan on a Pareto scatter (by default extra
 * cost vs lost units; recommended = diamond, guide-only = dashed ring, frontier = dashed line)
 * plus the full scorecard table with the recommended and guide-only markers and their reasons
 * (M8: and, for CP-SAT plans, how each option's whole units were found).
 */
export function ScenarioCompare({ props, envelope }: BlockComponentProps<ScenarioCompareProps>) {
  const theme = useDocumentTheme();
  const reducedMotion = usePrefersReducedMotion();
  const { x, y } = axesOf(props);
  const metrics = useMemo(() => scorecardColumns(props), [props]);
  const option = useMemo(() => compareChartOption(props, theme, reducedMotion), [props, theme, reducedMotion]);
  const recommended = props.options.find((o) => o.recommended);
  const guided = props.options.filter((o) => o.guided_only).length;

  return (
    <BlockFrame envelope={envelope} defaultTitle="Compare options" icon={<ChartScatter />} actions={<AdvisoryBadge advisory={props.advisory} />}>
      <AdvisoryNote advisory={props.advisory} />
      <p className="text-ui text-fg-muted">
        {formatValue(props.options.length, "count")} {props.options.length === 1 ? "option" : "options"} compared on{" "}
        {metricMeta[x].label.toLowerCase()} and {metricMeta[y].label.toLowerCase()}
        {recommended ? (
          <>
            {" "}
            · recommended: <span className="font-medium text-fg">{recommended.title}</span>
          </>
        ) : null}
        {guided ? ` · ${formatValue(guided, "count")} can only be guided` : ""}.
      </p>
      <figure className="flex flex-col gap-1" aria-label={`${metricMeta[x].label} against ${metricMeta[y].label} by option`}>
        <EChart option={option} height={300} kind="scenario-compare" />
        <figcaption className="text-caption text-fg-subtle">
          Each point is an option: ◆ recommended, dashed ring = guide only (can&apos;t be applied automatically); the dashed line
          joins the best trade-offs. The table below has every number.
        </figcaption>
      </figure>
      <ScorecardTable options={props.options} metrics={metrics} />
    </BlockFrame>
  );
}

export default ScenarioCompare;
