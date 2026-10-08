"use client";

import { Badge, Button, Table, toast } from "@keelson/ui";
import { opLabel } from "@keelson/viz";
import { RefreshCw } from "lucide-react";
import { useEffect, useEffectEvent, useState } from "react";
import { adminError, int, when } from "@/lib/admin/format";
import { SNAPSHOT_STATUS_LABELS, loadingSnapshot, queuedSince, type Capabilities, type Snapshot, type SnapshotHistory } from "@/lib/admin/mappings";
import { TIER_INFO } from "@/lib/admin/tools";
import { useAdminApi, useResource } from "@/lib/admin/useAdmin";
import { AdminSection, Facts, InlineError, ResourceView } from "./common";

export const SYNC_LABELS: Record<Capabilities["sync"], string> = {
  snapshot: "Consistent snapshot export (tier B)",
  crawl: "Crawl: pages through mapped read tools (tier A)",
  none: "No data sync",
};

export const COMMIT_LABELS: Record<Capabilities["commit"], string> = {
  change_set: "Change sets: all lines applied together",
  per_op: "One call per change, with a ledger and read-back",
  guide: "Guided: people make the changes by hand",
};

/** "Crawled" badge for data gathered by paging (not one consistent snapshot). */
export function ConsistencyBadge({ consistency }: { consistency: "consistent" | "crawled" | null | undefined }) {
  if (!consistency) return <span className="text-fg-muted">—</span>;
  return consistency === "crawled" ? (
    <Badge size="sm" tone="warning" variant="outline" title="Data gathered by paging, not a consistent snapshot: plans are advisory">
      Crawled
    </Badge>
  ) : (
    <Badge size="sm" tone="success" variant="outline">
      Consistent snapshot
    </Badge>
  );
}

/** "5 Oct 2026, 09:12 UTC – 5 Oct 2026, 09:19 UTC". */
export function asOfRangeText(range: { from: string; to: string } | null | undefined): string {
  if (!range) return "—";
  const from = when(range.from);
  const to = when(range.to);
  return from === to ? from : `${from} – ${to}`;
}

/**
 * Capability / tier summary (`docs/contracts/110` §4): how Keelson reads this system (snapshot vs
 * crawl), how it writes (change sets, per-op calls, or guide), the mapping versions in use and the
 * ops an action mapping covers. `GET /api/admin/connectors/{slug}/capabilities`.
 */
export function CapabilitiesSection({ slug }: { slug: string }) {
  const admin = useAdminApi();
  const caps = useResource(() => admin.capabilities(slug), `caps:${slug}`);
  if (caps.errorStatus === 404 && !caps.data) return null;
  return (
    <AdminSection title="Capabilities" description="How Keelson reads from and writes to this system, detected from its tools and your mappings.">
      <ResourceView resource={caps} label="capabilities" rows={2}>
        {(c) => (
          <div className="flex flex-col gap-3">
            <Facts
              items={[
                [
                  "Tiers",
                  <span key="t" className="flex flex-wrap gap-1">
                    {c.tiers.length ? c.tiers.map((t) => <Badge key={t} size="sm" tone="neutral" title={TIER_INFO[t]}>{TIER_INFO[t] ?? t}</Badge>) : "—"}
                  </span>,
                ],
                ["Reads", SYNC_LABELS[c.sync]],
                ["Writes", COMMIT_LABELS[c.commit]],
                ["Data", <ConsistencyBadge key="c" consistency={c.consistency} />],
                ["Read mapping", c.read_mapping_version ? `v${c.read_mapping_version}` : "None"],
                ["Action mapping", c.action_mapping_version ? `v${c.action_mapping_version}` : "None"],
              ]}
            />
            {c.advisory ? (
              <p role="note" className="rounded-sm border border-warning/40 bg-warning-soft px-3 py-2 text-ui">
                Plans built on this system&apos;s data are <strong>advisory</strong>: it is gathered by paging, not as one consistent snapshot.
              </p>
            ) : null}
            {c.commit === "per_op" || c.mapped_ops.length ? (
              <div>
                <p className="text-caption font-medium text-fg-muted">Changes Keelson can apply here ({c.mapped_ops.length}); anything else is guided</p>
                {c.mapped_ops.length ? (
                  <ul className="flex flex-wrap gap-1.5" aria-label="Mapped operations">
                    {c.mapped_ops.map((op) => (
                      <li key={op}>
                        <Badge size="sm" tone="info" title={op}>
                          {opLabel(op)}
                        </Badge>
                      </li>
                    ))}
                  </ul>
                ) : (
                  <p className="text-ui text-fg-muted">None mapped yet: every change is guided.</p>
                )}
              </div>
            ) : null}
          </div>
        )}
      </ResourceView>
    </AdminSection>
  );
}

const POLL_MS = 3000;

function rowsPerEntity(s: Snapshot): { entity: string; rows: number }[] {
  return Object.entries(s.row_counts)
    .map(([entity, rows]) => ({ entity, rows }))
    .sort((a, b) => a.entity.localeCompare(b.entity));
}

