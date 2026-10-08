"use client";

import { Badge, Button, Drawer, ProvenanceChip, Skeleton, toast } from "@keelson/ui";
import { BlockRenderer, DemoBadge, SeverityBadge, formatDateTime, humanize, kindLabel } from "@keelson/viz";
import { AlarmClock, BellOff, ExternalLink, FolderOpen } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import "@/lib/blocks/canvasBlocks";
import { describeError } from "@/lib/api/client";
import { LOCAL_UPDATE, useAlertChannel } from "@/lib/alerts/channel";
import type { AlertStatusAction } from "@/lib/alerts/client";
import {
  STATUS_LABELS,
  alertMapBlock,
  alertSources,
  alertTimeline,
  exposureBreakdown,
  exposureMethodText,
  exposureSummaryText,
  nodeTypeLabel,
} from "@/lib/alerts/format";
import type { Alert, AlertDetailView } from "@/lib/alerts/types";
import { uuid } from "@/lib/ids";
import { GroupingExplainer } from "./GroupingExplainer";

type Load = { status: "loading" } | { status: "error"; message: string } | { status: "ready"; detail: AlertDetailView };
type Settled = { key: string; alertId: string } & ({ status: "error"; message: string } | { status: "ready"; detail: AlertDetailView });

const CLOSED_STATUSES: readonly Alert["status"][] = ["dismissed", "resolved"];

function noop(): void {}

/** Map of the event footprint (+ exposed nodes when the engine sends them), via the NetworkMap block. */
function AlertMap({ detail }: { detail: AlertDetailView }) {
  const block = useMemo(() => alertMapBlock(detail), [detail]);
  if (!block) return <p className="text-ui text-fg-muted">No map: this alert has no location outline.</p>;
  return <BlockRenderer envelope={block.envelope} data={block.data} onAction={noop} density="inline" />;
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section aria-label={title} className="flex flex-col gap-2">
      <h3 className="text-caption font-medium tracking-wide text-fg-subtle uppercase">{title}</h3>
      {children}
    </section>
  );
}

function DetailBody({ detail }: { detail: AlertDetailView }) {
  const { alert, event, exposures } = detail;
  const sources = useMemo(() => alertSources(detail.signals), [detail.signals]);
  const timeline = useMemo(() => alertTimeline(detail), [detail]);
  const breakdown = exposureBreakdown(alert.exposure_summary);
  const units = alert.exposure_summary.units_inbound;
  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <SeverityBadge severity={alert.severity} />
          <Badge>{STATUS_LABELS[alert.status]}</Badge>
          {alert.simulated ? <DemoBadge size="md" /> : null}
          <span className="text-caption text-fg-muted">
            {kindLabel(event.kind)} · {humanize(event.subtype)}
          </span>
        </div>
        <p className="text-body text-fg">{alert.why}</p>
        <p className="text-caption text-fg-muted">
          Raised {formatDateTime(alert.first_raised_at)}
          {alert.updated_at !== alert.first_raised_at ? ` · updated ${formatDateTime(alert.updated_at)}` : ""}
        </p>
      </div>

      <Section title="Exposure">
        <p className="text-ui font-medium text-fg">{exposureSummaryText(alert.exposure_summary)}</p>
        {breakdown.length > 0 || units !== null ? (
          <p className="text-caption text-fg-muted">
            {[...breakdown, ...(units !== null && units > 0 ? [`${new Intl.NumberFormat("en-GB").format(units)} units inbound`] : [])].join(" · ")}
          </p>
        ) : null}
        <AlertMap detail={detail} />
        {exposures.length > 0 ? (
          <ul aria-label="Exposed sites and lanes" className="flex flex-col divide-y divide-border rounded-md border border-border">
            {exposures.map((e) => (
              <li key={`${e.node_type}:${e.node_code}`} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 px-3 py-2">
                <span className="font-mono text-ui text-fg">{e.node_code}</span>
                <span className="text-caption text-fg-muted">{humanize(nodeTypeLabel(e.node_type))}</span>
                <span className="ml-auto text-caption text-fg-muted">{exposureMethodText(e)}</span>
              </li>
            ))}
          </ul>
        ) : null}
      </Section>

      <Section title="Sources">
        {sources.length === 0 ? (
          <p className="text-ui text-fg-muted">No source recorded.</p>
        ) : (
          <ul className="flex flex-col gap-2">
            {sources.map((s) => (
              <li key={s.key} className="flex flex-wrap items-center gap-x-2 gap-y-1">
                {s.url ? (
                  <a
                    href={s.url}
                    target="_blank"
                    rel="noopener noreferrer nofollow"
                    className="inline-flex items-center gap-1 text-ui font-medium text-accent underline-offset-4 hover:underline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
                  >
                    {s.attribution}
                    <ExternalLink aria-hidden="true" className="size-3.5" />
                    <span className="sr-only">(opens in a new tab)</span>
                  </a>
                ) : (
                  <span className="text-ui font-medium text-fg">{s.attribution}</span>
                )}
                <ProvenanceChip prov={s.provenance} />
                <span className="text-caption text-fg-muted">Retrieved {formatDateTime(s.retrievedAt)}</span>
              </li>
            ))}
          </ul>
        )}
        {sources.some((s) => s.provenance === "inference") ? (
          <p className="text-caption text-fg-muted">News-based details are unconfirmed until an official source reports them.</p>
        ) : null}
      </Section>

      <GroupingExplainer detail={detail} />

      <Section title="Updates">
        <ol aria-label="Updates" className="flex flex-col gap-2 border-l border-border pl-4">
          {timeline.map((t) => (
            <li key={t.key} className="flex flex-col">
              <span className="text-caption text-fg-muted">
                <time dateTime={t.at}>{formatDateTime(t.at)}</time>
                {t.source ? ` · ${t.source}` : ""}
              </span>
              <span className="text-ui break-words text-fg">{t.text}</span>
            </li>
          ))}
        </ol>
      </Section>
    </div>
  );
}

