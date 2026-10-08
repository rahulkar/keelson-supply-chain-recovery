import { IconInfoCircle } from "@tabler/icons-react";
import type { Metadata } from "next";
import Link from "next/link";

import { CreateTransferOrder } from "@/components/forms/ManualForms";
import { Card } from "@/components/ui/Card";
import { PageHeader } from "@/components/ui/PageHeader";
import { ProblemPanel } from "@/components/ui/ProblemPanel";
import { StatusBadge } from "@/components/ui/StatusBadge";
import { formatDate, formatInt, isoWeek } from "@/lib/format";
import { transferProblem } from "@/lib/manual-change";
import { param, parseSkuQtyList, type SearchParams } from "@/lib/params";
import { apiGetAll, load } from "@/lib/server-api";
import type { Dc, InventoryPosition } from "@/lib/types";

export const metadata: Metadata = { title: "New transfer order" };

type Props = { searchParams: Promise<SearchParams> };

/**
 * New transfer order. Operator-guide deep links (`?from=DE1&to=NL1&ship_date=…&lines=SKU:qty,…`)
 * land here pre-filled; the page checks availability at the source DC line by line and
 * **Create transfer order** saves it (`POST /v1/transfer-orders` with an Idempotency-Key —
 * the same validation as a change batch; merxa-api audits it and emits
 * `transfer_order.created`).
 */
