"use client";

import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { useCallback, useMemo, useState } from "react";

import { changeSetCodeFromCreatedVia } from "@/lib/attribution";
import { formatDate, formatDateTime, formatInt } from "@/lib/format";
import type { TransferOrder } from "@/lib/types";
import { dateCol, dateTimeCol, monoCol, numberCol, statusCol } from "../grid/cells";
import { DataGrid } from "../grid/DataGrid";
import { KeyValues } from "../ui/Card";
import { Drawer } from "../ui/Drawer";
import { CreatedVia } from "../ui/ExternalLink";
import { StatusBadge } from "../ui/StatusBadge";

function CreatedViaCell(p: ICellRendererParams<TransferOrder>) {
  if (!p.data || p.node.rowPinned) return null;
  return <CreatedVia value={p.data.created_via} />;
}

export function TransferOrderGrid({ rows, initialSelected }: { rows: TransferOrder[]; initialSelected: string | null }) {
  const [selected, setSelected] = useState<string | null>(initialSelected);
  const current = useMemo(() => rows.find((r) => r.code === selected) ?? null, [rows, selected]);
  const close = useCallback(() => setSelected(null), []);

  const columns = useMemo<ColDef<TransferOrder>[]>(
    () => [
      monoCol("code", "Transfer order", { width: 140, pinned: "left" }),
      monoCol("from_dc", "From", { width: 80 }),
      monoCol("to_dc", "To", { width: 80 }),
      dateCol("ship_date", "Ship date"),
      dateCol("eta_date", "ETA"),
      statusCol("status", "Status", { width: 110 }),
      numberCol<TransferOrder>(undefined, "Lines", { colId: "line_count", valueGetter: (p) => (p.node?.rowPinned ? null : (p.data?.lines?.length ?? null)), width: 80 }),
      numberCol("total_qty", "Units", { width: 100 }),
      { field: "created_via", headerName: "Created via", cellRenderer: CreatedViaCell, width: 180, flex: 1 },
      dateTimeCol("created_at", "Created"),
    ],
    [],
  );
  const rowClassRules = useMemo(
    () => ({ "mx-row-external": (p: { data?: TransferOrder }) => !!p.data && changeSetCodeFromCreatedVia(p.data.created_via) !== null }),
    [],
  );
  const totals = useMemo(() => ({ code: "Total", total_qty: rows.reduce((s, r) => s + r.total_qty, 0) }), [rows]);

  return (
    <>
      <DataGrid<TransferOrder>
        label="Transfer orders"
        rows={rows}
        columns={columns}
        getRowId={(r) => r.code}
        rowClassRules={rowClassRules}
        onRowClicked={(r) => setSelected(r.code)}
        pinnedBottomRow={totals}
        exportFileName="merxa-transfer-orders"
        emptyMessage="No transfer orders match the filter."
        footer={<span>Select a row to see its lines.</span>}
      />
      {current && (
        <Drawer
          title={
            <>
              <span className="mx-mono">{current.code}</span> <StatusBadge status={current.status} />
            </>
          }
          onClose={close}
        >
          <KeyValues
            items={[
              { label: "From DC", value: <span className="mx-mono">{current.from_dc}</span> },
              { label: "To DC", value: <span className="mx-mono">{current.to_dc}</span> },
              { label: "Ship date", value: formatDate(current.ship_date) },
              { label: "ETA", value: formatDate(current.eta_date) },
              { label: "Created via", value: <CreatedVia value={current.created_via} /> },
              { label: "Created", value: formatDateTime(current.created_at) },
              { label: "Units", value: formatInt(current.total_qty) },
              { label: "Record version", value: `v${current.version}` },
            ]}
          />
          <table className="mx-table">
            <thead>
              <tr>
                <th scope="col" className="mx-num">
                  Line
                </th>
                <th scope="col">SKU</th>
                <th scope="col" className="mx-num">
                  Qty
                </th>
              </tr>
            </thead>
            <tbody>
              {current.lines.map((l) => (
                <tr key={l.line_no}>
                  <td className="mx-num mx-mono">{l.line_no}</td>
                  <td className="mx-mono">{l.sku}</td>
                  <td className="mx-num">{formatInt(l.qty)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Drawer>
      )}
    </>
  );
}
