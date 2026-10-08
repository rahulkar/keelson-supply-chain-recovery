"use client";

import { Badge, Button, Select, Table, toast, type TableColumn } from "@keelson/ui";
import { AlertTriangle, Check, UsersRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";
import type { ToolPatch } from "@/lib/admin/client";
import { adminError } from "@/lib/admin/format";
import {
  CLASSIFICATION_FIELDS,
  STATUS_LABELS,
  approveBlockedReason,
  differsFromSuggestion,
  isRemovedTool,
  label,
  readDowngradeLocked,
  statusTone,
  trustUpgradeLocked,
  type ClassificationKey,
} from "@/lib/admin/tools";
import { TOOL_BINDINGS, type ConnectorTool, type ToolBinding } from "@/lib/admin/types";
import { useAdminApi } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { announceChangeRequest } from "./SecondAdmin";

export const BINDING_LABELS: Record<ToolBinding, string> = {
  auto: "Automatic",
  approval_gate: "Ask for approval",
  deny: "Never",
};

export type ToolsTableProps = {
  slug: string;
  tools: readonly ConnectorTool[];
  /** Called with the server's answer (or the optimistic value when it answers without a body). */
  onToolUpdated: (tool: ConnectorTool) => void;
  caption?: string | undefined;
};

/**
 * Tool classification editors (access · content · egress · idempotency) and review actions.
 * Every change is saved right away (`PATCH …/tools/{name}`); tools that still need review are
 * highlighted and never reach agents. M7 four-eyes: for a tenant admin, turning a write /
 * destructive tool into a read tool, marking untrusted text as trusted data, or approving a tool
 * classified against the server's suggestion is a guarded change — the option stays available,
 * says "needs a second admin", and the server answers with a change request (Pending changes).
 */
export function ToolsTable({ slug, tools, onToolUpdated, caption = "Tools" }: ToolsTableProps) {
  const admin = useAdminApi();
  const { me } = useSession();
  const router = useRouter();
  const [busy, setBusy] = useState<string | null>(null);

  const patch = async (tool: ConnectorTool, change: ToolPatch, what: string) => {
    setBusy(tool.name);
    try {
      const out = await admin.patchTool(slug, tool.name, change);
      if (out.kind === "change_request") {
        announceChangeRequest(out.request, router.push);
        return;
      }
      onToolUpdated(out.value ?? { ...tool, ...change });
      toast.success(`${tool.name}: ${what}`);
    } catch (err) {
      toast.error(`Couldn't update ${tool.name}`, { description: adminError(err) });
    } finally {
      setBusy(null);
    }
  };

  /** Agent binding (plan §10): `auto` on a write / destructive tool is guarded for tenant admins. */
  const setBinding = async (tool: ConnectorTool, binding: ToolBinding) => {
    setBusy(tool.name);
    try {
      const out = await admin.setToolBinding(slug, tool.name, binding);
      if (out.kind === "change_request") {
        announceChangeRequest(out.request, router.push);
        return;
      }
      onToolUpdated(out.value ?? { ...tool, binding });
      toast.success(`${tool.name}: ${BINDING_LABELS[binding].toLowerCase()}`);
    } catch (err) {
      toast.error(`Couldn't update ${tool.name}`, { description: adminError(err) });
    } finally {
      setBusy(null);
    }
  };
  const platformAdmin = me.user.roles.includes("platform-admin");

  const classificationColumn = (f: (typeof CLASSIFICATION_FIELDS)[number]): TableColumn<ConnectorTool> => ({
    id: f.key,
    header: f.label,
    cell: (t) => {
      // The option a tenant admin can only *request*: "Read" for a write tool, "Trusted data" for untrusted text.
      const lockedValue = isRemovedTool(t)
        ? null
        : f.key === "access" && readDowngradeLocked(t, me.user)
          ? "read"
          : f.key === "content_trust" && trustUpgradeLocked(t, me.user)
            ? "trusted_data"
            : null;
      const suggested = t.suggested?.[f.key];
      const differs = differsFromSuggestion(t).includes(f.key as ClassificationKey);
      return (
        <div className="flex min-w-38 flex-col gap-0.5">
          <Select
            aria-label={`${f.label} of ${t.name}`}
            value={t[f.key]}
            disabled={busy === t.name || isRemovedTool(t)}
            options={f.values.map((v) => {
              const text = (f.labels as Record<string, string>)[v] ?? v;
              const guarded = lockedValue !== null && v === lockedValue && t[f.key] !== v;
              return { value: v, label: guarded ? `${text} (needs a second admin)` : text };
            })}
            onChange={(v) => void patch(t, { [f.key]: v } as ToolPatch, `${f.label.toLowerCase()} set to ${label(f.labels, v)}`)}
          />
          {lockedValue !== null && t[f.key] !== lockedValue ? (
            <span className="flex items-center gap-1 text-caption text-fg-subtle" data-second-admin-note="">
              <UsersRound aria-hidden="true" className="size-3" /> {label(f.labels, lockedValue)} needs a second admin
            </span>
          ) : null}
          {differs && suggested ? <span className="text-caption text-fg-subtle">Suggested: {label(f.labels, suggested)}</span> : null}
        </div>
      );
    },
  });

  const columns: TableColumn<ConnectorTool>[] = [
    {
      id: "name",
      header: "Tool",
      rowHeader: true,
      cell: (t) => (
        <div className="flex min-w-40 flex-col gap-0.5 text-left">
          <span className="flex flex-wrap items-center gap-1.5">
            <span className="font-mono text-caption font-medium">{t.name}</span>
            {t.drift ? (
              <Badge size="sm" tone="warning" icon={<AlertTriangle />}>
                Changed
              </Badge>
            ) : null}
          </span>
          {t.title || t.description ? <span className="text-caption font-normal text-fg-muted">{t.title ?? t.description}</span> : null}
        </div>
      ),
    },
    ...CLASSIFICATION_FIELDS.map(classificationColumn),
    {
      id: "binding",
      header: "When agents call it",
      cell: (t) => {
        const current = (TOOL_BINDINGS as readonly string[]).includes(t.binding ?? "") ? (t.binding as ToolBinding) : "approval_gate";
        const autoGuarded = !platformAdmin && t.access !== "read" && current !== "auto";
        return (
          <div className="flex min-w-38 flex-col gap-0.5">
            <Select
              aria-label={`When agents call ${t.name}`}
              value={current}
              disabled={busy === t.name || isRemovedTool(t)}
              options={TOOL_BINDINGS.map((b) => ({ value: b, label: b === "auto" && autoGuarded ? `${BINDING_LABELS[b]} (needs a second admin)` : BINDING_LABELS[b] }))}
              onChange={(v) => void setBinding(t, v as ToolBinding)}
            />
            {autoGuarded ? (
              <span className="flex items-center gap-1 text-caption text-fg-subtle" data-second-admin-note="">
                <UsersRound aria-hidden="true" className="size-3" /> Automatic needs a second admin
              </span>
            ) : null}
          </div>
        );
      },
    },
    {
      id: "status",
      header: "Status",
      nowrap: true,
      cell: (t) => (
        <Badge size="sm" tone={statusTone(t.status)} dot>
          {STATUS_LABELS[t.status] ?? t.status}
        </Badge>
      ),
    },
    {
      id: "actions",
      header: "Actions",
      srOnlyHeader: true,
      nowrap: true,
      cell: (t) => {
        if (isRemovedTool(t)) {
          return <span className="text-caption whitespace-normal text-fg-subtle">No longer offered by the server.</span>;
        }
        const blocked = t.status !== "approved" ? approveBlockedReason(t, me.user) : null;
        const reasonId = `approve-why-${t.name}`;
        return (
        <span className="flex flex-wrap items-center gap-1">
          {t.status !== "approved" ? (
            <Button
              size="sm"
              variant={t.status === "needs_review" ? "primary" : "secondary"}
              icon={<Check className="size-3.5" />}
              loading={busy === t.name}
              aria-label={`Approve ${t.name}`}
              aria-describedby={blocked ? reasonId : undefined}
              onClick={() => void patch(t, { status: "approved" }, "approved")}
            >
              Approve
            </Button>
          ) : null}
          {t.status !== "disabled" ? (
            <Button
              size="sm"
              variant="ghost"
              disabled={busy === t.name}
              aria-label={`Disable ${t.name}`}
              onClick={() => void patch(t, { status: "disabled" }, "disabled")}
            >
              Disable
            </Button>
          ) : null}
          {blocked ? (
            <span id={reasonId} className="basis-full text-caption whitespace-normal text-fg-subtle">
              {blocked}
            </span>
          ) : null}
        </span>
        );
      },
    },
  ];

  return (
    <Table
      caption={caption}
      hideCaption
      rows={tools}
      rowKey={(t) => t.name}
      rowTone={(t) =>
        isRemovedTool(t) || t.status === "disabled" ? "muted" : t.status === "needs_review" || t.drift ? "warning" : undefined
      }
      rowProps={(t) => ({ "data-status": t.status })}
      columns={columns}
      empty="No tools discovered yet. Run a test to list them."
    />
  );
}
