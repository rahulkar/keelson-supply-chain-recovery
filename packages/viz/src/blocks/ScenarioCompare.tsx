"use client";

import type { CompareOption, ScenarioCompareProps } from "@keelson/contracts";
import { Badge, Table, usePrefersReducedMotion, type TableColumn } from "@keelson/ui";
import { ChartBarBig, ChartScatter, Hand, Leaf, Star, Trophy } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { formatValue } from "../format";
import { EChart } from "../lib/EChart";
import { ChoiceChips, ViewToggle, type ChoiceChip, type VisualView } from "../lib/controls";
import { useDocumentTheme } from "../lib/theme";
import {
  axesOf,
  bandOf,
  bandSamples,
  compareChartOption,
  formatMetric,
  hasMetric,
  leverLabel,
  likelyText,
  metricMeta,
  metricValue,
  optionTag,
  SCORECARD_COLUMNS,
  scorecardColumns,
  withXAxis,
  type BandedMetric,
  type ScorecardMetric,
} from "../compare/model";
import { hasBands, rangeChartOption, robustnessRows, ROBUSTNESS_METRICS, samplesSentence, type RobustnessRow } from "../compare/robustness";
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

/** M8: rank the options under one weight profile in the scorecard table. */
export type RankBy = { profileId: string; label: string };

function rankOf(o: CompareOption, profileId: string): number | null {
  return o.scores?.find((s) => s.profile_id === profileId)?.rank ?? null;
}

/** A value with its likely range ("likely A–B") and, for screen readers, what the range means. */
function MetricCell({ o, m, samples }: { o: CompareOption; m: ScorecardMetric; samples: number | null }) {
  const band = bandOf(o, m);
  if (!band) return <>{formatMetric(m, metricValue(o, m))}</>;
  return (
    <span className="flex flex-col items-end">
      <span className="whitespace-nowrap">{formatMetric(m, metricValue(o, m))}</span>
      <span className="text-caption whitespace-nowrap text-fg-subtle" data-band={m}>
        {likelyText(m, band)}
        <span className="sr-only">, 10th–90th percentile of {formatValue(samples ?? 0, "count")} sampled futures</span>
      </span>
    </span>
  );
}

