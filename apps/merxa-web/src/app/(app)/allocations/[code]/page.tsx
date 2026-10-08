import { IconListCheck } from "@tabler/icons-react";
import type { Metadata } from "next";
import Link from "next/link";

import { AuditTable } from "@/components/change-batches/AuditTable";
import { AllocationQtyForm } from "@/components/forms/ManualForms";
import { Card, KeyValues } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { formatInt } from "@/lib/format";
import { decodeParam } from "@/lib/http";
import { parseQty, plannedVersion } from "@/lib/manual-change";
import { param, type SearchParams } from "@/lib/params";
import { apiGet, apiGetLatest, load } from "@/lib/server-api";
import type { Allocation, AuditRow } from "@/lib/types";

type Props = {
  params: Promise<{ code: string }>;
  searchParams: Promise<SearchParams>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return { title: `Allocation ${decodeParam((await params).code)}` };
}

/**
 * One allocation with an **Edit quantity** form. Operator-guide deep links
 * (`?action=set-qty&qty=…&version=…`) open it pre-filled; saving sends `PATCH
 * /v1/allocations/{code}` with If-Match + Idempotency-Key (merxa-api emits `allocation.updated`).
 */
export default async function AllocationPage({ params, searchParams }: Props) {
  const code = decodeParam((await params).code);
  const sp = await searchParams;
  const a = await apiGet<Allocation>(`/v1/allocations/${encodeURIComponent(code)}`, undefined, { notFoundOn404: true });
  const history = await load(
    apiGetLatest<AuditRow>("/v1/audit-log", { entity_type: "allocation", entity_ref: a.code }, { keep: 200 }),
  );
  const fromGuide = param(sp, "action") === "set-qty";
  const guideQty = fromGuide ? parseQty(param(sp, "qty") ?? "") : null;

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Inventory", href: "/inventory" }, { label: "Allocations" }, { label: a.code }]}
        title={<span className="mx-mono">{a.code}</span>}
        badge={<StatusBadge status={a.status} />}
        subtitle={`${a.sku} · ${a.dc_code} → ${a.store_code} · ship week ${a.ship_week}`}
      />
      {fromGuide && (
        <div className="mx-banner mx-banner--info" role="note">
          <span className="mx-banner__icon" aria-hidden="true">
            <IconListCheck size={18} />
          </span>
          <div className="mx-banner__body">
            <div className="mx-banner__title">Operator guide step</div>
            Set the quantity to {guideQty === null ? "the value in your guide" : formatInt(guideQty)} and save.
          </div>
        </div>
      )}
      <Card title="Details">
        <KeyValues
          items={[
            { label: "SKU", value: <span className="mx-mono">{a.sku}</span> },
            { label: "From DC", value: <span className="mx-mono">{a.dc_code}</span> },
            { label: "Store", value: <span className="mx-mono">{a.store_code}</span> },
            { label: "Ship week", value: <span className="mx-mono">{a.ship_week}</span> },
            { label: "Quantity", value: formatInt(a.qty) },
            { label: "Status", value: <StatusBadge status={a.status} /> },
            { label: "Record version", value: `v${a.version}` },
          ]}
        />
      </Card>
      <Card title="Edit quantity">
        <AllocationQtyForm allocation={a} initialQty={guideQty} planned={plannedVersion(param(sp, "version"))} />
      </Card>
      <Card title="History" meta="Audit entries for this allocation" flush>
        {history.ok ? <AuditTable rows={history.data.items} showChangeSet /> : <ProblemPanel error={history.error} what="allocation history" />}
      </Card>
      <p className="mx-muted">
        <Link href={`/inventory?location_type=dc&location_code=${encodeURIComponent(a.dc_code)}`}>Inventory at {a.dc_code}</Link>
      </p>
    </>
  );
}
