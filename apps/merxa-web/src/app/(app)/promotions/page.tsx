import type { Metadata } from "next";
import Link from "next/link";

import { PromotionGrid } from "@/components/demand/DemandGrids";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { isIsoDate, sortPromotions } from "@/lib/m2";
import { param, type SearchParams } from "@/lib/params";
import { apiGetAll, load } from "@/lib/server-api";
import type { Promotion } from "@/lib/types";

export const metadata: Metadata = { title: "Promotions" };

type Props = { searchParams: Promise<SearchParams> };

export default async function PromotionsPage({ searchParams }: Props) {
  const sp = await searchParams;
  const activeParam = param(sp, "active_on");
  const activeOn = isIsoDate(activeParam) ? activeParam : undefined;
  const promos = await load(apiGetAll<Promotion>("/v1/promotions", { active_on: activeOn }));
  return (
    <>
      <PageHeader
        title="Promotions"
        subtitle="Price promotions by market, store grade and style, with the discount and the expected sales uplift."
      />
      <form className="mx-toolbar" method="get" aria-label="Filter promotions">
        <label className="mx-field">
          <span className="mx-field__label">Active on</span>
          <input className="mx-input" type="date" name="active_on" defaultValue={activeOn ?? ""} />
        </label>
        <button type="submit" className="mx-btn">
          Apply filter
        </button>
        {activeOn && (
          <Link className="mx-btn mx-btn--ghost" href="/promotions">
            Reset
          </Link>
        )}
      </form>
      {promos.ok ? (
        <PromotionGrid rows={sortPromotions(promos.data.items)} />
      ) : (
        <ProblemPanel error={promos.error} what="promotions" />
      )}
    </>
  );
}
