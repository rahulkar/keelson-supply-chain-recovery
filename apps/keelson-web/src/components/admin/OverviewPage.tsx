"use client";

import { Badge, Meter, Skeleton } from "@keelson/ui";
import { ArrowRight } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";
import { eur, when } from "@/lib/admin/format";
import { CONNECTOR_STATUS_LABELS, connectorStatusTone } from "@/lib/admin/tools";
import { useAdminApi, useResource, type Resource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { AdminPageHeader, AdminSection } from "./common";

function Tile<T>({ resource, label, children }: { resource: Resource<T>; label: string; children: (d: T) => ReactNode }) {
  if (resource.data !== undefined) return <>{children(resource.data)}</>;
  if (resource.error) return <p className="text-ui text-fg-muted">Couldn&apos;t load {label}: {resource.error}</p>;
  return <Skeleton className="h-10 w-full" label={`Loading ${label}`} />;
}

function MoreLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Link
      href={href}
      className="inline-flex items-center gap-1 text-ui font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
    >
      {children}
      <ArrowRight aria-hidden="true" className="size-3.5" />
    </Link>
  );
}

/** Admin overview: workspace, active spec version, connectors status, budget used, links. */
export function OverviewPage() {
  const admin = useAdminApi();
  const { me } = useSession();
  const tenant = me.user.active_tenant;
  const spec = useResource(() => admin.getSpec(), `spec:${tenant}`);
  const connectors = useResource(() => admin.connectors(), `connectors:${tenant}`);
  const gateway = useResource(() => admin.gatewayTenant(), `gw:${tenant}`);

  return (
    <>
      <AdminPageHeader title="Overview" description={tenant ? `Workspace ${tenant}` : "No workspace selected for your account."} />
      <div className="grid gap-4 md:grid-cols-3">
        <AdminSection title="Agents" description="Settings every new run uses.">
          <Tile resource={spec} label="the agent settings">
            {(s) => (
              <p className="flex flex-wrap items-center gap-2 text-ui text-fg">
                Active version <Badge tone="accent">{s.version}</Badge>
                {s.created_at ? <span className="text-caption text-fg-muted">since {when(s.created_at)}</span> : null}
              </p>
            )}
          </Tile>
          <MoreLink href="/admin/agents">Edit agents</MoreLink>
        </AdminSection>

        <AdminSection title="Connectors" description="Systems your agents can read from.">
          <Tile resource={connectors} label="connectors">
            {(list) =>
              list.length === 0 ? (
                <p className="text-ui text-fg-muted">No connectors yet.</p>
              ) : (
                <ul className="flex flex-col gap-1.5" aria-label="Connector status">
                  {list.map((c) => (
                    <li key={c.slug} className="flex flex-wrap items-center gap-2 text-ui">
                      <span className="min-w-0 flex-1 truncate text-fg">{c.name}</span>
                      <Badge size="sm" tone={connectorStatusTone(c.status)} dot>
                        {CONNECTOR_STATUS_LABELS[c.status] ?? c.status}
                      </Badge>
                      {c.tools.needs_review > 0 ? (
                        <Badge size="sm" tone="warning" variant="outline">
                          {c.tools.needs_review} to review
                        </Badge>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )
            }
          </Tile>
          <MoreLink href="/admin/connectors">Manage connectors</MoreLink>
        </AdminSection>

        <AdminSection title="AI budget" description="Spend through the AI gateway this period.">
          <Tile resource={gateway} label="the budget">
            {(g) =>
              typeof g.budget_eur === "number" && g.budget_eur > 0 ? (
                <Meter
                  label="Budget used"
                  value={g.spend_eur ?? 0}
                  max={g.budget_eur}
                  valueText={`${eur(g.spend_eur ?? 0)} of ${eur(g.budget_eur)}${g.budget_duration ? ` per ${g.budget_duration}` : ""}`}
                />
              ) : (
                <p className="text-ui text-fg-muted">Spent {eur(g.spend_eur ?? 0)} (no budget set).</p>
              )
            }
          </Tile>
          <MoreLink href="/admin/gateway">Open AI Gateway</MoreLink>
        </AdminSection>
      </div>
    </>
  );
}
