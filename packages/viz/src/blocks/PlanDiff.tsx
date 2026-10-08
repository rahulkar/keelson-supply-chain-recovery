"use client";

import type { PlanDiffProps, PlanDiffRow } from "@keelson/contracts";
import { Badge, Button, cn } from "@keelson/ui";
import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type SortingState,
} from "@tanstack/react-table";
import { useVirtualizer } from "@tanstack/react-virtual";
import { ArrowDown, ArrowRight, ArrowUp, ArrowUpDown, GitCompareArrows, Maximize2 } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { formatValue, opLabel } from "../format";
import type { BlockComponentProps } from "../types";
import { BlockFrame } from "./BlockFrame";

/** Rows above this count are virtualised (contract: > 200). */
export const PLAN_DIFF_VIRTUALIZE_THRESHOLD = 200;
/** Rows shown in the thread before "View all changes" (canvas shows everything). */
export const PLAN_DIFF_INLINE_ROWS = 5;
const ROW_HEIGHT = 44;

const col = createColumnHelper<PlanDiffRow>();

/** A before/after text with more list items than this is collapsed to its first items. */
export const LONG_TEXT_ITEMS = 4;
/** …or, without list separators, longer than this many characters. */
export const LONG_TEXT_CHARS = 160;

/**
 * Split a long before/after text ("SKU × 79, SKU × 81, …" for a transfer order with many lines)
 * into a short preview and the rest. `null` when the text is short enough to show as is.
 */
export function splitLongText(text: string): { preview: string; rest: number; unit: "items" | "chars" } | null {
  const parts = text.split(/,\s+/);
  if (parts.length > LONG_TEXT_ITEMS) {
    return { preview: parts.slice(0, LONG_TEXT_ITEMS - 1).join(", "), rest: parts.length - (LONG_TEXT_ITEMS - 1), unit: "items" };
  }
  if (text.length > LONG_TEXT_CHARS) {
    const cut = text.lastIndexOf(" ", LONG_TEXT_CHARS - 20);
    return { preview: text.slice(0, cut > 40 ? cut : LONG_TEXT_CHARS - 20), rest: text.length, unit: "chars" };
  }
  return null;
}

/** Long cell text, collapsed to a preview with a "Show all" toggle (keeps the table scannable). */
function LongText({ text, className }: { text: string; className?: string | undefined }) {
  const [open, setOpen] = useState(false);
  const split = splitLongText(text);
  if (!split) return <span className={className}>{text}</span>;
  return (
    <span className={cn("inline-flex flex-wrap items-baseline gap-x-1.5", className)}>
      <span>{open ? text : `${split.preview}…`}</span>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="rounded-xs text-caption font-medium whitespace-nowrap text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-focus"
      >
        {open ? "Show less" : split.unit === "items" ? `Show all (${split.rest} more)` : "Show all"}
      </button>
    </span>
  );
}

const columns = [
  col.accessor("seq", { header: "#", cell: (c) => <span className="text-fg-subtle tabular-nums">{c.getValue()}</span> }),
  col.accessor("op", {
    header: "Change",
    cell: (c) => <span className="font-medium text-fg">{opLabel(c.getValue())}</span>,
    sortingFn: (a, b) => opLabel(a.original.op).localeCompare(opLabel(b.original.op)),
  }),
  col.accessor("target_label", { header: "What", cell: (c) => <span className="text-fg">{c.getValue()}</span> }),
  col.display({
    id: "change",
    header: "Before → after",
    cell: ({ row }) => (
      <span className="inline-flex flex-wrap items-center gap-1.5 text-fg-muted">
        <span>{row.original.before_text || "—"}</span>
        <ArrowRight aria-label="to" className="size-3.5 shrink-0 text-fg-subtle" />
        <LongText text={row.original.after_text || "—"} className="font-medium text-fg" />
      </span>
    ),
  }),
  col.accessor("qty", {
    header: "Qty",
    cell: (c) => {
      const v = c.getValue();
      return <span className="tabular-nums">{v === null || v === undefined ? "—" : formatValue(v, "units")}</span>;
    },
    sortUndefined: "last",
  }),
  col.accessor("system", { header: "System", cell: (c) => <span className="text-fg-muted">{c.getValue()}</span> }),
];

function SortIcon({ dir }: { dir: false | "asc" | "desc" }) {
  if (dir === "asc") return <ArrowUp aria-hidden="true" className="size-3.5" />;
  if (dir === "desc") return <ArrowDown aria-hidden="true" className="size-3.5" />;
  return <ArrowUpDown aria-hidden="true" className="size-3.5 opacity-40" />;
}

export type PlanDiffTableProps = {
  rows: PlanDiffRow[];
  caption: string;
  /** Force virtualisation on/off (default: on above the threshold). */
  virtualize?: boolean | undefined;
  /** Initial viewport size for the virtualiser (SSR / tests). */
  initialHeight?: number | undefined;
};

