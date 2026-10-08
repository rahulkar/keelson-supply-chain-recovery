import type { Metadata } from "next";
import Link from "next/link";

import { ClusterGrid, DcGrid, StoreGrid } from "@/components/stock/LocationGrids";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { buildQuery } from "@/lib/http";
import { oneOf, param, type SearchParams } from "@/lib/params";
import { apiGetAll, load } from "@/lib/server-api";
import type { Cluster, Dc, Store } from "@/lib/types";

export const metadata: Metadata = { title: "DCs & Stores" };

const TABS = ["dcs", "clusters", "stores"] as const;
type Tab = (typeof TABS)[number];
const TAB_LABELS: Record<Tab, string> = { dcs: "Distribution centres", clusters: "Store clusters", stores: "Stores" };

type Props = { searchParams: Promise<SearchParams> };

export default async function LocationsPage({ searchParams }: Props) {
  const sp = await searchParams;
  const tab: Tab = oneOf(sp, "tab", TABS) ?? "dcs";
  const dc = param(sp, "dc");
  const cluster = param(sp, "cluster");
  const market = param(sp, "market");

  const [dcs, clusters] = await Promise.all([load(apiGetAll<Dc>("/v1/dcs")), load(apiGetAll<Cluster>("/v1/clusters"))]);
  const stores =
    tab === "stores"
      ? await load(apiGetAll<Store>("/v1/stores", { dc_code: dc, cluster_code: cluster, market }, { max: 5000 }))
      : null;
  const storeCounts: Record<string, number> = {};
  for (const c of clusters.ok ? clusters.data.items : []) storeCounts[c.dc_code] = (storeCounts[c.dc_code] ?? 0) + c.store_count;

  return (
    <>
      <PageHeader title="DCs & Stores" subtitle="Distribution network: distribution centres, store clusters and stores." />
      <nav className="mx-tabs" aria-label="Location views">
        {TABS.map((t) => (
          <Link key={t} href={`/locations${buildQuery({ tab: t })}`} className="mx-tab" aria-current={tab === t ? "page" : undefined}>
            {TAB_LABELS[t]}
          </Link>
        ))}
      </nav>

      {tab === "dcs" &&
        (dcs.ok ? <DcGrid rows={dcs.data.items} storeCounts={storeCounts} /> : <ProblemPanel error={dcs.error} what="distribution centres" />)}

      {tab === "clusters" &&
        (clusters.ok ? <ClusterGrid rows={clusters.data.items} /> : <ProblemPanel error={clusters.error} what="store clusters" />)}

      {tab === "stores" && (
        <>
          <form className="mx-toolbar" method="get" aria-label="Filter stores">
            <input type="hidden" name="tab" value="stores" />
            <label className="mx-field">
              <span className="mx-field__label">Home DC</span>
              <select className="mx-select" name="dc" defaultValue={dc ?? ""}>
                <option value="">All DCs</option>
                {(dcs.ok ? dcs.data.items : []).map((d) => (
                  <option key={d.code} value={d.code}>
                    {d.code} · {d.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="mx-field">
              <span className="mx-field__label">Cluster</span>
              <select className="mx-select" name="cluster" defaultValue={cluster ?? ""}>
                <option value="">All clusters</option>
                {(clusters.ok ? clusters.data.items : []).map((c) => (
                  <option key={c.code} value={c.code}>
                    {c.code}
                  </option>
                ))}
              </select>
            </label>
            <label className="mx-field">
              <span className="mx-field__label">Market</span>
              <input className="mx-input mx-mono" name="market" defaultValue={market ?? ""} placeholder="e.g. GB" maxLength={8} />
            </label>
            <button type="submit" className="mx-btn">
              Apply filter
            </button>
            {(dc || cluster || market) && (
              <Link className="mx-btn mx-btn--ghost" href="/locations?tab=stores">
                Reset
              </Link>
            )}
          </form>
          {stores && (stores.ok ? <StoreGrid rows={stores.data.items} /> : <ProblemPanel error={stores.error} what="stores" />)}
        </>
      )}
    </>
  );
}
