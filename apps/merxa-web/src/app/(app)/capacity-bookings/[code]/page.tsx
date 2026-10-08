import { IconListCheck } from "@tabler/icons-react";
import type { Metadata } from "next";
import Link from "next/link";

import { AuditTable } from "@/components/change-batches/AuditTable";
import { CancelBookingForm } from "@/components/forms/ManualForms";
import { Card, KeyValues } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { formatDate, formatInt } from "@/lib/format";
import { decodeParam } from "@/lib/http";
import { plannedVersion } from "@/lib/manual-change";
import { param, type SearchParams } from "@/lib/params";
import { apiGet, apiGetLatest, load } from "@/lib/server-api";
import type { AuditRow, CapacityBookingDetail } from "@/lib/types";

type Props = {
  params: Promise<{ code: string }>;
  searchParams: Promise<SearchParams>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return { title: `Capacity booking ${decodeParam((await params).code)}` };
}

/**
 * One capacity booking: lane week, linked shipments, history and **Cancel booking**
 * (`POST /v1/capacity-bookings/{code}:cancel` with If-Match + Idempotency-Key; merxa-api
 * emits `capacity_booking.cancelled`). Operator-guide links use `?action=cancel&version=…`.
 */
export default async function CapacityBookingPage({ params, searchParams }: Props) {
  const code = decodeParam((await params).code);
  const sp = await searchParams;
  const b = await apiGet<CapacityBookingDetail>(`/v1/capacity-bookings/${encodeURIComponent(code)}`, undefined, {
    notFoundOn404: true,
  });
  const history = await load(
    apiGetLatest<AuditRow>("/v1/audit-log", { entity_type: "capacity_booking", entity_ref: b.code }, { keep: 200 }),
  );
  const fromGuide = param(sp, "action") === "cancel";
  const lw = b.lane_week;

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Sailings & Lanes", href: "/sailings?tab=capacity" }, { label: "Capacity bookings" }, { label: b.code }]}
        title={<span className="mx-mono">{b.code}</span>}
        badge={<StatusBadge status={b.status} />}
        subtitle={`${b.lane_code} · ${b.iso_week} · ${formatInt(b.qty_units)} units`}
      />
      {fromGuide && (
        <div className="mx-banner mx-banner--info" role="note">
          <span className="mx-banner__icon" aria-hidden="true">
            <IconListCheck size={18} />
          </span>
          <div className="mx-banner__body">
            <div className="mx-banner__title">Operator guide step</div>
            Cancel this booking to release its capacity.
          </div>
        </div>
      )}
      <Card title="Details">
        <KeyValues
          items={[
            { label: "Lane", value: <span className="mx-mono">{b.lane_code}</span> },
            { label: "Week", value: <span className="mx-mono">{b.iso_week}</span> },
            { label: "Units", value: formatInt(b.qty_units) },
            { label: "Basis", value: b.basis.replace("per_", "per ") },
            { label: "Status", value: <StatusBadge status={b.status} /> },
            { label: "Created via", value: b.created_via },
            {
              label: "Lane capacity that week",
              value:
                lw.weekly_capacity === null
                  ? "No rate card"
                  : `${formatInt(lw.weekly_capacity)}${lw.remaining_capacity === null ? "" : ` · ${formatInt(lw.remaining_capacity)} remaining`}`,
            },
            { label: "Record version", value: `v${b.version}` },
          ]}
        />
      </Card>
      <Card title="Cancel booking">
        <CancelBookingForm booking={b} planned={plannedVersion(param(sp, "version"))} />
      </Card>
      <Card title="Shipments on this booking" meta={`${formatInt(b.shipments.length)} shipment(s)`} flush>
        {b.shipments.length === 0 ? (
          <p className="mx-muted" style={{ padding: 12 }}>
            No shipment references this booking.
          </p>
        ) : (
          <table className="mx-table">
            <thead>
              <tr>
                <th scope="col">Shipment</th>
                <th scope="col">PO</th>
                <th scope="col">Status</th>
                <th scope="col">ETD</th>
                <th scope="col">ETA</th>
              </tr>
            </thead>
            <tbody>
              {b.shipments.map((s) => (
                <tr key={s.code}>
                  <td>
                    <Link className="mx-mono" href={`/shipments?code=${encodeURIComponent(s.code)}`}>
                      {s.code}
                    </Link>
                  </td>
                  <td>
                    <Link className="mx-mono" href={`/purchase-orders/${encodeURIComponent(s.po_code)}`}>
                      {s.po_code}
                    </Link>
                  </td>
                  <td>
                    <StatusBadge status={s.status} />
                  </td>
                  <td>{formatDate(s.etd)}</td>
                  <td>{formatDate(s.eta)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>
      <Card title="History" meta="Audit entries for this booking" flush>
        {history.ok ? <AuditTable rows={history.data.items} showChangeSet /> : <ProblemPanel error={history.error} what="booking history" />}
      </Card>
    </>
  );
}
