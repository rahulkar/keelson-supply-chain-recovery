"use client";

import type { ColDef, ICellRendererParams, ValueFormatterParams } from "ag-grid-community";
import Link from "next/link";

import { formatDate, formatDateTime, formatInt } from "@/lib/format";
import { StatusBadge } from "../ui/StatusBadge";

/** Column helpers shared by Merxa list pages. */

export function intFormatter(p: ValueFormatterParams): string {
  return typeof p.value === "number" ? formatInt(p.value) : p.value == null ? "" : String(p.value);
}

export function dateFormatter(p: ValueFormatterParams): string {
  return typeof p.value === "string" ? formatDate(p.value) : "";
}

/** Date cell where null is an unbounded side of a `daterange` (rate-card validity, promotion period). */
export function openEndedDateFormatter(p: { value: unknown }): string {
  return typeof p.value === "string" ? formatDate(p.value) : "open-ended";
}

export function dateTimeFormatter(p: ValueFormatterParams): string {
  return typeof p.value === "string" ? formatDateTime(p.value) : "";
}

/** Wraps a value formatter so the pinned totals row stays blank (no "—" placeholders). */
export function unlessPinned<T>(format: (p: ValueFormatterParams<T>) => string) {
  return (p: ValueFormatterParams<T>): string => (p.node?.rowPinned ? "" : format(p));
}

export function StatusCell(p: ICellRendererParams) {
  if (p.node.rowPinned || p.value == null) return null;
  return <StatusBadge status={String(p.value)} />;
}

/** Cell renderer that links the value; `href` builds the target from the row. */
export function linkCell<T>(href: (row: T) => string, opts: { mono?: boolean } = {}) {
  function LinkCell(p: ICellRendererParams<T>) {
    if (p.node.rowPinned || !p.data || p.value == null) return <span>{p.value == null ? "" : String(p.value)}</span>;
    return (
      <Link
        href={href(p.data)}
        className={opts.mono === false ? undefined : "mx-cell-mono"}
        onClick={(e) => e.stopPropagation()}
      >
        {String(p.value)}
      </Link>
    );
  }
  return LinkCell;
}

export function numberCol<T>(field: ColDef<T>["field"], headerName: string, extra: Partial<ColDef<T>> = {}): ColDef<T> {
  return {
    field,
    headerName,
    type: "rightAligned",
    filter: "agNumberColumnFilter",
    valueFormatter: intFormatter,
    width: 110,
    ...extra,
  };
}

export function dateCol<T>(field: ColDef<T>["field"], headerName: string, extra: Partial<ColDef<T>> = {}): ColDef<T> {
  return { field, headerName, valueFormatter: dateFormatter, width: 120, filter: "agTextColumnFilter", ...extra };
}

export function dateTimeCol<T>(field: ColDef<T>["field"], headerName: string, extra: Partial<ColDef<T>> = {}): ColDef<T> {
  return { field, headerName, valueFormatter: dateTimeFormatter, width: 160, filter: "agTextColumnFilter", ...extra };
}

export function monoCol<T>(field: ColDef<T>["field"], headerName: string, extra: Partial<ColDef<T>> = {}): ColDef<T> {
  return { field, headerName, cellClass: "mx-cell-mono", width: 140, ...extra };
}

export function statusCol<T>(field: ColDef<T>["field"], headerName = "Status", extra: Partial<ColDef<T>> = {}): ColDef<T> {
  return { field, headerName, cellRenderer: StatusCell, width: 120, ...extra };
}
