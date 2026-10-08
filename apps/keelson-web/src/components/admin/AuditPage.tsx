"use client";

import { Badge, Button, Field, JsonDiff, Select, Table, TextInput } from "@keelson/ui";
import { ChevronDown, RotateCw } from "lucide-react";
import Link from "next/link";
import { useState, type FormEvent } from "react";
import {
  AUDIT_CATEGORIES,
  AUDIT_CATEGORY_LABELS,
  activityDetail,
  categoryOf,
  dayBounds,
  inDateRange,
  isAuditCategory,
  type ActivityEntry,
  type ActivityPage,
  type ActivityQuery,
  type AuditCategory,
} from "@/lib/admin/audit";
import { AUDIT_PAGE_MAX } from "@/lib/admin/client";
import { adminError, when } from "@/lib/admin/format";
import type { AuditEntry } from "@/lib/admin/types";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { useSession } from "@/lib/client/session";
import { hasAnyRole } from "@/lib/roles";
import { AdminPageHeader, AdminSection, InlineError, ResourceView } from "./common";

type Scope = "tenant" | "platform";

export type AuditFilter = {
  action: string;
  actor: string;
  text: string;
  /** M7: category (`""` = all) and an inclusive UTC day range (`YYYY-MM-DD`, `""` = open). */
  category?: AuditCategory | "" | undefined;
  from?: string | undefined;
  to?: string | undefined;
};

/** Client-side filter over the loaded audit rows (case-insensitive). */
export function filterAudit(rows: readonly AuditEntry[], f: AuditFilter): AuditEntry[] {
  const actor = f.actor.trim().toLowerCase();
  const text = f.text.trim().toLowerCase();
  return rows.filter(
    (r) =>
      (!f.action || r.action === f.action) &&
      (!actor || r.actorName.toLowerCase().includes(actor)) &&
      (!text || `${r.action} ${r.target ?? ""} ${r.tenant ?? ""}`.toLowerCase().includes(text)) &&
      (!f.category || categoryOf(r as AuditEntry & { category?: unknown }) === f.category) &&
      inDateRange(r.when, f.from ?? "", f.to ?? ""),
  );
}

function Changes({ entry }: { entry: AuditEntry }) {
  if (entry.before === undefined && entry.after === undefined) return <span className="text-fg-subtle">—</span>;
  if (entry.before == null && entry.after == null) return <span className="text-fg-subtle">—</span>;
  return (
    <details>
      <summary className="cursor-pointer text-caption font-medium text-accent">Show changes</summary>
      <JsonDiff
        className="mt-2"
        caption={`Changes for ${entry.action} ${entry.target ?? ""}`.trim()}
        before={entry.before ?? null}
        after={entry.after ?? null}
        empty="No field-level changes recorded."
      />
    </details>
  );
}

type ActivityForm = { category: AuditCategory | ""; actor: string; caseId: string; action: string; from: string; to: string };

const EMPTY_ACTIVITY_FORM: ActivityForm = { category: "", actor: "", caseId: "", action: "", from: "", to: "" };

/** The form → the engine's filters (`since` / `until` from UTC days; empty fields left out). */
export function activityQuery(f: ActivityForm): ActivityQuery {
  return {
    limit: 100,
    categories: f.category ? [f.category] : undefined,
    actor: f.actor.trim() || undefined,
    case_id: f.caseId.trim() || undefined,
    action: f.action.trim() || undefined,
    ...dayBounds(f.from, f.to),
  };
}

const CATEGORY_TONE: Record<AuditCategory, "info" | "success" | "warning" | "neutral" | "accent"> = {
  runs: "info",
  approvals: "success",
  change_sets: "accent",
  admin: "neutral",
  signals: "warning",
};

function ActivityChanges({ entry }: { entry: ActivityEntry }) {
  const line = activityDetail(entry);
  const d = entry.detail;
  if (entry.category === "admin" && d && (d["before"] != null || d["after"] != null)) {
    return (
      <details>
        <summary className="cursor-pointer text-caption font-medium text-accent">Show changes</summary>
        <JsonDiff
          className="mt-2"
          caption={`Changes for ${entry.action} ${entry.target ?? ""}`.trim()}
          before={d["before"] ?? null}
          after={d["after"] ?? null}
          empty="No field-level changes recorded."
        />
      </details>
    );
  }
  return line ? <span className="text-caption break-words">{line}</span> : <span className="text-fg-subtle">—</span>;
}

