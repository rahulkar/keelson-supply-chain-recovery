"use client";

import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { useMemo, type ReactNode } from "react";

import { isExternal } from "@/lib/attribution";
import { summarizeChange } from "@/lib/diff";
import { formatValue, humanize } from "@/lib/format";
import type { AuditRow } from "@/lib/types";
import { dateTimeCol, linkCell, monoCol, numberCol } from "../grid/cells";
import { DataGrid } from "../grid/DataGrid";
import { ExternalChip } from "../ui/StatusBadge";

function ViaCell(p: ICellRendererParams<AuditRow>) {
  if (!p.data?.via) return null;
  return isExternal(p.data) ? <ExternalChip label={p.data.via} /> : <span>{p.data.via}</span>;
}

export function AuditGrid({ rows, footer }: { rows: AuditRow[]; footer?: ReactNode }) {
  const columns = useMemo<ColDef<AuditRow>[]>(
    () => [
      numberCol("id", "#", { width: 80, valueFormatter: (p) => String(p.value ?? "") }),
      dateTimeCol("at", "At", { width: 150 }),
      {
        colId: "actor",
        headerName: "Actor",
        valueGetter: (p) => p.data?.actor_display || p.data?.actor_username || "",
        width: 150,
      },
      monoCol("client_id", "Client", { width: 150 }),
      { field: "via", headerName: "Via", cellRenderer: ViaCell, width: 120 },
      monoCol("approval_ref", "Approval", { width: 110 }),
      monoCol("approved_by", "Approved by", { width: 150 }),
      { field: "initiated_by", headerName: "Initiated by", width: 130 },
      monoCol("change_set_code", "Batch", {
        width: 100,
        cellRenderer: linkCell<AuditRow>((r) => `/change-batches/${encodeURIComponent(r.change_set_code ?? "")}`),
      }),
      { field: "entity_type", headerName: "Entity type", width: 140, valueFormatter: (p) => humanize(p.value as string) },
      monoCol("entity_ref", "Entity", { width: 200 }),
      { field: "action", headerName: "Action", width: 110, valueFormatter: (p) => humanize(p.value as string) },
      {
        colId: "change",
        headerName: "Change",
        valueGetter: (p) => (p.data ? summarizeChange(p.data.before, p.data.after, formatValue) : ""),
        tooltipValueGetter: (p) => (p.data ? summarizeChange(p.data.before, p.data.after, formatValue, 50) : null),
        flex: 1,
        minWidth: 280,
      },
    ],
    [],
  );
  const rowClassRules = useMemo(() => ({ "mx-row-external": (p: { data?: AuditRow }) => !!p.data && isExternal(p.data) }), []);
  return (
    <DataGrid<AuditRow>
      label="Audit log"
      rows={rows}
      columns={columns}
      getRowId={(r) => String(r.id)}
      rowClassRules={rowClassRules}
      exportFileName="merxa-audit-log"
      emptyMessage="No audit entries match the filter."
      footer={footer}
    />
  );
}
