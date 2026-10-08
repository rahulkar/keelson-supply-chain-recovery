"use client";

import type { StockoutTimelineData, StockoutTimelineProps } from "@keelson/contracts";
import { hexColor } from "@keelson/tokens";
import { Badge, cn, usePrefersReducedMotion } from "@keelson/ui";
import { CalendarRange } from "lucide-react";
import { useId, useMemo, useState } from "react";
import { formatValue } from "../format";
import { ChoiceChips, ViewToggle, type VisualView } from "../lib/controls";
import { EChart } from "../lib/EChart";
import { cellMeta, type CellStatus } from "../lib/status";
import { useDocumentTheme } from "../lib/theme";
import { breachShiftText, shortWeek, weekLabel, type BreachShift } from "../lib/week";
import {
  buildTimeline,
  cellText,
  chartHeight,
  NO_ACTION,
  rowLabel,
  summarize,
  timelineChartOption,
  type TimelineRowView,
  type TimelineSummary,
} from "../timeline/model";
import type { BlockComponentProps } from "../types";
import { BlockFrame } from "./BlockFrame";

const shiftTone = (s: BreachShift) =>
  s.kind === "avoided" || s.kind === "later" ? "success" : s.kind === "earlier" || s.kind === "new" ? "danger" : "neutral";

function CellKey({ theme, optionTitle }: { theme: "light" | "dark"; optionTitle: string | null }) {
  const items: CellStatus[] = ["ok", "tight", "short"];
  return (
    <ul aria-label="Legend" className="flex flex-wrap gap-x-4 gap-y-1 text-caption text-fg-muted">
      {items.map((s) => (
        <li key={s} className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block size-3 rounded-xs" style={{ backgroundColor: hexColor(cellMeta[s].color, theme) }} />
          {cellMeta[s].label}
        </li>
      ))}
      <li className="inline-flex items-center gap-1.5">
        <span aria-hidden="true" className="inline-block size-2.5 rounded-pill border-2" style={{ borderColor: hexColor("fg", theme) }} />
        First stock-out without action
      </li>
      {optionTitle ? (
        <li className="inline-flex items-center gap-1.5">
          <span aria-hidden="true" className="inline-block size-2.5 rotate-45" style={{ backgroundColor: hexColor("accent", theme) }} />
          First stock-out with {optionTitle} (dashed line: how far it moves)
        </li>
      ) : null}
    </ul>
  );
}

/** "With <option>: 4 stock-outs avoided, 1 later, 0 earlier, 1 unchanged, 1 new." (new = a row that only runs short with the option). */
export function breachSummaryText(optionTitle: string, s: TimelineSummary): string {
  const n = (v: number) => formatValue(v, "count");
  const added = s.added > 0 ? `, ${n(s.added)} new` : "";
  return `With ${optionTitle}: ${n(s.avoided)} stock-outs avoided, ${n(s.later)} later, ${n(s.earlier)} earlier, ${n(s.unchanged)} unchanged${added}.`;
}