/**
 * Activity timeline (`docs/contracts/110` §6): runs, approvals, change sets, admin changes and
 * signals of this workspace in one newest-first list, filtered on the server (category, person,
 * case, action prefix, UTC day range) and paged with an opaque cursor.
 */
function ActivitySection() {
  const admin = useAdminApi();
  const [form, setForm] = useState<ActivityForm>(EMPTY_ACTIVITY_FORM);
  const [applied, setApplied] = useState<ActivityForm>(EMPTY_ACTIVITY_FORM);
  const query = activityQuery(applied);
  const key = JSON.stringify(query);
  const first = useResource(() => admin.activity(query), `activity:${key}`);
  const [more, setMore] = useState<{ key: string; page: ActivityPage } | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const extra = more && more.key === `${key}:${first.data?.items[0]?.key ?? ""}` ? more.page : null;
  const next = extra ? extra.nextBefore : (first.data?.nextBefore ?? null);
  const dateError = form.from && form.to && form.from > form.to ? "The start day is after the end day." : null;

  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (dateError) return;
    setApplied(form);
  };

  const loadMore = async () => {
    if (!next || !first.data) return;
    setLoadingMore(true);
    setMoreError(null);
    try {
      const page = await admin.activity({ ...query, before: next });
      setMore({ key: `${key}:${first.data.items[0]?.key ?? ""}`, page: { items: [...(extra?.items ?? []), ...page.items], nextBefore: page.nextBefore } });
    } catch (err) {
      setMoreError(adminError(err));
    } finally {
      setLoadingMore(false);
    }
  };

  return (
    <AdminSection
      title="Activity"
      description="Everything that happened in this workspace: runs, approvals, change sets, admin changes and signals."
      actions={
        <Button size="sm" variant="secondary" icon={<RotateCw className="size-4" />} loading={first.loading && first.data !== undefined} onClick={first.reload}>
          Refresh activity
        </Button>
      }
    >
      <form role="search" aria-label="Filter the activity" className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3" onSubmit={submit} noValidate>
        <Field label="Category">
          <Select
            value={form.category}
            onChange={(v) => setForm({ ...form, category: isAuditCategory(v) ? v : "" })}
            options={[{ value: "", label: "Everything" }, ...AUDIT_CATEGORIES.map((c) => ({ value: c, label: AUDIT_CATEGORY_LABELS[c] }))]}
          />
        </Field>
        <Field label="Username" description="Exact username, e.g. sam.">
          <TextInput value={form.actor} maxLength={200} autoComplete="off" onChange={(e) => setForm({ ...form, actor: e.target.value })} />
        </Field>
        <Field label="Case id">
          <TextInput value={form.caseId} maxLength={64} mono autoComplete="off" onChange={(e) => setForm({ ...form, caseId: e.target.value })} />
        </Field>
        <Field label="Action starts with" description="e.g. run. or connector.mapping">
          <TextInput value={form.action} maxLength={100} mono autoComplete="off" onChange={(e) => setForm({ ...form, action: e.target.value })} />
        </Field>
        <Field label="From day (UTC)">
          <TextInput type="date" value={form.from} max={form.to || undefined} onChange={(e) => setForm({ ...form, from: e.target.value })} />
        </Field>
        <Field label="To day (UTC)" error={dateError ?? undefined}>
          <TextInput type="date" value={form.to} min={form.from || undefined} onChange={(e) => setForm({ ...form, to: e.target.value })} />
        </Field>
        <div className="flex flex-wrap gap-2 sm:col-span-2 lg:col-span-3">
          <Button size="sm" type="submit" variant="primary">
            Apply filters
          </Button>
          <Button
            size="sm"
            variant="ghost"
            onClick={() => {
              setForm(EMPTY_ACTIVITY_FORM);
              setApplied(EMPTY_ACTIVITY_FORM);
            }}
          >
            Clear
          </Button>
        </div>
      </form>
      <ResourceView resource={first} label="the activity">
        {(page) => {
          const rows = [...page.items, ...(extra?.items ?? [])];
          return (
            <div className="flex flex-col gap-3">
              <p className="text-caption text-fg-muted" aria-live="polite">
                {rows.length} {rows.length === 1 ? "entry" : "entries"}
                {next ? " (more available)" : ""}
              </p>
              <Table<ActivityEntry>
                caption="Activity"
                hideCaption
                rows={rows}
                rowKey={(r) => r.key}
                empty="Nothing matches these filters."
                columns={[
                  { id: "when", header: "When", nowrap: true, cell: (r) => <span className="text-caption">{when(r.at)}</span> },
                  {
                    id: "category",
                    header: "Category",
                    nowrap: true,
                    cell: (r) =>
                      r.category ? (
                        <Badge size="sm" tone={CATEGORY_TONE[r.category]} variant="outline">
                          {AUDIT_CATEGORY_LABELS[r.category]}
                        </Badge>
                      ) : (
                        <span className="text-fg-subtle">—</span>
                      ),
                  },
                  { id: "actor", header: "Who", cell: (r) => r.actorName },
                  { id: "action", header: "Action", rowHeader: true, cell: (r) => <span className="font-mono text-caption font-normal">{r.action}</span> },
                  { id: "target", header: "Target", cell: (r) => <span className="font-mono text-caption break-all">{r.target ?? "—"}</span> },
                  {
                    id: "case",
                    header: "Case",
                    cell: (r) =>
                      r.case_id ? (
                        <Link href={`/cases/${encodeURIComponent(r.case_id)}`} className="font-mono text-caption text-accent underline-offset-2 hover:underline">
                          {r.case_id.slice(0, 8)}
                          <span className="sr-only"> (open case {r.case_id})</span>
                        </Link>
                      ) : (
                        <span className="text-fg-subtle">—</span>
                      ),
                  },
                  { id: "detail", header: "Details", cell: (r) => <ActivityChanges entry={r} /> },
                ]}
              />
              {moreError ? <InlineError message={moreError} onRetry={() => void loadMore()} /> : null}
              {next ? (
                <div>
                  <Button size="sm" variant="secondary" icon={<ChevronDown className="size-4" />} loading={loadingMore} onClick={() => void loadMore()}>
                    Load older activity
                  </Button>
                </div>
              ) : null}
            </div>
          );
        }}
      </ResourceView>
    </AdminSection>
  );
}

