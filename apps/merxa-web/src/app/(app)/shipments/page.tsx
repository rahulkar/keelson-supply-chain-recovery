import type { Metadata } from "next";
import Link from "next/link";

import { ShipmentGrid } from "@/components/logistics/LogisticsGrids";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { oneOf, param, type SearchParams } from "@/lib/params";
import { apiGetAll, load } from "@/lib/server-api";
import type { Dc, Shipment } from "@/lib/types";

export const metadata: Metadata = { title: "Shipments" };

const STATUSES = ["planned", "booked", "in_transit", "arrived", "cancelled"] as const;

type Props = { searchParams: Promise<SearchParams> };

export default async function ShipmentsPage({ searchParams }: Props) {
  const sp = await searchParams;
  const status = oneOf(sp, "status", STATUSES);
  const destDc = param(sp, "dest_dc");
  const poCode = param(sp, "po_code")?.toUpperCase();
  const sailing = param(sp, "sailing_code")?.toUpperCase();
  const [shipments, dcs] = await Promise.all([
    load(
      apiGetAll<Shipment>("/v1/shipments", { status, dest_dc: destDc, po_code: poCode, sailing_code: sailing }, { max: 5000 }),
    ),
    load(apiGetAll<Dc>("/v1/dcs")),
  ]);

  return (
    <>
      <PageHeader
        title="Shipments"
        subtitle="Inbound freight per purchase order: mode, lane, sailing or capacity booking, and arrival at the DC."
      />
      <form className="mx-toolbar" method="get" aria-label="Filter shipments">
        <label className="mx-field">
          <span className="mx-field__label">Status</span>
          <select className="mx-select" name="status" defaultValue={status ?? ""}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s.replace("_", " ")}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Destination DC</span>
          <select className="mx-select" name="dest_dc" defaultValue={destDc ?? ""}>
            <option value="">Any DC</option>
            {(dcs.ok ? dcs.data.items : []).map((d) => (
              <option key={d.code} value={d.code}>
                {d.code} · {d.name}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Purchase order</span>
          <input className="mx-input mx-mono" name="po_code" defaultValue={poCode ?? ""} placeholder="PO-BD-0001" maxLength={40} />
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Sailing</span>
          <input className="mx-input mx-mono" name="sailing_code" defaultValue={sailing ?? ""} placeholder="SAIL-0001" maxLength={40} />
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {(status || destDc || poCode || sailing) && (
          <Link className="mx-btn mx-btn--ghost" href="/shipments">
            Reset
          </Link>
        )}
      </form>
      {shipments.ok ? (
        <ShipmentGrid rows={shipments.data.items} initialSelected={param(sp, "code") ?? null} />
      ) : (
        <ProblemPanel error={shipments.error} what="shipments" />
      )}
    </>
  );
}
