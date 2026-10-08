import { IconListCheck } from "@tabler/icons-react";
import type { Metadata } from "next";
import Link from "next/link";

import { AuditTable } from "@/components/change-batches/AuditTable";
import { PoLineEditForm, type PoLineAction } from "@/components/forms/ManualForms";
import { Card, KeyValues } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { ExternalChip, StatusBadge } from "@/components/ui/StatusBadge";
import { isExternal } from "@/lib/attribution";
import { formatDate, formatInt, isoWeek } from "@/lib/format";
import { decodeParam } from "@/lib/http";
import { parseQty, plannedVersion } from "@/lib/manual-change";
import { apiGet, apiGetAll, apiGetLatest, apiWalk, load } from "@/lib/server-api";
import { param, type SearchParams } from "@/lib/params";
import type { AuditRow, Factory, PurchaseOrder, Supplier } from "@/lib/types";

type Props = {
  params: Promise<{ code: string }>;
  searchParams: Promise<SearchParams>;
};

export async function generateMetadata({ params }: Props): Promise<Metadata> {
  return { title: `Purchase order ${decodeParam((await params).code)}` };
}

/**
 * Operator-guide deep links: `?line=3&action=reschedule&eta=2026-11-10&version=2` or
 * `action=change-qty&qty=…`. Reschedule / change-qty open the line's edit form pre-filled.
 */
function guideInstruction(sp: SearchParams): { line: number; text: string; edit: PoLineAction | null } | null {
  const line = Number(param(sp, "line"));
  const action = param(sp, "action");
  if (!Number.isInteger(line) || line <= 0 || !action) return null;
  if (action === "reschedule" && param(sp, "eta"))
    return { line, text: `Reschedule line ${line} to ETA ${formatDate(param(sp, "eta"))}.`, edit: "reschedule" };
  if (action === "change-qty" && param(sp, "qty"))
    return { line, text: `Change line ${line} ordered quantity to ${formatInt(Number(param(sp, "qty")))}.`, edit: "change-qty" };
  return { line, text: `Review line ${line}.`, edit: null };
}

const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