export function AuditPage() {
  const admin = useAdminApi();
  const { me } = useSession();
  const platform = hasAnyRole(me.user, ["platform-admin"]);
  const [scope, setScope] = useState<Scope>("tenant");
  const audit = useResource(() => admin.audit({ limit: AUDIT_PAGE_MAX, scope }), `audit:${scope}`);
  /** Older pages appended by "Load older entries" (reset with the first page). */
  const [older, setOlder] = useState<{ key: string; items: AuditEntry[]; nextBefore: number | null } | null>(null);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [olderError, setOlderError] = useState<string | null>(null);
  const [filter, setFilter] = useState<AuditFilter>({ action: "", actor: "", text: "" });

  // Appended pages belong to the first page they continue (a refresh or scope change drops them).
  const firstPage = audit.data;
  const pageKey = firstPage ? `${scope}:${firstPage.items[0]?.key ?? ""}:${firstPage.nextBefore ?? ""}` : "";
  const extra = older && older.key === pageKey ? older : null;
  const nextBefore = extra ? extra.nextBefore : (firstPage?.nextBefore ?? null);

  const loadOlder = async () => {
    if (nextBefore === null) return;
    setLoadingOlder(true);
    setOlderError(null);
    try {
      const page = await admin.audit({ limit: AUDIT_PAGE_MAX, scope, before: nextBefore });
      setOlder({ key: pageKey, items: [...(extra?.items ?? []), ...page.items], nextBefore: page.nextBefore });
    } catch (err) {
      setOlderError(adminError(err));
    } finally {
      setLoadingOlder(false);
    }
  };
  return (
    <>
      <AdminPageHeader title="Audit" description="Runs, approvals, change sets, admin changes and signals, newest first." />
      <ActivitySection />
      <AdminSection
        title="Admin audit log"
        description="Every settings change with what it changed."
        actions={
          <Button size="sm" variant="secondary" icon={<RotateCw className="size-4" />} loading={audit.loading && audit.data !== undefined} onClick={audit.reload}>
            Refresh
          </Button>
        }
      >
        <ResourceView resource={audit} label="the audit log">
          {(page) => {
            const rows = [...page.items, ...(extra?.items ?? [])];
            const actions = [...new Set(rows.map((r) => r.action))].sort();
            const shown = filterAudit(rows, filter);
            return (
              <div className="flex flex-col gap-3">
                <form
                  role="search"
                  aria-label="Filter the audit log"
                  className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4"
                  onSubmit={(e) => e.preventDefault()}
                >
                  {platform ? (
                    <Field label="Scope" description="Platform: tenants, packs and gateway models.">
                      <Select
                        value={scope}
                        onChange={(v) => setScope(v === "platform" ? "platform" : "tenant")}
                        options={[
                          { value: "tenant", label: `This workspace${me.user.active_tenant ? ` (${me.user.active_tenant})` : ""}` },
                          { value: "platform", label: "Platform-wide" },
                        ]}
                      />
                    </Field>
                  ) : null}
                  <Field label="Action">
                    <Select
                      value={filter.action}
                      onChange={(action) => setFilter({ ...filter, action })}
                      options={[{ value: "", label: "All actions" }, ...actions.map((a) => ({ value: a, label: a }))]}
                    />
                  </Field>
                  <Field label="Who">
                    <TextInput value={filter.actor} placeholder="Name or username" onChange={(e) => setFilter({ ...filter, actor: e.target.value })} />
                  </Field>
                  <Field label="Search">
                    <TextInput type="search" value={filter.text} placeholder="Target, tenant…" onChange={(e) => setFilter({ ...filter, text: e.target.value })} />
                  </Field>
                </form>
                <p className="text-caption text-fg-muted" aria-live="polite">
                  {shown.length === rows.length ? `${rows.length} entries` : `${shown.length} of ${rows.length} entries`}
                </p>
                <Table<AuditEntry>
                  caption="Admin audit log"
                  hideCaption
                  rows={shown}
                  rowKey={(r) => r.key}
                  empty="No entries match."
                  columns={[
                    { id: "when", header: "When", nowrap: true, cell: (r) => <span className="text-caption">{when(r.when)}</span> },
                    { id: "actor", header: "Who", cell: (r) => r.actorName },
                    {
                      id: "category",
                      header: "Category",
                      cell: (r) => {
                        const c = categoryOf(r as AuditEntry & { category?: unknown });
                        return c ? AUDIT_CATEGORY_LABELS[c] : <span className="text-fg-subtle">—</span>;
                      },
                    },
                    { id: "tenant", header: "Tenant", cell: (r) => <span className="font-mono text-caption">{r.tenant ?? "platform"}</span> },
                    { id: "action", header: "Action", rowHeader: true, cell: (r) => <span className="font-mono text-caption font-normal">{r.action}</span> },
                    { id: "target", header: "Target", cell: (r) => <span className="font-mono text-caption break-all">{r.target ?? "—"}</span> },
                    { id: "changes", header: "Changes", cell: (r) => <Changes entry={r} /> },
                  ]}
                />
                {olderError ? <InlineError message={olderError} onRetry={() => void loadOlder()} /> : null}
                {nextBefore !== null ? (
                  <div>
                    <Button size="sm" variant="secondary" icon={<ChevronDown className="size-4" />} loading={loadingOlder} onClick={() => void loadOlder()}>
                      Load older entries
                    </Button>
                  </div>
                ) : null}
              </div>
            );
          }}
        </ResourceView>
      </AdminSection>
    </>
  );
}
