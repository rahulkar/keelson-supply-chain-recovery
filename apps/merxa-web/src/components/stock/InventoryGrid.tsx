"use client";

import type { CellClassParams, ColDef, ICellRendererParams } from "ag-grid-community";
import Link from "next/link";
import { useMemo } from "react";

import { formatDateTime } from "@/lib/format";
import type { InventoryPosition } from "@/lib/types";
import { monoCol, numberCol } from "../grid/cells";
import { DataGrid } from "../grid/DataGrid";
import { ExternalChip } from "../ui/StatusBadge";

export type InventoryRow = InventoryPosition & {
  /** Change-batch code (or channel) of the latest external change to this position. */
  external_change: string | null;
  external_at: string | null;
};

function ExternalCell(p: ICellRendererParams<InventoryRow>) {
  const code = p.data?.external_change;
  if (!code || p.node.rowPinned) return null;
  return (
    <span className="mx-inline-list" title={p.data?.external_at ? `Changed ${formatDateTime(p.data.external_at)}` : undefined}>
      <ExternalChip />
      {code.startsWith("CS-") ? (
        <Link className="mx-cell-mono" href={`/change-batches/${encodeURIComponent(code)}`} onClick={(e) => e.stopPropagation()}>
          {code}
        </Link>
      ) : (
        code
      )}
    </span>
  );
}

// Keeps the right alignment of the numeric column type (a custom cellClass replaces it).
const atpClass = (p: CellClassParams<InventoryRow>) =>
  typeof p.value === "number" && p.value <= 0 && !p.node.rowPinned ? ["ag-right-aligned-cell", "mx-cell-warn"] : ["ag-right-aligned-cell"];

export function InventoryGrid({
  rows,
  truncated,
  showLocationType,
}: {
  rows: InventoryRow[];
  truncated: boolean;
  showLocationType: boolean;
}) {
  const columns = useMemo<ColDef<InventoryRow>[]>(
    () => [
      { field: "location_type", headerName: "Type", width: 80, hide: !showLocationType, valueFormatter: (p) => (p.value === "dc" ? "DC" : p.value === "store" ? "Store" : "") },
      monoCol("location_code", "Location", { width: 120, pinned: "left" }),
      monoCol("sku", "SKU", { width: 160, pinned: "left" }),
      numberCol("on_hand", "On hand"),
      numberCol("allocated", "Allocated"),
      numberCol("reserved", "Reserved"),
      numberCol("in_transit", "In transit"),
      numberCol("atp", "ATP", { cellClass: atpClass, headerTooltip: "Available to promise = on hand − allocated − reserved" }),
      numberCol("version", "Ver.", { width: 80 }),
      { field: "external_change", headerName: "External change", cellRenderer: ExternalCell, width: 170, flex: 1 },
    ],
    [showLocationType],
  );
  const totals = useMemo(() => {
    const sum = (k: "on_hand" | "allocated" | "reserved" | "in_transit" | "atp") => rows.reduce((s, r) => s + r[k], 0);
    return { location_code: "Total", on_hand: sum("on_hand"), allocated: sum("allocated"), reserved: sum("reserved"), in_transit: sum("in_transit"), atp: sum("atp") };
  }, [rows]);
  const rowClassRules = useMemo(() => ({ "mx-row-external": (p: { data?: InventoryRow }) => !!p.data?.external_change }), []);

  return (
    <DataGrid<InventoryRow>
      label="Inventory positions"
      rows={rows}
      columns={columns}
      getRowId={(r) => `${r.location_type}/${r.location_code}/${r.sku}`}
      rowClassRules={rowClassRules}
      pinnedBottomRow={totals}
      exportFileName="merxa-inventory"
      emptyMessage="No inventory positions match the filter."
      footer={
        <>
          {truncated && <strong>Showing the first 10,000 positions: narrow the filter to see the rest.</strong>}
          <span>Amber rows were last changed by an external system (e.g. a transfer reserved through a change batch).</span>
        </>
      }
    />
  );
}