export default async function PurchaseOrderPage({ params, searchParams }: Props) {
  const code = decodeParam((await params).code);
  const sp = await searchParams;
  const po = await apiGet<PurchaseOrder>(`/v1/purchase-orders/${encodeURIComponent(code)}`, undefined, { notFoundOn404: true });
  const [suppliers, factories, lineAudit, headerAudit] = await Promise.all([
    load(apiGetAll<Supplier>("/v1/suppliers")),
    load(apiGetAll<Factory>("/v1/factories")),
    // The audit log pages oldest → newest and line refs are `<po>#<line>`: walk the po_line
    // history and keep this order's lines, so the newest changes are never cut off.
    load(
      (async () => {
        const rows: AuditRow[] = [];
        const prefix = `${po.code}#`;
        await apiWalk<AuditRow>("/v1/audit-log", { entity_type: "po_line" }, (page) => {
          for (const a of page) if (a.entity_ref.startsWith(prefix)) rows.push(a);
        });
        return rows;
      })(),
    ),
    load(apiGetLatest<AuditRow>("/v1/audit-log", { entity_type: "purchase_order", entity_ref: po.code }, { keep: 500 })),
  ]);
  const supplier = suppliers.ok ? suppliers.data.items.find((s) => s.code === po.supplier_code) : undefined;
  const factory = factories.ok ? factories.data.items.find((f) => f.code === po.factory_code) : undefined;
  const history = [
    ...(lineAudit.ok ? lineAudit.data : []),
    ...(headerAudit.ok ? headerAudit.data.items : []),
  ].sort((a, b) => a.id - b.id);
  const externallyChanged = new Map<number, AuditRow>();
  for (const a of history) {
    const lineNo = Number(a.entity_ref.split("#")[1]);
    if (Number.isInteger(lineNo) && isExternal(a)) externallyChanged.set(lineNo, a);
  }
  const guide = guideInstruction(sp);
  const guideLine = guide ? po.lines.find((l) => l.line_no === guide.line) : undefined;
  const etaParam = param(sp, "eta");
  const open = po.lines.reduce((s, l) => s + (l.qty_ordered - l.qty_received), 0);

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Purchase Orders", href: "/purchase-orders" }, { label: po.code }]}
        title={<span className="mx-mono">{po.code}</span>}
        badge={<StatusBadge status={po.status} />}
        subtitle={`${supplier?.name ?? po.supplier_code} → ${po.dest_dc} · ETA ${formatDate(po.eta_dc)} (${isoWeek(po.eta_dc)})`}
      />
      {guide && (
        <div className="mx-banner mx-banner--info" role="note">
          <span className="mx-banner__icon" aria-hidden="true">
            <IconListCheck size={18} />
          </span>
          <div className="mx-banner__body">
            <div className="mx-banner__title">Operator guide step</div>
            {guide.text} The line is highlighted below.
            {guide.edit && !guideLine ? ` Line ${guide.line} does not exist on this order.` : ""}
          </div>
        </div>
      )}
      {guide?.edit && guideLine && (
        <Card title={`Edit line ${guideLine.line_no}`} meta={`${guideLine.sku} · version ${guideLine.version}`}>
          <PoLineEditForm
            poCode={po.code}
            line={guideLine}
            action={guide.edit}
            initialEta={etaParam && ISO_DATE.test(etaParam) ? etaParam : null}
            initialQty={parseQty(param(sp, "qty") ?? "")}
            planned={plannedVersion(param(sp, "version"))}
          />
        </Card>
      )}
      <Card title="Details">
        <KeyValues
          items={[
            { label: "Supplier", value: supplier ? `${supplier.name} (${supplier.code})` : po.supplier_code },
            { label: "Factory", value: factory ? `${factory.name}, ${factory.country} (${factory.code})` : po.factory_code },
            { label: "Destination DC", value: <span className="mx-mono">{po.dest_dc}</span> },
            { label: "Status", value: <StatusBadge status={po.status} /> },
            { label: "Ex-factory", value: formatDate(po.ex_factory_date) },
            { label: "ETA DC", value: `${formatDate(po.eta_dc)} · ${isoWeek(po.eta_dc)}` },
            { label: "Lines", value: formatInt(po.line_count) },
            { label: "Units ordered", value: formatInt(po.total_qty) },
            { label: "Units open", value: formatInt(open) },
            { label: "Record version", value: `v${po.version}` },
          ]}
        />
      </Card>
      <Card title="Lines" meta={`${formatInt(po.lines.length)} line(s)`} flush>
        <div className="mx-table-wrap">
          <table className="mx-table">
            <thead>
              <tr>
                <th scope="col" className="mx-num">
                  Line
                </th>
                <th scope="col">SKU</th>
                <th scope="col" className="mx-num">
                  Ordered
                </th>
                <th scope="col" className="mx-num">
                  Received
                </th>
                <th scope="col" className="mx-num">
                  Open
                </th>
                <th scope="col">ETA DC</th>
                <th scope="col">Week</th>
                <th scope="col">Status</th>
                <th scope="col" className="mx-num">
                  Version
                </th>
                <th scope="col">Last external change</th>
              </tr>
            </thead>
            <tbody>
              {po.lines.map((l) => {
                const ext = externallyChanged.get(l.line_no);
                const cls = guide?.line === l.line_no ? "is-highlighted" : ext ? "is-changed" : undefined;
                return (
                  <tr key={l.line_no} className={cls} id={`line-${l.line_no}`}>
                    <td className="mx-num mx-mono">{l.line_no}</td>
                    <td className="mx-mono">{l.sku}</td>
                    <td className="mx-num">{formatInt(l.qty_ordered)}</td>
                    <td className="mx-num">{formatInt(l.qty_received)}</td>
                    <td className="mx-num">{formatInt(l.qty_ordered - l.qty_received)}</td>
                    <td>{formatDate(l.eta_dc)}</td>
                    <td className="mx-mono">{isoWeek(l.eta_dc)}</td>
                    <td>
                      <StatusBadge status={l.status} />
                    </td>
                    <td className="mx-num">v{l.version}</td>
                    <td>
                      {ext ? (
                        <span className="mx-inline-list">
                          <ExternalChip label={ext.via ?? "External"} />
                          {ext.change_set_code ? (
                            <Link className="mx-mono" href={`/change-batches/${encodeURIComponent(ext.change_set_code)}`}>
                              {ext.change_set_code}
                            </Link>
                          ) : null}
                        </span>
                      ) : (
                        "—"
                      )}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </Card>
      <Card title="History" meta="Audit entries for this order and its lines" flush>
        {lineAudit.ok ? <AuditTable rows={history} showChangeSet /> : <ProblemPanel error={lineAudit.error} what="order history" />}
      </Card>
    </>
  );
}
