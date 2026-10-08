"use client";

import { Badge, Table } from "@keelson/ui";
import { Plus } from "lucide-react";
import Link from "next/link";
import { when } from "@/lib/admin/format";
import { AUTH_KIND_LABELS, CONNECTOR_STATUS_LABELS, TIER_INFO, connectorStatusTone, label } from "@/lib/admin/tools";
import type { ConnectorSummary } from "@/lib/admin/types";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { AdminPageHeader, AdminSection, ResourceView } from "./common";

export function TierBadges({ tiers }: { tiers: readonly string[] }) {
  if (tiers.length === 0) return <span className="text-caption text-fg-subtle">Not tested</span>;
  return (
    <ul className="flex flex-wrap gap-1" aria-label="Capability tiers">
      {tiers.map((t) => (
        <li key={t}>
          <Badge size="sm" tone="info" variant="outline" title={TIER_INFO[t]}>
            <span aria-hidden="true">{t}</span>
            <span className="sr-only">{TIER_INFO[t] ?? `Tier ${t}`}</span>
          </Badge>
        </li>
      ))}
    </ul>
  );
}

export const linkClass =
  "font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus";

export function ConnectorsPage() {
  const admin = useAdminApi();
  const { me } = useSession();
  const list = useResource(() => admin.connectors(), `connectors:${me.user.active_tenant}`);
  return (
    <>
      <AdminPageHeader
        title="Connectors"
        description="Systems your agents read from over MCP. Tools must be reviewed before agents can use them."
        actions={
          <Link
            href="/admin/connectors/new"
            className="inline-flex h-8 items-center gap-1.5 rounded-pill bg-accent px-3 text-caption font-medium text-accent-fg shadow-sm hover:bg-accent-hover focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            <Plus aria-hidden="true" className="size-4" />
            Add connector
          </Link>
        }
      />
      <AdminSection title="Registered connectors">
        <ResourceView resource={list} label="connectors">
          {(rows) => (
            <Table<ConnectorSummary>
              caption="Registered connectors"
              hideCaption
              rows={rows}
              rowKey={(c) => c.slug}
              rowTone={(c) => (c.tools.needs_review > 0 ? "warning" : undefined)}
              empty="No connectors yet. Add one to let agents read from another system."
              columns={[
                {
                  id: "name",
                  header: "Connector",
                  rowHeader: true,
                  cell: (c) => (
                    <div className="flex flex-col text-left">
                      <Link href={`/admin/connectors/${encodeURIComponent(c.slug)}`} className={linkClass}>
                        {c.name}
                      </Link>
                      <span className="font-mono text-caption font-normal text-fg-muted">{c.slug}</span>
                    </div>
                  ),
                },
                { id: "url", header: "URL", cell: (c) => <span className="font-mono text-caption break-all text-fg-muted">{c.url ?? "—"}</span> },
                { id: "auth", header: "Authentication", cell: (c) => label(AUTH_KIND_LABELS, c.auth_kind) },
                { id: "tier", header: "Tier", cell: (c) => <TierBadges tiers={c.tier} /> },
                {
                  id: "status",
                  header: "Status",
                  nowrap: true,
                  cell: (c) => (
                    <Badge size="sm" tone={connectorStatusTone(c.status)} dot>
                      {CONNECTOR_STATUS_LABELS[c.status] ?? c.status}
                    </Badge>
                  ),
                },
                {
                  id: "tools",
                  header: "Tools",
                  cell: (c) => (
                    <span className="flex flex-wrap gap-1 text-caption">
                      <span>{c.tools.approved} approved</span>
                      {c.tools.needs_review > 0 ? (
                        <Badge size="sm" tone="warning">
                          {c.tools.needs_review} to review
                        </Badge>
                      ) : null}
                      {c.tools.disabled > 0 ? <span className="text-fg-muted">· {c.tools.disabled} disabled</span> : null}
                    </span>
                  ),
                },
                { id: "tested", header: "Last test", nowrap: true, cell: (c) => <span className="text-caption">{when(c.last_test_at)}</span> },
              ]}
            />
          )}
        </ResourceView>
      </AdminSection>
    </>
  );
}
