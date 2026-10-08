"use client";

import { useId, type ReactNode } from "react";
import { cn } from "../lib/cn";

export type TableColumn<T> = {
  id: string;
  header: ReactNode;
  cell: (row: T, index: number) => ReactNode;
  align?: "start" | "end" | "center" | undefined;
  /** Never wrap this column (codes, numbers, badges). */
  nowrap?: boolean | undefined;
  /** Header announced but not shown (icon / action columns). */
  srOnlyHeader?: boolean | undefined;
  className?: string | undefined;
  /** This column's cell is the row header (`th scope="row"`). */
  rowHeader?: boolean | undefined;
};

export type TableRowTone = "accent" | "warning" | "danger" | "muted";

export type TableProps<T> = {
  columns: readonly TableColumn<T>[];
  rows: readonly T[];
  rowKey: (row: T, index: number) => string;
  /** Names the table (`<caption>`); also names the scroll region. */
  caption: string;
  /** Visually hide the caption (it still names the table). */
  hideCaption?: boolean | undefined;
  /** Shown in a full-width row when `rows` is empty. */
  empty?: ReactNode | undefined;
  /** `compact` rows are 32 px (admin), `comfortable` 40 px. */
  density?: "compact" | "comfortable" | undefined;
  /** Highlight a row (e.g. needs review); pair with text in a cell, colour never stands alone. */
  rowTone?: ((row: T) => TableRowTone | undefined) | undefined;
  /** Extra attributes per row (e.g. `data-*`, `aria-current`). */
  rowProps?: ((row: T) => Record<string, string | undefined>) | undefined;
  /** Sets `aria-busy` while data refreshes. */
  busy?: boolean | undefined;
  className?: string | undefined;
};

const align = { start: "text-left", end: "text-right", center: "text-center" } as const;

const rowTones: Record<TableRowTone, string> = {
  accent: "bg-accent-soft/50",
  warning: "bg-warning-soft/60",
  danger: "bg-danger-soft/60",
  muted: "text-fg-muted",
};

/**
 * Semantic data table (`caption`, `th scope`), horizontally scrollable inside a focusable group
 * (named by the caption, not a landmark) so keyboard users can scroll wide tables. Compact by default.
 */
export function Table<T>({
  columns,
  rows,
  rowKey,
  caption,
  hideCaption = false,
  empty = "Nothing to show.",
  density = "compact",
  rowTone,
  rowProps,
  busy,
  className,
}: TableProps<T>) {
  const captionId = `tbl-${useId()}`;
  const cellPad = density === "compact" ? "px-3 py-1.5" : "px-3 py-2.5";
  return (
    <div
      role="group"
      aria-labelledby={captionId}
      tabIndex={0}
      className={cn(
        "w-full overflow-x-auto rounded-md border border-border bg-surface focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus",
        className,
      )}
    >
      <table className="w-full border-collapse text-ui" aria-busy={busy || undefined}>
        <caption id={captionId} className={cn("px-3 pt-2 pb-1 text-left text-caption font-medium text-fg-muted", hideCaption && "sr-only")}>
          {caption}
        </caption>
        <thead>
          <tr className="border-b border-border bg-surface-2">
            {columns.map((c) => (
              <th
                key={c.id}
                scope="col"
                className={cn(
                  cellPad,
                  "text-caption font-medium whitespace-nowrap text-fg-muted",
                  align[c.align ?? "start"],
                  c.className,
                )}
              >
                {c.srOnlyHeader ? <span className="sr-only">{c.header}</span> : c.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 ? (
            <tr>
              <td colSpan={columns.length} className="px-3 py-6 text-center text-ui text-fg-muted">
                {empty}
              </td>
            </tr>
          ) : (
            rows.map((row, i) => {
              const tone = rowTone?.(row);
              return (
                <tr
                  key={rowKey(row, i)}
                  data-tone={tone}
                  {...(rowProps?.(row) ?? {})}
                  className={cn("border-b border-border last:border-b-0", tone && rowTones[tone])}
                >
                  {columns.map((c) => {
                    const cls = cn(cellPad, "align-middle text-fg", align[c.align ?? "start"], c.nowrap && "whitespace-nowrap", c.className);
                    return c.rowHeader ? (
                      <th key={c.id} scope="row" className={cn(cls, "font-medium")}>
                        {c.cell(row, i)}
                      </th>
                    ) : (
                      <td key={c.id} className={cls}>
                        {c.cell(row, i)}
                      </td>
                    );
                  })}
                </tr>
              );
            })
          )}
        </tbody>
      </table>
    </div>
  );
}
