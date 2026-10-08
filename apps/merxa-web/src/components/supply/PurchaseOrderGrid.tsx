"use client";

import type { ColDef } from "ag-grid-community";
import { useRouter } from "next/navigation";
import { useMemo } from "react";

import { isoWeek } from "@/lib/format";
import type { PurchaseOrderSummary } from "@/lib/types";
import { dateCol, linkCell, monoCol, numberCol, statusCol } from "../grid/cells";
import { DataGrid } from "../grid/DataGrid";

const href = (r: PurchaseOrderSummary) => `/purchase-orders/${encodeURIComponent(r.code)}`;

export function PurchaseOrderGrid({
  rows,
  supplierNames,
  factoryNames,
}: {
  rows: PurchaseOrderSummary[];
  supplierNames: Record<string, string>;
  factoryNames: Record<string, string>;
}) {
  const router = useRouter();
  const columns = useMemo<ColDef<PurchaseOrderSummary>[]>(
    () => [
      monoCol("code", "PO", { cellRenderer: linkCell(href), width: 130, pinned: "left" }),
      monoCol("supplier_code", "Supplier", { width: 120 }),
      {
        colId: "supplier_name",
        headerName: "Supplier name",
        valueGetter: (p) => (p.data ? (supplierNames[p.data.supplier_code] ?? "") : ""),
        flex: 1,
        minWidth: 160,
      },
      monoCol("factory_code", "Factory", { width: 100, tooltipValueGetter: (p) => (p.data ? factoryNames[p.data.factory_code] : null) }),
      monoCol("dest_dc", "Dest. DC", { width: 90 }),
      statusCol("status", "Status", { width: 120 }),
      dateCol("ex_factory_date", "Ex-factory"),
      dateCol("eta_dc", "ETA DC"),
      { colId: "eta_week", headerName: "ETA week", valueGetter: (p) => (p.node?.rowPinned ? "" : isoWeek(p.data?.eta_dc)), width: 100, cellClass: "mx-cell-mono" },
      numberCol("line_count", "Lines", { width: 80 }),
      numberCol("total_qty", "Units ordered", { width: 120 }),
    ],
    [supplierNames, factoryNames],
  );
  const totals = useMemo(
    () => ({ code: "Total", line_count: rows.reduce((s, r) => s + r.line_count, 0), total_qty: rows.reduce((s, r) => s + r.total_qty, 0) }),
    [rows],
  );
  return (
    <DataGrid<PurchaseOrderSummary>
      label="Purchase orders"
      rows={rows}
      columns={columns}
      getRowId={(r) => r.code}
      onRowClicked={(r) => router.push(href(r))}
      pinnedBottomRow={totals}
      exportFileName="merxa-purchase-orders"
      emptyMessage="No purchase orders match the filter."
    />
  );
}
