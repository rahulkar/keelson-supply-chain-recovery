import Link from "next/link";

import { isExternal } from "@/lib/attribution";
import { summarizeChange } from "@/lib/diff";
import { formatDateTime, formatValue, humanize } from "@/lib/format";
import { buildQuery } from "@/lib/http";
import type { AuditRow } from "@/lib/types";
import { EmptyState } from "../ui/Card";
import { ExternalChip } from "../ui/StatusBadge";

export function entityHref(row: Pick<AuditRow, "entity_type" | "entity_ref">): string | null {
  if (row.entity_type === "change_set") return `/change-batches/${encodeURIComponent(row.entity_ref)}`;
  if (row.entity_type === "po_line" || row.entity_type === "purchase_order") {
    const [po] = row.entity_ref.split("#");
    return po ? `/purchase-orders/${encodeURIComponent(po)}` : null;
  }
  if (row.entity_type === "transfer_order") return `/transfer-orders${buildQuery({ code: row.entity_ref })}`;
  if (row.entity_type === "inventory_position") {
    const [type, loc, sku] = row.entity_ref.split("/");
    if (type && loc && sku) return `/inventory${buildQuery({ location_type: type, location: loc, sku })}`;
  }
  return null;
}

/** Append-only audit rows (one per entity change), oldest first. */
export function AuditTable({ rows, showChangeSet = false }: { rows: AuditRow[]; showChangeSet?: boolean }) {
  if (rows.length === 0) return <EmptyState title="No audit entries." />;
  return (
    <div className="mx-table-wrap">
      <table className="mx-table mx-table--tight">
        <thead>
          <tr>
            <th scope="col">At</th>
            <th scope="col">Actor</th>
            <th scope="col">Via</th>
            <th scope="col">Approval</th>
            {showChangeSet && <th scope="col">Batch</th>}
            <th scope="col">Entity</th>
            <th scope="col">Action</th>
            <th scope="col">Change</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const href = entityHref(r);
            const external = isExternal(r);
            return (
              <tr key={r.id} className={external ? "is-changed" : undefined}>
                <td style={{ whiteSpace: "nowrap" }}>{formatDateTime(r.at)}</td>
                <td>
                  {r.actor_display || r.actor_username || "—"}
                  {r.client_id && <div className="mx-muted mx-mono">{r.client_id}</div>}
                </td>
                <td>{external ? <ExternalChip label={r.via ?? "External"} /> : r.via || "—"}</td>
                <td className="mx-mono">{r.approval_ref || "—"}</td>
                {showChangeSet && (
                  <td className="mx-mono">
                    {r.change_set_code ? <Link href={`/change-batches/${encodeURIComponent(r.change_set_code)}`}>{r.change_set_code}</Link> : "—"}
                  </td>
                )}
                <td>
                  <span className="mx-muted">{humanize(r.entity_type)}</span>{" "}
                  {href ? (
                    <Link href={href} className="mx-mono">
                      {r.entity_ref}
                    </Link>
                  ) : (
                    <span className="mx-mono">{r.entity_ref}</span>
                  )}
                </td>
                <td>{humanize(r.action)}</td>
                <td style={{ minWidth: 220 }}>{summarizeChange(r.before, r.after, formatValue) || "—"}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
