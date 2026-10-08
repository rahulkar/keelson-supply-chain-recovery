"use client";

import type { ColDef, ICellRendererParams } from "ag-grid-community";
import Link from "next/link";
import { useCallback, useMemo, useState } from "react";

import { changeSetCodeFromCreatedVia } from "@/lib/attribution";
import { formatDate, formatInt, formatRate, modeLabel } from "@/lib/format";
import { poLineHref } from "@/lib/m2";
import type { CapacityBooking, Lane, RateCard, Sailing, Shipment } from "@/lib/types";
import {
  dateCol,
  dateTimeCol,
  linkCell,
  monoCol,
  numberCol,
  openEndedDateFormatter,
  statusCol,
  unlessPinned,
} from "../grid/cells";
import { DataGrid } from "../grid/DataGrid";
import { KeyValues } from "../ui/Card";
import { Drawer } from "../ui/Drawer";
import { CreatedVia } from "../ui/ExternalLink";
import { StatusBadge } from "../ui/StatusBadge";

const openEnded = openEndedDateFormatter;

const daysBetween = (a: string, b: string) => Math.round((Date.parse(b) - Date.parse(a)) / 86_400_000);

function CreatedViaCell(p: ICellRendererParams<{ created_via: string }>) {
  if (!p.data || p.node.rowPinned) return null;
  return <CreatedVia value={p.data.created_via} />;
}

const externalRule = {
  "mx-row-external": (p: { data?: { created_via?: string } }) =>
    !!p.data && changeSetCodeFromCreatedVia(p.data.created_via) !== null,
};

// ───────────────────────── sailings ─────────────────────────
type SailingRow = Sailing & { transit_days: number; shipment_count: number };

export function SailingGrid({ rows, shipmentCounts }: { rows: Sailing[]; shipmentCounts: Record<string, number> }) {
  const data = useMemo<SailingRow[]>(
    () => rows.map((s) => ({ ...s, transit_days: daysBetween(s.etd, s.eta), shipment_count: shipmentCounts[s.code] ?? 0 })),
    [rows, shipmentCounts],
  );
  const columns = useMemo<ColDef<SailingRow>[]>(
    () => [
      monoCol("code", "Sailing", { width: 105, pinned: "left" }),
      statusCol("status", "Status", { width: 105 }),
      monoCol("lane_code", "Lane", { width: 125 }),
      monoCol("origin", "From", { width: 80 }),
      monoCol("dest", "To", { width: 80 }),
      { field: "vessel", headerName: "Vessel", flex: 1, minWidth: 130 },
      dateCol("cutoff", "Cut-off", { width: 110 }),
      dateCol("etd", "ETD", { width: 110 }),
      dateCol("eta", "ETA port", { width: 110 }),
      numberCol("transit_days", "Days", { width: 75 }),
      numberCol("capacity_teu", "TEU", { width: 85 }),
      numberCol("shipment_count", "Shipments", {
        width: 105,
        cellRenderer: linkCell<SailingRow>((r) => `/shipments?sailing_code=${encodeURIComponent(r.code)}`, { mono: false }),
      }),
    ],
    [],
  );
  return (
    <DataGrid<SailingRow>
      label="Sailings"
      rows={data}
      columns={columns}
      getRowId={(r) => r.code}
      layout="auto"
      exportFileName="merxa-sailings"
      emptyMessage="No sailings match the filter."
      footer={<span>Cut-off is the last day to gate cargo in at the origin port. Shipments opens the cargo booked on a sailing.</span>}
    />
  );
}

// ───────────────────────── lanes ─────────────────────────
export type LaneRow = Lane & {
  rate_cents: number | null;
  basis: string | null;
  currency: string | null;
  weekly_capacity: number | null;
  /** Null when the week's capacity is unknown (no rate card, or bookings could not be loaded). */
  remaining: number | null;
};