/** Sortable diff table (TanStack Table 8); virtualised with spacer rows above the threshold. */
export function PlanDiffTable({ rows, caption, virtualize, initialHeight = 480 }: PlanDiffTableProps) {
  const [sorting, setSorting] = useState<SortingState>([]);
  const table = useReactTable({
    data: rows,
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getRowId: (r) => String(r.seq),
  });
  const modelRows = table.getRowModel().rows;
  const useVirtual = virtualize ?? rows.length > PLAN_DIFF_VIRTUALIZE_THRESHOLD;
  const scrollRef = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: useVirtual ? modelRows.length : 0,
    getScrollElement: () => scrollRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 12,
    initialRect: { width: 960, height: initialHeight },
  });
  const virtualItems = useVirtual ? virtualizer.getVirtualItems() : [];
  const visible = useVirtual ? virtualItems.map((v) => modelRows[v.index]!).filter(Boolean) : modelRows;
  const padTop = useVirtual && virtualItems.length ? virtualItems[0]!.start : 0;
  const padBottom =
    useVirtual && virtualItems.length ? virtualizer.getTotalSize() - virtualItems[virtualItems.length - 1]!.end : 0;
  const colCount = columns.length;

  return (
    <div
      ref={scrollRef}
      className={cn(
        "overflow-auto rounded-md border border-border focus-visible:outline-2 focus-visible:outline-focus",
        useVirtual && "max-h-[60vh]",
      )}
      // Always keyboard-scrollable: six columns overflow horizontally on phones even when the
      // rows are not virtualised (WCAG 2.1.1, axe scrollable-region-focusable).
      tabIndex={0}
      role="region"
      aria-label={caption}
      data-virtualized={useVirtual || undefined}
    >
      <table className="w-full border-collapse text-left text-ui" aria-rowcount={useVirtual ? rows.length + 1 : undefined}>
        <caption className="sr-only">{caption}</caption>
        <thead className="sticky top-0 z-[1] bg-surface-2">
          {table.getHeaderGroups().map((hg) => (
            <tr key={hg.id} aria-rowindex={useVirtual ? 1 : undefined}>
              {hg.headers.map((h) => {
                const sortable = h.column.getCanSort();
                const dir = h.column.getIsSorted();
                return (
                  <th
                    key={h.id}
                    scope="col"
                    aria-sort={dir === "asc" ? "ascending" : dir === "desc" ? "descending" : sortable ? "none" : undefined}
                    className="border-b border-border px-3 py-2 text-caption font-medium whitespace-nowrap text-fg-muted"
                  >
                    {sortable ? (
                      <button
                        type="button"
                        onClick={h.column.getToggleSortingHandler()}
                        className="inline-flex items-center gap-1 rounded-xs hover:text-fg focus-visible:outline-2 focus-visible:outline-focus"
                      >
                        {flexRender(h.column.columnDef.header, h.getContext())}
                        <SortIcon dir={dir} />
                      </button>
                    ) : (
                      flexRender(h.column.columnDef.header, h.getContext())
                    )}
                  </th>
                );
              })}
            </tr>
          ))}
        </thead>
        <tbody>
          {padTop > 0 ? (
            <tr aria-hidden="true">
              <td colSpan={colCount} style={{ height: padTop, padding: 0 }} />
            </tr>
          ) : null}
          {visible.map((row) => (
            <tr
              key={row.id}
              aria-rowindex={useVirtual ? row.index + 2 : undefined}
              className="border-b border-border last:border-b-0 hover:bg-surface-2/60"
              style={useVirtual ? { height: ROW_HEIGHT } : undefined}
            >
              {row.getVisibleCells().map((cell) => (
                <td key={cell.id} className="px-3 py-2 align-middle">
                  {flexRender(cell.column.columnDef.cell, cell.getContext())}
                </td>
              ))}
            </tr>
          ))}
          {padBottom > 0 ? (
            <tr aria-hidden="true">
              <td colSpan={colCount} style={{ height: padBottom, padding: 0 }} />
            </tr>
          ) : null}
        </tbody>
      </table>
    </div>
  );
}

/** `plan_diff@1` — the changes that would land in the connected system. */
export function PlanDiff({ props, envelope, onAction, density }: BlockComponentProps<PlanDiffProps>) {
  const inline = density === "inline";
  const rows = useMemo(() => [...props.rows].sort((a, b) => a.seq - b.seq), [props.rows]);
  const shown = inline ? rows.slice(0, PLAN_DIFF_INLINE_ROWS) : rows;
  const hidden = rows.length - shown.length;
  const systems = [...new Set(rows.map((r) => r.system))];

  return (
    <BlockFrame
      envelope={envelope}
      defaultTitle="Changes"
      eyebrow={systems.length ? `Proposed changes · ${systems.join(", ")}` : "Proposed changes"}
      icon={<GitCompareArrows />}
      actions={<Badge tone="neutral">Plan v{props.plan_version}</Badge>}
    >
      <dl className="flex flex-wrap gap-x-6 gap-y-1 text-ui">
        <div className="flex gap-1.5">
          <dt className="text-fg-muted">Lines</dt>
          <dd className="font-semibold tabular-nums">{formatValue(props.totals.lines, "count")}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-fg-muted">Units</dt>
          <dd className="font-semibold tabular-nums">{formatValue(props.totals.units, "units")}</dd>
        </div>
        <div className="flex gap-1.5">
          <dt className="text-fg-muted">Value</dt>
          <dd className="font-semibold tabular-nums">{formatValue(props.totals.value_cents, "eur_cents")}</dd>
        </div>
      </dl>
      {rows.length === 0 ? (
        <p className="text-ui text-fg-muted">No changes in this plan.</p>
      ) : (
        <PlanDiffTable rows={shown} caption={`${envelope.title || "Changes"} (${rows.length} lines)`} />
      )}
      {inline && hidden > 0 ? (
        <div>
          <Button
            variant="secondary"
            size="sm"
            icon={<Maximize2 className="size-3.5" />}
            onClick={() => void onAction({ type: "open_canvas", blockId: envelope.id })}
          >
            View all {formatValue(rows.length, "count")} changes
          </Button>
        </div>
      ) : null}
    </BlockFrame>
  );
}
