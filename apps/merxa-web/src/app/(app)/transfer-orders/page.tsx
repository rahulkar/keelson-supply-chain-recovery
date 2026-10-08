import type { Metadata } from "next";
import Link from "next/link";

import { TransferOrderGrid } from "@/components/stock/TransferOrderGrid";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { oneOf, param, type SearchParams } from "@/lib/params";
import { apiGetAll, load } from "@/lib/server-api";
import type { Dc, TransferOrder } from "@/lib/types";

export const metadata: Metadata = { title: "Transfer Orders" };

const STATUSES = ["planned", "released", "shipped", "received", "cancelled"] as const;

type Props = { searchParams: Promise<SearchParams> };

export default async function TransferOrdersPage({ searchParams }: Props) {
  const sp = await searchParams;
  const status = oneOf(sp, "status", STATUSES);
  const fromDc = param(sp, "from_dc");
  const toDc = param(sp, "to_dc");
  const selected = param(sp, "code");
  const [tos, dcs] = await Promise.all([
    load(apiGetAll<TransferOrder>("/v1/transfer-orders", { status, from_dc: fromDc, to_dc: toDc })),
    load(apiGetAll<Dc>("/v1/dcs")),
  ]);
  const dcOptions = dcs.ok ? dcs.data.items : [];

  return (
    <>
      <PageHeader
        title="Transfer Orders"
        subtitle="Stock moves between distribution centres. Orders created by change batches are marked External."
        actions={
          <Link className="mx-btn" href="/transfer-orders/new">
            New transfer order
          </Link>
        }
      />
      <form className="mx-toolbar" method="get" aria-label="Filter transfer orders">
        <label className="mx-field">
          <span className="mx-field__label">Status</span>
          <select className="mx-select" name="status" defaultValue={status ?? ""}>
            <option value="">All statuses</option>
            {STATUSES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
        </label>
        {(["from_dc", "to_dc"] as const).map((name) => (
          <label className="mx-field" key={name}>
            <span className="mx-field__label">{name === "from_dc" ? "From DC" : "To DC"}</span>
            <select className="mx-select" name={name} defaultValue={(name === "from_dc" ? fromDc : toDc) ?? ""}>
              <option value="">Any DC</option>
              {dcOptions.map((d) => (
                <option key={d.code} value={d.code}>
                  {d.code} · {d.name}
                </option>
              ))}
            </select>
          </label>
        ))}
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {(status || fromDc || toDc) && (
          <Link className="mx-btn mx-btn--ghost" href="/transfer-orders">
            Reset
          </Link>
        )}
      </form>
      {tos.ok ? (
        <TransferOrderGrid rows={tos.data.items} initialSelected={selected ?? null} />
      ) : (
        <ProblemPanel error={tos.error} what="transfer orders" />
      )}
    </>
  );
}