export default async function NewTransferOrderPage({ searchParams }: Props) {
  const sp = await searchParams;
  const from = param(sp, "from");
  const to = param(sp, "to");
  const shipDate = param(sp, "ship_date");
  const linesText = param(sp, "lines");
  const { lines, invalid } = parseSkuQtyList(linesText);

  const dcs = await load(apiGetAll<Dc>("/v1/dcs"));
  const dcCodes = dcs.ok ? dcs.data.items.map((d) => d.code) : [];
  const ready = !!from && !!to && lines.length > 0;
  const inventory = ready
    ? await load(
        apiGetAll<InventoryPosition>("/v1/inventory-positions", {
          location_type: "dc",
          location_code: [from, to],
          sku: lines.map((l) => l.sku),
        }),
      )
    : null;
  const pos = new Map((inventory?.ok ? inventory.data.items : []).map((p) => [`${p.location_code}/${p.sku}`, p]));
  // Running ATP draw per SKU (the same SKU may appear twice).
  const drawn = new Map<string, number>();
  const checked = lines.map((l) => {
    const src = pos.get(`${from}/${l.sku}`);
    const dst = pos.get(`${to}/${l.sku}`);
    const available = (src?.atp ?? 0) - (drawn.get(l.sku) ?? 0);
    drawn.set(l.sku, (drawn.get(l.sku) ?? 0) + l.qty);
    return { ...l, known: !!src, available, ok: !!src && l.qty <= available, toInTransit: dst?.in_transit ?? 0 };
  });
  const allOk = checked.length > 0 && checked.every((c) => c.ok);
  const problem =
    transferProblem(from, to, shipDate, lines, invalid) ?? (allOk ? null : "Some lines exceed the available stock at the source DC.");
  const units = lines.reduce((s, l) => s + l.qty, 0);
  const sameDc = !!from && from === to;

  return (
    <>
      <PageHeader
        crumbs={[{ label: "Transfer Orders", href: "/transfer-orders" }, { label: "New" }]}
        title="New transfer order"
        subtitle="Plan a DC-to-DC transfer and check availability at the source DC."
      />
      <div className="mx-banner mx-banner--info" role="note">
        <span className="mx-banner__icon" aria-hidden="true">
          <IconInfoCircle size={18} />
        </span>
        <div className="mx-banner__body">
          Check the transfer, then choose <strong>Create transfer order</strong>. Merxa validates stock at the source DC
          again when you save, reserves it and records the change in the audit log. Links from an operator guide
          arrive pre-filled.
        </div>
      </div>
      <form className="mx-toolbar" method="get" aria-label="Transfer details">
        <label className="mx-field">
          <span className="mx-field__label">From DC</span>
          <select className="mx-select" name="from" defaultValue={from ?? ""} required>
            <option value="">Select…</option>
            {dcCodes.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">To DC</span>
          <select className="mx-select" name="to" defaultValue={to ?? ""} required>
            <option value="">Select…</option>
            {dcCodes.map((c) => (
              <option key={c} value={c}>
                {c}
              </option>
            ))}
          </select>
        </label>
        <label className="mx-field">
          <span className="mx-field__label">Ship date</span>
          <input className="mx-input" type="date" name="ship_date" defaultValue={shipDate ?? ""} />
        </label>
        <label className="mx-field" style={{ flex: 1, minWidth: 280 }}>
          <span className="mx-field__label">Lines (SKU:qty, comma separated)</span>
          <input
            className="mx-input mx-mono"
            name="lines"
            defaultValue={linesText ?? ""}
            placeholder="PUF-W-BLK-M:120, PUF-W-BLK-L:80"
            required
          />
        </label>
        <button type="submit" className="mx-btn mx-btn--primary">
          Check availability
        </button>
      </form>
      {dcs.ok ? null : <ProblemPanel error={dcs.error} what="distribution centres" />}
      {invalid.length > 0 && (
        <div className="mx-banner mx-banner--danger" role="alert">
          Unreadable line(s): <span className="mx-mono">{invalid.join(", ")}</span>. Use <code>SKU:qty</code> with a positive whole
          quantity.
        </div>
      )}
      {sameDc && (
        <div className="mx-banner mx-banner--danger" role="alert">
          From and To DC must differ.
        </div>
      )}
      {inventory && !inventory.ok && <ProblemPanel error={inventory.error} what="inventory at the selected DCs" />}
      {ready && inventory?.ok && (
        <Card
          title={`${from} → ${to}`}
          meta={`${formatInt(lines.length)} line(s) · ${formatInt(units)} units${shipDate ? ` · ships ${formatDate(shipDate)} (${isoWeek(shipDate)})` : ""}`}
          actions={<StatusBadge status={allOk && !sameDc ? "ok" : "error"} label={allOk && !sameDc ? "Available" : "Not available"} />}
          flush
        >
          <table className="mx-table">
            <thead>
              <tr>
                <th scope="col">SKU</th>
                <th scope="col" className="mx-num">
                  Qty
                </th>
                <th scope="col" className="mx-num">
                  ATP at {from}
                </th>
                <th scope="col" className="mx-num">
                  ATP after
                </th>
                <th scope="col" className="mx-num">
                  In transit to {to} after
                </th>
                <th scope="col">Check</th>
              </tr>
            </thead>
            <tbody>
              {checked.map((c, i) => (
                <tr key={`${c.sku}-${i}`} className={c.ok ? undefined : "is-changed"}>
                  <td className="mx-mono">{c.sku}</td>
                  <td className="mx-num">{formatInt(c.qty)}</td>
                  <td className="mx-num">{c.known ? formatInt(c.available) : "—"}</td>
                  <td className="mx-num">{c.known ? formatInt(c.available - c.qty) : "—"}</td>
                  <td className="mx-num">{formatInt(c.toInTransit + c.qty)}</td>
                  <td>
                    {c.ok ? (
                      <StatusBadge status="ok" label="OK" />
                    ) : (
                      <StatusBadge status="error" label={c.known ? "Insufficient ATP" : "No stock record"} />
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ padding: 12, borderTop: "1px solid var(--mx-border)" }}>
            <CreateTransferOrder
              from={from ?? ""}
              to={to ?? ""}
              shipDate={shipDate ?? ""}
              lines={lines}
              problem={problem}
            />
          </div>
        </Card>
      )}
      <p className="mx-muted">
        <Link href="/transfer-orders">Back to Transfer Orders</Link>
      </p>
    </>
  );
}
