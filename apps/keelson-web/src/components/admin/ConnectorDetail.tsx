"use client";

import { Badge, Button, ConfirmDialog, EmptyState, Select, StepLog, Table, cn, toast } from "@keelson/ui";
import { FileCode2, Play, Plug, Power, PowerOff, Trash2 } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState, type ReactNode } from "react";
import { adminError, when } from "@/lib/admin/format";
import { AUTH_KIND_LABELS, CONNECTOR_STATUS_LABELS, connectorStatusTone, label } from "@/lib/admin/tools";
import { TRUST_LEVEL_LABELS } from "@/lib/admin/wizard";
import { connectorTestSchema, type ConnectorEvent, type ConnectorTest } from "@/lib/admin/types";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { hasAnyRole } from "@/lib/roles";
import { testLogItems, type TestState } from "@/lib/admin/wizard";
import { AdminPageHeader, AdminSection, Facts, InlineError, ResourceView } from "./common";
import { CapabilitiesSection, CrawlSection } from "./ConnectorCapabilities";
import { SecondAdminNote, announceChangeRequest } from "./SecondAdmin";
import { TierBadges, linkClass } from "./ConnectorsPage";
import { ToolsTable } from "./ToolsTable";

/** Connector detail: tools table, last test log, recent webhook events; test / activate / disable / delete. */
export function ConnectorDetail({ slug }: { slug: string }) {
  const admin = useAdminApi();
  const router = useRouter();
  // The detail route carries the stored last test (the list doesn't).
  const list = useResource(() => admin.connector(slug), `connector:${slug}`);
  const tools = useResource(() => admin.connectorTools(slug), `tools:${slug}`);
  const events = useResource(() => admin.connectorEvents(slug, 20), `events:${slug}`);
  const [test, setTest] = useState<TestState>({ status: "idle" });
  const [busy, setBusy] = useState<"activate" | "disable" | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [trustBusy, setTrustBusy] = useState(false);
  const { me } = useSession();
  // Platform admins' guarded changes apply directly; a tenant admin's become change requests.
  const guarded = !hasAnyRole(me.user, ["platform-admin"]);

  const connector = list.data;
  // The list may carry the stored last test (`last_test jsonb`); a test run here replaces it.
  const storedTest = connector ? connectorTestSchema.safeParse(connector.last_test) : null;
  const shownTest: TestState = test.status !== "idle" ? test : storedTest?.success ? { status: "done", result: storedTest.data } : { status: "idle" };

  const runTest = async () => {
    setTest({ status: "running" });
    try {
      const result: ConnectorTest = await admin.testConnector(slug);
      setTest({ status: "done", result });
      list.reload();
      tools.reload();
    } catch (err) {
      setTest({ status: "error", message: adminError(err) });
    }
  };

  const setStatus = async (kind: "activate" | "disable") => {
    setBusy(kind);
    setActionError(null);
    try {
      if (kind === "activate") {
        const out = await admin.activateConnector(slug);
        // Enabling an external-egress connector is guarded: a second admin approves it.
        if (out.kind === "change_request") {
          announceChangeRequest(out.request, router.push);
          return;
        }
      } else await admin.disableConnector(slug);
      toast.success(kind === "activate" ? "Connector activated" : "Connector disabled");
      list.reload();
    } catch (err) {
      setActionError(adminError(err));
    } finally {
      setBusy(null);
    }
  };

  const setTrust = async (level: "first_party" | "third_party") => {
    if (!connector || level === connector.trust_level) return;
    setTrustBusy(true);
    setActionError(null);
    try {
      const out = await admin.setConnectorTrust(slug, level);
      if (out.kind === "change_request") {
        announceChangeRequest(out.request, router.push);
        return;
      }
      toast.success(`Trust level: ${label(TRUST_LEVEL_LABELS, level)}`);
      list.reload();
    } catch (err) {
      setActionError(adminError(err));
    } finally {
      setTrustBusy(false);
    }
  };

  if (!connector && list.errorStatus === 404) {
    return (
      <EmptyState
        icon={<Plug />}
        headingLevel={1}
        title="Connector not found"
        description={`There is no connector "${slug}" in this workspace.`}
        action={
          <Link href="/admin/connectors" className={linkClass}>
            Back to connectors
          </Link>
        }
      />
    );
  }

  return (
    <>
      <AdminPageHeader
        title={connector?.name ?? slug}
        description={
          connector ? (
            <span className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-caption">{connector.slug}</span>
              <Badge size="sm" tone={connectorStatusTone(connector.status)} dot>
                {CONNECTOR_STATUS_LABELS[connector.status] ?? connector.status}
              </Badge>
              <TierBadges tiers={connector.tier} />
            </span>
          ) : (
            "Loading…"
          )
        }
        actions={
          <>
            <Link href="/admin/connectors" className={cn(linkClass, "text-ui")}>
              All connectors
            </Link>
            {connector?.tier.includes("A") ? (
              <Link
                href={`/admin/connectors/${encodeURIComponent(slug)}/mappings`}
                className="inline-flex h-8 items-center gap-1.5 rounded-sm border border-border-strong px-3 text-ui font-medium text-fg hover:bg-surface-2 focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-focus"
              >
                <FileCode2 aria-hidden="true" className="size-4" /> Mappings
              </Link>
            ) : null}
            <Button size="sm" variant="secondary" icon={<Play className="size-4" />} loading={test.status === "running"} onClick={() => void runTest()}>
              Run test
            </Button>
            {connector?.status === "active" ? (
              <Button size="sm" variant="secondary" icon={<PowerOff className="size-4" />} loading={busy === "disable"} onClick={() => void setStatus("disable")}>
                Disable
              </Button>
            ) : (
              <Button size="sm" variant="primary" icon={<Power className="size-4" />} loading={busy === "activate"} onClick={() => void setStatus("activate")}>
                Activate
              </Button>
            )}
            <Button size="sm" variant="ghost" icon={<Trash2 className="size-4" />} onClick={() => setConfirmDelete(true)}>
              Delete
            </Button>
          </>
        }
      />
      {actionError ? <InlineError message={actionError} /> : null}

      <AdminSection title="Details">
        <ResourceView resource={list} label="the connector">
          {() =>
            connector ? (
              <Facts
                items={[
                  ["URL", <span key="u" className="font-mono text-caption break-all">{connector.url ?? "—"}</span>],
                  ["Authentication", label(AUTH_KIND_LABELS, connector.auth_kind)],
                  [
                    "Trust level",
                    <span key="t" className="flex flex-col gap-1">
                      <Select
                        aria-label={`Trust level of ${connector.name ?? slug}`}
                        value={connector.trust_level ?? "third_party"}
                        disabled={trustBusy}
                        onChange={(v) => void setTrust(v === "first_party" ? "first_party" : "third_party")}
                        options={Object.entries(TRUST_LEVEL_LABELS).map(([value, text]) => ({ value, label: text }))}
                      />
                      {guarded && connector.trust_level !== "first_party" ? <SecondAdminNote>Making it first party needs a second admin</SecondAdminNote> : null}
                    </span>,
                  ],
                  ...(connector.webhook_path
                    ? ([["Webhook endpoint", <span key="w" className="font-mono text-caption break-all">{connector.webhook_path}</span>]] as [string, ReactNode][])
                    : []),
                  ["Last test", when(connector.last_test_at)],
                ]}
              />
            ) : null
          }
        </ResourceView>
      </AdminSection>

      <CapabilitiesSection slug={slug} />
      {connector?.tier.includes("B") ? <CrawlSection slug={slug} sync="snapshot" /> : connector?.tier.includes("A") ? <CrawlSection slug={slug} sync="crawl" /> : null}

      <AdminSection title="Tools" description="Only approved, read-only tools that return trusted data are given to agents.">
        <ResourceView resource={tools} label="tools">
          {(rows) => <ToolsTable slug={slug} tools={rows} onToolUpdated={(t) => tools.setData((prev) => prev?.map((x) => (x.name === t.name ? t : x)))} />}
        </ResourceView>
      </AdminSection>

      <AdminSection title="Last test">
        {shownTest.status === "idle" ? (
          <p className="text-ui text-fg-muted">No test log yet. Run a test to see each step.</p>
        ) : (
          <>
            <StepLog label="Connection test" steps={testLogItems(shownTest)} />
            {shownTest.status === "error" ? <InlineError message={`The test could not run: ${shownTest.message}`} /> : null}
          </>
        )}
      </AdminSection>

      <AdminSection title="Recent webhook events" description="Signed events received from this system (newest first).">
        <ResourceView resource={events} label="events">
          {(rows) => (
            <Table<ConnectorEvent>
              caption="Recent webhook events"
              hideCaption
              rows={rows}
              rowKey={(e) => e.key}
              empty="No events received."
              columns={[
                { id: "type", header: "Type", rowHeader: true, cell: (e) => <span className="font-mono text-caption">{e.type ?? "—"}</span> },
                { id: "id", header: "Event ID", cell: (e) => <span className="font-mono text-caption break-all text-fg-muted">{e.event_id ?? "—"}</span> },
                { id: "received", header: "Received", nowrap: true, cell: (e) => when(e.received_at) },
                { id: "processed", header: "Processed", nowrap: true, cell: (e) => (e.processed_at ? when(e.processed_at) : <span className="text-fg-muted">Not yet</span>) },
              ]}
            />
          )}
        </ResourceView>
      </AdminSection>

      <ConfirmDialog
        open={confirmDelete}
        onOpenChange={setConfirmDelete}
        title={`Delete ${connector?.name ?? slug}?`}
        description={
          guarded
            ? "Its tools are removed from this workspace. Deleting needs a second admin: this sends a request for approval. A connector bound in the agent settings can't be deleted; unbind it on the Agents page first."
            : "Its tools are removed from this workspace. A connector bound in the agent settings can't be deleted; unbind it on the Agents page first."
        }
        confirmLabel={guarded ? "Request deletion" : "Delete connector"}
        tone="danger"
        onConfirm={async () => {
          try {
            const out = await admin.deleteConnector(slug);
            if (out.kind === "change_request") {
              announceChangeRequest(out.request, router.push);
              return;
            }
            toast.success("Connector deleted");
            router.push("/admin/connectors");
          } catch (err) {
            toast.error("Couldn't delete the connector", { description: adminError(err) });
            return false;
          }
        }}
      />
    </>
  );
}