/**
 * Alert detail drawer (M6, `docs/contracts/100` §8): why, exposure (map + exposed nodes), sources
 * with licence / attribution, update timeline; **Open case** / Snooze / Dismiss for write roles.
 * Opening a new alert marks it seen. Untrusted text (titles, news headlines) is plain text only.
 */
export function AlertDrawer({
  alertId,
  onClose,
  canWrite,
}: {
  alertId: string | null;
  onClose: () => void;
  canWrite: boolean;
}) {
  const channel = useAlertChannel();
  const router = useRouter();
  const [settled, setSettled] = useState<Settled | null>(null);
  const [reload, setReload] = useState(0);
  const [busy, setBusy] = useState<"open" | AlertStatusAction | null>(null);
  const openKeys = useRef(new Map<string, string>());
  const markedSeen = useRef(new Set<string>());
  const api = channel?.api;

  useEffect(() => {
    if (!alertId || !api) return;
    const ctrl = new AbortController();
    const key = `${alertId}#${reload}`;
    api
      .get(alertId, { signal: ctrl.signal })
      .then((detail) => {
        if (!ctrl.signal.aborted) setSettled({ key, alertId, status: "ready", detail });
      })
      .catch((err: unknown) => {
        if (!ctrl.signal.aborted) setSettled({ key, alertId, status: "error", message: describeError(err) });
      });
    return () => ctrl.abort();
  }, [alertId, api, reload]);
  // While a live update reloads the same alert, the detail on screen stays (no skeleton flash).
  const load: Load =
    settled && settled.alertId === alertId && (settled.key === `${alertId}#${reload}` || settled.status === "ready")
      ? settled
      : { status: "loading" };

  // Live: the stream updated this alert (new advisory, severity change, case opened, removed) →
  // reload the detail.
  useEffect(() => {
    if (!channel || !alertId) return;
    return channel.subscribe((u) => {
      if (u.alertId === alertId && u.name !== LOCAL_UPDATE) setReload((n) => n + 1);
    });
  }, [channel, alertId]);

  // Opening a new alert marks it seen (write roles; best effort).
  const detail = load.status === "ready" ? load.detail : null;
  useEffect(() => {
    if (!detail || !api || !channel || !canWrite) return;
    const a = detail.alert;
    if (a.status !== "new" || markedSeen.current.has(a.id)) return;
    markedSeen.current.add(a.id);
    api
      .setStatus(a.id, "seen")
      .then((updated) => channel.applyLocal(updated ?? { ...a, status: "seen" }))
      .catch(() => undefined);
  }, [detail, api, channel, canWrite]);

  const alert = detail?.alert ?? null;

  const openCase = async () => {
    if (!alert || !api || busy) return;
    const key = openKeys.current.get(alert.id) ?? uuid();
    openKeys.current.set(alert.id, key);
    setBusy("open");
    try {
      const out = await api.openCase(alert.id, key);
      openKeys.current.delete(alert.id);
      channel?.applyLocal({ ...alert, status: "opened", case_id: out.case_id });
      router.push(`/cases/${encodeURIComponent(out.case_id)}`);
    } catch (err) {
      setBusy(null);
      toast.error("Couldn't open a case", { description: describeError(err) });
    }
  };

  const changeStatus = async (action: "snooze" | "dismiss") => {
    if (!alert || !api || busy) return;
    setBusy(action);
    try {
      const updated = await api.setStatus(alert.id, action);
      channel?.applyLocal(updated ?? { ...alert, status: action === "snooze" ? "snoozed" : "dismissed" });
      toast.success(action === "snooze" ? "Alert snoozed" : "Alert dismissed");
      onClose();
    } catch (err) {
      toast.error(action === "snooze" ? "Couldn't snooze the alert" : "Couldn't dismiss the alert", { description: describeError(err) });
    } finally {
      setBusy(null);
    }
  };

  const closed = alert ? CLOSED_STATUSES.includes(alert.status) : true;
  const footer = alert ? (
    canWrite ? (
      <>
        {!closed ? (
          <>
            <Button variant="ghost" icon={<BellOff aria-hidden="true" />} loading={busy === "dismiss"} disabled={busy !== null} onClick={() => void changeStatus("dismiss")}>
              Dismiss
            </Button>
            {alert.status !== "snoozed" ? (
              <Button variant="secondary" icon={<AlarmClock aria-hidden="true" />} loading={busy === "snooze"} disabled={busy !== null} onClick={() => void changeStatus("snooze")}>
                Snooze
              </Button>
            ) : null}
          </>
        ) : null}
        {alert.case_id ? (
          <Link
            href={`/cases/${encodeURIComponent(alert.case_id)}`}
            className="inline-flex h-10 items-center gap-2 rounded-pill bg-accent px-4 text-ui font-medium text-accent-fg hover:bg-accent/90 focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-focus"
          >
            <FolderOpen aria-hidden="true" className="size-4" />
            Go to the case
          </Link>
        ) : !closed ? (
          <Button variant="primary" icon={<FolderOpen aria-hidden="true" />} loading={busy === "open"} disabled={busy !== null} onClick={() => void openCase()}>
            Open case
          </Button>
        ) : null}
      </>
    ) : alert.case_id ? (
      <Link href={`/cases/${encodeURIComponent(alert.case_id)}`} className="text-ui font-medium text-accent underline-offset-4 hover:underline">
        Go to the case
      </Link>
    ) : (
      <p className="text-caption text-fg-muted">Your role can view alerts. A planner can open a case from this alert.</p>
    )
  ) : undefined;

  return (
    <Drawer
      open={alertId !== null}
      onOpenChange={(open) => {
        if (!open) onClose();
      }}
      size="lg"
      title={alert?.title ?? "Alert"}
      footer={footer}
    >
      {load.status === "loading" ? (
        <div className="flex flex-col gap-3" role="status" aria-label="Loading the alert">
          <Skeleton className="h-6 w-1/2" />
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-20 w-full" />
        </div>
      ) : load.status === "error" ? (
        <div className="flex flex-col items-start gap-3">
          <p className="text-ui text-fg">Couldn&apos;t load this alert. {load.message}</p>
          <Button size="sm" onClick={() => setReload((n) => n + 1)}>
            Try again
          </Button>
        </div>
      ) : (
        <DetailBody detail={load.detail} />
      )}
    </Drawer>
  );
}
