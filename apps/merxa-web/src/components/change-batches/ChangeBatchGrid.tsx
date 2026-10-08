"use client";

import type { ColDef, ICellRendererParams } from "ag-grid-community";
import { useRouter } from "next/navigation";
import { useMemo, type ReactNode } from "react";

import { attributionText, isExternal, isRecentlyApplied } from "@/lib/attribution";
import { shortHash } from "@/lib/format";
import type { ChangeSetSummary } from "@/lib/types";
import { useNow } from "@/lib/use-now";
import { dateTimeCol, linkCell, monoCol, numberCol, statusCol } from "../grid/cells";
import { DataGrid } from "../grid/DataGrid";
import { ExternalChip } from "../ui/StatusBadge";

function ViaCell(p: ICellRendererParams<ChangeSetSummary>) {
  if (!p.data?.via) return null;
  return isExternal(p.data) ? <ExternalChip label={p.data.via} title={`Applied via ${p.data.via}`} /> : <span>{p.data.via}</span>;
}

const href = (r: ChangeSetSummary) => `/change-batches/${encodeURIComponent(r.code)}`;

export function ChangeBatchGrid({ rows, footer }: { rows: ChangeSetSummary[]; footer?: ReactNode }) {
  const router = useRouter();
  // "Recently applied" depends on the wall clock: evaluated client side only.
  const now = useNow();

  const columns = useMemo<ColDef<ChangeSetSummary>[]>(
    () => [
      monoCol("code", "Batch", { cellRenderer: linkCell(href), width: 110, pinned: "left" }),
      { field: "title", headerName: "Title", flex: 2, minWidth: 220, tooltipField: "title" },
      statusCol("status", "Status", { width: 110 }),
      numberCol("items_count", "Items", { width: 80 }),
      { field: "via", headerName: "Via", cellRenderer: ViaCell, width: 110 },
      monoCol("approval_ref", "Approval", { width: 110 }),
      {
        colId: "attribution",
        headerName: "Attribution",
        valueGetter: (p) => (p.data ? (attributionText(p.data) ?? "") : ""),
        flex: 3,
        minWidth: 300,
        tooltipValueGetter: (p) => (p.data ? attributionText(p.data) : null),
      },
      dateTimeCol("created_at", "Created"),
      dateTimeCol("applied_at", "Applied"),
      monoCol("preview_hash", "Preview hash", { valueFormatter: (p) => shortHash(p.value as string | null), width: 170 }),
    ],
    [],
  );

  const rowClassRules = useMemo(
    () => ({ "mx-row-external": (p: { data?: ChangeSetSummary }) => !!p.data && now !== null && isRecentlyApplied(p.data, now) }),
    [now],
  );

  return (
    <DataGrid<ChangeSetSummary>
      label="Change batches"
      rows={rows}
      columns={columns}
      getRowId={(r) => r.change_set_id}
      rowClassRules={rowClassRules}
      onRowClicked={(r) => router.push(href(r))}
      exportFileName="merxa-change-batches"
      emptyMessage="No change batches yet. Integrations such as Keelson create them through the Merxa API."
      footer={footer}
    />
  );
}