/** The heat strip as a table: one row per DC / cluster / style, one column per week. */
export function TimelineTable({
  weeks,
  views,
  optionTitle,
  hidden,
}: {
  weeks: readonly string[];
  views: readonly TimelineRowView[];
  optionTitle: string | null;
  /** Visually hidden (the chart is shown) but still read by assistive tech. */
  hidden: boolean;
}) {
  const captionId = useId();
  return (
    <div
      role="group"
      aria-labelledby={captionId}
      tabIndex={hidden ? -1 : 0}
      className={cn(
        hidden
          ? "sr-only"
          : "w-full overflow-x-auto rounded-md border border-border bg-surface focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
      )}
      data-timeline-table=""
    >
      <table className="w-full border-collapse text-ui">
        <caption id={captionId} className="px-3 pt-2 pb-1 text-left text-caption font-medium text-fg-muted">
          Stock by week{optionTitle ? `, and the first stock-out with ${optionTitle}` : ""}
        </caption>
        <thead>
          <tr className="border-b border-border bg-surface-2">
            <th scope="col" className="px-3 py-1.5 text-left text-caption font-medium text-fg-muted">
              Style · DC / cluster
            </th>
            {weeks.map((w) => (
              <th key={w} scope="col" className="px-2 py-1.5 text-left text-caption font-medium whitespace-nowrap text-fg-muted">
                <abbr title={weekLabel(w)} className="no-underline">
                  {shortWeek(w)}
                </abbr>
              </th>
            ))}
            <th scope="col" className="px-3 py-1.5 text-left text-caption font-medium whitespace-nowrap text-fg-muted">
              First stock-out
            </th>
            {optionTitle ? (
              <>
                <th scope="col" className="px-3 py-1.5 text-left text-caption font-medium whitespace-nowrap text-fg-muted">
                  With {optionTitle}
                </th>
                <th scope="col" className="px-3 py-1.5 text-left text-caption font-medium whitespace-nowrap text-fg-muted">
                  Change
                </th>
              </>
            ) : null}
          </tr>
        </thead>
        <tbody>
          {views.map((v) => (
            <tr key={v.row.id} data-row-id={v.row.id} className="border-b border-border last:border-b-0">
              <th scope="row" className="px-3 py-1.5 text-left font-medium text-fg">
                {rowLabel(v.row)}
              </th>
              {v.cells.map((c, i) => (
                <td key={weeks[i]} data-status={c?.status ?? "none"} className="px-2 py-1.5 whitespace-nowrap text-fg">
                  {cellText(c)}
                </td>
              ))}
              <td className="px-3 py-1.5 whitespace-nowrap text-fg">{v.baseline ? weekLabel(v.baseline) : "None"}</td>
              {optionTitle ? (
                <>
                  <td className="px-3 py-1.5 whitespace-nowrap text-fg">{v.withOption ? weekLabel(v.withOption) : "None"}</td>
                  <td className="px-3 py-1.5 whitespace-nowrap">
                    <Badge size="sm" tone={shiftTone(v.shift)}>
                      {breachShiftText(v.shift)}
                    </Badge>
                  </td>
                </>
              ) : null}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/**
 * `stockout_timeline@1` (canvas) — heat strip of the most exposed DC / cluster / style rows by
 * week (enough / tight / short) with each row's first stock-out. The option switcher shows how
 * a plan option moves that breach (diamond + dashed shift line; table column "Change").
 */
export function StockoutTimeline({ props, data, envelope }: BlockComponentProps<StockoutTimelineProps, StockoutTimelineData>) {
  const theme = useDocumentTheme();
  const reducedMotion = usePrefersReducedMotion();
  const regionId = `timeline-${useId()}`;
  const [view, setView] = useState<VisualView>("visual");
  const [option, setOption] = useState<string>(NO_ACTION);
  const titles = useMemo(() => new Map(props.options.map((o) => [o.id, o.title])), [props.options]);
  const selected = titles.has(option) ? option : NO_ACTION;
  const optionTitle = selected === NO_ACTION ? null : (titles.get(selected) ?? null);

  const views = useMemo(() => buildTimeline(props, data, selected), [props, data, selected]);
  const summary = useMemo(() => summarize(views, selected), [views, selected]);
  const chartOption = useMemo(
    () => timelineChartOption(props.weeks, views, optionTitle, theme, reducedMotion),
    [props.weeks, views, optionTitle, theme, reducedMotion],
  );
  const empty = views.length === 0;
  const shown: VisualView = empty ? "table" : view;

  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle="Stock-out timeline"
      icon={<CalendarRange />}
      actions={
        empty ? undefined : (
          <ViewToggle view={view} onChange={setView} visualLabel="chart" visualIcon={<CalendarRange aria-hidden="true" />} controls={regionId} />
        )
      }
    >
      <p className="text-ui text-fg-muted">
        {summary.shortRows > 0 ? (
          <>
            <span className="font-medium text-fg">
              {formatValue(summary.shortRows, "count")} of {formatValue(summary.rows, "count")} rows run short
            </span>
            {summary.firstBreach ? `, the first in ${weekLabel(summary.firstBreach)}` : ""} without action.
          </>
        ) : (
          `No stock-outs in ${formatValue(props.weeks.length, "count")} weeks.`
        )}
      </p>

      {props.options.length > 0 && !empty ? (
        <div className="flex flex-col gap-1.5">
          <ChoiceChips
            label="Compare with option"
            value={selected}
            onChange={setOption}
            choices={[{ id: NO_ACTION, label: "No action" }, ...props.options.map((o) => ({ id: o.id, label: o.title }))]}
          />
          <p aria-live="polite" className="text-caption text-fg-muted" data-breach-summary="">
            {optionTitle
              ? breachSummaryText(optionTitle, summary)
              : "Pick an option to see how it moves each first stock-out."}
          </p>
        </div>
      ) : null}

      {empty ? (
        <p className="text-ui text-fg-muted">No rows to show yet.</p>
      ) : (
        <div id={regionId} className="flex flex-col gap-2">
          {shown === "visual" ? <EChart option={chartOption} height={chartHeight(views.length)} kind="stockout-timeline" /> : null}
          <TimelineTable weeks={props.weeks} views={views} optionTitle={optionTitle} hidden={shown === "visual"} />
          {shown === "visual" ? <CellKey theme={theme} optionTitle={optionTitle} /> : null}
        </div>
      )}
    </BlockFrame>
  );
}

export default StockoutTimeline;