export function LaneGrid({ rows, week }: { rows: LaneRow[]; week: string | null }) {
  const columns = useMemo<ColDef<LaneRow>[]>(
    () => [
      monoCol("code", "Lane", { width: 125, pinned: "left" }),
      monoCol("origin", "Origin", { width: 80 }),
      monoCol("dest", "Dest.", { width: 80 }),
      { field: "mode", headerName: "Mode", width: 85, valueFormatter: (p) => modeLabel(p.value as string) },
      {
        field: "carrier_name",
        headerName: "Carrier",
        flex: 1,
        minWidth: 140,
        valueFormatter: (p) => (p.data ? `${p.data.carrier_name} (${p.data.carrier_code})` : ""),
      },
      numberCol("transit_days", "Days", { width: 75 }),
      {
        field: "via",
        headerName: "Via",
        width: 110,
        valueFormatter: (p) => (Array.isArray(p.value) && p.value.length ? (p.value as string[]).join(", ") : "—"),
        cellClass: "mx-cell-mono",
      },
      monoCol("alt_lane_code", "Alternative", { width: 115, valueFormatter: (p) => (p.value as string | null) ?? "—" }),
      {
        colId: "rate",
        headerName: week ? `Rate ${week}` : "Current rate",
        width: 130,
        type: "rightAligned",
        valueGetter: (p) => p.data?.rate_cents ?? null,
        valueFormatter: (p) => (p.data && p.data.rate_cents !== null ? formatRate(p.data.rate_cents, p.data.basis, p.data.currency) : "—"),
      },
      numberCol("weekly_capacity", "Capacity / wk", { width: 115 }),
      numberCol("remaining", "Remaining / wk", {
        width: 125,
        cellClassRules: { "mx-cell-warn": (p) => typeof p.value === "number" && p.value <= 0 },
      }),
    ],
    [week],
  );
  return (
    <DataGrid<LaneRow>
      label="Lanes"
      rows={rows}
      columns={columns}
      getRowId={(r) => r.code}
      exportFileName="merxa-lanes"
      emptyMessage="No lanes match the filter."
      footer={
        <span>
          Origins and destinations are UN/LOCODEs or DC codes. Capacity is per week in the rate-card basis (units or TEU);
          remaining deducts held and confirmed per-unit bookings.
        </span>
      }
    />
  );
}

// ───────────────────────── rate cards ─────────────────────────
type RateRow = RateCard & { mode: string | null };

export function RateCardGrid({ rows, lanes }: { rows: RateCard[]; lanes: Lane[] }) {
  const data = useMemo<RateRow[]>(() => {
    const mode = Object.fromEntries(lanes.map((l) => [l.code, l.mode]));
    return rows.map((r) => ({ ...r, mode: mode[r.lane_code] ?? null }));
  }, [rows, lanes]);
  const columns = useMemo<ColDef<RateRow>[]>(
    () => [
      monoCol("lane_code", "Lane", { width: 130, pinned: "left" }),
      { field: "mode", headerName: "Mode", width: 90, valueFormatter: (p) => modeLabel(p.value as string) },
      dateCol("valid_from", "Valid from", { valueFormatter: openEnded }),
      dateCol("valid_to", "Valid to", { valueFormatter: openEnded }),
      {
        field: "rate_cents",
        headerName: "Rate",
        width: 150,
        type: "rightAligned",
        filter: "agNumberColumnFilter",
        valueFormatter: (p) => (p.data ? formatRate(p.data.rate_cents, p.data.basis, p.data.currency) : ""),
      },
      monoCol("currency", "Currency", { width: 100 }),
      numberCol("weekly_capacity", "Capacity / wk", { width: 130 }),
      { field: "basis", headerName: "Basis", width: 100, valueFormatter: (p) => String(p.value ?? "").replace("per_", "per ") },
    ],
    [],
  );
  return (
    <DataGrid<RateRow>
      label="Rate cards"
      rows={data}
      columns={columns}
      getRowId={(r) => `${r.lane_code}|${r.valid_from}`}
      exportFileName="merxa-rate-cards"
      emptyMessage="No rate cards match the filter."
      footer={<span>Validity is inclusive. Rate cards of a lane never overlap; peak season starts 1 November.</span>}
    />
  );
}

// ───────────────────────── capacity bookings ─────────────────────────
export function BookingGrid({ rows }: { rows: CapacityBooking[] }) {
  const columns = useMemo<ColDef<CapacityBooking>[]>(
    () => [
      monoCol("code", "Booking", {
        width: 110,
        pinned: "left",
        cellRenderer: linkCell<CapacityBooking>((r) => `/capacity-bookings/${encodeURIComponent(r.code)}`),
      }),
      monoCol("lane_code", "Lane", { width: 130 }),
      monoCol("iso_week", "Week", { width: 100 }),
      numberCol("qty_units", "Quantity", { width: 110 }),
      { field: "basis", headerName: "Basis", width: 100, valueFormatter: (p) => String(p.value ?? "").replace("per_", "per ") },
      statusCol("status", "Status", { width: 110 }),
      dateTimeCol("expires_at", "Hold expires"),
      { field: "created_via", headerName: "Created via", cellRenderer: CreatedViaCell, flex: 1, minWidth: 170 },
    ],
    [],
  );
  return (
    <DataGrid<CapacityBooking>
      label="Capacity bookings"
      rows={rows}
      columns={columns}
      getRowId={(r) => r.code}
      rowClassRules={externalRule}
      layout="auto"
      exportFileName="merxa-capacity-bookings"
      emptyMessage="No capacity bookings."
      footer={<span>Bookings created by change batches are marked External.</span>}
    />
  );
}

