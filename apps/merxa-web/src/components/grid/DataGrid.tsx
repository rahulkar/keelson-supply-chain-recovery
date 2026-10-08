"use client";

import { IconDownload, IconSearch } from "@tabler/icons-react";
import type {
  CellKeyDownEvent,
  ColDef,
  FullWidthCellKeyDownEvent,
  GetRowIdFunc,
  GridApi,
  GridReadyEvent,
  RowClassRules,
  RowClickedEvent,
} from "ag-grid-community";
import { AgGridReact } from "ag-grid-react";
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";

import { csvSafeCell } from "@/lib/csv";
import { DENSITY_METRICS } from "@/lib/density";
import { formatInt } from "@/lib/format";
import { useDensity } from "../shell/DensityProvider";
import { gridModules, merxaGridTheme } from "./gridTheme";

export type DataGridProps<T> = {
  rows: T[];
  columns: ColDef<T>[];
  getRowId: (row: T) => string;
  /** Accessible name of the grid region. */
  label: string;
  /** Show the quick filter, row count and CSV export. */
  toolbar?: boolean;
  /** Extra controls rendered next to the quick filter. */
  toolbarExtra?: ReactNode;
  exportFileName?: string;
  rowClassRules?: RowClassRules<T>;
  onRowClicked?: (row: T) => void;
  pinnedBottomRow?: Partial<T>;
  /** "fill" stretches to the remaining page height; "auto" sizes to the rows. */
  layout?: "fill" | "auto";
  emptyMessage?: string;
  footer?: ReactNode;
};

const DEFAULT_COL: ColDef = {
  sortable: true,
  resizable: true,
  filter: true,
  minWidth: 70,
};

function isPlainEnter(event: Event | null | undefined): event is KeyboardEvent {
  if (!event || event.type !== "keydown") return false;
  const k = event as KeyboardEvent;
  return k.key === "Enter" && !k.altKey && !k.ctrlKey && !k.metaKey && !k.shiftKey;
}

function escapeHtml(s: string): string {
  return s.replace(/[&<>"']/g, (c) => `&#${c.charCodeAt(0)};`);
}

export function DataGrid<T>({
  rows,
  columns,
  getRowId,
  label,
  toolbar = true,
  toolbarExtra,
  exportFileName,
  rowClassRules,
  onRowClicked,
  pinnedBottomRow,
  layout = "fill",
  emptyMessage = "No records match.",
  footer,
}: DataGridProps<T>) {
  const { density } = useDensity();
  const metrics = DENSITY_METRICS[density];
  const apiRef = useRef<GridApi<T> | null>(null);
  const [quick, setQuick] = useState("");
  const [shown, setShown] = useState<number>(rows.length);

  const rowId = useCallback<GetRowIdFunc<T>>((p) => getRowId(p.data), [getRowId]);
  const pinned = useMemo(() => (pinnedBottomRow ? [pinnedBottomRow as T] : undefined), [pinnedBottomRow]);
  const recount = useCallback(() => {
    if (apiRef.current) setShown(apiRef.current.getDisplayedRowCount());
  }, []);
  const onReady = useCallback(
    (e: GridReadyEvent<T>) => {
      apiRef.current = e.api;
      recount();
    },
    [recount],
  );
  const onClick = useCallback(
    (e: RowClickedEvent<T>) => {
      if (onRowClicked && e.data && !e.rowPinned) onRowClicked(e.data);
    },
    [onRowClicked],
  );
  // Keyboard equivalent of a row click: Enter on a focused cell opens the row (AG Grid
  // fires rowClicked for the mouse only). Enter on a link or button inside the cell keeps
  // its own meaning.
  const onKeyDown = useCallback(
    (e: CellKeyDownEvent<T> | FullWidthCellKeyDownEvent<T>) => {
      if (!onRowClicked || !e.data || e.rowPinned) return;
      if (!isPlainEnter(e.event)) return;
      const target = e.event.target;
      if (target instanceof Element && target.closest("a, button, input, select, textarea")) return;
      onRowClicked(e.data);
    },
    [onRowClicked],
  );
  // Row classes can depend on the wall clock (e.g. "applied in the last 10 minutes"):
  // re-evaluate them whenever the rules object changes.
  useEffect(() => {
    apiRef.current?.redrawRows();
  }, [rowClassRules]);
  const exportCsv = useCallback(() => {
    apiRef.current?.exportDataAsCsv({
      fileName: `${exportFileName ?? "merxa-export"}.csv`,
      // Formatted values as shown, with formula-looking text neutralised (CSV injection).
      processCellCallback: (p) => csvSafeCell(p.value, p.formatValue(p.value)),
    });
  }, [exportFileName]);

  return (
    <div className={layout === "fill" ? "mx-grid mx-grid--fill" : "mx-grid"} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      {toolbar && (
        <div className="mx-toolbar" style={{ alignItems: "center" }}>
          <label className="mx-field" style={{ flexDirection: "row", alignItems: "center", gap: 6 }}>
            <IconSearch size={14} aria-hidden="true" />
            <span className="mx-visually-hidden">Filter {label}</span>
            <input
              className="mx-input mx-input--search"
              type="search"
              placeholder="Quick filter…"
              value={quick}
              onChange={(e) => setQuick(e.target.value)}
            />
          </label>
          {toolbarExtra}
          <span className="mx-toolbar__spacer" />
          <span className="mx-muted" aria-live="polite">
            {formatInt(shown)} of {formatInt(rows.length)} rows
          </span>
          <button type="button" className="mx-btn mx-btn--sm" onClick={exportCsv}>
            <IconDownload size={14} aria-hidden="true" /> CSV
          </button>
        </div>
      )}
      <div style={layout === "fill" ? { flex: 1, minHeight: 320 } : undefined} role="region" aria-label={label}>
        <AgGridReact<T>
          modules={gridModules}
          theme={merxaGridTheme}
          rowData={rows}
          columnDefs={columns}
          defaultColDef={DEFAULT_COL as ColDef<T>}
          getRowId={rowId}
          rowHeight={metrics.rowHeight}
          headerHeight={metrics.headerHeight}
          quickFilterText={quick}
          rowClassRules={rowClassRules}
          onRowClicked={onClick}
          onCellKeyDown={onKeyDown}
          onGridReady={onReady}
          onFilterChanged={recount}
          onRowDataUpdated={recount}
          pinnedBottomRowData={pinned}
          domLayout={layout === "auto" ? "autoHeight" : "normal"}
          containerStyle={layout === "fill" ? { height: "100%" } : undefined}
          enableCellTextSelection
          ensureDomOrder
          animateRows={false}
          overlayNoRowsTemplate={`<span>${escapeHtml(emptyMessage)}</span>`}
          rowStyle={onRowClicked ? { cursor: "pointer" } : undefined}
        />
      </div>
      {footer && <div className="mx-grid-footer">{footer}</div>}
    </div>
  );
}
