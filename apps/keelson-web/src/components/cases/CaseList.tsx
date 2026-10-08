import { Badge } from "@keelson/ui";
import { formatDateTime } from "@keelson/viz";
import Link from "next/link";
import type { CaseSummary } from "@/lib/api/types";
import { UpdatedBadge } from "./UpdatedBadge";

export function caseStatusLabel(status: string): string {
  const s = status.replace(/_/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : "Open";
}

function statusTone(status: string): "neutral" | "accent" | "success" | "warning" {
  if (status === "awaiting_approval") return "warning";
  if (status === "closed" || status === "resolved" || status === "committed") return "success";
  if (status === "open" || status === "investigating") return "accent";
  return "neutral";
}

/** Case history rows (history drawer and `/cases`). Each row is one link. */
export function CaseList({ items, onNavigate }: { items: CaseSummary[]; onNavigate?: (() => void) | undefined }) {
  return (
    <ul className="flex flex-col gap-1" aria-label="Cases">
      {items.map((c) => (
        <li key={c.id}>
          <Link
            href={`/cases/${encodeURIComponent(c.id)}`}
            {...(onNavigate ? { onClick: onNavigate } : {})}
            className="flex flex-col gap-1 rounded-md px-3 py-2.5 hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus"
          >
            <span className="flex items-center gap-2">
              <span className="min-w-0 flex-1 truncate text-ui font-medium text-fg">{c.title || "Untitled case"}</span>
              <UpdatedBadge caseId={c.id} updatedAt={c.updated_at} />
              {c.mode === "what_if" ? <Badge tone="info">What-if</Badge> : null}
              <Badge tone={statusTone(c.status)}>{caseStatusLabel(c.status)}</Badge>
            </span>
            {c.last_message_preview ? (
              <span className="line-clamp-1 text-caption text-fg-muted">{c.last_message_preview}</span>
            ) : null}
            {c.updated_at ? (
              <span className="text-caption text-fg-muted">
                <span className="sr-only">Updated </span>
                {formatDateTime(c.updated_at)}
              </span>
            ) : null}
          </Link>
        </li>
      ))}
    </ul>
  );
}