// ───────────────────────── shipments ─────────────────────────
export function ShipmentGrid({ rows, initialSelected }: { rows: Shipment[]; initialSelected: string | null }) {
  const [selected, setSelected] = useState<string | null>(initialSelected);
  const current = useMemo(() => rows.find((r) => r.code === selected) ?? null, [rows, selected]);
  const close = useCallback(() => setSelected(null), []);
  const columns = useMemo<ColDef<Shipment>[]>(
    () => [
      monoCol("code", "Shipment", { width: 115, pinned: "left" }),
      statusCol("status", "Status", { width: 100 }),
      monoCol("po_code", "PO", {
        width: 110,
        cellRenderer: linkCell<Shipment>((r) => `/purchase-orders/${encodeURIComponent(r.po_code)}`),
      }),
      { field: "mode", headerName: "Mode", width: 80, valueFormatter: unlessPinned((p) => modeLabel(p.value as string)) },
      numberCol("total_qty", "Units", { width: 85 }),
      monoCol("dest_dc", "To DC", { width: 75 }),
      dateCol("eta", "ETA DC", { width: 110 }),
      dateCol("etd", "ETD", { width: 110 }),
      dateCol("ata", "Arrived", { width: 110 }),
      monoCol("lane_code", "Lane", { width: 120 }),
      monoCol("sailing_code", "Sailing", {
        width: 95,
        cellRenderer: linkCell<Shipment>((r) => `/sailings?lane_code=${encodeURIComponent(r.lane_code ?? "")}`),
      }),
      monoCol("booking_code", "Booking", { width: 85 }),
      monoCol("origin", "Origin", { width: 80 }),
      { field: "created_via", headerName: "Created via", cellRenderer: CreatedViaCell, width: 170 },
    ],
    [],
  );
  const totals = useMemo(() => ({ code: "Total", total_qty: rows.reduce((s, r) => s + r.total_qty, 0) }), [rows]);
  return (
    <>
      <DataGrid<Shipment>
        label="Shipments"
        rows={rows}
        columns={columns}
        getRowId={(r) => r.code}
        rowClassRules={externalRule}
        onRowClicked={(r) => setSelected(r.code)}
        pinnedBottomRow={totals as Partial<Shipment>}
        exportFileName="merxa-shipments"
        emptyMessage="No shipments match the filter."
        footer={<span>Select a row to see its lines. Shipments created by change batches (e.g. sea-air splits) are marked External.</span>}
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
              {
                label: "Purchase order",
                value: (
                  <Link className="mx-mono" href={`/purchase-orders/${encodeURIComponent(current.po_code)}`}>
                    {current.po_code}
                  </Link>
                ),
              },
              { label: "Mode", value: modeLabel(current.mode) },
              { label: "Lane", value: <span className="mx-mono">{current.lane_code ?? "—"}</span> },
              { label: "Sailing", value: <span className="mx-mono">{current.sailing_code ?? "—"}</span> },
              { label: "Capacity booking", value: <span className="mx-mono">{current.booking_code ?? "—"}</span> },
              { label: "Route", value: <span className="mx-mono">{`${current.origin || "—"} → ${current.dest_dc}`}</span> },
              { label: "ETD", value: formatDate(current.etd) },
              { label: "ETA DC", value: formatDate(current.eta) },
              { label: "Arrived", value: formatDate(current.ata) },
              { label: "Created via", value: <CreatedVia value={current.created_via} /> },
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
                <th scope="col">PO line</th>
                <th scope="col">SKU</th>
                <th scope="col" className="mx-num">
                  Qty
                </th>
              </tr>
            </thead>
            <tbody>
              {current.lines.map((l) => {
                const href = poLineHref(l.po_line_ref);
                return (
                  <tr key={l.line_no}>
                    <td className="mx-num mx-mono">{l.line_no}</td>
                    <td className="mx-mono">{href ? <Link href={href}>{l.po_line_ref}</Link> : l.po_line_ref}</td>
                    <td className="mx-mono">{l.sku}</td>
                    <td className="mx-num">{formatInt(l.qty)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </Drawer>
      )}
    </>
  );
}
