import Link from "next/link";

import { diffItem, numericDelta } from "@/lib/diff";
import { formatDelta, formatValue, opLabel } from "@/lib/format";
import type { ChangeSetItem, Conflict } from "@/lib/types";
import { EmptyState } from "../ui/Card";
import { StatusBadge } from "../ui/StatusBadge";

/** Link target for an item's affected record, when Merxa has a page for it. */
export function targetHref(item: Pick<ChangeSetItem, "op" | "target_ref">): string | null {
  if (!item.target_ref) return null;
  if (item.op.startsWith("po_line.") || item.op.startsWith("po.")) {
    const [po] = item.target_ref.split("#");
    return po ? `/purchase-orders/${encodeURIComponent(po)}` : null;
  }
  return null;
}

/** Per-item before/after table of a change batch (values from `:validate`). */
export function ItemDiffTable({ items, conflicts = [] }: { items: ChangeSetItem[]; conflicts?: Conflict[] }) {
  if (items.length === 0) return <EmptyState title="This batch has no items." />;
  const conflictBySeq = new Map(conflicts.map((c) => [c.seq, c]));
  return (
    <div>
      {items.map((item) => {
        const rows = diffItem(item.before, item.after);
        const href = targetHref(item);
        const conflict = conflictBySeq.get(item.seq);
        return (
          <section className="mx-item" key={item.seq} aria-label={`Item ${item.seq}: ${opLabel(item.op)}`}>
            <div className="mx-item__head">
              <span className="mx-item__seq">#{item.seq}</span>
              <span className="mx-item__op">{opLabel(item.op)}</span>
              <code>{item.op}</code>
              {item.target_ref &&
                (href ? (
                  <Link href={href} className="mx-mono">
                    {item.target_ref}
                  </Link>
                ) : (
                  <span className="mx-mono">{item.target_ref}</span>
                ))}
              {item.expected_version !== null && <span className="mx-muted">expected v{item.expected_version}</span>}
              <span style={{ marginLeft: "auto" }}>
                <StatusBadge status={item.status} />
              </span>
            </div>
            {(item.error || conflict) && (
              <div className="mx-item__error" role="alert">
                {conflict
                  ? `Conflict: ${conflict.reason}` +
                    (conflict.actual_version !== null ? ` (expected v${conflict.expected_version ?? "?"}, found v${conflict.actual_version})` : "")
                  : item.error}
              </div>
            )}
            {rows.length === 0 ? (
              <p className="mx-muted" style={{ margin: 0, padding: "6px 12px" }}>
                {item.status === "error"
                  ? "No before/after values were recorded for this item (it failed validation or apply)."
                  : "Not validated yet: before/after values appear once the batch is validated."}
              </p>
            ) : (
              <div className="mx-table-wrap">
                <table className="mx-table mx-table--tight">
                  <thead>
                    <tr>
                      <th scope="col" style={{ width: "40%" }}>
                        Field
                      </th>
                      <th scope="col" className="mx-num">
                        Before
                      </th>
                      <th scope="col" className="mx-num">
                        After
                      </th>
                      <th scope="col" className="mx-num">
                        Change
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => {
                      const delta = r.changed ? numericDelta(r) : null;
                      return (
                        <tr key={r.path} className={r.changed ? "is-changed" : undefined} data-changed={r.changed || undefined}>
                          <td>{r.label}</td>
                          <td className="mx-num mx-diff-old">{formatValue(r.before)}</td>
                          <td className={r.changed ? "mx-num mx-diff-new" : "mx-num"}>{formatValue(r.after)}</td>
                          <td className="mx-num">
                            {r.changed ? (delta !== null ? <span className="mx-diff-delta">{formatDelta(delta)}</span> : "changed") : ""}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </section>
        );
      })}
    </div>
  );
}
