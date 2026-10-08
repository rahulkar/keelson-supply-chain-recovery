import type { Metadata } from "next";
import Link from "next/link";

import { PurchaseOrderGrid } from "@/components/supply/PurchaseOrderGrid";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { apiGetAll, load } from "@/lib/server-api";
import type { Dc, Factory, PurchaseOrderSummary, Supplier } from "@/lib/types";
import { param, type SearchParams } from "@/lib/params";

export const metadata: Metadata = { title: "Purchase Orders" };

const STATUSES = ["open", "in_production", "shipped", "received", "cancelled"] as const;

type Props = { searchParams: Promise<SearchParams> };


export default async function PurchaseOrdersPage({ searchParams }: Props) {
  const sp = await searchParams;
  const status = STATUSES.find((s) => s === param(sp, "status"));
  const destDc = param(sp, "dest_dc");
  const supplier = param(sp, "supplier_code");
  const [pos, dcs, suppliers, factories] = await Promise.all([
    load(apiGetAll<PurchaseOrderSummary>("/v1/purchase-orders", { status, dest_dc: destDc, supplier_code: supplier })),
    load(apiGetAll<Dc>("/v1/dcs")),
    load(apiGetAll<Supplier>("/v1/suppliers")),
    load(apiGetAll<Factory>("/v1/factories")),
  ]);
  const supplierNames = Object.fromEntries((suppliers.ok ? suppliers.data.items : []).map((s) => [s.code, s.name]));
  const factoryNames = Object.fromEntries((factories.ok ? factories.data.items : []).map((f) => [f.code, `${f.name}, ${f.country}`]));

  return (
    <>
      <PageHeader title="Purchase Orders" subtitle="Inbound supply from suppliers and factories to distribution centres." />
      <form className="mx-toolbar" method="get" aria-label="Filter purchase orders">
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
            <option value="">All DCs</option>
            {(dcs.ok ? dcs.data.items : []).map((d) => (
              <option key={d.code} value={d.code}>
                {d.code} · {d.name}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Supplier</span>
          <select className="mx-select" name="supplier_code" defaultValue={supplier ?? ""}>
            <option value="">All suppliers</option>
            {(suppliers.ok ? suppliers.data.items : []).map((s) => (
              <option key={s.code} value={s.code}>
                {s.code} · {s.name}
              </option>
            ))}
          </select>
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {(status || destDc || supplier) && (
          <Link className="mx-btn mx-btn--ghost" href="/purchase-orders">
            Reset
          </Link>
        )}
      </form>
      {pos.ok ? (
        <PurchaseOrderGrid rows={pos.data.items} supplierNames={supplierNames} factoryNames={factoryNames} />
      ) : (
        <ProblemPanel error={pos.error} what="purchase orders" />
      )}
    </>
  );
}
