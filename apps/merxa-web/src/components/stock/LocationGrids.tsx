"use client";

import type { ColDef } from "ag-grid-community";
import { useMemo } from "react";

import { humanize } from "@/lib/format";
import type { Cluster, Dc, Store } from "@/lib/types";
import { linkCell, monoCol, numberCol } from "../grid/cells";
import { DataGrid } from "../grid/DataGrid";

const coord = (v: unknown) => (typeof v === "number" ? v.toFixed(4) : "");

type DcRow = Dc & { store_count: number };

export function DcGrid({ rows, storeCounts }: { rows: Dc[]; storeCounts: Record<string, number> }) {
  const data = useMemo<DcRow[]>(() => rows.map((d) => ({ ...d, store_count: storeCounts[d.code] ?? 0 })), [rows, storeCounts]);
  const columns = useMemo<ColDef<DcRow>[]>(
    () => [
      monoCol("code", "DC", {
        width: 90,
        pinned: "left",
        cellRenderer: linkCell<DcRow>((r) => `/inventory?location_type=dc&dc=${encodeURIComponent(r.code)}`),
      }),
      { field: "name", headerName: "Name", flex: 1, minWidth: 160 },
      { field: "city", headerName: "City", width: 130 },
      monoCol("country", "Country", { width: 90 }),
      numberCol("inbound_cap_units_wk", "Inbound cap / wk", { width: 140 }),
      numberCol("outbound_cap_units_wk", "Outbound cap / wk", { width: 150 }),
      numberCol("store_count", "Stores served", { width: 120 }),
      { field: "lat", headerName: "Lat", width: 100, type: "rightAligned", valueFormatter: (p) => coord(p.value) },
      { field: "lon", headerName: "Lon", width: 100, type: "rightAligned", valueFormatter: (p) => coord(p.value) },
    ],
    [],
  );
  return (
    <DataGrid<DcRow>
      label="Distribution centres"
      rows={data}
      columns={columns}
      getRowId={(r) => r.code}
      layout="auto"
      exportFileName="merxa-dcs"
      footer={<span>Select a DC code to open its inventory.</span>}
    />
  );
}

export function ClusterGrid({ rows }: { rows: Cluster[] }) {
  const columns = useMemo<ColDef<Cluster>[]>(
    () => [
      monoCol("code", "Cluster", {
        width: 150,
        pinned: "left",
        cellRenderer: linkCell<Cluster>((r) => `/locations?tab=stores&cluster=${encodeURIComponent(r.code)}`),
      }),
      monoCol("dc_code", "DC", { width: 90 }),
      { field: "climate", headerName: "Climate", width: 110, valueFormatter: (p) => humanize(p.value as string) },
      { field: "grade_band", headerName: "Grade band", width: 110 },
      numberCol("store_count", "Stores", { width: 100 }),
    ],
    [],
  );
  return (
    <DataGrid<Cluster>
      label="Store clusters"
      rows={rows}
      columns={columns}
      getRowId={(r) => r.code}
      layout="auto"
      exportFileName="merxa-clusters"
    />
  );
}

export function StoreGrid({ rows }: { rows: Store[] }) {
  const columns = useMemo<ColDef<Store>[]>(
    () => [
      monoCol("code", "Store", {
        width: 120,
        pinned: "left",
        cellRenderer: linkCell<Store>((r) => `/inventory?location_type=store&location=${encodeURIComponent(r.code)}`),
      }),
      { field: "name", headerName: "Name", flex: 1, minWidth: 180 },
      monoCol("market", "Market", { width: 90 }),
      { field: "city", headerName: "City", width: 130 },
      { field: "grade", headerName: "Grade", width: 80 },
      monoCol("cluster", "Cluster", { width: 140 }),
      monoCol("home_dc", "Home DC", { width: 100 }),
      numberCol("sales_index", "Sales index", {
        width: 110,
        valueFormatter: (p) => (typeof p.value === "number" ? p.value.toFixed(2) : ""),
      }),
    ],
    [],
  );
  return (
    <DataGrid<Store>
      label="Stores"
      rows={rows}
      columns={columns}
      getRowId={(r) => r.code}
      exportFileName="merxa-stores"
      emptyMessage="No stores match the filter."
    />
  );
}