/**
 * Snapshot status (`docs/contracts/110` §4): the snapshot plans are built on — for a tier-A
 * connector a **crawl** (consistency badge "Crawled", as-of range = first and last page read,
 * pages, truncated entities), for tier B a consistent export — plus an attempt in progress or the
 * last failure, rows per entity and **Crawl now** / **Sync now** (`POST …/sync`). Re-read every
 * 3 s while an attempt is loading. `GET /api/admin/connectors/{slug}/snapshots`.
 */
export function CrawlSection({ slug, sync }: { slug: string; sync?: Capabilities["sync"] | undefined }) {
  const admin = useAdminApi();
  const history = useResource(() => admin.snapshots(slug, 10), `snapshots:${slug}`);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const loading = history.data ? loadingSnapshot(history.data) : null;
  const queued = history.data ? queuedSince(history.data) : null;
  const running = loading !== null;
  const crawl = sync === "crawl";

  // Re-read while an attempt is loading or queued (the worker picks a queued one up shortly).
  const poll = useEffectEvent(() => history.reload());
  const active = running || queued !== null;
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(() => poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [active]);

  if (sync === "none" || (history.errorStatus === 404 && history.data === undefined)) return null;

  const start = async () => {
    setStarting(true);
    setError(null);
    try {
      await admin.syncNow(slug);
      toast.success(crawl ? "Crawl queued" : "Sync queued", {
        description: crawl ? "Keelson pages through the mapped read tools; this can take a few minutes." : "Keelson loads a fresh snapshot in the background.",
      });
      history.reload();
    } catch (err) {
      setError(adminError(err));
    } finally {
      setStarting(false);
    }
  };

  return (
    <AdminSection
      title={crawl ? "Crawl snapshot" : "Data snapshot"}
      description={
        crawl
          ? "Tier-A data is gathered by paging the mapped read tools, so it is not one consistent snapshot: plans built on it are advisory."
          : "The canonical snapshot plans are built on."
      }
      actions={
        <Button size="sm" variant="secondary" icon={<RefreshCw className="size-4" />} loading={starting || running} onClick={() => void start()}>
          {running ? (crawl ? "Crawling…" : "Syncing…") : crawl ? "Crawl now" : "Sync now"}
        </Button>
      }
    >
      {error ? <InlineError message={error} /> : null}
      <ResourceView resource={history} label="the snapshot status" rows={2}>
        {(h: SnapshotHistory) => {
          const current = h.current;
          const lastFailed = h.items.find((s) => s.status === "failed" && (!current || (s.created_at ?? "") > (current.created_at ?? "")));
          return (
            <div className="flex flex-col gap-3" aria-busy={running || undefined}>
              {loading ? (
                <p className="text-ui text-fg-muted" role="status">
                  {crawl ? "Crawling" : "Loading"} since {when(loading.created_at)}…
                </p>
              ) : queued ? (
                <p className="text-ui text-fg-muted" role="status" data-sync-queued="">
                  {crawl ? "Crawl" : "Sync"} queued ({when(queued)}): it starts shortly.
                </p>
              ) : null}
              {lastFailed?.error ? <InlineError message={`The last ${crawl ? "crawl" : "sync"} failed (${when(lastFailed.created_at)}): ${lastFailed.error}`} /> : null}
              {!current ? (
                <p className="text-ui text-fg-muted">{crawl ? "Not crawled yet. Save a read mapping, then crawl." : "No snapshot yet."}</p>
              ) : (
                <>
                  <Facts
                    items={[
                      [
                        "Status",
                        <Badge key="s" size="sm" tone={SNAPSHOT_STATUS_LABELS[current.status]?.tone ?? "neutral"} dot>
                          {SNAPSHOT_STATUS_LABELS[current.status]?.label ?? current.status}
                        </Badge>,
                      ],
                      ["Consistency", <ConsistencyBadge key="c" consistency={current.consistency} />],
                      ["Data as of", current.consistency === "crawled" ? asOfRangeText(current.as_of_range) : when(current.as_of)],
                      ["Loaded", when(current.ready_at ?? current.created_at)],
                      ["Rows", int(current.rows ?? null)],
                      ...(current.crawl ? ([["Pages read", int(current.crawl.pages ?? null)]] as [string, string][]) : []),
                    ]}
                  />
                  {current.crawl?.truncated.length ? (
                    <p role="note" className="rounded-sm border border-warning/40 bg-warning-soft px-3 py-2 text-ui">
                      Stopped at the page limit for {current.crawl.truncated.join(", ")}: raise <span className="font-mono">max_pages</span> in the read mapping to read everything.
                    </p>
                  ) : null}
                  {Object.keys(current.row_counts).length ? (
                    <Table
                      caption="Rows per entity"
                      rows={rowsPerEntity(current)}
                      rowKey={(e) => e.entity}
                      rowTone={(e) => (current.crawl?.truncated.includes(e.entity) ? "warning" : undefined)}
                      columns={[
                        { id: "entity", header: "Entity", rowHeader: true, cell: (e) => <span className="font-mono text-caption">{e.entity}</span> },
                        { id: "rows", header: "Rows", align: "end", cell: (e) => int(e.rows) },
                      ]}
                    />
                  ) : null}
                </>
              )}
            </div>
          );
        }}
      </ResourceView>
    </AdminSection>
  );
}
