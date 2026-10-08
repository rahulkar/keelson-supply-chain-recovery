"use client";

import type { ColDef } from "ag-grid-community";
import { useCallback, useMemo, useState } from "react";

import { formatBp, formatDate, formatDateTime, formatInt, humanize, isoWeek } from "@/lib/format";
import { isOrderAtRisk, promotionRow, type PromotionDisplay } from "@/lib/m2";
import type { OnlineOrder, Promotion } from "@/lib/types";
import { dateCol, dateTimeCol, monoCol, numberCol, openEndedDateFormatter, statusCol, unlessPinned } from "../grid/cells";
import { DataGrid } from "../grid/DataGrid";
import { KeyValues } from "../ui/Card";
import { Drawer } from "../ui/Drawer";
import { StatusBadge } from "../ui/StatusBadge";

type OrderRow = OnlineOrder & { units: number; line_count: number; risk: boolean };

const CHANNEL: Record<string, string> = { online: "Home delivery", click_collect: "Click & collect" };

export function OnlineOrderGrid({ rows, initialSelected }: { rows: OnlineOrder[]; initialSelected: string | null }) {
  const data = useMemo<OrderRow[]>(
    () =>
      rows.map((o) => ({ ...o, units: o.lines.reduce((s, l) => s + l.qty, 0), line_count: o.lines.length, risk: isOrderAtRisk(o) })),
    [rows],
  );
  const [selected, setSelected] = useState<string | null>(initialSelected);
  const current = useMemo(() => data.find((r) => r.code === selected) ?? null, [data, selected]);
  const close = useCallback(() => setSelected(null), []);
  const columns = useMemo<ColDef<OrderRow>[]>(
    () => [
      monoCol("code", "Order", { width: 120, pinned: "left" }),
      monoCol("market", "Market", { width: 90 }),
      {
        field: "channel",
        headerName: "Channel",
        width: 140,
        valueFormatter: unlessPinned((p) => CHANNEL[p.value as string] ?? humanize(p.value as string)),
      },
      monoCol("store_code", "Collect store", { width: 130, valueFormatter: unlessPinned((p) => (p.value as string | null) ?? "—") }),
      dateCol("promise_date", "Promise date"),
      {
        colId: "promise_week",
        headerName: "Week",
        width: 100,
        cellClass: "mx-cell-mono",
        valueGetter: (p) => (p.data && !p.node?.rowPinned ? isoWeek(p.data.promise_date) : null),
      },
      statusCol("status", "Status", { width: 110 }),
      numberCol("line_count", "Lines", { width: 80 }),
      numberCol("units", "Units", { width: 90 }),
      numberCol("shortfall", "Unreserved", {
        width: 120,
        cellClassRules: { "mx-cell-warn": (p) => typeof p.value === "number" && p.value > 0 },
      }),
      dateTimeCol("created_at", "Placed"),
    ],
    [],
  );
  const rowClassRules = useMemo(() => ({ "mx-row-risk": (p: { data?: OrderRow }) => !!p.data?.risk }), []);
  const totals = useMemo(
    () => ({
      code: "Total",
      units: data.reduce((s, r) => s + r.units, 0),
      shortfall: data.reduce((s, r) => s + r.shortfall, 0),
    }),
    [data],
  );
  return (
    <>
      <DataGrid<OrderRow>
        label="Online orders"
        rows={data}
        columns={columns}
        getRowId={(r) => r.code}
        rowClassRules={rowClassRules}
        onRowClicked={(r) => setSelected(r.code)}
        pinnedBottomRow={totals as Partial<OrderRow>}
        exportFileName="merxa-online-orders"
        emptyMessage="No online orders match the filter."
        footer={<span>Open or picking orders with lines whose reserved quantity is below the ordered quantity are at risk. Select a row to see its lines.</span>}
      />
      {current && (
        <Drawer
          title={
            <>
              <span className="mx-mono">{current.code}</span> <StatusBadge status={current.status} />
              {current.risk && <StatusBadge status="at_risk" tone="danger" label="At risk" />}
            </>
          }
          onClose={close}
        >
          <KeyValues
            items={[
              { label: "Market", value: <span className="mx-mono">{current.market}</span> },
              { label: "Channel", value: CHANNEL[current.channel] ?? current.channel },
              { label: "Collect store", value: <span className="mx-mono">{current.store_code ?? "—"}</span> },
              { label: "Promise date", value: `${formatDate(current.promise_date)} (${isoWeek(current.promise_date)})` },
              { label: "Placed", value: formatDateTime(current.created_at) },
              { label: "Unreserved units", value: formatInt(current.shortfall) },
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
                  Ordered
                </th>
                <th scope="col" className="mx-num">
                  Reserved
                </th>
              </tr>
            </thead>
            <tbody>
              {current.lines.map((l) => (
                <tr key={l.line_no} className={l.reserved_qty < l.qty ? "is-changed" : undefined}>
                  <td className="mx-num mx-mono">{l.line_no}</td>
                  <td className="mx-mono">{l.sku}</td>
                  <td className="mx-num">{formatInt(l.qty)}</td>
                  <td className="mx-num">{formatInt(l.reserved_qty)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Drawer>
      )}
    </>
  );
}

type PromoRow = PromotionDisplay;

export function PromotionGrid({ rows }: { rows: Promotion[] }) {
  const data = useMemo<PromoRow[]>(() => rows.map(promotionRow), [rows]);
  const list = (v: unknown) => (Array.isArray(v) && v.length ? v.join(", ") : "All");
  const columns = useMemo<ColDef<PromoRow>[]>(
    () => [
      monoCol("code", "Promotion", { width: 120, pinned: "left" }),
      { field: "name", headerName: "Name", width: 160 },
      dateCol("period_start", "Starts", { valueFormatter: openEndedDateFormatter }),
      dateCol("period_end", "Ends", { valueFormatter: openEndedDateFormatter }),
      { field: "weeks", headerName: "Weeks", width: 150, cellClass: "mx-cell-mono" },
      numberCol("days", "Days", { width: 70 }),
      { field: "discount_bp", headerName: "Discount", width: 90, type: "rightAligned", valueFormatter: (p) => formatBp(p.value as number) },
      { field: "uplift_bp", headerName: "Uplift", width: 85, type: "rightAligned", valueFormatter: (p) => formatBp(p.value as number) },
      { field: "markets", headerName: "Markets", flex: 1, minWidth: 160, valueFormatter: (p) => list(p.value) },
      { field: "grades", headerName: "Grades", width: 90, valueFormatter: (p) => list(p.value) },
      {
        field: "style_codes",
        headerName: "Styles",
        width: 220,
        cellClass: "mx-cell-mono",
        valueFormatter: (p) => list(p.value),
      },
    ],
    [],
  );
  return (
    <DataGrid<PromoRow>
      label="Promotions"
      rows={data}
      columns={columns}
      getRowId={(r) => r.code}
      layout="auto"
      exportFileName="merxa-promotions"
      emptyMessage="No promotions match the filter."
      footer={<span>Periods are inclusive. Expected uplift is applied to the demand forecast of the listed styles in the listed markets and store grades.</span>}
    />
  );
}