/** Scorecard of every option (the same numbers the scatter plots). */
export function ScorecardTable({
  options,
  metrics = SCORECARD_COLUMNS,
  rankBy,
}: {
  options: readonly CompareOption[];
  /** Score columns (default {@link SCORECARD_COLUMNS}; the block adds its plotted axes). */
  metrics?: readonly ScorecardMetric[] | undefined;
  /** M8: each option's rank under this weight profile, with a "Top under …" badge on rank 1. */
  rankBy?: RankBy | null | undefined;
}) {
  const samples = bandSamples(options);
  const rankColumn: TableColumn<CompareOption>[] = rankBy
    ? [
        {
          id: "rank",
          header: `Rank (${rankBy.label})`,
          align: "end",
          nowrap: true,
          cell: (o) => {
            const r = rankOf(o, rankBy.profileId);
            return r === null ? "—" : formatValue(r, "count");
          },
        },
      ]
    : [];
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
            {rankBy && rankOf(o, rankBy.profileId) === 1 ? (
              <Badge size="sm" tone="success" variant="outline" icon={<Trophy />}>
                Top under {rankBy.label}
              </Badge>
            ) : null}
          </span>
          {o.guided_only && o.guided_reason ? <span className="text-caption font-normal text-fg-muted">{o.guided_reason}</span> : null}
          <span className="text-caption font-normal text-fg-subtle">
            {o.levers.length ? o.levers.map(leverLabel).join(" + ") : "No changes"}
          </span>
        </span>
      ),
    },
    ...rankColumn,
    ...metrics.map<TableColumn<CompareOption>>((m) => ({
      id: m,
      header: metricMeta[m].label,
      align: "end",
      nowrap: m !== "incremental_cost_cents",
      cell: (o) =>
        m === "incremental_cost_cents" ? (
          <span className="flex flex-col items-end">
            <span className="whitespace-nowrap">{formatMetric(m, metricValue(o, m))}</span>
            {costBreakdown(o) ? <span className="text-caption text-fg-subtle">{costBreakdown(o)}</span> : null}
          </span>
        ) : (
          <MetricCell o={o} m={m} samples={samples} />
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

/** "How sure are we?" — p10 / median / p90 of one measure per option over the same sampled futures (M8). */
export function RobustnessPanel({ props }: { props: ScenarioCompareProps }) {
  const theme = useDocumentTheme();
  const reducedMotion = usePrefersReducedMotion();
  const [metric, setMetric] = useState<BandedMetric>("lost_margin_cents");
  const [view, setView] = useState<VisualView>("visual");
  const regionId = useId();
  const rows = useMemo(() => robustnessRows(props.options, metric), [props.options, metric]);
  const option = useMemo(() => rangeChartOption(rows, metric, theme, reducedMotion), [rows, metric, theme, reducedMotion]);
  const samples = bandSamples(props.options);
  const sentence = samplesSentence(props) ?? `${formatValue(samples ?? 0, "count")} sampled futures`;
  const showWins = rows.some((r) => r.winRateBp !== null);
  const columns: TableColumn<RobustnessRow>[] = [
    { id: "option", header: "Option", rowHeader: true, cell: (r) => r.title },
    { id: "p10", header: "10th percentile", align: "end", nowrap: true, cell: (r) => formatMetric(metric, r.band.p10) },
    { id: "p50", header: "Median", align: "end", nowrap: true, cell: (r) => formatMetric(metric, r.band.p50) },
    { id: "p90", header: "90th percentile", align: "end", nowrap: true, cell: (r) => formatMetric(metric, r.band.p90) },
    { id: "nominal", header: "Planned", align: "end", nowrap: true, cell: (r) => formatMetric(metric, r.nominal) },
    { id: "stockout", header: "Chance of a stock-out", align: "end", nowrap: true, cell: (r) => formatValue(r.stockoutChanceBp, "pct_bp") },
  ];
  if (showWins) {
    columns.push({
      id: "wins",
      header: "Best in % of futures",
      align: "end",
      nowrap: true,
      cell: (r) => (r.winRateBp === null ? "—" : formatValue(r.winRateBp, "pct_bp")),
    });
  }
  const choices: ChoiceChip[] = ROBUSTNESS_METRICS.map((m) => ({ id: m, label: metricMeta[m].label }));
  return (
    <section aria-labelledby={`${regionId}-h`} className="flex flex-col gap-2" data-robustness="">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h3 id={`${regionId}-h`} className="text-ui font-semibold text-fg">
          How sure are we?
        </h3>
        <ViewToggle view={view} onChange={setView} visualLabel="chart" visualIcon={<ChartBarBig aria-hidden="true" />} controls={regionId} />
      </div>
      <p className="text-caption text-fg-muted">
        Each plan was re-run in {sentence} — the same futures for every option. Bars span the 10th to 90th percentile, the tick is
        the median and ◆ the planned value.
      </p>
      <ChoiceChips label="Measure" choices={choices} value={metric} onChange={(id) => setMetric(id as BandedMetric)} />
      <div id={regionId}>
        {view === "visual" ? (
          <figure className="flex flex-col gap-1" aria-label={`Likely range of ${metricMeta[metric].label.toLowerCase()} by option`}>
            <EChart option={option} height={Math.max(140, 44 * rows.length + 48)} kind="robustness-range" />
            <figcaption className="text-caption text-fg-subtle">
              {metricMeta[metric].label}: {metricMeta[metric].better} is better. The table has every number.
            </figcaption>
          </figure>
        ) : (
          <Table caption={`Likely range of ${metricMeta[metric].label.toLowerCase()}`} columns={columns} rows={rows} rowKey={(r) => r.id} />
        )}
      </div>
    </section>
  );
}

function EmissionsFootnote({ props }: { props: ScenarioCompareProps }) {
  const e = props.emissions;
  if (!e) return null;
  return (
    <p className="flex items-start gap-1.5 text-caption text-fg-subtle" data-emissions-note="">
      <Leaf aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />
      <span>
        Extra CO₂e: transport only ({e.basis === "WTW" ? "well-to-wheel" : "tank-to-wheel"}), factors from {e.source_label}
        {e.indicative ? " (indicative figures)" : ""}; unit weights are assumptions and distances follow the map&apos;s routes.
        {e.coverage_bp < 10_000 ? ` Covers ${formatValue(e.coverage_bp, "pct_bp")} of the moved units.` : ""}
      </span>
    </p>
  );
}

/**
 * `scenario_compare@1` (canvas) — every candidate plan on a Pareto scatter (by default extra
 * cost vs lost units; recommended = diamond, guide-only = dashed ring, frontier = dashed line)
 * plus the full scorecard table with the recommended and guide-only markers and their reasons.
 *
 * M8: an "Extra cost | Extra CO₂e" x-axis switch (frontier recomputed client-side; the CO₂e chip
 * is disabled with a reason while an option has none), likely ranges (p10–p90) as whiskers and
 * table captions, "Rank by" weight-profile chips (ranking only: the recommendation follows the
 * default profile), the "How sure are we?" panel and the CO₂e footnote.
 */
export function ScenarioCompare({ props, envelope }: BlockComponentProps<ScenarioCompareProps>) {
  const theme = useDocumentTheme();
  const reducedMotion = usePrefersReducedMotion();
  const server = axesOf(props);
  const baseX: ScorecardMetric = server.x === "incremental_co2e_kg" ? "incremental_cost_cents" : server.x;
  const missingCo2 = props.options.filter((o) => !Number.isFinite(metricValue(o, "incremental_co2e_kg"))).map((o) => o.title);
  const anyCo2 = missingCo2.length < props.options.length;
  const co2Ready = hasMetric(props.options, "incremental_co2e_kg");
  const [x, setX] = useState<ScorecardMetric>(server.x);
  const effectiveX: ScorecardMetric = x === "incremental_co2e_kg" && !co2Ready ? baseX : x;
  const shown = useMemo(() => withXAxis(props, effectiveX), [props, effectiveX]);
  const { y } = server;
  const metrics = useMemo(() => scorecardColumns(shown), [shown]);
  const option = useMemo(() => compareChartOption(shown, theme, reducedMotion), [shown, theme, reducedMotion]);
  const profiles = props.profiles ?? [];
  const defaultProfile = profiles.find((p) => p.default) ?? profiles[0];
  const [profileId, setProfileId] = useState<string | null>(defaultProfile?.id ?? null);
  const rankBy = profiles.find((p) => p.id === profileId);
  const recommended = props.options.find((o) => o.recommended);
  const guided = props.options.filter((o) => o.guided_only).length;
  const banded = hasBands(props);
  const xChoices: ChoiceChip[] = [
    { id: baseX, label: metricMeta[baseX].label },
    {
      id: "incremental_co2e_kg",
      label: metricMeta.incremental_co2e_kg.label,
      disabled: !co2Ready,
      title: co2Ready ? undefined : `No CO₂e for ${missingCo2.join(", ")}`,
    },
  ];

  return (
    <BlockFrame envelope={envelope} defaultTitle="Compare options" icon={<ChartScatter />} actions={<AdvisoryBadge advisory={props.advisory} />}>
      <AdvisoryNote advisory={props.advisory} />
      <p className="text-ui text-fg-muted">
        {formatValue(props.options.length, "count")} {props.options.length === 1 ? "option" : "options"} compared on{" "}
        {metricMeta[effectiveX].label.toLowerCase()} and {metricMeta[y].label.toLowerCase()}
        {recommended ? (
          <>
            {" "}
            · recommended: <span className="font-medium text-fg">{recommended.title}</span>
          </>
        ) : null}
        {guided ? ` · ${formatValue(guided, "count")} can only be guided` : ""}.
      </p>
      {anyCo2 ? (
        <div className="flex flex-col gap-1">
          <ChoiceChips label="Compare against" choices={xChoices} value={effectiveX} onChange={(id) => setX(id as ScorecardMetric)} />
          {!co2Ready ? (
            <p className="text-caption text-fg-subtle" data-co2-disabled="">
              Extra CO₂e can&apos;t be compared: it is missing for {missingCo2.join(", ")}.
            </p>
          ) : null}
        </div>
      ) : null}
      <figure className="flex flex-col gap-1" aria-label={`${metricMeta[effectiveX].label} against ${metricMeta[y].label} by option`}>
        <EChart option={option} height={300} kind="scenario-compare" />
        <figcaption className="text-caption text-fg-subtle">
          Each point is an option: ◆ recommended, dashed ring = guide only (can&apos;t be applied automatically); the dashed line
          joins the best trade-offs{banded ? "; whiskers show the likely range (10th–90th percentile of the sampled futures)" : ""}.
          The table below has every number.
        </figcaption>
      </figure>
      {profiles.length > 1 ? (
        <div className="flex flex-col gap-1" data-profiles="">
          <ChoiceChips
            label="Rank by"
            choices={profiles.map((p) => ({ id: p.id, label: p.label, title: p.default ? "The workspace default" : undefined }))}
            value={profileId ?? ""}
            onChange={setProfileId}
          />
          <p className="text-caption text-fg-subtle">
            Ranking only: the recommendation follows the workspace default{defaultProfile ? ` (${defaultProfile.label})` : ""}.
          </p>
        </div>
      ) : null}
      <ScorecardTable options={shown.options} metrics={metrics} rankBy={rankBy ? { profileId: rankBy.id, label: rankBy.label } : null} />
      {banded ? <RobustnessPanel props={props} /> : null}
      <EmissionsFootnote props={props} />
    </BlockFrame>
  );
}

export default ScenarioCompare;
