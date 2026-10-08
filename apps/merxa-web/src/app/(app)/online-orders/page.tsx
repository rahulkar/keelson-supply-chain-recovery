import type { Metadata } from "next";
import Link from "next/link";

import { OnlineOrderGrid } from "@/components/demand/DemandGrids";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { buildQuery } from "@/lib/http";
import { isIsoDate, isOrderAtRisk } from "@/lib/m2";
import { oneOf, param, type SearchParams } from "@/lib/params";
import { apiGetAll, load } from "@/lib/server-api";
import type { OnlineOrder } from "@/lib/types";

export const metadata: Metadata = { title: "Online Orders" };

const STATUSES = ["open", "picking", "shipped", "delivered", "cancelled"] as const;
const RISK = ["at_risk", "covered"] as const;

type Props = { searchParams: Promise<SearchParams> };

export default async function OnlineOrdersPage({ searchParams }: Props) {
  const sp = await searchParams;
  const risk = oneOf(sp, "risk", RISK);
  const status = oneOf(sp, "status", STATUSES);
  const market = param(sp, "market")?.toUpperCase();
  const promiseParam = param(sp, "promise_before");
  const promiseBefore = isIsoDate(promiseParam) ? promiseParam : undefined;
  const orders = await load(
    apiGetAll<OnlineOrder>(
      "/v1/online-orders",
      {
        market,
        status,
        promise_before: promiseBefore,
        at_risk: risk === "at_risk" ? true : risk === "covered" ? false : undefined,
      },
      { max: 10_000 },
    ),
  );
  const items = orders.ok ? orders.data.items : [];
  const atRisk = items.filter(isOrderAtRisk);
  const markets = [...new Set(items.map((o) => o.market))].sort();
  const filtered = !!(risk || status || market || promiseBefore);

  return (
    <>
      <PageHeader
        title="Online Orders"
        subtitle="E-commerce and click & collect orders with their promise dates and stock reservations."
        actions={
          <Link className="mx-btn" href={`/online-orders${buildQuery({ risk: "at_risk", market, promise_before: promiseBefore })}`}>
            Show orders at risk
          </Link>
        }
      />
      <form className="mx-toolbar" method="get" aria-label="Filter online orders">
        <label className="mx-field">
          <span className="mx-field__label">Reservation</span>
          <select className="mx-select" name="risk" defaultValue={risk ?? ""}>
            <option value="">All orders</option>
            <option value="at_risk">At risk (not fully reserved)</option>
            <option value="covered">Fully reserved</option>
          </select>
        </label>
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
        <label className="mx-field">
          <span className="mx-field__label">Market</span>
          <input className="mx-input mx-mono" name="market" defaultValue={market ?? ""} placeholder="e.g. GB" maxLength={8} list="mx-markets" />
          <datalist id="mx-markets">
            {markets.map((m) => (
              <option key={m} value={m} />
            ))}
          </datalist>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Promised before</span>
          <input className="mx-input" type="date" name="promise_before" defaultValue={promiseBefore ?? ""} />
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {filtered && (
          <Link className="mx-btn mx-btn--ghost" href="/online-orders">
            Reset
          </Link>
        )}
      </form>
      {orders.ok && atRisk.length > 0 && risk !== "at_risk" && (
        <div className="mx-banner mx-banner--warning" role="status">
          <div className="mx-banner__body">
            <strong>{atRisk.length}</strong> of {items.length} listed orders are at risk (
            {atRisk.reduce((s, o) => s + o.shortfall, 0).toLocaleString("en-GB")} units not reserved).
          </div>
        </div>
      )}
      {orders.ok ? (
        <OnlineOrderGrid rows={items} initialSelected={param(sp, "code") ?? null} />
      ) : (
        <ProblemPanel error={orders.error} what="online orders" />
      )}
    </>
  );
}
