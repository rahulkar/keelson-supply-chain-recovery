import type { Metadata } from "next";

import { DemoDirector } from "@/components/demo/DemoDirector";
import { EmptyState } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { apiGetAll, getMe, load } from "@/lib/server-api";
import type { Lane, PoStatus, PurchaseOrderSummary } from "@/lib/types";

const DELAYABLE: readonly PoStatus[] = ["open", "in_production", "shipped"];

export const metadata: Metadata = { title: "Demo Director" };

/** Demo controls (DEMO_MODE=1 and role merxa-admin; merxa-api enforces both). */
export default async function DemoDirectorPage() {
  const me = await getMe();
  const allowed = me.demo_mode && me.user.roles.includes("merxa-admin");
  const [pos, lanes] = allowed
    ? await Promise.all([load(apiGetAll<PurchaseOrderSummary>("/v1/purchase-orders")), load(apiGetAll<Lane>("/v1/lanes"))])
    : [null, null];
  // Lane capacity cuts: sea-air lanes first (the Act 3 inject), then the rest.
  const laneCodes = lanes?.ok
    ? [...lanes.data.items]
        .sort((a, b) => Number(b.mode === "sea_air") - Number(a.mode === "sea_air") || a.code.localeCompare(b.code))
        .map((l) => l.code)
    : [];
  // The ETA injection moves open / in-production / shipped lines (merxa-api admin service).
  const delayable = pos?.ok ? pos.data.items.filter((p) => DELAYABLE.includes(p.status)).map((p) => p.code) : [];
  return (
    <>
      <PageHeader
        title="Demo Director"
        subtitle="Reset the scenario, inject supplier and carrier events and move the simulation clock. Demo environments only."
      />
      {allowed ? (
        <DemoDirector openPoCodes={delayable} laneCodes={laneCodes} />
      ) : (
        <div className="mx-card">
          <EmptyState title="Not available">The demo director needs demo mode and the Merxa Administrator role.</EmptyState>
        </div>
      )}
    </>
  );
}
